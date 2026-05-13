"""Tests for :mod:`lossless_hermes.plugin.db_backup`.

Covers the acceptance criteria from ``epics/08-cli-ops/08-09-backup.md``:

* Valid SQLite output (``PRAGMA integrity_check`` returns ``"ok"``).
* Backup file is fully compacted — no ``-shm`` / ``-wal`` sidecar.
* Subsecond consecutive backups produce distinct paths.
* In-memory DBs raise :class:`LcmDatabaseBackupError`.
* Permission denied / destination directory failure surfaces as
  :class:`LcmDatabaseBackupError` with the underlying :class:`OSError` chained.
* Defensive ``ROLLBACK`` is a no-op when no transaction is active.
* No active transaction state remains on ``db`` after the function returns.
* ``label="doctor-cleaners"`` appears in the destination path before the
  timestamp segment.

TS source has no dedicated test for ``lcm-db-backup.ts`` — coverage in TS is
via ``test/v41-data-cleanup.test.ts`` and ``test/lcm-command.test.ts``. The
Python port adds direct unit tests because the primitive is small enough that
exhaustive direct testing is cheap (~30 LOC each test).

See:

* ``epics/08-cli-ops/08-09-backup.md`` — issue spec + acceptance criteria.
* ``lossless-claw/src/plugin/lcm-db-backup.ts`` — TS source at commit
  ``1f07fbd``.
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

import pytest

from lossless_hermes.plugin.db_backup import (
    LcmDatabaseBackupError,
    build_lcm_database_backup_path,
    write_lcm_database_backup,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def file_backed_db(tmp_path: Path) -> tuple[sqlite3.Connection, Path]:
    """Return ``(conn, path)`` for a small file-backed SQLite database.

    The DB is created at ``tmp_path/lcm.db`` with one table and a few rows
    so :func:`write_lcm_database_backup` produces a non-empty backup that
    can be opened and integrity-checked.

    Tests share this fixture rather than each spinning up their own
    connection — pytest creates a new ``tmp_path`` per test so the DB
    paths don't collide.
    """
    db_path = tmp_path / "lcm.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, content TEXT)")
    conn.execute("INSERT INTO messages (content) VALUES ('hello'), ('world'), ('lcm')")
    conn.commit()
    yield conn, db_path
    conn.close()


@pytest.fixture
def in_memory_db() -> sqlite3.Connection:
    """Return a bare in-memory SQLite connection (no migrations)."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# build_lcm_database_backup_path — path construction tests
# ---------------------------------------------------------------------------


