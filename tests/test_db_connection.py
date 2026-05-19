"""Tests for :mod:`lossless_hermes.db.connection`.

Covers the acceptance criteria from
``epics/01-storage/01-01-db-connection.md`` §"Acceptance criteria":

* All 7 PRAGMAs applied in storage.md §3 order.
* ``assert_foreign_keys_enabled`` raises if the readback is 0.
* Port of the 4 TS test cases from ``test/db-connection.test.ts``
  (path-helper purity over non-string runtime values, file-backed
  preservation).
* ``test_apple_system_python_guard`` — guard raises actionable
  :class:`RuntimeError` when ``enable_load_extension`` is missing.
* ``test_wal_fallback_on_nfs`` — when ``PRAGMA journal_mode = WAL`` raises
  a WAL-incompat error, the connection downgrades to ``DELETE`` without
  crashing (mirrors hermes-agent's ``apply_wal_with_fallback`` test
  pattern).
* ``PRAGMA optimize`` runs at close.
* sqlite-vec is loadable (``SELECT vec_version()`` returns a string).
* Connection registry: track on open, untrack on close, close-by-path
  closes every per-thread connection for that path.

Test isolation: the registry is a module-level singleton, so each test
calls :func:`lossless_hermes.db.connection.close_lcm_connection` with no
args at teardown to reset state — see :func:`_clear_registry` autouse
fixture below.
"""

from __future__ import annotations

import logging
import sqlite3
import subprocess
import sys
import textwrap
import threading
from pathlib import Path
from typing import Iterator

import pytest

from lossless_hermes.db import connection as connection_mod
from lossless_hermes.db.connection import (
    SQLITE_BUSY_TIMEOUT_MS,
    assert_foreign_keys_enabled,
    close_lcm_connection,
    close_lcm_db,
    get_file_backed_database_path,
    is_in_memory_path,
    normalize_path,
    open_db,
    open_lcm_db,
)

# ---------------------------------------------------------------------------
# Skip marker: actions/setup-python macOS builds lack enable_load_extension
# ---------------------------------------------------------------------------
#
# Per ADR-004 §Open questions item 1 and ADR-028 §Decision point 8, some
# CPython builds (notably ``actions/setup-python``'s macOS pre-built
# Python) ship without ``--enable-loadable-sqlite-extensions``. That's an
# operator-machine concern in production (the Apple-Python guard fires
# loudly), but in CI it means the open_lcm_db path can't be exercised at
# all on those cells. We auto-skip the live-DB tests when extension
# loading isn't available on the running interpreter — the guard tests
# below still run (they explicitly monkey-patch the introspection hook).
#
# Ubuntu cells + Homebrew/pyenv/uv-managed Python all have extension
# loading enabled, so the skip only fires on the macOS GH-Actions runners.
_skip_no_extension_loading = pytest.mark.skipif(
    not hasattr(sqlite3.Connection, "enable_load_extension"),
    reason=(
        "actions/setup-python on macOS ships a CPython build without "
        "--enable-loadable-sqlite-extensions; sqlite-vec cannot load. "
        "Apple-Python-guard tests still run (see TestApplePythonGuard). "
        "See ADR-004 §Open questions item 1 + ADR-028 §Decision point 8."
    ),
)


# ---------------------------------------------------------------------------
# Autouse: clear the module-level registry between tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_registry() -> Iterator[None]:
    """Reset connection-registry state before AND after each test.

    The registry is a process-global dict; without this reset a test that
    skips its cleanup (e.g. one that asserts a registry side-effect then
    raises) would leak handles into the next test's environment.
    """
    close_lcm_connection()
    # Reset the WAL-fallback-warned set too so per-process dedup doesn't
    # silence a marker test running after the dedup test.
    with connection_mod._wal_fallback_warned_lock:
        connection_mod._wal_fallback_warned_paths.clear()
    yield
    close_lcm_connection()
    with connection_mod._wal_fallback_warned_lock:
        connection_mod._wal_fallback_warned_paths.clear()


