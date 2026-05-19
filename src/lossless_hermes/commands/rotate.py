"""``/lcm rotate`` — force a SQLite DB rotation for the current session.

Replaces the Epic 08-01 stub with the real handler. Ports the TS
``case "rotate"`` body of ``lossless-claw/src/plugin/lcm-command.ts``
(``buildRotateText``) plus ``engine.ts:rotateSessionStorageWithBackup``,
pinned at commit ``1f07fbd`` (pr-613 head).

### Why this is much narrower than the TS source

In OpenClaw / lossless-claw, ``/lcm rotate`` physically **rotated the
session's JSONL transcript file** — created a timestamped backup of the
``.jsonl`` and replaced it with a bootstrap-plus-fresh-tail. Per
[ADR-024](../../docs/adr/024-project-layout.md) §"Consequences" and the
Epic 01 README ("JSONL bootstrap, file-anchor checkpointing,
session-file rollover" drop entirely), **Hermes has no JSONL
transcript** — there is no transcript file to rotate. So ``/lcm rotate``
in the Hermes port is SQLite-only. What it does instead:

1. **Backup the current DB** via
   :func:`lossless_hermes.plugin.db_backup.write_lcm_database_backup`
   with ``label="rotate"`` (mirrors the TS
   ``createLcmDatabaseBackup({..., label: "rotate"})`` inside
   ``rotateSessionStorageWithBackup``). The 30s DB-lock timeout the TS
   contract applies maps onto the backup step — ``VACUUM INTO`` holds
   the connection briefly; there is no separate lock to acquire in the
   sync Python port (per [ADR-017](../../docs/adr/017-sync-vs-async-db.md)
   the DB surface is single-threaded).
2. **Clear the assemble snapshot cache** for the current session via
   :meth:`LCMEngine.clear_assemble_snapshot` — drops the per-conversation
   prefix-stability snapshot so the next assemble pass rebuilds from
   scratch.
3. **Optionally compact the WAL** via ``PRAGMA wal_checkpoint(TRUNCATE)``
   — best-effort; a failure is swallowed (the DB is still fine, the WAL
   sidecar just stays larger).
4. **Stamp ``state_meta.last_rotate_at``** via
   :meth:`LCMEngine.write_state_meta` so ``/lcm status`` can later show
   "last rotated N ago".

### Not owner-gated

Per ``docs/porting-guides/plugin-glue.md`` §"/lcm slash commands — full
inventory" line 427, ``rotate`` is NOT in the owner-gated list — it is
safe for any agent to call. It creates a new file (a read-only writer on
existing data) and clears in-memory cache. Owner-gating, where it
applies, is enforced upstream by Hermes's ``SlashAccessPolicy`` per
[ADR-013](../../docs/adr/013-owner-gating.md); this handler does not
gate itself.

### No JSONL is touched

Invariant from ADR-024 / the Epic 01 README: no ``.jsonl`` file path
appears anywhere in this module's function bodies. The handler operates
exclusively on the ``lcm.db`` file family (the DB itself + the
``.bak`` backup it writes).

See:

* ``epics/08-cli-ops/08-16-rotate.md`` — this issue.
* ``lossless-claw/src/plugin/lcm-command.ts`` (case ``"rotate"``) +
  ``src/engine.ts:rotateSessionStorageWithBackup`` — TS source at commit
  ``1f07fbd``.
* ``src/lossless_hermes/plugin/db_backup.py`` — the backup primitive.
* ``src/lossless_hermes/engine/lifecycle.py`` —
  :meth:`clear_assemble_snapshot` + :meth:`write_state_meta`.
* ``docs/adr/024-project-layout.md`` — JSONL drop; SQLite-only.
* ``docs/adr/013-owner-gating.md`` — handler is non-gated.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from lossless_hermes.plugin.db_backup import (
    LcmDatabaseBackupError,
    write_lcm_database_backup,
)

logger = logging.getLogger("lossless_hermes.commands.rotate")


# ---------------------------------------------------------------------------
# Unavailable-reason classification — mirrors TS getLcmBackupUnavailableReason
# ---------------------------------------------------------------------------
#
# Rotate backs the DB up as its first step, so it inherits the same
# file-backed-database precondition as ``/lcm backup``. We re-implement
# the check locally rather than import from ``commands/backup.py`` —
# ``rotate`` is a leaf command and pulling backup.py's import graph for a
# six-line helper is the same anti-coupling reasoning backup.py itself
# documents (when 08-04+ lands a shared ``commands/_shared.py`` the
# duplication folds away).


def _get_unavailable_reason(database_path: str | None) -> str | None:
    """Return a string reason if rotate is unavailable; ``None`` if available.

    Ports the TS ``getLcmBackupUnavailableReason`` precondition (the
    rotate path reaches the same backup primitive). Three unavailable
    cases:

    * ``database_path`` is :data:`None` or not a string — "Invalid
      database path." (defensive — config schema should never produce
      this).
    * ``database_path`` is empty after trim — "Rotate requires a
      file-backed SQLite database."
    * ``database_path`` is ``":memory:"`` or starts with
      ``"file::memory:"`` — same "file-backed required" reason.

    Available case returns :data:`None` and the caller proceeds.
    """
    if database_path is None or not isinstance(database_path, str):
        return "Invalid database path."
    trimmed = database_path.strip()
    if not trimmed or trimmed == ":memory:" or trimmed.startswith("file::memory:"):
        return "Rotate requires a file-backed SQLite database."
    return None


# ---------------------------------------------------------------------------
# Public entry point — the dispatcher routes ``/lcm rotate`` here
# ---------------------------------------------------------------------------


def run(parsed: Any) -> str:
    """Render ``/lcm rotate`` — force a SQLite rotation for the current session.

    Per ``lcm-command.ts:441`` ``/lcm rotate`` accepts no extra
    arguments; the dispatcher already routes any trailing tokens to the
    ``help`` handler with an "does not accept extra arguments" error, so
    this handler does not re-parse ``parsed.tokens``.

    Reads:

    * ``parsed.engine`` — :class:`LCMEngine` set by the dispatcher.
    * ``parsed.engine.current_session_id`` — the engine-tracked
      replacement for the TS ``ctx.sessionId`` (set by
      ``on_session_start``; :data:`None` pre-first-message).
    * ``parsed.engine._db`` — open :class:`sqlite3.Connection` (or
      :data:`None` pre-``on_session_start``).
    * ``parsed.engine.config.database_path`` — source DB filesystem
      path.

    Behavior, in order (matching the issue 08-16 algorithm):

    1. No active session → return ``"[lcm] rotate: no active session"``
       and do nothing else.
    2. No DB / in-memory DB → return an "unavailable" message; nothing
       is mutated.
    3. Backup the DB with ``label="rotate"``. On
       :class:`LcmDatabaseBackupError`, return the error message — **no
       partial state changes** (steps 4-6 do not run).
    4. Clear the assemble snapshot cache for the current session.
    5. Best-effort ``PRAGMA wal_checkpoint(TRUNCATE)`` — a failure is
       swallowed.
    6. Stamp ``state_meta.last_rotate_at`` with the current ISO-8601 UTC
       timestamp.

    Returns a multi-line text block. Never raises — any unexpected
    error is logged and surfaced as a ``"/lcm rotate failed: ..."``
    string so the dispatcher's last-resort catch-all does not mask the
    cause.
    """
    engine = getattr(parsed, "engine", None)
    if engine is None:
        logger.warning("[lcm] /lcm rotate invoked with no engine on parsed")
        return "/lcm rotate: dispatcher misconfigured (no engine reference)."

    # Step 0: resolve the current session. ``current_session_id`` is
    # ``None`` before the first ``on_session_start`` (CLI pre-first-
    # message; gateway with no active conversation). Mirrors the TS
    # source's ``ctx.sessionId``-unavailable branch — but the Hermes
    # port collapses it to one terse line per the 08-16 spec algorithm.
    session_id = getattr(engine, "current_session_id", None)
    if session_id is None:
        return "[lcm] rotate: no active session"

    db = getattr(engine, "_db", None)
    if db is None:
        # Engine constructed but ``on_session_start`` has not yet run —
        # there is no DB to back up. Render a friendly message rather
        # than raising; an operator may type ``/lcm rotate`` very early.
        return "[lcm] rotate: unavailable\nReason: engine not yet initialized (no DB open)."

    database_path = getattr(getattr(engine, "config", None), "database_path", None)
    unavailable_reason = _get_unavailable_reason(database_path)
    if unavailable_reason is not None:
        return f"[lcm] rotate: unavailable\nReason: {unavailable_reason}"

    # ``database_path`` is guaranteed non-None / non-empty / non-memory
    # here by the unavailable-reason check above. The assert narrows the
    # type for the type-checker (parity with backup.py:187).
    assert database_path is not None  # noqa: S101 -- narrowing for type-checker

    # Step 1: backup with label="rotate". On failure return the error
    # message — and crucially do NOT proceed to steps 2-4, so a failed
    # rotate leaves zero partial state changes (08-16 AC: "Backup
    # failure → returns the error message; no partial state changes").
    try:
        backup_path = write_lcm_database_backup(
            db,
            label="rotate",
            db_path=database_path,
        )
    except LcmDatabaseBackupError as exc:
        logger.warning("[lcm] /lcm rotate failed at backup step: %s", exc)
        return f"[lcm] rotate failed at backup step: {exc}"
    except Exception as exc:  # noqa: BLE001 -- last-resort guard
        # Anything that is not LcmDatabaseBackupError is a bug — log the
        # full traceback so operators can file an issue, but still
        # return a string rather than re-raising into the dispatcher.
        logger.exception("[lcm] /lcm rotate: unexpected error at backup step")
        return f"/lcm rotate failed: unexpected error at backup step — {exc!s}"

    # Step 2: clear the assemble snapshot cache for this session. This
    # drops the per-conversation prefix-stability snapshot so the next
    # assemble pass rebuilds from scratch. Best-effort inside the engine
    # method (no-op if there is no conversation row yet).
    snapshot_cleared = engine.clear_assemble_snapshot(session_id)

    # Step 3: optional WAL compaction. ``PRAGMA wal_checkpoint(TRUNCATE)``
    # checkpoints the WAL into the main DB file and truncates the WAL
    # sidecar to zero bytes. Best-effort — a failure (DB locked by
    # another connection, journal mode not WAL, etc.) is swallowed: the
    # DB is still consistent, the sidecar just stays larger. ``OK"``
    # is the common case but the result row is not inspected.
    wal_compacted = True
    try:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError as exc:
        # Swallow — best-effort. Logged at debug so an operator chasing
        # a large -wal file has a breadcrumb, but not surfaced as a
        # failure (rotate's primary work — the backup — already
        # succeeded).
        wal_compacted = False
        logger.debug("[lcm] /lcm rotate: WAL checkpoint skipped (%s)", exc)

    # Step 4: stamp ``state_meta.last_rotate_at``. ISO-8601 UTC with a
    # trailing ``Z``. ``datetime.now(timezone.utc)`` is the non-
    # deprecated replacement for ``datetime.utcnow()`` (removed-in-3.12
    # deprecation); ``isoformat()`` on an aware UTC datetime yields a
    # ``+00:00`` suffix which we normalize to ``Z`` so the stored value
    # matches the conventional ISO-8601 UTC form the spec algorithm
    # writes (``datetime.utcnow().isoformat() + "Z"``).
    rotated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        engine.write_state_meta("last_rotate_at", rotated_at)
        state_meta_written = True
    except Exception as exc:  # noqa: BLE001 -- last-resort guard
        # The backup (rotate's primary work) already succeeded, so a
        # failure to stamp ``state_meta`` should not present as a total
        # failure. Log it and report the degraded outcome in the text.
        state_meta_written = False
        logger.warning("[lcm] /lcm rotate: state_meta write failed: %s", exc)

    # Render the outcome. The backup always succeeded by the time we
    # reach here; the snapshot-clear / WAL / state_meta lines reflect
    # the actual (best-effort) results.
    lines = [
        "[lcm] rotate complete",
        f"Backup: {backup_path}",
    ]
    if snapshot_cleared:
        lines.append(f"Snapshot cache cleared for session {session_id}")
    else:
        lines.append(
            f"Snapshot cache: nothing to clear for session {session_id} (no cached assembly)"
        )
    wal_label = "compacted" if wal_compacted else "compaction skipped (best-effort)"
    if state_meta_written:
        lines.append(f"WAL {wal_label}; state_meta.last_rotate_at updated")
    else:
        lines.append(f"WAL {wal_label}; state_meta.last_rotate_at write FAILED")
    return "\n".join(lines)
