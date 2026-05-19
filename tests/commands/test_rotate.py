"""Tests for ``/lcm rotate`` — issue 08-16.

Ports the TS ``test/lcm-command.test.ts`` rotate scenario
(``"rotates the current session and replaces the latest rotate
backup"``, line 1148) into the Hermes SQLite-only model. Per
[ADR-024](../../docs/adr/024-project-layout.md) the TS JSONL-transcript
rotation is dropped entirely — the Hermes ``/lcm rotate`` backs up the
DB, clears the assemble snapshot cache, compacts the WAL, and stamps
``state_meta.last_rotate_at``.

Covers (per the 08-16 acceptance criteria):

* No active session (``current_session_id is None``) → terse no-op
  message; nothing else happens.
* Backup failure → the error message is returned; **no partial state
  changes** (no ``state_meta`` row written).
* :meth:`LCMEngine.clear_assemble_snapshot` removes the per-conversation
  entry from ``_previous_assembled_messages_by_conversation`` — verified
  by direct dict inspection.
* WAL checkpoint failure is swallowed (best-effort).
* ``state_meta.last_rotate_at`` is written and is ISO-8601 UTC.
* Happy-path roundtrip — backup file exists on disk, all four steps
  reported.
* No ``.jsonl`` file path appears in the handler source (ADR-024
  invariant).

See:

* ``epics/08-cli-ops/08-16-rotate.md`` — issue spec.
* ``lossless-claw/src/plugin/lcm-command.ts`` (case ``"rotate"``) +
  ``src/engine.ts:rotateSessionStorageWithBackup`` — TS source at commit
  ``1f07fbd`` (pr-613 head).
* ``src/lossless_hermes/commands/rotate.py`` — the handler under test.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lossless_hermes.commands.rotate import _get_unavailable_reason, run
from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.engine import LCMEngine
from lossless_hermes.store.conversation import (
    ConversationStore,
    CreateConversationInput,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def file_db(tmp_path: Path) -> Iterator[tuple[sqlite3.Connection, Path]]:
    """A file-backed, migrated LCM database.

    Rotate's first step is a backup via ``VACUUM INTO``, which requires
    a *file-backed* source — so unlike ``test_status.py`` we cannot use a
    ``:memory:`` DB here.

    We deliberately use a bare :func:`sqlite3.connect` rather than
    :func:`lossless_hermes.db.connection.open_lcm_db`. ``open_lcm_db``
    runs the Apple-system-Python guard (ADR-004) and loads the
    ``sqlite-vec`` extension; the macOS CI matrix uses an
    ``actions/setup-python`` CPython build that lacks
    ``sqlite3.Connection.enable_load_extension``, so ``open_lcm_db``
    raises :class:`RuntimeError` there. Rotate reads nothing that needs
    ``sqlite-vec`` (it backs the DB up, checkpoints the WAL, and writes
    ``state_meta``), so a plain connection is sufficient and portable —
    this mirrors ``tests/commands/test_backup.py``'s use of bare
    ``sqlite3.connect``.

    The connection is configured to match the load-bearing aspects of
    a production ``open_lcm_db`` connection for this test:

    * ``isolation_level=None`` (autocommit) — so the migration ladder
      leaves no dangling write transaction into the backup's
      ``VACUUM INTO`` (which fails with "database is locked" if one is
      open).
    * ``PRAGMA journal_mode = WAL`` — so the rotate handler's
      ``PRAGMA wal_checkpoint(TRUNCATE)`` step is meaningful.
    * ``PRAGMA foreign_keys = ON`` — production parity.

    Yields a ``(connection, db_path)`` pair; the connection is closed on
    teardown and ``tmp_path`` cleanup removes the file + any ``.bak``.
    """
    db_path = tmp_path / "lcm.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        # FTS5 may be absent on some CPython builds — the migration
        # ladder skips the FTS branches when told so. Rotate reads
        # nothing from FTS, so skipping is safe.
        run_lcm_migrations(conn, fts5_available=False)
    except Exception:
        conn.close()
        raise
    try:
        yield conn, db_path
    finally:
        conn.close()


def _make_engine(
    db: sqlite3.Connection | None = None,
    *,
    current_session_id: str | None = None,
    database_path: str = "",
) -> Any:
    """Build a real :class:`LCMEngine` shell with state wired by hand.

    Rotate calls two engine methods (:meth:`clear_assemble_snapshot`,
    :meth:`write_state_meta`) so a bare ``MagicMock`` would not exercise
    the real bodies. We construct the real shell and set the three
    attributes the handler + those methods read — bypassing
    ``on_session_start`` (which would open its own DB).
    """
    engine = LCMEngine()
    engine._db = db
    engine.current_session_id = current_session_id
    engine.config.database_path = database_path
    if db is not None:
        engine._conversation_store = ConversationStore(db, fts5_available=False)
    return engine


def _make_parsed(engine: Any) -> Any:
    """Build a ``ParsedLcmCommand``-shaped namespace.

    The dispatcher attaches ``engine`` to the parsed object before
    invoking the handler (``commands.py`` ``setattr(parsed, "engine",
    ...)``); we mirror that. ``/lcm rotate`` takes no args, so
    ``tokens`` is empty.
    """
    return SimpleNamespace(engine=engine, name="rotate", raw_args="", tokens=[])


def _seed_conversation(engine: Any, session_id: str) -> Any:
    """Insert + COMMIT a conversation row for ``session_id``.

    ``ConversationStore.create_conversation`` issues a bare ``INSERT``;
    under stdlib sqlite3's default isolation mode the INSERT auto-opens a
    deferred transaction that is NOT committed. ``/lcm rotate``'s backup
    step does a defensive ``ROLLBACK`` (``VACUUM INTO`` cannot run inside
    a transaction) — which would discard an uncommitted seed row. In
    production, conversation rows are committed during ingest
    (``ConversationStore._transaction`` wraps writes in ``BEGIN
    IMMEDIATE`` ... ``COMMIT``) long before any ``/lcm rotate``, so the
    row is durable. This helper mirrors that: commit the seed so the
    test models the real lifecycle ordering.

    Returns the inserted :class:`ConversationRecord`.
    """
    record = engine._conversation_store.create_conversation(
        CreateConversationInput(session_id=session_id)
    )
    engine._db.commit()
    return record


def _read_state_meta(db: sqlite3.Connection, key: str) -> tuple[str, str] | None:
    """Return ``(value, updated_at)`` for ``key`` in ``state_meta`` or ``None``.

    Used by the assertion that ``last_rotate_at`` was written. Returns
    ``None`` if the table does not exist (so the no-partial-state test
    can assert "table absent OR row absent" uniformly).
    """
    try:
        row = db.execute(
            "SELECT value, updated_at FROM state_meta WHERE key = ?",
            (key,),
        ).fetchone()
    except sqlite3.OperationalError:
        # state_meta table never created — also a valid "no row" result.
        return None
    if row is None:
        return None
    return row[0], row[1]


# ISO-8601 UTC with a trailing ``Z`` (the form rotate.py stamps).
_ISO8601_UTC_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")


# ---------------------------------------------------------------------------
# No active session — the short-circuit branch
# ---------------------------------------------------------------------------


def test_no_active_session() -> None:
    """``current_session_id is None`` → no-op message; nothing else runs.

    08-16 AC: "No active session → returns ``[lcm] rotate: no active
    session`` and does nothing else."
    """
    engine = _make_engine(db=None, current_session_id=None)
    out = run(_make_parsed(engine))
    assert out == "[lcm] rotate: no active session"


def test_no_active_session_does_not_touch_db(file_db: Any) -> None:
    """Even with a live DB, a ``None`` session_id short-circuits before any write.

    Wires a real migrated DB but leaves ``current_session_id`` as
    ``None`` — confirms no ``state_meta`` row is created (the handler
    returns before step 4).
    """
    conn, db_path = file_db
    engine = _make_engine(db=conn, current_session_id=None, database_path=str(db_path))
    out = run(_make_parsed(engine))
    assert out == "[lcm] rotate: no active session"
    assert _read_state_meta(conn, "last_rotate_at") is None


def test_engine_none_returns_dispatcher_misconfigured() -> None:
    """Missing ``parsed.engine`` is a programmer error — render a clear msg."""
    parsed = SimpleNamespace(name="rotate", raw_args="", tokens=[])
    out = run(parsed)
    assert "dispatcher misconfigured" in out


# ---------------------------------------------------------------------------
# Unavailable branches — no DB / in-memory DB
# ---------------------------------------------------------------------------


def test_no_db_open_returns_unavailable() -> None:
    """``engine._db is None`` (pre-on_session_start) renders "unavailable"."""
    engine = _make_engine(db=None, current_session_id="sess-1", database_path="/some/path.db")
    out = run(_make_parsed(engine))
    assert "rotate: unavailable" in out
    assert "engine not yet initialized" in out


@pytest.mark.parametrize(
    "path",
    ["", "   ", ":memory:", "file::memory:?cache=shared"],
)
def test_in_memory_or_empty_path_returns_unavailable(path: str) -> None:
    """In-memory / empty DB paths render "unavailable" — backup needs a file."""
    conn = sqlite3.connect(":memory:")
    try:
        engine = _make_engine(db=conn, current_session_id="sess-1", database_path=path)
        out = run(_make_parsed(engine))
        assert "rotate: unavailable" in out
        assert "file-backed SQLite database" in out
    finally:
        conn.close()


def test_get_unavailable_reason_invalid_type() -> None:
    """Defensive: a non-string ``database_path`` → "Invalid database path."."""
    assert _get_unavailable_reason(None) == "Invalid database path."
    assert _get_unavailable_reason(123) == "Invalid database path."  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Backup failure — no partial state changes
# ---------------------------------------------------------------------------


def test_backup_failure_returns_error_and_no_partial_state(
    file_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backup failure returns the error; no ``state_meta`` row is written.

    08-16 AC: "Backup failure → returns the error message; no partial
    state changes." We force the failure by pre-creating a directory at
    the deterministic ``VACUUM INTO`` destination so the primitive
    raises :class:`LcmDatabaseBackupError`.
    """
    from lossless_hermes.plugin import db_backup as db_backup_mod

    conn, db_path = file_db
    # Deterministic suffix so the destination path is predictable.
    monkeypatch.setattr(db_backup_mod.secrets, "token_hex", lambda n: "deadbe")
    target = db_backup_mod.build_lcm_database_backup_path(str(db_path), label="rotate")
    target.mkdir(parents=True, exist_ok=False)

    engine = _make_engine(db=conn, current_session_id="sess-1", database_path=str(db_path))
    out = run(_make_parsed(engine))

    assert "rotate failed at backup step" in out
    # No partial state: state_meta.last_rotate_at must NOT have been
    # written — the handler returns before step 4.
    assert _read_state_meta(conn, "last_rotate_at") is None