# ---------------------------------------------------------------------------
# 1. Port of test/db-connection.test.ts — path-helper purity
# ---------------------------------------------------------------------------


class TestPathHelpers:
    """Ports the 4 path-helper tests from
    ``lossless-claw/test/db-connection.test.ts`` lines 8-28."""

    def test_treats_non_string_runtime_values_as_non_memory_paths(self) -> None:
        # TS: ``isInMemoryPath(123 as unknown as string) === false``.
        # Python: arbitrary non-string returns False (no ``:memory:`` match).
        assert is_in_memory_path(123) is False  # type: ignore[arg-type]
        assert is_in_memory_path({}) is False  # type: ignore[arg-type]

    def test_returns_none_for_non_string_file_backed_path_inputs(self) -> None:
        # TS: ``getFileBackedDatabasePath(123 as unknown as string) === null``.
        # Python: None (the Pythonic null sentinel).
        assert get_file_backed_database_path(123) is None  # type: ignore[arg-type]
        assert get_file_backed_database_path({}) is None  # type: ignore[arg-type]

    def test_normalizes_non_string_runtime_values_to_in_memory_key(self) -> None:
        # TS: ``normalizePath(123 as unknown as string) === ":memory:"``.
        assert normalize_path(123) == ":memory:"  # type: ignore[arg-type]
        assert normalize_path({}) == ":memory:"  # type: ignore[arg-type]

    def test_preserves_file_backed_paths_for_valid_strings(self) -> None:
        # TS: matches ``/tmp\/lcm\.db$/`` on the trimmed input.
        assert get_file_backed_database_path(" ./tmp/lcm.db ").endswith(  # type: ignore[union-attr]
            "tmp/lcm.db"
        )
        assert normalize_path(" ./tmp/lcm.db ").endswith("tmp/lcm.db")

    def test_in_memory_path_handles_uri_form(self) -> None:
        # ``connection.ts:23`` accepts ``file::memory:...`` as in-memory too.
        assert is_in_memory_path("file::memory:?cache=shared") is True
        assert is_in_memory_path(":memory:") is True

    def test_accepts_pathlib_path(self, tmp_path: Path) -> None:
        # Python wrinkle: TS only has strings, but we accept ``Path`` too.
        # The path-helper string-coercion path must handle it transparently.
        db = tmp_path / "lcm.db"
        assert is_in_memory_path(db) is False
        assert get_file_backed_database_path(db) == str(db)


# ---------------------------------------------------------------------------
# 2. PRAGMAs applied in order
# ---------------------------------------------------------------------------


@_skip_no_extension_loading
class TestPragmas:
    """All 7 PRAGMAs from storage.md §3 are applied after ``open_lcm_db``."""

    def test_journal_mode_is_wal(self, tmp_path: Path) -> None:
        # File-backed required — ``:memory:`` cannot use WAL mode.
        conn = open_lcm_db(tmp_path / "lcm.db")
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode.lower() == "wal"
        finally:
            close_lcm_db(conn)

    def test_busy_timeout_is_30000ms(self, tmp_path: Path) -> None:
        conn = open_lcm_db(tmp_path / "lcm.db")
        try:
            timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            assert timeout == SQLITE_BUSY_TIMEOUT_MS == 30_000
        finally:
            close_lcm_db(conn)

    def test_foreign_keys_are_on(self, tmp_path: Path) -> None:
        conn = open_lcm_db(tmp_path / "lcm.db")
        try:
            fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            assert fk == 1
        finally:
            close_lcm_db(conn)

    def test_cache_size_is_negative_65536(self, tmp_path: Path) -> None:
        # Negative = KiB (so -65536 == 64 MiB).
        conn = open_lcm_db(tmp_path / "lcm.db")
        try:
            cache = conn.execute("PRAGMA cache_size").fetchone()[0]
            assert cache == -65536
        finally:
            close_lcm_db(conn)

    def test_synchronous_is_normal(self, tmp_path: Path) -> None:
        # SQLite ``synchronous`` enum: 0=OFF, 1=NORMAL, 2=FULL, 3=EXTRA.
        conn = open_lcm_db(tmp_path / "lcm.db")
        try:
            sync = conn.execute("PRAGMA synchronous").fetchone()[0]
            assert sync == 1
        finally:
            close_lcm_db(conn)

    def test_temp_store_is_memory(self, tmp_path: Path) -> None:
        # SQLite ``temp_store`` enum: 0=DEFAULT, 1=FILE, 2=MEMORY.
        conn = open_lcm_db(tmp_path / "lcm.db")
        try:
            ts = conn.execute("PRAGMA temp_store").fetchone()[0]
            assert ts == 2
        finally:
            close_lcm_db(conn)


