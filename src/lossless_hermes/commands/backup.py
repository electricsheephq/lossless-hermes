"""``/lcm backup`` — ``VACUUM INTO`` a timestamped ``.bak`` file.

Ports the TS ``buildBackupText`` from
``lossless-claw/src/plugin/lcm-command.ts:1356-1411`` (case ``"backup"`` in
``parseLcmCommand`` and the dispatcher at line 2625). The handler renders
markdown with three sections — header, plugin name, and a Backup status
block reporting "created" / "unavailable" / "failed".

### Behavior

1. Resolve the engine's open DB connection (``engine._db``) and the
   configured database path (``engine.config.database_path``).
2. If the engine is uninitialized (no DB open), render a graceful
   "engine not yet initialized" message — operators may type ``/lcm
   backup`` very early in a debug session.
3. If the database path is in-memory or empty, render
   "status: unavailable / reason: Backup requires a file-backed SQLite
   database." (matches TS ``getLcmBackupUnavailableReason``).
4. Call the primitive :func:`lossless_hermes.plugin.db_backup.write_lcm_database_backup`
   with ``label="backup"`` (mirrors TS ``createLcmDatabaseBackup({db,
   databasePath, label: "backup"})`` at lines 1380-1383).
5. Render "status: created / db path: ... / backup path: ..." on success.
6. On :class:`LcmDatabaseBackupError`, render
   "status: failed / reason: <error>".

### Why two modules (this + ``plugin/db_backup.py``)

The primitive lives in :mod:`lossless_hermes.plugin.db_backup` because
two other callers consume it (``/lcm rotate``, the doctor cleaners apply
path) — see ``epics/08-cli-ops/08-09-backup.md`` §"What this issue
covers". This module is the ``/lcm backup`` slash-command surface; it
imports the primitive and renders chat-friendly text. The split mirrors
the TS source's split between ``lcm-db-backup.ts`` (primitive) and
``lcm-command.ts`` (slash output).

See:

* ``epics/08-cli-ops/08-09-backup.md`` — this issue.
* ``lossless-claw/src/plugin/lcm-command.ts:1356-1411`` — TS ``buildBackupText``.
* ``lossless-claw/src/plugin/lcm-db-backup.ts`` — TS backup primitive.
* ``docs/adr/013-owner-gating.md`` — handler is non-gated; ``/lcm
  backup`` is read-only on data the caller already accesses.
"""

from __future__ import annotations

import logging
from typing import Any

from lossless_hermes.plugin.db_backup import (
    LcmDatabaseBackupError,
    write_lcm_database_backup,
)

logger = logging.getLogger("lossless_hermes.commands.backup")


# ---------------------------------------------------------------------------
# Section/stat helpers — local copies of status.py's helpers
# ---------------------------------------------------------------------------
#
# We deliberately don't import from ``status.py`` because backup is a
# leaf command — pulling status.py's import graph (compaction, telemetry,
# embeddings) for two two-line helpers would slow the entire dispatcher.
# When 08-04+ lands a shared ``commands/_shared.py`` for these helpers,
# this duplication folds away (each command currently re-defines them).


def _build_header_lines() -> list[str]:
    """Render the two-line header. Parity with ``status.py:_build_header_lines``."""
    from lossless_hermes.commands.status import _resolve_package_version

    version = _resolve_package_version()
    return [
        f"**Lossless Hermes v{version}**",
        "Help: `/lcm help`",
    ]


def _build_section(title: str, lines: list[str]) -> str:
    """Render a section with indented stat lines. Parity with status.py."""
    body = "\n".join(f"  {line}" for line in lines)
    return f"**{title}**\n{body}"


def _build_stat_line(label: str, value: str) -> str:
    """``"label: value"`` — parity with status.py:_build_stat_line."""
    return f"{label}: {value}"


# ---------------------------------------------------------------------------
# Unavailable-reason classification — mirrors TS getLcmBackupUnavailableReason
# ---------------------------------------------------------------------------