def test_backup_failure_does_not_clear_snapshot(
    file_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backup failure leaves the assemble snapshot cache untouched.

    "No partial state changes" extends to the in-memory snapshot dict:
    a seeded snapshot entry must still be present after a failed rotate.
    """
    from lossless_hermes.plugin import db_backup as db_backup_mod

    conn, db_path = file_db
    monkeypatch.setattr(db_backup_mod.secrets, "token_hex", lambda n: "deadbe")
    target = db_backup_mod.build_lcm_database_backup_path(str(db_path), label="rotate")
    target.mkdir(parents=True, exist_ok=False)

    engine = _make_engine(db=conn, current_session_id="sess-1", database_path=str(db_path))
    # Seed a conversation + a snapshot entry keyed by its conversation_id.
    record = _seed_conversation(engine, "sess-1")
    engine._previous_assembled_messages_by_conversation[record.conversation_id] = [
        {"role": "user", "content": "seed"}
    ]

    run(_make_parsed(engine))

    # Snapshot still present — the failed rotate did NOT reach step 2.
    assert record.conversation_id in engine._previous_assembled_messages_by_conversation


# ---------------------------------------------------------------------------
# clear_assemble_snapshot — engine method behavior
# ---------------------------------------------------------------------------


def test_clears_assemble_snapshot(file_db: Any) -> None:
    """Rotate clears the per-session entry from the snapshot dict.

    08-16 AC: "``engine.clear_assemble_snapshot(session_id)`` removes the
    per-session entry from ``_previous_assembled_messages_by_conversation``
    (verified via direct dict inspection in test)."
    """
    conn, db_path = file_db
    engine = _make_engine(db=conn, current_session_id="sess-1", database_path=str(db_path))
    record = _seed_conversation(engine, "sess-1")
    # Seed the snapshot dict — keyed by conversation_id, not session_id.
    engine._previous_assembled_messages_by_conversation[record.conversation_id] = [
        {"role": "assistant", "content": "prior assembly"}
    ]

    out = run(_make_parsed(engine))

    # Direct dict inspection: the entry is gone.
    assert record.conversation_id not in engine._previous_assembled_messages_by_conversation
    assert "Snapshot cache cleared for session sess-1" in out


def test_clear_assemble_snapshot_no_conversation_is_noop(file_db: Any) -> None:
    """A session with no conversation row → snapshot clear is a clean no-op.

    The handler still completes (backup + WAL + state_meta); the output
    reports "nothing to clear".
    """
    conn, db_path = file_db
    # No conversation row created for sess-orphan.
    engine = _make_engine(db=conn, current_session_id="sess-orphan", database_path=str(db_path))
    assert engine.clear_assemble_snapshot("sess-orphan") is False
    out = run(_make_parsed(engine))
    assert "nothing to clear" in out
    # Rotate still succeeded overall.
    assert "rotate complete" in out


def test_clear_assemble_snapshot_other_sessions_untouched(file_db: Any) -> None:
    """Clearing one session's snapshot leaves other conversations' entries.

    The snapshot dict is keyed by conversation_id; rotate must only drop
    the rotated session's conversation, not every entry.
    """
    conn, db_path = file_db
    engine = _make_engine(db=conn, current_session_id="sess-1", database_path=str(db_path))
    rotated = _seed_conversation(engine, "sess-1")
    other = _seed_conversation(engine, "sess-2")
    engine._previous_assembled_messages_by_conversation[rotated.conversation_id] = [
        {"role": "user", "content": "a"}
    ]
    engine._previous_assembled_messages_by_conversation[other.conversation_id] = [
        {"role": "user", "content": "b"}
    ]

    run(_make_parsed(engine))

    assert rotated.conversation_id not in engine._previous_assembled_messages_by_conversation
    # The unrelated conversation's snapshot survives.
    assert other.conversation_id in engine._previous_assembled_messages_by_conversation


# ---------------------------------------------------------------------------
# state_meta.last_rotate_at — written + ISO-8601 UTC
# ---------------------------------------------------------------------------


def test_state_meta_written(file_db: Any) -> None:
    """Rotate writes a ``last_rotate_at`` row into ``state_meta``.

    08-16 AC: "**New test:** ... confirms ``last_rotate_at`` row."
    """
    conn, db_path = file_db
    engine = _make_engine(db=conn, current_session_id="sess-1", database_path=str(db_path))
    run(_make_parsed(engine))

    stored = _read_state_meta(conn, "last_rotate_at")
    assert stored is not None, "expected a state_meta.last_rotate_at row"
    value, _updated_at = stored
    assert value, "last_rotate_at value should be non-empty"


def test_state_meta_last_rotate_at_is_iso8601_utc(file_db: Any) -> None:
    """``last_rotate_at`` is an ISO-8601 UTC timestamp with a ``Z`` suffix.

    08-16 AC: "``state_meta.last_rotate_at`` is ISO8601 UTC."
    """
    conn, db_path = file_db
    engine = _make_engine(db=conn, current_session_id="sess-1", database_path=str(db_path))
    run(_make_parsed(engine))

    stored = _read_state_meta(conn, "last_rotate_at")
    assert stored is not None
    value, _updated_at = stored
    assert _ISO8601_UTC_Z.match(value), (
        f"last_rotate_at {value!r} is not ISO-8601 UTC with a Z suffix"
    )


def test_state_meta_upserts_on_repeated_rotate(file_db: Any) -> None:
    """A second rotate overwrites ``last_rotate_at`` rather than duplicating.

    ``state_meta`` is a single-row-per-key store (PRIMARY KEY on
    ``key``); the ``ON CONFLICT`` UPSERT must keep exactly one row.
    """
    conn, db_path = file_db
    engine = _make_engine(db=conn, current_session_id="sess-1", database_path=str(db_path))
    run(_make_parsed(engine))
    run(_make_parsed(engine))

    count = conn.execute("SELECT COUNT(*) FROM state_meta WHERE key = 'last_rotate_at'").fetchone()[
        0
    ]
    assert count == 1


# ---------------------------------------------------------------------------
# WAL checkpoint — best-effort (failure swallowed)
# ---------------------------------------------------------------------------


def test_wal_checkpoint_failure_is_swallowed(file_db: Any) -> None:
    """A ``PRAGMA wal_checkpoint`` ``OperationalError`` does not fail rotate.

    08-16 AC: "WAL checkpoint failure is swallowed (best-effort)."

    Rather than fault-inject at the connection level (``sqlite3.Connection``
    is a C type — its ``execute`` cannot be monkey-patched on an
    instance, and a ``factory=`` subclass over a live migrated DB
    introduces cursor-lifetime races with the backup ``VACUUM INTO``),
    we substitute ``engine._db`` with a thin proxy that fails only the
    WAL-checkpoint PRAGMA. The handler calls
    ``engine._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")``; the proxy
    raises :class:`sqlite3.OperationalError` for that exact statement
    and forwards everything else (including the backup ``VACUUM INTO``)
    to the real connection unchanged — see :class:`_WalFaultConnectionProxy`.
    """
    conn, db_path = file_db
    engine = _make_engine(db=conn, current_session_id="sess-1", database_path=str(db_path))

    # Wrap the connection in a tiny proxy that fails only the WAL
    # checkpoint PRAGMA. The proxy is set as ``engine._db`` — rotate
    # reads ``engine._db`` for every DB op, so the backup primitive (it
    # receives ``engine._db``) and the ``write_state_meta`` /
    # ``clear_assemble_snapshot`` engine methods all route through it.
    engine._db = _WalFaultConnectionProxy(conn)
    engine._conversation_store = ConversationStore(engine._db, fts5_available=False)

    out = run(_make_parsed(engine))

    # Rotate still reports completion; the WAL line records the skip.
    assert "rotate complete" in out
    assert "compaction skipped" in out
    # state_meta still written — step 4 runs after the swallowed
    # failure (proves the swallow did not short-circuit the handler).
    assert _read_state_meta(conn, "last_rotate_at") is not None


def _is_wal_checkpoint_pragma(sql: str) -> bool:
    """Return ``True`` iff ``sql`` is a ``PRAGMA wal_checkpoint`` statement.

    Used by :class:`_WalFaultConnectionProxy` to fault-inject the rotate
    handler's WAL-compaction step. The match is deliberately precise: a
    naive ``"wal_checkpoint" in sql`` substring test would *also* match
    the backup's ``VACUUM INTO '<path>'`` statement whenever the
    destination path happens to contain the substring — and pytest
    derives the ``tmp_path`` directory name from the test function name,
    so a test literally named ``test_wal_checkpoint_failure_*`` produces
    a backup path under ``.../test_wal_checkpoint_failure_is0/`` that
    contains ``wal_checkpoint``. Matching only ``PRAGMA``-prefixed
    statements whose pragma body is ``wal_checkpoint`` avoids that
    false positive.
    """
    stripped = sql.strip().upper()
    return stripped.startswith("PRAGMA") and "WAL_CHECKPOINT" in stripped


class _WalFaultConnectionProxy:
    """A connection proxy that fails only ``PRAGMA wal_checkpoint``.

    Delegates every attribute to a wrapped real :class:`sqlite3.Connection`
    except :meth:`execute`, which raises :class:`sqlite3.OperationalError`
    for the ``PRAGMA wal_checkpoint`` statement (per
    :func:`_is_wal_checkpoint_pragma`) and forwards everything else —
    notably the backup's ``VACUUM INTO`` and defensive ``ROLLBACK`` —
    straight through to the real connection.

    Used by :func:`test_wal_checkpoint_failure_is_swallowed` to exercise
    the best-effort swallow without subclassing the C connection type
    (which introduces cursor-lifetime races with ``VACUUM INTO``). The
    proxy is a plain Python object so it can be substituted freely as
    ``engine._db``.
    """

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real

    def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        if _is_wal_checkpoint_pragma(sql):
            raise sqlite3.OperationalError("database is locked")
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        # Everything else (commit, cursor, in_transaction, ...) forwards
        # to the wrapped real connection.
        return getattr(self._real, name)


# ---------------------------------------------------------------------------
# Happy path — full roundtrip
# ---------------------------------------------------------------------------


def test_happy_path_full_rotate(file_db: Any) -> None:
    """A complete rotate: backup file on disk + all four steps reported."""
    conn, db_path = file_db
    # A little data so the backup is non-trivial.
    conn.execute("INSERT INTO conversations (session_id, active) VALUES ('sess-1', 1)")
    conn.commit()

    engine = _make_engine(db=conn, current_session_id="sess-1", database_path=str(db_path))
    out = run(_make_parsed(engine))

    lines = out.splitlines()
    assert lines[0] == "[lcm] rotate complete"

    # Backup line points at a real .bak file.
    backup_line = next(line for line in lines if line.startswith("Backup:"))
    backup_path_str = backup_line.split("Backup:", 1)[1].strip()
    assert backup_path_str.endswith(".bak")
    assert Path(backup_path_str).is_file()
    # The backup is itself a valid SQLite DB.
    backup_conn = sqlite3.connect(backup_path_str)
    try:
        integrity = backup_conn.execute("PRAGMA integrity_check").fetchone()[0]
        assert integrity == "ok"
    finally:
        backup_conn.close()

    # Snapshot + WAL + state_meta lines all present.
    assert any("Snapshot cache" in line for line in lines)
    assert any("WAL" in line and "state_meta.last_rotate_at" in line for line in lines)


def test_rotate_label_appears_in_backup_filename(file_db: Any) -> None:
    """The backup is written with ``label="rotate"`` — the filename proves it.

    Mirrors the TS contract (``createLcmDatabaseBackup({..., label:
    "rotate"})`` inside ``rotateSessionStorageWithBackup``).
    """
    conn, db_path = file_db
    engine = _make_engine(db=conn, current_session_id="sess-1", database_path=str(db_path))
    out = run(_make_parsed(engine))
    backup_line = next(line for line in out.splitlines() if line.startswith("Backup:"))
    backup_path_str = backup_line.split("Backup:", 1)[1].strip()
    # Path format: <db>.rotate.<timestamp>-<rand>.bak
    assert ".rotate." in Path(backup_path_str).name


# ---------------------------------------------------------------------------
# ADR-024 invariant — no JSONL touched
# ---------------------------------------------------------------------------


def test_no_jsonl_touched() -> None:
    """No ``.jsonl`` file path appears in the rotate handler source.

    08-16 AC: "No JSONL file is touched ... ``grep -nr "\\.jsonl"
    src/.../rotate.py`` returns 0 lines." Per ADR-024 / the Epic 01
    README, Hermes has no JSONL transcript — the SQLite-only rotation
    must never reference one. We scan only the executable lines (the
    module docstring legitimately *mentions* JSONL to explain why it is
    absent — that is documentation, not a file access).
    """
    import lossless_hermes.commands.rotate as rotate_mod

    source_path = rotate_mod.__file__
    assert source_path is not None
    source = Path(source_path).read_text(encoding="utf-8")

    # Strip the module docstring (it explains the JSONL-drop rationale).
    # Everything after the docstring's closing triple-quote is code.
    parts = source.split('"""')
    code_only = "".join(parts[2:]) if len(parts) >= 3 else source

    assert ".jsonl" not in code_only, (
        "rotate.py executable code must not reference a .jsonl path "
        "(ADR-024: Hermes is SQLite-only, no JSONL transcript)."
    )