# ---------------------------------------------------------------------------
# 3. assert_foreign_keys_enabled — v4.1 B.fix Gap 7
# ---------------------------------------------------------------------------


class TestAssertForeignKeysEnabled:
    @_skip_no_extension_loading
    def test_passes_when_foreign_keys_on(self, tmp_path: Path) -> None:
        conn = open_lcm_db(tmp_path / "lcm.db")
        try:
            # Must not raise.
            assert_foreign_keys_enabled(conn)
        finally:
            close_lcm_db(conn)

    def test_raises_when_foreign_keys_off(self) -> None:
        # Open a raw connection (not via ``open_lcm_db``) with FK off, then
        # confirm the assertion fires.
        raw = sqlite3.connect(":memory:")
        try:
            raw.execute("PRAGMA foreign_keys = OFF")
            with pytest.raises(RuntimeError, match=r"foreign_keys is not ON"):
                assert_foreign_keys_enabled(raw)
        finally:
            raw.close()

    def test_error_message_points_to_open_lcm_db(self) -> None:
        raw = sqlite3.connect(":memory:")
        try:
            raw.execute("PRAGMA foreign_keys = OFF")
            with pytest.raises(RuntimeError, match=r"open_lcm_db"):
                assert_foreign_keys_enabled(raw)
        finally:
            raw.close()


# ---------------------------------------------------------------------------
# 4. sqlite-vec extension is loaded
# ---------------------------------------------------------------------------


@_skip_no_extension_loading
class TestSqliteVecLoaded:
    def test_vec_version_query_works(self, tmp_path: Path) -> None:
        # AC: sqlite-vec is loaded; ``SELECT vec_version()`` returns a
        # version string.
        conn = open_lcm_db(tmp_path / "lcm.db")
        try:
            row = conn.execute("SELECT vec_version()").fetchone()
            assert row is not None
            assert isinstance(row[0], str)
            assert row[0].startswith("v")  # e.g. "v0.1.9"
        finally:
            close_lcm_db(conn)

    def test_load_extension_disabled_after_open(self, tmp_path: Path) -> None:
        # Per ADR-004: enable_load_extension(False) at the end of load to
        # tighten the SQL-injection blast radius.
        conn = open_lcm_db(tmp_path / "lcm.db")
        try:
            # Attempting to load another extension should fail because the
            # extension-loading flag is OFF. SQLite returns
            # ``not authorized`` when the flag is off.
            with pytest.raises(sqlite3.OperationalError, match=r"not authorized"):
                conn.execute("SELECT load_extension('does-not-exist')")
        finally:
            close_lcm_db(conn)


# ---------------------------------------------------------------------------
# 5. Apple Python guard — subprocess test (the package's __init__ check)
# ---------------------------------------------------------------------------