class TestBuildBackupPath:
    """Cover :func:`build_lcm_database_backup_path`'s format contract."""

    def test_no_label_produces_timestamp_rand_suffix(self, tmp_path: Path) -> None:
        """Format: ``<abs_db>.<YYYY-MM-DDTHHMMSS>-<rand6>.bak``."""
        db_path = tmp_path / "lcm.db"
        result = build_lcm_database_backup_path(str(db_path))
        # Filename should be of shape lcm.db.<timestamp>-<rand>.bak.
        # Timestamp format: YYYY-MM-DDTHHMMSS (15 chars after the prefix).
        # Random suffix: 6 lowercase hex chars.
        assert result.parent == tmp_path
        assert re.match(
            r"^lcm\.db\.\d{4}-\d{2}-\d{2}T\d{6}-[0-9a-f]{6}\.bak$",
            result.name,
        ), f"unexpected filename shape: {result.name!r}"

    def test_label_appears_before_timestamp(self, tmp_path: Path) -> None:
        """Format with label: ``<abs_db>.<label>.<YYYY-MM-DDTHHMMSS>-<rand6>.bak``.

        Acceptance criterion: "``label='doctor-cleaners'`` appears in path
        before timestamp."
        """
        db_path = tmp_path / "lcm.db"
        result = build_lcm_database_backup_path(str(db_path), label="doctor-cleaners")
        assert re.match(
            r"^lcm\.db\.doctor-cleaners\.\d{4}-\d{2}-\d{2}T\d{6}-[0-9a-f]{6}\.bak$",
            result.name,
        ), f"unexpected filename shape: {result.name!r}"

    def test_empty_label_is_treated_as_no_label(self, tmp_path: Path) -> None:
        """Empty-string label is defensively treated as no label.

        Without this guard, the format would produce ``lcm.db..2026-...``
        with a doubled dot — confusing but not load-bearing. Better to
        reject the duplication at construction time.
        """
        db_path = tmp_path / "lcm.db"
        result = build_lcm_database_backup_path(str(db_path), label="")
        # Should NOT contain two consecutive dots before the timestamp.
        assert ".." not in result.name, f"unexpected double-dot in: {result.name!r}"

    def test_path_object_input_accepted(self, tmp_path: Path) -> None:
        """``db_path`` accepts :class:`pathlib.Path`, not just str."""
        db_path = tmp_path / "lcm.db"
        result = build_lcm_database_backup_path(db_path)
        assert result.parent == tmp_path
        assert result.name.startswith("lcm.db.")

    def test_in_memory_raises(self) -> None:
        """``:memory:`` source raises :class:`LcmDatabaseBackupError`.

        Acceptance criterion: "In-memory DBs (`:memory:`) raise
        ``LcmDatabaseBackupError``."
        """
        with pytest.raises(LcmDatabaseBackupError, match="in-memory"):
            build_lcm_database_backup_path(":memory:")

    def test_file_uri_memory_raises(self) -> None:
        """SQLite URI ``file::memory:...`` also raises (matches TS classification)."""
        with pytest.raises(LcmDatabaseBackupError, match="in-memory"):
            build_lcm_database_backup_path("file::memory:?cache=shared")

    def test_empty_path_raises(self) -> None:
        """Empty/whitespace path raises (no anchor for the backup name)."""
        with pytest.raises(LcmDatabaseBackupError):
            build_lcm_database_backup_path("")
        with pytest.raises(LcmDatabaseBackupError):
            build_lcm_database_backup_path("   ")

    def test_subsecond_unique_paths(self, tmp_path: Path) -> None:
        """Two calls within the same second produce distinct paths.

        Acceptance criterion: "Subsecond consecutive backups produce
        distinct paths (random suffix collision check)."

        The timestamp resolution is 1 second; the random suffix is what
        guarantees uniqueness for same-second calls.
        """
        db_path = tmp_path / "lcm.db"
        # 50 calls within ~50 ms — virtually guaranteed to share at least
        # one timestamp second on any machine. With 24-bit entropy in the
        # random suffix the collision probability across 50 samples is
        # ~50²/2^25 ≈ 7.5e-5 — small enough that the test is reliable.
        paths = {build_lcm_database_backup_path(str(db_path)) for _ in range(50)}
        assert len(paths) == 50, "expected 50 unique paths, got collisions"


# ---------------------------------------------------------------------------
# write_lcm_database_backup — the core ``VACUUM INTO`` integration
# ---------------------------------------------------------------------------


