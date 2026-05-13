"""LCM database backup primitive — ``VACUUM INTO`` to a timestamped ``.bak``.

Ports ``lossless-claw/src/plugin/lcm-db-backup.ts`` (commit ``1f07fbd``, 82
LOC) to Python. The module exposes three public callables consumed by three
disjoint callers in later epics:

1. ``/lcm backup`` (Epic 08 dispatch) → :func:`write_lcm_database_backup`
   directly, via :mod:`lossless_hermes.commands.backup`.
2. ``/lcm rotate`` (Epic 08-16) → backs up before performing the rotation.
3. ``apply_doctor_cleaners`` (Epic 08-08) → backs up BEFORE the destructive
   ``BEGIN IMMEDIATE``. The caller intercepts :class:`LcmDatabaseBackupError`
   and converts it to a structured ``{"kind": "unavailable", "reason": ...}``
   per the unavailable-reason contract.

### Why ``VACUUM INTO`` and not ``.backup()``?

The TS source uses ``VACUUM INTO``. The two SQLite primitives differ in
output shape:

* ``VACUUM INTO 'path'`` — writes a fresh, fully-compacted database file
  with no WAL sidecar. The result file is portable and immediately usable
  as a standalone DB (open with any SQLite tool, no ``-shm`` / ``-wal``
  companions needed). Requires no active transaction on the source conn.
* ``conn.backup(dest_conn)`` — Python stdlib ``sqlite3.Connection.backup``;
  copies pages from source to dest. The result file may carry a WAL
  sidecar depending on dest's journal mode. Works concurrently with an
  open transaction on source.

For backup-then-restore workflows (the ``/lcm backup`` + ``apply doctor
cleaners`` use cases) the portable, no-WAL result of ``VACUUM INTO`` is
strictly preferable. We match the TS choice.

### Path format (deviates from TS)

The Python port follows the spec example format
(``epics/08-cli-ops/08-09-backup.md``):

* No label: ``<db_path>.<YYYY-MM-DDTHHMMSS>-<rand6>.bak``
  e.g. ``/path/to/lcm.db.2026-05-13T143055-a3f9b2.bak``
* With label: ``<db_path>.<label>.<YYYY-MM-DDTHHMMSS>-<rand6>.bak``
  e.g. ``/path/to/lcm.db.doctor-cleaners.2026-05-13T143055-a3f9b2.bak``

This differs slightly from the TS source (which strips all of ``-:.`` from
the ISO string and joins label-timestamp-rand with hyphens — see
``lossless-claw/src/plugin/lcm-db-backup.ts:25-30``). The Python format
keeps the ``YYYY-MM-DD`` hyphens and uses a dot to separate the optional
label segment from the timestamp segment, matching the spec's example
literally. Acceptance criteria in the spec assert the spec format; the
backup files don't cross the TS↔Python boundary so the format diff is
internal-only.

### Random suffix collision avoidance

The 6-character random suffix prevents collisions on subsecond consecutive
backups. The TS source uses ``Math.random().toString(36).slice(2, 8)``
(~36 bits of entropy from a non-cryptographic PRNG); the Python port uses
:func:`secrets.token_hex(3)` (48 bits of cryptographic entropy). The
spec's example ``a3f9b2`` is base36-styled but the hex form is equally
valid as a collision-avoidance suffix — both produce 6 lowercase
alphanumeric chars.

### In-memory DB guard

``VACUUM INTO`` cannot copy from a ``:memory:`` database to a file path
without first re-opening the source as a file-backed DB — the spec
mandates raising :class:`LcmDatabaseBackupError` for in-memory inputs so
callers ``/lcm backup`` / ``apply_doctor_cleaners`` can render the
"unavailable" branch consistently. We re-use
:func:`lossless_hermes.db.connection.get_file_backed_database_path` for
the file-vs-memory classification so all path-handling lives in one
place.

### Wave-N provenance

The TS source ``lcm-db-backup.ts`` has NO Wave-N audit comments (verified
via ``grep -n "Wave-" src/plugin/lcm-db-backup.ts`` against commit
``1f07fbd``). Per ADR-029 this module is not tagged. The same applies to
the test module: no Wave-N regression tests required.

See:

* ``epics/08-cli-ops/08-09-backup.md`` — this issue.
* ``lossless-claw/src/plugin/lcm-db-backup.ts`` — TS source at commit
  ``1f07fbd`` (pr-613 head).
* ``docs/porting-guides/plugin-glue.md`` §"/lcm slash commands — full
  inventory" line 427 — ``VACUUM INTO`` to ``<db>.<timestamp>-<rand>.bak``.
* ``docs/adr/029-wave-fix-provenance.md`` — provenance policy; this
  module has no Wave-N markers (TS source has none).
"""

from __future__ import annotations

import os
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Union

from lossless_hermes.db.connection import Connection, get_file_backed_database_path