def _get_unavailable_reason(database_path: str | None) -> str | None:
    """Return a string reason if backup is unavailable; ``None`` if available.

    Ports the TS ``getLcmBackupUnavailableReason`` (``lcm-command.ts:1347-1354``).
    Three unavailable cases:

    * ``database_path`` is :data:`None` or not a string — "Invalid database
      path." (defensive — config schema should never produce this).
    * ``database_path`` is empty after trim — "Backup requires a
      file-backed SQLite database."
    * ``database_path`` is ``":memory:"`` or starts with ``"file::memory:"``
      — same "file-backed required" reason.

    Available case returns :data:`None` and the caller proceeds to call
    the primitive.
    """
    if database_path is None or not isinstance(database_path, str):
        return "Invalid database path."
    trimmed = database_path.strip()
    if not trimmed or trimmed == ":memory:" or trimmed.startswith("file::memory:"):
        return "Backup requires a file-backed SQLite database."
    return None


# ---------------------------------------------------------------------------
# Public entry point — the dispatcher routes ``/lcm backup`` here
# ---------------------------------------------------------------------------


def run(parsed: Any) -> str:
    """Render ``/lcm backup`` — create a fresh SQLite backup of ``lcm.db``.

    Reads:

    * ``parsed.engine`` — :class:`LCMEngine` set by the dispatcher.
    * ``parsed.engine._db`` — open :class:`sqlite3.Connection` (or
      :data:`None` for the pre-on_session_start branch).
    * ``parsed.engine.config.database_path`` — source DB filesystem path.

    Returns multi-line markdown matching the TS ``buildBackupText`` output:

    * Header + blank + "Lossless Claw Backup" + blank.
    * Backup section: ``status / reason`` (unavailable, failed) or
      ``status / db path / backup path`` (created).

    Never raises — any unexpected error is logged and surfaced as a
    "/lcm backup failed: <reason>" string so the dispatcher's last-resort
    catch-all doesn't have to mask the cause.
    """
    engine = getattr(parsed, "engine", None)
    if engine is None:
        logger.warning("[lcm] /lcm backup invoked with no engine on parsed")
        return "/lcm backup: dispatcher misconfigured (no engine reference)."

    lines = list(_build_header_lines())
    lines.append("")
    lines.append("Lossless Claw Backup")
    lines.append("")

    db = getattr(engine, "_db", None)
    if db is None:
        # Engine constructed but on_session_start has not yet run.
        lines.append(
            _build_section(
                "Backup",
                [
                    _build_stat_line("status", "unavailable"),
                    _build_stat_line("reason", "Engine not yet initialized (no DB open)."),
                ],
            )
        )
        return "\n".join(lines)

    database_path = getattr(getattr(engine, "config", None), "database_path", None)

    unavailable_reason = _get_unavailable_reason(database_path)
    if unavailable_reason is not None:
        lines.append(
            _build_section(
                "Backup",
                [
                    _build_stat_line("status", "unavailable"),
                    _build_stat_line("reason", unavailable_reason),
                ],
            )
        )
        return "\n".join(lines)

    # database_path is guaranteed non-None / non-empty / non-memory here
    # by the unavailable_reason check above. The type-narrowing is implicit
    # to ty / mypy via the None check.
    assert database_path is not None  # noqa: S101 -- narrowing for type-checker
    try:
        backup_path = write_lcm_database_backup(
            db,
            label="backup",
            db_path=database_path,
        )
    except LcmDatabaseBackupError as exc:
        logger.warning("[lcm] /lcm backup failed: %s", exc)
        lines.append(
            _build_section(
                "Backup",
                [
                    _build_stat_line("status", "failed"),
                    _build_stat_line("reason", str(exc)),
                ],
            )
        )
        return "\n".join(lines)
    except Exception as exc:  # noqa: BLE001 -- last-resort guard
        # Anything that isn't LcmDatabaseBackupError is a bug — log with
        # full traceback so operators can file an issue, but still return
        # a string rather than re-raising into the dispatcher.
        logger.exception("[lcm] /lcm backup: unexpected error")
        return f"/lcm backup failed: unexpected error — {exc!s}"

    lines.append(
        _build_section(
            "Backup",
            [
                _build_stat_line("status", "created"),
                _build_stat_line("db path", database_path),
                _build_stat_line("backup path", str(backup_path)),
            ],
        )
    )
    return "\n".join(lines)