class TestWriteBackup:
    """Cover :func:`write_lcm_database_backup`'s SQLite integration."""

    def test_vacuum_into_produces_valid_db(
        self,
        file_backed_db: tuple[sqlite3.Connection, Path],
    ) -> None:
        """Backup file passes ``PRAGMA integrity_check``.

        Acceptance criterion: "The backup file is a valid SQLite database
        (verified by opening with ``sqlite3.connect()`` and running
        ``PRAGMA integrity_check``)."
        """
        conn, db_path = file_backed_db
        backup_path = write_lcm_database_backup(conn, db_path=str(db_path))

        # The backup file exists.
        assert backup_path.is_file()
        assert backup_path.suffix == ".bak"

        # Open the backup with a fresh connection and run integrity_check.
        backup_conn = sqlite3.connect(str(backup_path))
        try:
            row = backup_conn.execute("PRAGMA integrity_check").fetchone()
            assert row is not None
            assert row[0] == "ok", f"integrity_check failed: {row[0]!r}"

            # Verify the data round-trips.
            count_row = backup_conn.execute("SELECT COUNT(*) FROM messages").fetchone()
            assert count_row is not None
            assert count_row[0] == 3
        finally:
            backup_conn.close()

    def test_backup_has_no_wal_sidecar(
        self,
        file_backed_db: tuple[sqlite3.Connection, Path],
    ) -> None:
        """The backup is fully compacted — no ``-shm`` / ``-wal`` files alongside.

        Acceptance criterion: "The backup file is fully compacted (no WAL
        sidecar, no ``-shm`` / ``-wal`` files alongside it)."

        ``VACUUM INTO`` always produces a clean DB with the default
        journal mode (DELETE); it never carries over WAL state from the
        source.
        """
        conn, db_path = file_backed_db
        # Enable WAL on the source to make the test load-bearing — if the
        # source is in WAL mode and the backup somehow inherited it,
        # we'd see -wal/-shm next to the .bak file.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("INSERT INTO messages (content) VALUES ('wal-test')")
        conn.commit()

        backup_path = write_lcm_database_backup(conn, db_path=str(db_path))
        assert backup_path.is_file()

        # Check for sidecar files.
        wal_sidecar = backup_path.with_name(backup_path.name + "-wal")
        shm_sidecar = backup_path.with_name(backup_path.name + "-shm")
        assert not wal_sidecar.exists(), f"unexpected WAL sidecar: {wal_sidecar}"
        assert not shm_sidecar.exists(), f"unexpected SHM sidecar: {shm_sidecar}"

    def test_label_in_path(
        self,
        file_backed_db: tuple[sqlite3.Connection, Path],
    ) -> None:
        """``label='doctor-cleaners'`` is embedded in the resulting path.

        Acceptance criterion: "``label='doctor-cleaners'`` appears in path
        before timestamp."
        """
        conn, db_path = file_backed_db
        backup_path = write_lcm_database_backup(
            conn,
            label="doctor-cleaners",
            db_path=str(db_path),
        )
        assert "doctor-cleaners" in backup_path.name
        # And before the timestamp (which starts with the current year).
        # Find indices.
        label_idx = backup_path.name.index("doctor-cleaners")
        # Timestamp starts with "2" (year 2xxx) — find the next dot after label.
        ts_idx = backup_path.name.index(".", label_idx + len("doctor-cleaners"))
        # Just verify the label appears at all; the format test above
        # locks down the precise positions.
        assert label_idx < ts_idx

    def test_in_memory_raises(self, in_memory_db: sqlite3.Connection) -> None:
        """``:memory:`` source raises :class:`LcmDatabaseBackupError`.

        Acceptance criterion: "In-memory DBs (`:memory:`) raise
        ``LcmDatabaseBackupError``."
        """
        with pytest.raises(LcmDatabaseBackupError, match="in-memory"):
            write_lcm_database_backup(in_memory_db, db_path=":memory:")

    def test_destination_directory_unwritable_raises(
        self,
        file_backed_db: tuple[sqlite3.Connection, Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A ``VACUUM INTO`` failure surfaces as :class:`LcmDatabaseBackupError`.

        Acceptance criterion: "Permission denied / disk full surfaces as
        ``LcmDatabaseBackupError`` with the underlying ``OSError`` chained."

        We monkeypatch :meth:`pathlib.Path.mkdir` to raise :class:`OSError`
        so the test is hermetic and works on every platform (real
        permission-denied tests are flaky across Linux containers /
        macOS / Windows CI).
        """
        conn, db_path = file_backed_db

        original_mkdir = Path.mkdir

        def _failing_mkdir(self: Path, **kwargs: object) -> None:
            # Only fail for the backup directory; let other mkdirs (e.g.
            # test scaffolding) pass through.
            if str(self) == str(db_path.parent):
                raise OSError("simulated permission denied")
            return original_mkdir(self, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "mkdir", _failing_mkdir)

        with pytest.raises(LcmDatabaseBackupError) as exc_info:
            write_lcm_database_backup(conn, db_path=str(db_path))

        # The underlying OSError should be chained via __cause__.
        assert isinstance(exc_info.value.__cause__, OSError)

    def test_vacuum_into_failure_is_wrapped(
        self,
        file_backed_db: tuple[sqlite3.Connection, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A ``sqlite3.OperationalError`` from ``VACUUM INTO`` is wrapped.

        Simulates a destination-collision: pre-create a *directory* at the
        path :func:`build_lcm_database_backup_path` will return next, so
        the ``VACUUM INTO`` call fails with
        :class:`sqlite3.OperationalError`. The wrapper must catch it and
        re-raise as :class:`LcmDatabaseBackupError` with the underlying
        error chained via ``raise ... from``.

        We monkeypatch :func:`secrets.token_hex` to make the random suffix
        deterministic so we can pre-create the colliding directory.

        ``sqlite3.Connection.execute`` is read-only on stdlib, so we
        cannot monkeypatch the method itself; pre-creating the
        destination as a directory is the cleanest cross-platform way
        to reliably trigger the underlying ``VACUUM INTO`` failure.
        """
        from lossless_hermes.plugin import db_backup as db_backup_mod

        conn, db_path = file_backed_db

        # Force a deterministic random suffix so we can pre-create the
        # exact destination path as a directory.
        monkeypatch.setattr(db_backup_mod.secrets, "token_hex", lambda n: "deadbe")

        # Build the (deterministic) destination path and pre-create it as
        # a directory — ``VACUUM INTO`` cannot write a database file over
        # an existing directory.
        target = build_lcm_database_backup_path(str(db_path))
        target.mkdir(parents=True, exist_ok=False)

        with pytest.raises(LcmDatabaseBackupError, match="VACUUM INTO"):
            write_lcm_database_backup(conn, db_path=str(db_path))

    def test_defensive_rollback_when_no_transaction(
        self,
        file_backed_db: tuple[sqlite3.Connection, Path],
    ) -> None:
        """Defensive ``ROLLBACK`` is a no-op when no transaction is active.

        Acceptance criterion: "Defensive ``ROLLBACK`` is a no-op when no
        transaction is active (test with both states)."

        Verified by simply calling the function on a connection that has
        no open transaction — it must succeed without raising.
        """
        conn, db_path = file_backed_db
        assert not conn.in_transaction  # baseline
        backup_path = write_lcm_database_backup(conn, db_path=str(db_path))
        assert backup_path.is_file()

    def test_defensive_rollback_when_transaction_open(
        self,
        file_backed_db: tuple[sqlite3.Connection, Path],
    ) -> None:
        """Defensive ``ROLLBACK`` rolls back an open transaction before VACUUM.

        Acceptance criterion: "No active transaction state remains on the
        DB connection after ``write_lcm_database_backup`` returns".

        Open a transaction on the connection (the stdlib opens one
        implicitly when DML runs), call the backup primitive, then
        assert ``conn.in_transaction is False``.
        """
        conn, db_path = file_backed_db
        # Trigger an implicit transaction by doing a DML.
        conn.execute("INSERT INTO messages (content) VALUES ('opens txn')")
        assert conn.in_transaction is True

        backup_path = write_lcm_database_backup(conn, db_path=str(db_path))
        assert backup_path.is_file()

        # The defensive ROLLBACK must have left the connection with no
        # in-flight transaction.
        assert conn.in_transaction is False, "transaction state leaked after backup"

    def test_returns_absolute_path(
        self,
        file_backed_db: tuple[sqlite3.Connection, Path],
    ) -> None:
        """The returned :class:`Path` is absolute.

        Acceptance criterion: "``write_lcm_database_backup(...)`` ...
        returns the absolute path."
        """
        conn, db_path = file_backed_db
        backup_path = write_lcm_database_backup(conn, db_path=str(db_path))
        assert backup_path.is_absolute()

    def test_sql_injection_in_path_is_escaped(
        self,
        tmp_path: Path,
    ) -> None:
        """Single quotes in the destination path are properly escaped.

        ``VACUUM INTO`` doesn't accept parameter binding so the path is
        SQL-literal-quoted. Verify the doubling-quote escape works on a
        path containing a single quote — otherwise a malicious or
        creatively-named directory could break the SQL.
        """
        # Build a directory with a single quote in the name.
        weird_dir = tmp_path / "it's a directory"
        weird_dir.mkdir()
        db_path = weird_dir / "lcm.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (42)")
        conn.commit()

        try:
            backup_path = write_lcm_database_backup(conn, db_path=str(db_path))
            assert backup_path.is_file()
            assert "it's a directory" in str(backup_path)
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Subsecond-uniqueness integration test — write two backups quickly
# ---------------------------------------------------------------------------


def test_subsecond_unique_writes(
    file_backed_db: tuple[sqlite3.Connection, Path],
) -> None:
    """Two ``write_lcm_database_backup`` calls within 100 ms produce two files.

    Acceptance criterion: "two backups within 100ms produce different
    paths." This is an end-to-end version of the path-only test above —
    catches a bug where the path is unique but the second write would
    overwrite the first because of e.g. a sentinel ``.bak`` overwrite.
    """
    conn, db_path = file_backed_db
    start = time.monotonic()
    path_a = write_lcm_database_backup(conn, db_path=str(db_path))
    path_b = write_lcm_database_backup(conn, db_path=str(db_path))
    elapsed = time.monotonic() - start

    # Sanity: this should be well under 1 second on any CI machine.
    assert elapsed < 5.0, f"backup took {elapsed:.2f}s — investigate"

    assert path_a != path_b
    assert path_a.is_file()
    assert path_b.is_file()