__all__ = [
    "LcmDatabaseBackupError",
    "build_lcm_database_backup_path",
    "write_lcm_database_backup",
]


class LcmDatabaseBackupError(RuntimeError):
    """Raised when a backup cannot be written.

    Mirrors the TS ``LcmDatabaseBackupError`` class (which doesn't exist in
    the TS source — the TS code throws raw ``Error`` instances; the
    Python port introduces a typed exception so callers can ``except``
    narrowly).

    Three failure surfaces, each surfacing as this exception:

    * In-memory source DB (``:memory:`` / ``file::memory:...``) — the
      destination path cannot be constructed because there is no source
      file path to anchor it.
    * ``VACUUM INTO`` itself fails (destination disk full, permission
      denied, destination path already exists and is a directory).
    * Destination parent directory cannot be created.

    Callers (notably :mod:`lossless_hermes.commands.doctor` cleaners and
    ``/lcm backup``) intercept this exception and render an "unavailable"
    or "backup failed" branch in the command output.
    """


# ---------------------------------------------------------------------------
# SQL-quoting helper — VACUUM INTO doesn't accept parameter binding
# ---------------------------------------------------------------------------


def _quote_sql_string(value: str) -> str:
    """SQL-literal-quote a string by doubling single quotes.

    Ports the TS ``quoteSqlString`` helper at ``lcm-db-backup.ts:6-8``.
    ``VACUUM INTO`` requires the destination path as a literal string in
    the SQL statement (SQLite does not bind parameters to ``VACUUM INTO``);
    the only safe escape is ``'`` → ``''``. Both the TS and Python ports
    use this approach.
    """
    return "'" + value.replace("'", "''") + "'"


# ---------------------------------------------------------------------------
# Path construction — public + private helpers
# ---------------------------------------------------------------------------


def _format_backup_timestamp() -> str:
    """Return the timestamp segment for a backup path: ``YYYY-MM-DDTHHMMSS``.

    Uses UTC (matches the TS ``new Date().toISOString()`` UTC semantics).
    Strips colons and dots from the ISO 8601 form; keeps the ``T`` separator
    and the ``YYYY-MM-DD`` dashes. Milliseconds and the trailing ``Z`` are
    omitted because the spec's example format
    (``epics/08-cli-ops/08-09-backup.md`` lines 53-54) doesn't include them.

    See module docstring §"Path format" for the rationale.
    """
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H%M%S")


def build_lcm_database_backup_path(
    db_path: Union[str, Path],
    *,
    label: str | None = None,
) -> Path:
    """Build the absolute destination path for a backup of ``db_path``.

    Ports the TS ``buildLcmDatabaseBackupPath`` (lines 19-31). Returns the
    absolute path that :func:`write_lcm_database_backup` will pass to
    ``VACUUM INTO``.

    Path format (see module docstring):

    * No label: ``<abs_db_path>.<YYYY-MM-DDTHHMMSS>-<rand6>.bak``
    * With label: ``<abs_db_path>.<label>.<YYYY-MM-DDTHHMMSS>-<rand6>.bak``

    The 6-char random suffix uses :func:`secrets.token_hex(3)` to avoid
    collisions on subsecond consecutive backups (acceptance criterion:
    "Subsecond consecutive backups produce distinct paths").

    Args:
        db_path: Source database path. ``:memory:`` and ``file::memory:...``
            inputs raise :class:`LcmDatabaseBackupError` because in-memory
            DBs have no file path to anchor the backup name to.
        label: Optional label embedded in the path before the timestamp.
            The spec doesn't normalize the label (unlike TS which lowercases
            and collapses non-alphanumeric runs to dashes); callers pass
            either ``None`` or a pre-normalized string like
            ``"doctor-cleaners"`` / ``"backup"`` / ``"rotate"``. Empty
            string is treated as no label (defensive — the
            ``""`` form would otherwise produce ``lcm.db..2026-...`` with
            a doubled dot).

    Returns:
        Absolute :class:`pathlib.Path` to the destination ``.bak`` file.

    Raises:
        LcmDatabaseBackupError: ``db_path`` is in-memory or empty.
    """
    file_backed = get_file_backed_database_path(db_path)
    if file_backed is None:
        raise LcmDatabaseBackupError(
            f"Cannot back up in-memory database (db_path={db_path!r}). "
            "VACUUM INTO requires a file-backed source — "
            "open the DB via open_lcm_db(path=<filesystem path>)."
        )

    timestamp = _format_backup_timestamp()
    suffix = secrets.token_hex(3)  # 6 lowercase hex chars; 24 bits entropy.

    # Spec format: <abs_db_path>.<label>.<YYYY-MM-DDTHHMMSS>-<rand6>.bak
    # or <abs_db_path>.<YYYY-MM-DDTHHMMSS>-<rand6>.bak if no label.
    if label:
        filename = f"{os.path.basename(file_backed)}.{label}.{timestamp}-{suffix}.bak"
    else:
        filename = f"{os.path.basename(file_backed)}.{timestamp}-{suffix}.bak"

    return Path(os.path.dirname(file_backed)) / filename


