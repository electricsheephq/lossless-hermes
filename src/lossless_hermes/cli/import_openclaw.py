"""``lossless-hermes import-openclaw`` and ``/lcm import-openclaw`` body.

Owner-gated one-shot migration from an existing OpenClaw ``~/.openclaw/``
tree to a fresh Hermes-side ``~/.hermes/lossless-hermes/`` tree per
ADR-025 §"Decision".

Exposes three callables:

* :func:`main` — argparse-driven CLI entry (``lossless-hermes import-openclaw``).
* :func:`run_slash` — slash-command handler (``/lcm import-openclaw``)
  consumed by the dispatcher in
  :mod:`lossless_hermes.plugin.commands` (issue 08-01 wired the entry).
* :func:`import_openclaw` — programmatic API used by both fronts.

### Algorithm (ADR-025 §"Decision" lines 85-94, verbatim)

1. Verify source path exists; ``PRAGMA integrity_check`` on ``<source>/lcm.db``.
2. Verify destination is writable. If dest DB exists, refuse unless ``--force``;
   the refusal message reports the existing-data summary (conversation count +
   date range).
3. :func:`shutil.copy2` ``lcm.db`` (preserve timestamps; no symlink, no move).
4. :func:`shutil.copytree` ``lcm-files/`` → ``<dest>/large-files/`` (ADR-002
   layout convention; skip if source dir missing).
5. :func:`shutil.copy2` ``credentials/voyage-api-key`` → ``<dest>/credentials/``;
   chmod 0o600 on the key file, parent dir 0o700.
6. Open destination DB and run :func:`run_lcm_migrations` (idempotent;
   preserves ``lcm_migration_state`` per ADR-026).
7. Sample N rows from ``messages`` (default N=100); recompute
   ``build_message_identity_hash(role, content)`` and compare to stored
   ``identity_hash``. Per ADR-025 line 91 + Spike 003: mismatches are
   expected for legacy back-fill drift and are reported but non-fatal.
8. Insert/upsert ``state_meta`` row ``lcm_db_imported_at = NOW(),
   source_path = <source>`` so subsequent ``import-openclaw`` calls
   fast-fail unless ``--force``.
9. Print operator next-steps (config.yaml edits per ADR-001 consequences).

### Owner-gating (ADR-025 §"Consequences" + ADR-013)

* The standalone CLI (``lossless-hermes import-openclaw``) bypasses the
  gateway gate entirely — single-user CLI invocation is implicitly
  authorized.
* The ``/lcm import-openclaw`` slash invocation goes through the upstream
  ``slash_access.SlashAccessPolicy`` configured in the Hermes config
  (this module never inspects owner-status).

### ``state_meta`` design note

There is no ``state_meta`` table in the upstream lossless-claw TS schema
(grep over commit ``1f07fbd``: 0 hits). Adding it to
:mod:`lossless_hermes.db.migration` would surface in
``scripts/schema_diff.sh --verify-subset`` as a "Python object the
reference does not have" → exit 5 (per the script's documented behavior).
The migration table is therefore created **ad-hoc inside this command**
with ``IF NOT EXISTS`` and confined to Hermes-side bookkeeping. Future
Hermes-side state should reuse this table; if it grows, promote to
``migration.py`` only behind a schema-diff scope adjustment.

See:

* ADR-025 — OpenClaw migration.
* ADR-013 — owner gating.
* ADR-001 / ADR-002 — Hermes plugin data layout.
* ADR-026 — schema versioning (migration ladder idempotency).
* ``docs/spike-results/003-identity-hash.md`` — byte-identical hash
  cross-language.
* ``docs/porting-guides/storage.md`` §10.1 — migration story.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lossless_hermes.db.connection import close_lcm_db, open_lcm_db
from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.store.message_identity import build_message_identity_hash

__all__ = [
    "ImportResult",
    "import_openclaw",
    "main",
    "run_slash",
]

_log = logging.getLogger("lossless_hermes.cli.import_openclaw")

# Defaults per ADR-025 §"Decision" line 78. Resolved at call time via
# ``Path.expanduser`` so the home directory of the invoking user wins
# (not the directory of whoever installed the package).
_DEFAULT_FROM = "~/.openclaw"
_DEFAULT_TO = "~/.hermes/lossless-hermes"
_DEFAULT_VALIDATE_ROWS = 100

# Disk-space safety margin per ADR-025 line 113 (1.2× source size).
_DISK_SAFETY_MARGIN = 1.2


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class ImportResult:
    """Outcome of an :func:`import_openclaw` invocation.

    Used by the CLI / slash handlers to render operator-facing output.
    ``ok=False`` paths populate ``error`` with a one-line summary; the CLI
    main translates that into a non-zero exit code.

    Attributes:
        ok: ``True`` on success, ``False`` on a recoverable failure
            (source missing, destination refused without ``--force``,
            schema-newer-than-supported, etc.). Unrecoverable failures
            (corrupt source DB after PRAGMA integrity_check, OS-level
            disk-full) raise instead.
        dry_run: ``True`` when the invocation was a dry run; the report
            describes what would have happened.
        report: Multi-line operator-facing summary. Final newline omitted.
        error: One-line error message when ``ok=False``; empty otherwise.
        validated: Number of message rows sampled for identity_hash check.
        matched: Number of sampled rows where computed hash matched stored.
        mismatched: ``validated - matched``. Per ADR-025 line 91, non-fatal.
    """

    ok: bool
    dry_run: bool
    report: str
    error: str = ""
    validated: int = 0
    matched: int = 0
    mismatched: int = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_path(raw: str | os.PathLike[str]) -> Path:
    """Expand ``~`` and resolve to an absolute :class:`Path`.

    Matches the CLI invocation surface (``--from ~/.openclaw``) — leaves
    the path in its expanded form so error messages echo what the
    operator actually intended (not a partially-expanded form).
    """
    return Path(os.path.expanduser(str(raw))).resolve()


def _format_bytes(n: int) -> str:
    """Render a byte count as a human-friendly GB/MB string."""
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f} GB"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} MB"
    if n >= 1_000:
        return f"{n / 1_000:.1f} KB"
    return f"{n} B"


def _existing_data_summary(dest_db: Path) -> str:
    """Return a "N conversations recorded between $start and $end" summary.

    Best-effort: opens the destination DB read-only and queries
    ``conversations``. If the table doesn't exist (un-migrated stray
    file), the summary degrades to a generic message. Used by step 2's
    refusal text + the disk-warning text in step 3.

    The connection is opened directly via :func:`sqlite3.connect` (NOT
    :func:`open_lcm_db`) so we don't load sqlite-vec or apply pragmas
    against a file we're only inspecting — the destination DB at this
    point may have been written by a different Hermes version and we
    don't want to mutate it inside a probe.
    """
    try:
        conn = sqlite3.connect(f"file:{dest_db}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return f"destination {dest_db} exists but could not be opened read-only"
    try:
        try:
            row = conn.execute(
                "SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM conversations"
            ).fetchone()
        except sqlite3.OperationalError:
            return f"destination {dest_db} exists but has no conversations table"
        count, start, end = row if row is not None else (0, None, None)
        if not count:
            return f"destination {dest_db} exists but has 0 conversations"
        return f"destination has {count} conversation(s) recorded between {start} and {end}"
    finally:
        conn.close()


def _disk_space_ok(source_db: Path, dest_dir: Path) -> tuple[bool, str]:
    """Check destination has at least 1.2× source size free.

    Returns ``(ok, message)`` where ``message`` is empty when ``ok=True``
    and a one-line warning otherwise.
    """
    try:
        source_size = source_db.stat().st_size
    except OSError as exc:
        return True, f"could not stat source ({exc}); skipping disk-space precheck"
    try:
        free = shutil.disk_usage(dest_dir).free
    except OSError as exc:
        return True, f"could not measure dest free space ({exc}); skipping disk-space precheck"
    required = int(source_size * _DISK_SAFETY_MARGIN)
    if free >= required:
        return True, ""
    return False, (
        f"WARNING: destination has {_format_bytes(free)} free; "
        f"source is {_format_bytes(source_size)}. Import will likely fail."
    )


def _verify_source(source_dir: Path) -> str | None:
    """Verify ``source_dir`` is a usable OpenClaw root.

    Returns ``None`` on success, or a one-line error message on failure.
    Mirrors ADR-025 step 1.
    """
    if not source_dir.exists():
        return f"source path {source_dir} does not exist"
    if not source_dir.is_dir():
        return f"source path {source_dir} is not a directory"
    source_db = source_dir / "lcm.db"
    if not source_db.exists():
        return f"source path {source_dir} does not contain lcm.db"
    # PRAGMA integrity_check against a read-only handle. Per ADR-025 step
    # 1 we use a separate connection and don't run the full LCM connection
    # setup — we're inspecting the file, not opening it for use.
    try:
        conn = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    except sqlite3.OperationalError as exc:
        return f"source {source_db} is not a valid SQLite file: {exc}"
    try:
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.DatabaseError as exc:
            return f"source {source_db} failed integrity_check: {exc}"
    finally:
        conn.close()
    if row is None or (row[0] or "").lower() != "ok":
        return f"source {source_db} failed integrity_check: {row[0] if row else 'no result'}"
    return None


def _sample_identity_hashes(conn: sqlite3.Connection, n: int) -> tuple[int, int, int]:
    """Sample up to ``n`` message rows and validate ``identity_hash``.

    Returns ``(validated, matched, mismatched)``. Rows with ``NULL``
    ``identity_hash`` are not counted toward ``validated`` (pre-v4.1 rows
    that never had hashes written are not a mismatch — the back-fill
    helper will populate them on first ingest).

    Per ADR-025 step 7 + Spike 003: mismatches indicate pre-existing
    drift (Eva's legacy back-fill) and are non-fatal. The caller renders
    the count in the operator summary.
    """
    # SQLite's ``ORDER BY RANDOM() LIMIT n`` requires a full table scan but
    # for the validation use case (n ≤ 1000 typically) the cost is
    # acceptable — and unlike ``OFFSET`` on a row-count-derived index, it
    # avoids overcounting deleted rows.
    try:
        total_row = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE identity_hash IS NOT NULL"
        ).fetchone()
    except sqlite3.OperationalError:
        # No ``messages`` table on a freshly-imported pre-v4.1 DB? The
        # migration ladder above guarantees it exists, so this should
        # not fire. Defensive return.
        return (0, 0, 0)
    total = int(total_row[0]) if total_row else 0
    if total == 0:
        return (0, 0, 0)
    take = min(n, total)
    # Random sample. seed-able via the ``random`` module; tests can
    # ``random.seed(...)`` for determinism if needed.
    rows = list(
        conn.execute(
            "SELECT role, content, identity_hash FROM messages "
            "WHERE identity_hash IS NOT NULL "
            "ORDER BY RANDOM() LIMIT ?",
            (take,),
        )
    )
    matched = 0
    mismatched = 0
    for role, content, stored in rows:
        try:
            recomputed = build_message_identity_hash(role, content)
        except Exception:  # noqa: BLE001 -- defensive; build hash should never raise
            mismatched += 1
            continue
        if recomputed == stored:
            matched += 1
        else:
            mismatched += 1
    return (len(rows), matched, mismatched)


_SCHEMA_NEWER_MARKERS: tuple[str, ...] = (
    "no such column",
    "unknown column",
    "no column named",
)


def _is_schema_newer_error(exc: sqlite3.DatabaseError) -> bool:
    """Heuristic: did this DatabaseError look like 'source schema is newer'?

    Mirrors the spec's "source DB schema is newer than this port supports"
    clean-error branch. The migration ladder asserts presence of every
    column it knows about; an UNKNOWN column triggered by an ALTER from a
    future LCM version would surface here.
    """
    msg = str(exc).lower()
    return any(marker in msg for marker in _SCHEMA_NEWER_MARKERS)


def _ensure_state_meta_table(conn: sqlite3.Connection) -> None:
    """Create the Hermes-side ``state_meta`` table if missing.

    See module-docstring §"``state_meta`` design note" for why this lives
    in the import command rather than ``db/migration.py``: it's a
    Hermes-only concept and adding it to the migration ladder would
    surface in the schema-diff CI gate (script exits 5 on Python-only
    objects not in the TS reference).

    Schema: ``(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)``.
    Single-row-per-key store; callers UPSERT via ``ON CONFLICT``.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS state_meta (
          key TEXT PRIMARY KEY,
          value TEXT,
          updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )


def _write_imported_at(conn: sqlite3.Connection, source_path: Path) -> None:
    """UPSERT the ``lcm_db_imported_at`` + ``source_path`` rows.

    Two keys to keep them queryable independently:
    * ``lcm_db_imported_at`` — timestamp of the most-recent import.
    * ``lcm_db_imported_from`` — last source path (operator audit trail).

    Both UPSERT so a ``--force`` re-import overwrites cleanly rather than
    raising or duplicating rows (ADR-025 §"Open questions" #3 + the
    spec's 10% confidence risk).
    """
    conn.execute(
        """
        INSERT INTO state_meta (key, value, updated_at)
        VALUES ('lcm_db_imported_at', datetime('now'), datetime('now'))
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """
    )
    conn.execute(
        """
        INSERT INTO state_meta (key, value, updated_at)
        VALUES ('lcm_db_imported_from', ?, datetime('now'))
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (str(source_path),),
    )
    conn.commit()


def _check_destination_state(
    dest_db: Path, *, force: bool, dest_large_files: Path | None = None
) -> tuple[bool, str]:
    """Return ``(ok_to_proceed, message)`` for the destination DB.

    Step 2 of the spec. Two destructive targets are checked:

    1. ``dest_db`` (``<dest>/lcm.db``) — if present, any rows recorded
       since first import would be lost on overwrite.
    2. ``dest_large_files`` (``<dest>/large-files/``) — if present, any
       blob files would be lost on ``shutil.rmtree`` before
       ``copytree`` (see step 4 below).

    PR #79 review-fix: previously this only checked ``dest_db``. A
    partial-import state where ``lcm.db`` was deleted but
    ``large-files/`` survived would silently rmtree the large-files dir
    without ``--force`` consent. ADR-025 §Consequences makes ``--force``
    the operator-acknowledged destructive path for ANY existing data.
    """
    summaries: list[str] = []
    if dest_db.exists():
        summaries.append(_existing_data_summary(dest_db))
    if dest_large_files is not None and dest_large_files.exists():
        try:
            entry_count = sum(1 for _ in dest_large_files.rglob("*"))
        except OSError:
            entry_count = -1
        if entry_count == -1:
            summaries.append(f"{dest_large_files}/ exists (unreadable)")
        elif entry_count > 0:
            summaries.append(f"{dest_large_files}/ holds {entry_count} entries")

    if not summaries:
        return True, ""
    summary = "; ".join(summaries)
    if force:
        return True, (
            f"--force given; overwriting destination. {summary}; --force will discard them."
        )
    return False, (
        f"destination {dest_db.parent} holds existing data: {summary}. "
        f"Refusing without --force. Re-run with --force to overwrite "
        f"(acknowledges discarding existing rows + large-files)."
    )


def _operator_next_steps() -> str:
    """Render the post-import next-steps text per ADR-025 §"Decision" line 94."""
    return (
        "Import complete. Next steps:\n"
        "  1. Enable lossless-hermes in plugins.enabled in ~/.hermes/config.yaml\n"
        "  2. Set context.engine: lcm in ~/.hermes/config.yaml\n"
        "  3. Start Hermes - your conversations are immediately available."
    )


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------


def import_openclaw(
    *,
    source: str | os.PathLike[str] = _DEFAULT_FROM,
    destination: str | os.PathLike[str] = _DEFAULT_TO,
    force: bool = False,
    validate_rows: int = _DEFAULT_VALIDATE_ROWS,
    dry_run: bool = False,
    disk_check_yes: bool = False,
) -> ImportResult:
    """Run the OpenClaw → Hermes import.

    Programmatic entry point shared by :func:`main` and :func:`run_slash`.
    Steps follow ADR-025 §"Decision" lines 85-94 in order.

    Args:
        source: OpenClaw root directory (contains ``lcm.db``, optional
            ``lcm-files/``, optional ``credentials/voyage-api-key``).
        destination: Target directory under
            ``~/.hermes/<plugin-name>/``. Created if missing.
        force: When ``True``, overwrite an existing destination DB.
            Without ``--force``, an existing destination causes the
            command to refuse (step 2).
        validate_rows: Number of message rows to sample for
            ``identity_hash`` validation. Per ADR-025 step 7.
        dry_run: When ``True``, report what would happen and touch no
            files. Used by the ``--dry-run`` CLI flag and tests that
            need to verify no destination-side side effects.
        disk_check_yes: Treat the disk-space warning as auto-confirmed
            (``--force`` implies this for non-interactive use).

    Returns:
        An :class:`ImportResult` describing the outcome. ``ok=False`` is
        used for operator-facing recoverable failures (source missing,
        destination refused without ``--force``, schema-newer error);
        unrecoverable failures (OS-level disk-full mid-copy) raise.
    """
    source_dir = _resolve_path(source)
    dest_dir = _resolve_path(destination)
    source_db = source_dir / "lcm.db"
    dest_db = dest_dir / "lcm.db"

    report_lines: list[str] = []

    def _say(msg: str) -> None:
        report_lines.append(msg)

    _say(f"source:      {source_dir}")
    _say(f"destination: {dest_dir}")

    # Step 1: verify source.
    src_err = _verify_source(source_dir)
    if src_err is not None:
        return ImportResult(
            ok=False, dry_run=dry_run, report="\n".join(report_lines), error=src_err
        )
    _say("source: OK (integrity_check passed)")

    # Step 2: destination state check.
    dest_ok, dest_msg = _check_destination_state(
        dest_db, force=force, dest_large_files=dest_dir / "large-files"
    )
    if dest_msg:
        _say(dest_msg)
    if not dest_ok:
        return ImportResult(
            ok=False,
            dry_run=dry_run,
            report="\n".join(report_lines),
            error=dest_msg,
        )

    # Disk-space precheck (ADR-025 line 113). Use the parent of dest_dir
    # because dest_dir itself may not exist yet.
    disk_probe = dest_dir if dest_dir.exists() else dest_dir.parent
    if disk_probe.exists():
        disk_ok, disk_warn = _disk_space_ok(source_db, disk_probe)
        if not disk_ok:
            _say(disk_warn)
            if not (force or disk_check_yes):
                return ImportResult(
                    ok=False,
                    dry_run=dry_run,
                    report="\n".join(report_lines),
                    error=disk_warn + " (re-run with --force to bypass)",
                )

    if dry_run:
        _say("[dry-run] would copy lcm.db, lcm-files/, credentials/, run migrations, validate.")
        _say("[dry-run] no files have been touched.")
        return ImportResult(ok=True, dry_run=True, report="\n".join(report_lines))

    # Step 3: copy lcm.db. ``shutil.copy2`` preserves timestamps. Parent
    # ``mkdir`` is mandatory because dest_dir may not exist yet on a
    # first import.
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_db, dest_db)
    _say(f"copied {source_db.name} → {dest_db}")

    # Step 4: copy lcm-files/ → large-files/ (ADR-001/002 layout rename).
    src_files = source_dir / "lcm-files"
    dst_files = dest_dir / "large-files"
    if src_files.exists():
        if dst_files.exists():
            # ``copytree`` refuses an existing target. ``force`` is the
            # operator-acknowledged path; remove and retry.
            shutil.rmtree(dst_files)
        shutil.copytree(src_files, dst_files)
        _say(f"copied lcm-files/ → large-files/ ({sum(1 for _ in dst_files.rglob('*'))} entries)")
    else:
        _say("source has no lcm-files/ directory; skipping large-files copy")

    # Step 5: copy credentials with locked-down modes.
    src_cred = source_dir / "credentials" / "voyage-api-key"
    if src_cred.exists():
        dst_cred_dir = dest_dir / "credentials"
        dst_cred_dir.mkdir(parents=True, exist_ok=True)
        # mkdir doesn't set mode 0o700 on existing dirs; chmod afterwards.
        try:
            os.chmod(dst_cred_dir, 0o700)
        except OSError as exc:
            _log.warning("could not chmod credentials/ dir: %s", exc)
        dst_cred = dst_cred_dir / "voyage-api-key"
        shutil.copy2(src_cred, dst_cred)
        try:
            os.chmod(dst_cred, 0o600)
        except OSError as exc:
            _log.warning("could not chmod voyage-api-key: %s", exc)
        _say("copied credentials/voyage-api-key (mode 0o600, dir 0o700)")
    else:
        _say("source has no credentials/voyage-api-key; skipping credentials copy")

    # Step 6: open dest DB and run migrations. Errors classified as
    # "schema newer than supported" surface a clean message; everything
    # else propagates as a DatabaseError so operators get the raw error.
    conn = open_lcm_db(dest_db)
    try:
        try:
            run_lcm_migrations(conn)
        except sqlite3.DatabaseError as exc:
            if _is_schema_newer_error(exc):
                err = (
                    f"source DB schema is newer than this port supports "
                    f"({exc}); upgrade lossless-hermes first"
                )
                return ImportResult(
                    ok=False,
                    dry_run=False,
                    report="\n".join(report_lines),
                    error=err,
                )
            raise
        _say("ran migrations on destination DB (idempotent)")

        # Step 7: identity_hash sample validation.
        validated, matched, mismatched = _sample_identity_hashes(conn, validate_rows)
        if validated == 0:
            _say("identity_hash: no rows with non-null identity_hash to validate")
        else:
            note = ""
            if mismatched > 0:
                # Per ADR-025 line 91: pre-existing back-fill drift is
                # expected on legacy rows; the mismatched count is
                # non-fatal.
                note = " (likely pre-existing back-fill drift; not fatal)"
            _say(f"validated={validated}, matched={matched}, mismatched={mismatched}{note}")

        # Step 8: state_meta write.
        _ensure_state_meta_table(conn)
        _write_imported_at(conn, source_dir)
        _say("recorded state_meta.lcm_db_imported_at + lcm_db_imported_from")
    finally:
        close_lcm_db(conn)

    # Step 9: operator next steps.
    _say("")
    _say(_operator_next_steps())

    return ImportResult(
        ok=True,
        dry_run=False,
        report="\n".join(report_lines),
        validated=validated,
        matched=matched,
        mismatched=mismatched,
    )


# ---------------------------------------------------------------------------
# Argparse + CLI main
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Construct the argparse surface for ``lossless-hermes import-openclaw``.

    Exposed as a module-level function so tests can introspect the
    argument names (helps when adding new flags later — one parser
    definition, two consumers).
    """
    parser = argparse.ArgumentParser(
        prog="lossless-hermes import-openclaw",
        description=(
            "One-shot migration from an existing OpenClaw ~/.openclaw/ "
            "tree to a fresh Hermes ~/.hermes/lossless-hermes/ tree. "
            "Idempotent without --force; --force overwrites the destination "
            "(acknowledges discarding any existing Hermes-side data). "
            "Disk usage doubles temporarily during the import."
        ),
    )
    parser.add_argument(
        "--from",
        dest="source",
        default=_DEFAULT_FROM,
        help=f"OpenClaw root directory (default: {_DEFAULT_FROM}).",
    )
    parser.add_argument(
        "--to",
        dest="destination",
        default=_DEFAULT_TO,
        help=f"Hermes plugin data directory (default: {_DEFAULT_TO}).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Overwrite an existing destination DB. Discards any "
            "Hermes-side conversations recorded since first import."
        ),
    )

    def _positive_int(raw: str) -> int:
        # PR #79 review-fix: argparse `type=int` accepted negative values,
        # which would silently flow into `LIMIT -N` (SQLite treats negative
        # LIMIT as no limit → full-table scan, defeating "sample N").
        # Reject at parse time with an actionable error.
        try:
            value = int(raw)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"must be an integer: {raw!r}") from exc
        if value <= 0:
            raise argparse.ArgumentTypeError(
                f"--validate-rows must be a positive integer (got {value}). "
                "Use a value >= 1 to sample N rows. Negative / zero values "
                "would silently bypass the validation sampler."
            )
        return value

    parser.add_argument(
        "--validate-rows",
        type=_positive_int,
        default=_DEFAULT_VALIDATE_ROWS,
        metavar="N",
        help=(
            f"Sample N message rows (N >= 1) for identity_hash validation "
            f"(default: {_DEFAULT_VALIDATE_ROWS}). Mismatches are reported "
            f"but non-fatal per ADR-025."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would happen; touch no files.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """``lossless-hermes import-openclaw`` CLI entry.

    Returns a POSIX exit code: 0 on success, 1 on a recoverable failure
    (source missing, destination refused without ``--force``, schema-newer
    error). Unrecoverable failures (OS-level disk-full) propagate as
    :class:`SystemExit` from argparse / Python's default exception path.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]`` when
            ``None``). Exposed for tests that want to drive the CLI
            without subprocess overhead.

    Returns:
        ``0`` on success, ``1`` on a recoverable failure.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    result = import_openclaw(
        source=args.source,
        destination=args.destination,
        force=args.force,
        validate_rows=args.validate_rows,
        dry_run=args.dry_run,
        # --force implies "I have read the disk-space warning."
        disk_check_yes=args.force,
    )
    # Print the operator-facing report.
    if result.report:
        print(result.report)
    if not result.ok:
        if result.error:
            print(f"error: {result.error}", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------
# Slash-command bridge
# ---------------------------------------------------------------------------


def _parse_slash_tokens(tokens: list[str]) -> argparse.Namespace:
    """Re-parse slash-command tokens through the argparse surface.

    The router's :func:`parse_lcm_command` pre-parses ``--from``,
    ``--to``, etc. as a best-effort, but we re-invoke argparse here so
    the surface is identical to the standalone CLI (no drift between
    the two entry points). ``argparse.ArgumentParser.parse_args`` will
    raise :class:`SystemExit` on invalid input; we trap that and return
    a sentinel so the slash handler can surface a friendly error.
    """
    parser = build_parser()
    # argparse calls sys.exit on -h or invalid args. The slash dispatcher
    # cannot tolerate that — wrap it in ``exit_on_error=False`` (Python
    # 3.9+) and re-raise as the dispatcher's expected error type.
    parser.exit_on_error = False
    return parser.parse_args(tokens)


def run_slash(parsed: Any) -> str:
    """``/lcm import-openclaw`` slash handler.

    Reads the parsed slash command from
    :class:`lossless_hermes.plugin.commands.ParsedLcmCommand` and calls
    :func:`import_openclaw`. Owner-gating is upstream (ADR-013) — this
    function does NOT inspect owner-context.

    Returns the operator-facing text the dispatcher renders.
    """
    raw_tokens: list[str] = list(getattr(parsed, "tokens", []) or [])
    try:
        ns = _parse_slash_tokens(raw_tokens)
    except (argparse.ArgumentError, SystemExit) as exc:
        return (
            f"/lcm import-openclaw: argument parse error - {exc!s}. "
            "Usage: /lcm import-openclaw [--from PATH] [--to PATH] [--force] "
            "[--validate-rows N] [--dry-run]"
        )
    result = import_openclaw(
        source=ns.source,
        destination=ns.destination,
        force=ns.force,
        validate_rows=ns.validate_rows,
        dry_run=ns.dry_run,
        disk_check_yes=ns.force,
    )
    if not result.ok:
        prefix = f"/lcm import-openclaw failed: {result.error}\n\n" if result.error else ""
        return prefix + result.report
    return result.report


if __name__ == "__main__":  # pragma: no cover - entry-point shim
    sys.exit(main())