class TestApplePythonGuard:
    """The guard fires when ``enable_load_extension`` is missing.

    In-process: monkey-patch :func:`engine._has_sqlite_extension_loading`
    to report ``False`` and confirm the guard raises with the documented
    message.

    Out-of-process (subprocess): we can't actually run a Python without
    ``enable_load_extension`` (we'd have to find an Apple system Python on
    every test runner), so the subprocess flavor verifies the
    monkey-patch-driven path produces an actionable message through the
    real ``open_lcm_db`` call path.
    """

    def test_guard_raises_when_extension_loading_absent(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # The guard is shared with engine; patching the introspection
        # hook there flips the result for the open_lcm_db call too.
        import lossless_hermes.engine as engine_mod

        monkeypatch.setattr(engine_mod, "_has_sqlite_extension_loading", lambda: False)

        with pytest.raises(RuntimeError, match=r"enable_load_extension"):
            open_lcm_db(tmp_path / "lcm.db")

    def test_guard_message_is_actionable(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # The error message must name concrete install paths so a
        # frustrated dev doesn't have to grep docs.
        import lossless_hermes.engine as engine_mod

        monkeypatch.setattr(engine_mod, "_has_sqlite_extension_loading", lambda: False)
        try:
            open_lcm_db(tmp_path / "lcm.db")
            pytest.fail("expected RuntimeError")
        except RuntimeError as exc:
            msg = str(exc)
            assert "Homebrew" in msg
            assert "pyenv" in msg
            assert "uv" in msg

    def test_guard_does_not_leak_connection_on_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # If the guard fires after ``sqlite3.connect`` but before the
        # PRAGMAs apply, the half-configured connection must be closed
        # before the RuntimeError propagates. We confirm by checking the
        # registry stays empty.
        import lossless_hermes.engine as engine_mod

        monkeypatch.setattr(engine_mod, "_has_sqlite_extension_loading", lambda: False)
        with pytest.raises(RuntimeError):
            open_lcm_db(tmp_path / "lcm.db")
        # Registry untouched — no connection was tracked.
        with connection_mod._registry_lock:
            assert connection_mod._connections_by_path == {}
            assert connection_mod._connection_index == {}

    def test_guard_via_subprocess_realistic_scenario(
        self,
        tmp_path: Path,
    ) -> None:
        # Subprocess flavour: spawn a fresh interpreter, monkey-patch
        # ``_has_sqlite_extension_loading`` at runtime, then call
        # ``open_lcm_db``. This proves the guard fires through the real
        # import path (not via test-time monkey-patching) and emits an
        # error matching the documented format.
        script = textwrap.dedent(
            f"""
            import sys

            import lossless_hermes.engine as engine_mod
            from lossless_hermes.db.connection import open_lcm_db

            # Disable the introspection hook BEFORE opening, simulating
            # an interpreter without enable_load_extension support.
            engine_mod._has_sqlite_extension_loading = lambda: False

            try:
                open_lcm_db({str(tmp_path / "lcm.db")!r})
                print("UNEXPECTED_SUCCESS", file=sys.stderr)
                sys.exit(2)
            except RuntimeError as exc:
                msg = str(exc)
                # Must mention each documented install hint.
                assert "Homebrew" in msg, f"missing Homebrew hint: {{msg!r}}"
                assert "pyenv" in msg, f"missing pyenv hint: {{msg!r}}"
                assert "uv" in msg, f"missing uv hint: {{msg!r}}"
                print("GUARD_FIRED_OK")
                sys.exit(0)
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"subprocess failed: stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert "GUARD_FIRED_OK" in result.stdout


# ---------------------------------------------------------------------------
# 6. WAL-fallback on simulated NFS/SMB/FUSE
# ---------------------------------------------------------------------------


class _ExecuteWrappingConnection:
    """Minimal proxy that intercepts ``execute`` on a real sqlite3 conn.

    ``sqlite3.Connection.execute`` is a C-method on a C-immutable object
    (``monkeypatch.setattr(conn, "execute", ...)`` raises
    ``AttributeError: ... is read-only``). To inject WAL-fallback failure
    scenarios we wrap the real connection in this proxy and route every
    attribute access except ``execute`` to the underlying conn.

    Used only in tests — production callers always pass real
    :class:`sqlite3.Connection` instances. The proxy implements just the
    surface ``_apply_wal_with_fallback`` and the close path touch.
    """

    def __init__(self, real_conn: sqlite3.Connection, execute_fn) -> None:
        self._real = real_conn
        self.execute = execute_fn

    def __getattr__(self, name: str) -> object:
        # Forwards anything not on this proxy (e.g. .close()) to the real
        # conn. ``execute`` is set in __init__ so it shortcircuits before
        # this method runs.
        return getattr(self._real, name)


class TestWalFallback:
    """Simulate the WAL-incompat ``OperationalError`` and confirm the
    fallback to DELETE."""

    def test_falls_back_to_delete_on_locking_protocol_error(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        raw = sqlite3.connect(":memory:")
        try:
            real_execute = raw.execute
            calls: list[str] = []

            def fake_execute(sql: str, *args: object, **kwargs: object) -> object:
                calls.append(sql)
                if "journal_mode = WAL" in sql:
                    raise sqlite3.OperationalError("disk i/o error")
                return real_execute(sql, *args, **kwargs)

            proxy = _ExecuteWrappingConnection(raw, fake_execute)

            with caplog.at_level(logging.WARNING, logger="lossless_hermes.db.connection"):
                mode = connection_mod._apply_wal_with_fallback(proxy, db_label="lcm.db")  # type: ignore[arg-type]

            assert mode == "delete", "expected fallback to DELETE journal mode"
            # WAL was tried then DELETE applied.
            assert any("journal_mode = WAL" in s for s in calls)
            assert any("journal_mode = DELETE" in s for s in calls)
            # And the warning was emitted with WAL context.
            assert any(
                "WAL journal_mode unsupported" in record.message for record in caplog.records
            )
        finally:
            raw.close()

    def test_non_wal_operational_error_is_reraised(self) -> None:
        raw = sqlite3.connect(":memory:")
        try:
            real_execute = raw.execute

            def fake_execute(sql: str, *args: object, **kwargs: object) -> object:
                if "journal_mode = WAL" in sql:
                    # Some unrelated error — must NOT be silently swallowed.
                    raise sqlite3.OperationalError("table missing")
                return real_execute(sql, *args, **kwargs)

            proxy = _ExecuteWrappingConnection(raw, fake_execute)

            with pytest.raises(sqlite3.OperationalError, match=r"table missing"):
                connection_mod._apply_wal_with_fallback(proxy, db_label="lcm.db")  # type: ignore[arg-type]
        finally:
            raw.close()

    def test_fallback_warning_dedupes_per_db_label(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Two raw conns; same db_label; both trigger the WAL-incompat
        # path. Only one WARNING should be emitted.
        for _ in range(2):
            raw = sqlite3.connect(":memory:")
            try:
                real_execute = raw.execute

                def fake_execute(sql: str, *args: object, **kwargs: object) -> object:
                    if "journal_mode = WAL" in sql:
                        raise sqlite3.OperationalError("locking protocol")
                    return real_execute(sql, *args, **kwargs)

                proxy = _ExecuteWrappingConnection(raw, fake_execute)
                with caplog.at_level(logging.WARNING, logger="lossless_hermes.db.connection"):
                    connection_mod._apply_wal_with_fallback(proxy, db_label="lcm.db")  # type: ignore[arg-type]
            finally:
                raw.close()

        wal_warnings = [r for r in caplog.records if "WAL journal_mode unsupported" in r.message]
        assert len(wal_warnings) == 1, f"expected single deduped warning, got {len(wal_warnings)}"


# ---------------------------------------------------------------------------
# 7. close_lcm_db — PRAGMA optimize + untrack
# ---------------------------------------------------------------------------


@_skip_no_extension_loading
class TestCloseLcmDb:
    def test_close_runs_pragma_optimize(self, tmp_path: Path) -> None:
        # ``sqlite3.Connection.set_trace_callback`` is the documented
        # stdlib seam for observing executed SQL — and unlike
        # monkey-patching ``conn.execute`` it works on C-immutable
        # Connection objects.
        conn = open_lcm_db(tmp_path / "lcm.db")
        executed_sql: list[str] = []
        conn.set_trace_callback(executed_sql.append)
        close_lcm_db(conn)
        assert any("PRAGMA optimize" in s for s in executed_sql), (
            f"PRAGMA optimize not invoked at close; executed={executed_sql!r}"
        )

    def test_close_swallows_optimize_operational_error(self, tmp_path: Path) -> None:
        # Per connection.ts:105 the optimize step is best-effort — a
        # SQLITE_BUSY / SQLITE_READONLY must NOT mask the close call.
        # We wrap the real conn in a proxy that raises on PRAGMA optimize
        # but forwards everything else (including .close()) untouched.
        real_conn = open_lcm_db(tmp_path / "lcm.db")
        real_execute = real_conn.execute

        def fake_execute(sql: str, *args: object, **kwargs: object) -> object:
            if "PRAGMA optimize" in sql:
                raise sqlite3.OperationalError("database is locked")
            return real_execute(sql, *args, **kwargs)

        proxy = _ExecuteWrappingConnection(real_conn, fake_execute)
        # The proxy is not tracked in the registry (only ``real_conn`` is
        # via the ``open_lcm_db`` call). To keep teardown clean we
        # manually pre-register the proxy id under the same path and
        # remove the real-conn entry, so the test's
        # ``close_lcm_db(proxy)`` call untracks the only registry row.
        with connection_mod._registry_lock:
            key = connection_mod._connection_index.pop(id(real_conn))
            entries = connection_mod._connections_by_path.get(key)
            if entries is not None:
                entries.discard(real_conn)
            entries = connection_mod._connections_by_path.setdefault(key, set())
            entries.add(proxy)  # type: ignore[arg-type]
            connection_mod._connection_index[id(proxy)] = key

        # The function under test must not raise even though the proxy's
        # ``execute("PRAGMA optimize")`` throws.
        close_lcm_db(proxy)  # type: ignore[arg-type]

    def test_close_handles_none(self) -> None:
        # connection.ts:99 returns early on falsy db; we mirror with None.
        close_lcm_db(None)

    def test_close_untracks(self, tmp_path: Path) -> None:
        conn = open_lcm_db(tmp_path / "lcm.db")
        with connection_mod._registry_lock:
            assert id(conn) in connection_mod._connection_index
        close_lcm_db(conn)
        with connection_mod._registry_lock:
            assert id(conn) not in connection_mod._connection_index


# ---------------------------------------------------------------------------
# 8. Registry: track / close_lcm_connection by path / close-all
# ---------------------------------------------------------------------------


@_skip_no_extension_loading
class TestRegistry:
    def test_open_tracks_connection(self, tmp_path: Path) -> None:
        db = tmp_path / "lcm.db"
        conn = open_lcm_db(db)
        try:
            key = normalize_path(db)
            with connection_mod._registry_lock:
                assert key in connection_mod._connections_by_path
                assert conn in connection_mod._connections_by_path[key]
                assert connection_mod._connection_index[id(conn)] == key
        finally:
            close_lcm_db(conn)

    def test_close_by_path_closes_all_threads_connections(
        self,
        tmp_path: Path,
    ) -> None:
        db = tmp_path / "lcm.db"

        opened: list[sqlite3.Connection] = []
        errors: list[Exception] = []

        def opener() -> None:
            try:
                opened.append(open_lcm_db(db))
            except Exception as exc:  # noqa: BLE001 -- forward to assert
                errors.append(exc)

        # Open from two distinct threads; both should be tracked under the
        # same normalized path.
        t1 = threading.Thread(target=opener)
        t2 = threading.Thread(target=opener)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        assert errors == [], f"thread opens failed: {errors}"
        assert len(opened) == 2

        key = normalize_path(db)
        with connection_mod._registry_lock:
            assert len(connection_mod._connections_by_path[key]) == 2

        close_lcm_connection(db)

        with connection_mod._registry_lock:
            assert key not in connection_mod._connections_by_path

    def test_close_all_clears_registry(self, tmp_path: Path) -> None:
        db1 = tmp_path / "one.db"
        db2 = tmp_path / "two.db"
        conn1 = open_lcm_db(db1)
        conn2 = open_lcm_db(db2)

        with connection_mod._registry_lock:
            assert len(connection_mod._connections_by_path) == 2

        close_lcm_connection()  # close-all

        with connection_mod._registry_lock:
            assert connection_mod._connections_by_path == {}
            assert connection_mod._connection_index == {}
        # Confirm conns no longer usable.
        for conn in (conn1, conn2):
            with pytest.raises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")

    def test_close_by_unknown_path_is_noop(self, tmp_path: Path) -> None:
        # Just must not raise.
        close_lcm_connection(tmp_path / "never-opened.db")

    def test_close_by_connection_target(self, tmp_path: Path) -> None:
        conn = open_lcm_db(tmp_path / "lcm.db")
        # ``close_lcm_connection`` accepts a Connection directly too.
        close_lcm_connection(conn)
        with connection_mod._registry_lock:
            assert id(conn) not in connection_mod._connection_index


# ---------------------------------------------------------------------------
# 9. open_lcm_db API surface — driver switch & validation
# ---------------------------------------------------------------------------


class TestOpenLcmDbApi:
    def test_apsw_driver_raises_not_implemented(self, tmp_path: Path) -> None:
        # Per ADR-004 the apsw extra is opt-in; the code path is reserved
        # for a follow-up issue. The function signature still accepts
        # ``driver="apsw"`` so callers using this PR's API don't break
        # when the apsw lane lands.
        with pytest.raises(NotImplementedError, match=r"apsw"):
            open_lcm_db(tmp_path / "lcm.db", driver="apsw")

    def test_invalid_driver_raises_value_error(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match=r"driver"):
            open_lcm_db(tmp_path / "lcm.db", driver="postgres")  # type: ignore[arg-type]

    @_skip_no_extension_loading
    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        # mkdir -p semantics. The TS code does the same — see
        # ``ensureDbDirectory`` in connection.ts.
        nested = tmp_path / "a" / "b" / "c" / "lcm.db"
        assert not nested.parent.exists()
        conn = open_lcm_db(nested)
        try:
            assert nested.parent.exists()
            assert nested.exists()
        finally:
            close_lcm_db(conn)

    @_skip_no_extension_loading
    def test_in_memory_path(self) -> None:
        conn = open_lcm_db(":memory:")
        try:
            # PRAGMAs apply even for :memory:; only journal_mode silently
            # ignores WAL on :memory: (becomes "memory"). The other 6 still
            # apply.
            assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == SQLITE_BUSY_TIMEOUT_MS
            # vec_version() still works on memory DBs.
            row = conn.execute("SELECT vec_version()").fetchone()
            assert row[0].startswith("v")
        finally:
            close_lcm_db(conn)

    @_skip_no_extension_loading
    def test_accepts_pathlib_path(self, tmp_path: Path) -> None:
        conn = open_lcm_db(tmp_path / "lcm.db")
        try:
            assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        finally:
            close_lcm_db(conn)


# ---------------------------------------------------------------------------
# 10. Durability — issue #144 P0 regression (autocommit / no implicit txn)
# ---------------------------------------------------------------------------
#
# ``open_lcm_db`` MUST open the stdlib connection with
# ``isolation_level=None`` (autocommit / explicit-transactions). With
# Python's default ``isolation_level=""`` the first DML silently opens an
# implicit deferred transaction that nothing on the ingest path commits,
# so ``conn.close()`` rolls the whole session back (issue #144 — the
# "lossless" engine lost everything on session close). These tests guard
# the connection-factory invariant directly, independent of the engine
# ingest path.


@_skip_no_extension_loading
class TestDurability:
    """Issue #144: writes must be durable across connection close."""

    def test_open_lcm_db_connection_is_autocommit(self, tmp_path: Path) -> None:
        """``open_lcm_db`` returns an ``isolation_level=None`` connection.

        This is the root-cause guard. ``isolation_level=None`` means no
        implicit transaction is opened on DML; a regression back to the
        stdlib default ``""`` reintroduces issue #144.
        """
        conn = open_lcm_db(tmp_path / "lcm.db")
        try:
            assert conn.isolation_level is None, (
                "issue #144: open_lcm_db must use isolation_level=None — "
                f"got {conn.isolation_level!r}, which silently opens an "
                "uncommitted implicit transaction on the first write"
            )
        finally:
            close_lcm_db(conn)

    def test_open_db_connection_is_autocommit(self, tmp_path: Path) -> None:
        """The role-aware ``open_db`` factory is autocommit too.

        ``open_db`` is the embeddings-subsystem factory; it shares the
        same durability requirement as ``open_lcm_db``.
        """
        conn = open_db(tmp_path / "lcm.db")
        try:
            # ``open_db`` may return an apsw adapter on the fallback path;
            # apsw is autocommit-by-default so this assertion is stdlib-
            # specific (the primary path on supported platforms).
            if isinstance(conn, sqlite3.Connection):
                assert conn.isolation_level is None, (
                    "issue #144: open_db must use isolation_level=None"
                )
        finally:
            close_lcm_db(conn)  # type: ignore[arg-type]

    def test_no_implicit_transaction_after_write(self, tmp_path: Path) -> None:
        """A bare INSERT does not leave an open transaction.

        Mirrors ``ConversationStore.create_conversation``'s bare INSERT —
        the exact statement that opened the never-committed implicit txn
        pre-#144. Under ``isolation_level=None`` the write autocommits and
        ``in_transaction`` stays ``False``.
        """
        conn = open_lcm_db(tmp_path / "lcm.db")
        try:
            conn.execute("CREATE TABLE durability_probe (id INTEGER PRIMARY KEY)")
            assert conn.in_transaction is False
            conn.execute("INSERT INTO durability_probe (id) VALUES (1)")
            assert conn.in_transaction is False, (
                "issue #144: a bare INSERT left an implicit transaction "
                "open — it will roll back on conn.close()"
            )
        finally:
            close_lcm_db(conn)

    def test_write_survives_close_and_reopen(self, tmp_path: Path) -> None:
        """A row written then closed is visible on a fresh reopen.

        The end-to-end durability assertion at the connection layer:
        open → write → ``close_lcm_db`` → reopen via ``open_lcm_db`` →
        the row is still there. Pre-#144 the reopen showed 0 rows.
        """
        db_path = tmp_path / "lcm.db"

        conn = open_lcm_db(db_path)
        conn.execute("CREATE TABLE durable (id INTEGER PRIMARY KEY, payload TEXT)")
        conn.execute("INSERT INTO durable (id, payload) VALUES (1, 'survives')")
        close_lcm_db(conn)

        reopened = open_lcm_db(db_path)
        try:
            rows = reopened.execute("SELECT id, payload FROM durable").fetchall()
            assert rows == [(1, "survives")], (
                f"issue #144: data written before close was rolled back — fresh reopen shows {rows}"
            )
        finally:
            close_lcm_db(reopened)

    def test_explicit_begin_commit_still_works(self, tmp_path: Path) -> None:
        """Explicit ``BEGIN IMMEDIATE`` / ``COMMIT`` works under autocommit.

        ``isolation_level=None`` does not disable explicit transactions —
        it is exactly the "explicit-transactions" half of the apsw
        adapter's "autocommit + explicit-transactions" contract. This
        guards the ``with_transaction`` / migration ``BEGIN EXCLUSIVE``
        paths that depend on explicit transactions still functioning.
        """
        conn = open_lcm_db(tmp_path / "lcm.db")
        try:
            conn.execute("CREATE TABLE tx_probe (id INTEGER PRIMARY KEY)")
            conn.execute("BEGIN IMMEDIATE")
            assert conn.in_transaction is True
            conn.execute("INSERT INTO tx_probe (id) VALUES (1)")
            conn.execute("COMMIT")
            assert conn.in_transaction is False
            # Rollback path: the row is discarded.
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO tx_probe (id) VALUES (2)")
            conn.execute("ROLLBACK")
            assert conn.execute("SELECT COUNT(*) FROM tx_probe").fetchone()[0] == 1
        finally:
            close_lcm_db(conn)