# ---------------------------------------------------------------------------
# Public API: write_lcm_database_backup
# ---------------------------------------------------------------------------


def write_lcm_database_backup(
    db: Connection,
    *,
    label: str | None = None,
    db_path: Union[str, Path],
) -> Path:
    """Write a fresh SQLite backup file via ``VACUUM INTO`` and return its path.

    The single primitive consumed by ``/lcm backup``, ``/lcm rotate``, and
    ``apply_doctor_cleaners``. Performs, in order:

    1. Construct the destination path via
       :func:`build_lcm_database_backup_path` — raises
       :class:`LcmDatabaseBackupError` if ``db_path`` is in-memory.
    2. ``mkdir -p`` the destination's parent directory (no-op if already
       exists). Any :class:`OSError` is wrapped in
       :class:`LcmDatabaseBackupError` so callers see one exception type.
    3. Defensively ``ROLLBACK`` any in-flight transaction on ``db``.
       ``VACUUM INTO`` requires no active transaction on the source
       connection (SQLite raises ``SQLITE_ERROR: cannot VACUUM from within
       a transaction``). The transaction-mutex guarantees the outer
       caller releases the lock before this primitive runs, but defensive
       ``ROLLBACK`` is cheap and catches the corner case where a caller
       has called ``BEGIN`` without releasing.
    4. Execute ``VACUUM INTO '<dest_path>'`` — the SQL literal path is
       single-quote-escaped via :func:`_quote_sql_string` because
       ``VACUUM INTO`` doesn't accept parameter binding.
    5. Return the absolute destination :class:`Path`.

    On any :class:`sqlite3.OperationalError` from the ``VACUUM INTO`` step
    (destination disk full, permission denied, destination is a directory,
    etc.) the error is wrapped in :class:`LcmDatabaseBackupError` with the
    underlying exception chained via ``raise ... from``.

    Acceptance-criteria contract (from
    ``epics/08-cli-ops/08-09-backup.md`` §"Acceptance criteria"):

    * The backup file is a valid SQLite database (``PRAGMA
      integrity_check`` returns ``"ok"``).
    * The backup file is fully compacted (no ``-shm`` / ``-wal`` sidecar).
    * Subsecond consecutive backups produce distinct paths.
    * In-memory DBs raise :class:`LcmDatabaseBackupError`.
    * Permission denied / disk full surfaces as
      :class:`LcmDatabaseBackupError` with the underlying :class:`OSError`
      chained.
    * Defensive ``ROLLBACK`` is a no-op when no transaction is active.
    * No active transaction state remains on ``db`` after this returns
      (``db.in_transaction`` is :data:`False` on stdlib
      :class:`sqlite3.Connection`).

    Args:
        db: Open SQLite connection to back up. Must conform to the
            :class:`Connection` Protocol (stdlib :class:`sqlite3.Connection`
            or apsw equivalent). Per the transaction-mutex contract no
            other thread should be writing to the connection during the
            ``VACUUM INTO`` (SQLite holds a write lock for the duration).
        label: Optional label segment in the destination filename. See
            :func:`build_lcm_database_backup_path` for the format.
        db_path: Source filesystem path of the database (used to anchor
            the destination path next to the source). MUST be the same
            path the ``db`` connection was opened against — there is no
            way to read this back from a stdlib :class:`sqlite3.Connection`
            so the caller is responsible for threading it through.

    Returns:
        Absolute :class:`pathlib.Path` to the newly-written backup file.

    Raises:
        LcmDatabaseBackupError: Source DB is in-memory; destination
            directory cannot be created; ``VACUUM INTO`` itself fails.
    """
    backup_path = build_lcm_database_backup_path(db_path, label=label)

    try:
        backup_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise LcmDatabaseBackupError(
            f"Cannot create backup directory {backup_path.parent}: {exc}"
        ) from exc

    # VACUUM INTO requires no active transaction. The transaction-mutex
    # guarantees this — backup is called via with_database_transaction(...,
    # "BEGIN") at the OUTER level, then the mutex releases before this
    # function runs. Defensive: ROLLBACK any in-flight transaction.
    # The stdlib's autocommit/PEP-249 mode means ``conn.execute("ROLLBACK")``
    # raises ``sqlite3.OperationalError: no transaction is active`` when
    # nothing is open — swallow that one case and let any other error
    # propagate (we don't want to mask, e.g., a corruption error).
    try:
        db.execute("ROLLBACK")
    except sqlite3.OperationalError as exc:
        if "no transaction" not in str(exc).lower():
            raise

    try:
        db.execute(f"VACUUM INTO {_quote_sql_string(str(backup_path))}")
    except sqlite3.OperationalError as exc:
        raise LcmDatabaseBackupError(f"VACUUM INTO {backup_path} failed: {exc}") from exc

    return backup_path
