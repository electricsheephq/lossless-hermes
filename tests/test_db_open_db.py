"""Tests for :mod:`lossless_hermes.db.connection` 05-04 surface.

Covers the acceptance criteria from
``epics/05-embeddings/05-04-vec0-load-pattern.md``:

* ``open_db(path)`` returns a connection with vec0 loaded;
  ``vec0_version(conn)`` returns a string.
* ``open_db(..., role="gateway")`` sets ``busy_timeout=30000``;
  ``role="worker"`` sets ``5000``; an invalid role raises.
* Apple-system-Python guard fires through :func:`open_db` with the
  documented actionable message.
* ``try_load_sqlite_vec(conn)`` returns :data:`True` on a loadable
  conn; :data:`False` (with a WARNING) on a conn that lacks
  ``enable_load_extension`` AND when sqlite_vec.load raises
  :class:`sqlite3.OperationalError`.
* ``vec0_version(conn)`` returns :data:`None` for a conn without the
  extension loaded.
* apsw fallback (gated on :data:`HAS_APSW`): re-routes when stdlib
  ``sqlite3.connect`` raises :class:`OperationalError`.

Test isolation: ``open_db`` registers the connection in the same
process-global registry as :func:`open_lcm_db`, so we re-use the same
:func:`close_lcm_connection` reset fixture pattern.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Iterator
from unittest import mock

import pytest

from lossless_hermes.db import connection as connection_mod
from lossless_hermes.db.connection import (
    HAS_APSW,
    SQLITE_BUSY_TIMEOUT_GATEWAY_MS,
    SQLITE_BUSY_TIMEOUT_WORKER_MS,
    Connection,
    _busy_timeout_for_role,
    close_lcm_connection,
    close_lcm_db,
    open_db,
    try_load_sqlite_vec,
    vec0_version,
)

# ---------------------------------------------------------------------------
# Skip marker mirrors test_db_connection.py — actions/setup-python on macOS
# ships a CPython build without ``--enable-loadable-sqlite-extensions``,
# which would make every live-DB test fail. The Apple-Python guard tests
# still run since they explicitly monkey-patch the introspection hook.
# ---------------------------------------------------------------------------
_skip_no_extension_loading = pytest.mark.skipif(
    not hasattr(sqlite3.Connection, "enable_load_extension"),
    reason=(
        "actions/setup-python on macOS ships a CPython build without "
        "--enable-loadable-sqlite-extensions; sqlite-vec cannot load. "
        "Apple-Python-guard tests still run (see TestOpenDbApplePythonGuard)."
    ),
)


@pytest.fixture(autouse=True)
def _clear_registry() -> Iterator[None]:
    """Reset the connection registry before and after each test."""
    close_lcm_connection()
    with connection_mod._wal_fallback_warned_lock:
        connection_mod._wal_fallback_warned_paths.clear()
    yield
    close_lcm_connection()
    with connection_mod._wal_fallback_warned_lock:
        connection_mod._wal_fallback_warned_paths.clear()


# ---------------------------------------------------------------------------
# 1. _busy_timeout_for_role — internal helper
# ---------------------------------------------------------------------------


class TestBusyTimeoutForRole:
    """The role→ms map is the single source of truth — verify both branches."""

    def test_gateway_role_returns_30000(self) -> None:
        assert _busy_timeout_for_role("gateway") == 30_000
        assert _busy_timeout_for_role("gateway") == SQLITE_BUSY_TIMEOUT_GATEWAY_MS

    def test_worker_role_returns_5000(self) -> None:
        assert _busy_timeout_for_role("worker") == 5_000
        assert _busy_timeout_for_role("worker") == SQLITE_BUSY_TIMEOUT_WORKER_MS

    def test_invalid_role_raises_value_error(self) -> None:
        # Defensive guard for callers that bypass the DbRole typing.Literal
        # at runtime (e.g. config-driven role string).
        with pytest.raises(ValueError, match=r"role must be 'gateway' or 'worker'"):
            _busy_timeout_for_role("forground")  # type: ignore[arg-type]

    def test_invalid_role_error_message_references_adr(self) -> None:
        with pytest.raises(ValueError, match=r"ADR-018"):
            _busy_timeout_for_role("admin")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 2. open_db — role-based busy_timeout
# ---------------------------------------------------------------------------


@_skip_no_extension_loading
class TestOpenDbRole:
    def test_gateway_role_sets_busy_timeout_30000(self, tmp_path: Path) -> None:
        # AC: ``open_db(..., role="gateway")`` sets busy_timeout=30000.
        conn = open_db(tmp_path / "lcm.db", role="gateway")
        try:
            timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            assert timeout == SQLITE_BUSY_TIMEOUT_GATEWAY_MS == 30_000
        finally:
            close_lcm_db(conn)

    def test_worker_role_sets_busy_timeout_5000(self, tmp_path: Path) -> None:
        # AC: ``role="worker"`` sets busy_timeout=5000 — gateway always wins
        # contention (ADR-018).
        conn = open_db(tmp_path / "lcm.db", role="worker")
        try:
            timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            assert timeout == SQLITE_BUSY_TIMEOUT_WORKER_MS == 5_000
        finally:
            close_lcm_db(conn)

    def test_default_role_is_gateway(self, tmp_path: Path) -> None:
        # The signature ``open_db(path, *, role="gateway")`` defaults to
        # the high-timeout role — verify the default takes effect.
        conn = open_db(tmp_path / "lcm.db")
        try:
            timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            assert timeout == SQLITE_BUSY_TIMEOUT_GATEWAY_MS
        finally:
            close_lcm_db(conn)

    def test_invalid_role_raises_before_fs_work(self, tmp_path: Path) -> None:
        # The ValueError must fire BEFORE ``sqlite3.connect`` and any FS
        # ``mkdir -p`` — defensive ordering keeps the failure crisp.
        target = tmp_path / "nested-that-should-not-exist" / "lcm.db"
        with pytest.raises(ValueError, match=r"role must be 'gateway' or 'worker'"):
            open_db(target, role="admin")  # type: ignore[arg-type]
        # mkdir -p should NOT have run.
        assert not target.parent.exists()


# ---------------------------------------------------------------------------
# 3. open_db — vec0 loaded + Connection Protocol satisfied
# ---------------------------------------------------------------------------


@_skip_no_extension_loading
class TestOpenDbVec0:
    def test_returns_connection_with_vec0_loaded(self, tmp_path: Path) -> None:
        # AC: ``open_db("test.db")`` returns a connection with vec0 loaded;
        # ``vec0_version(conn)`` returns a string.
        conn = open_db(tmp_path / "lcm.db")
        try:
            v = vec0_version(conn)
            assert isinstance(v, str)
            assert v.startswith("v")  # e.g. "v0.1.9"
        finally:
            close_lcm_db(conn)

    def test_returned_object_satisfies_connection_protocol(self, tmp_path: Path) -> None:
        # The Connection Protocol is runtime_checkable — verify the
        # stdlib path returns a Protocol-compatible object.
        conn = open_db(tmp_path / "lcm.db")
        try:
            assert isinstance(conn, Connection)
            # All four contract methods are present.
            assert callable(conn.execute)
            assert callable(conn.executemany)
            assert callable(conn.commit)
            assert callable(conn.close)
        finally:
            close_lcm_db(conn)

    def test_in_memory_path_works(self) -> None:
        # ``:memory:`` is the canonical in-memory path. vec_version still
        # works on memory DBs even though WAL journal mode silently
        # downgrades to ``memory``.
        conn = open_db(":memory:")
        try:
            assert vec0_version(conn) is not None
        finally:
            close_lcm_db(conn)


# ---------------------------------------------------------------------------
# 4. open_db — Apple-system-Python guard (AC item 2)
# ---------------------------------------------------------------------------


class TestOpenDbApplePythonGuard:
    """The Apple-Python guard fires through :func:`open_db` with the
    documented actionable message. We patch
    :func:`engine._has_sqlite_extension_loading` to simulate the
    missing-attribute case (Apple's :file:`/usr/bin/python3`).
    """

    def test_guard_raises_runtime_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import lossless_hermes.engine as engine_mod

        monkeypatch.setattr(engine_mod, "_has_sqlite_extension_loading", lambda: False)

        with pytest.raises(RuntimeError, match=r"enable_load_extension"):
            open_db(tmp_path / "lcm.db")

    def test_guard_message_is_actionable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The message must name install paths so an operator doesn't have
        # to grep docs.
        import lossless_hermes.engine as engine_mod

        monkeypatch.setattr(engine_mod, "_has_sqlite_extension_loading", lambda: False)
        try:
            open_db(tmp_path / "lcm.db")
            pytest.fail("expected RuntimeError")
        except RuntimeError as exc:
            msg = str(exc)
            assert "Homebrew" in msg
            assert "pyenv" in msg
            assert "uv" in msg

    def test_guard_fires_before_apsw_fallback(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Even when HAS_APSW is True, the Apple-Python guard's
        # RuntimeError must not be silently caught and fall through to
        # apsw — Apple-Python is a configuration error, not a transient
        # stdlib failure.
        import lossless_hermes.engine as engine_mod

        monkeypatch.setattr(engine_mod, "_has_sqlite_extension_loading", lambda: False)
        # Force HAS_APSW=True for this test (whether or not the apsw
        # extra is actually installed in the test env).
        monkeypatch.setattr(connection_mod, "HAS_APSW", True)

        # If the guard didn't fire we'd see a different error path
        # (apsw fallback runs). The pytest.raises confirms RuntimeError
        # is what propagates.
        with pytest.raises(RuntimeError, match=r"Apple"):
            open_db(tmp_path / "lcm.db")

    def test_guard_does_not_leak_registry_entries(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # If the guard fires before ``sqlite3.connect`` succeeds, the
        # registry must stay empty.
        import lossless_hermes.engine as engine_mod

        monkeypatch.setattr(engine_mod, "_has_sqlite_extension_loading", lambda: False)
        with pytest.raises(RuntimeError):
            open_db(tmp_path / "lcm.db")
        with connection_mod._registry_lock:
            assert connection_mod._connections_by_path == {}
            assert connection_mod._connection_index == {}


# ---------------------------------------------------------------------------
# 5. try_load_sqlite_vec — public helper
# ---------------------------------------------------------------------------


@_skip_no_extension_loading
class TestTryLoadSqliteVec:
    def test_returns_true_on_loadable_connection(self) -> None:
        # AC: ``try_load_sqlite_vec(conn)`` returns True on a connection
        # where the extension is loadable.
        raw = sqlite3.connect(":memory:")
        try:
            assert try_load_sqlite_vec(raw) is True
            # And vec_version works as a follow-up.
            assert vec0_version(raw) is not None
        finally:
            raw.close()

    def test_returns_false_when_enable_load_extension_missing(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # AC: ``try_load_sqlite_vec(conn)`` returns False when the
        # extension is not loadable. Simulate by patching the connection
        # object's ``enable_load_extension`` away — equivalent to the
        # Apple-Python failure surface (where the method is missing).
        raw = sqlite3.connect(":memory:")
        try:
            # Patch ``sqlite_vec.load`` to raise AttributeError to mimic
            # the Apple-Python path that bubbles a missing attribute up
            # through the load helper. We can't directly delete the
            # method from a C-immutable sqlite3.Connection.
            def boom(_conn: object) -> None:
                raise AttributeError(
                    "'sqlite3.Connection' object has no attribute 'enable_load_extension'"
                )

            monkeypatch.setattr(connection_mod.sqlite_vec, "load", boom)
            with caplog.at_level(logging.WARNING, logger="lossless_hermes.db.connection"):
                result = try_load_sqlite_vec(raw)
            assert result is False
            # WARNING was emitted (not silent).
            assert any("failed to load sqlite-vec" in r.message for r in caplog.records)
        finally:
            raw.close()

    def test_returns_false_on_operational_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        raw = sqlite3.connect(":memory:")
        try:

            def boom(_conn: object) -> None:
                raise sqlite3.OperationalError("vec extension not found")

            monkeypatch.setattr(connection_mod.sqlite_vec, "load", boom)
            assert try_load_sqlite_vec(raw) is False
        finally:
            raw.close()

    def test_silent_true_suppresses_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Matches the TS opts.silent flag — failure path must not log
        # when silent=True.
        raw = sqlite3.connect(":memory:")
        try:

            def boom(_conn: object) -> None:
                raise sqlite3.OperationalError("simulated failure")

            monkeypatch.setattr(connection_mod.sqlite_vec, "load", boom)
            with caplog.at_level(logging.WARNING, logger="lossless_hermes.db.connection"):
                result = try_load_sqlite_vec(raw, silent=True)
            assert result is False
            assert not any("failed to load sqlite-vec" in r.message for r in caplog.records)
        finally:
            raw.close()

    def test_disables_extension_loading_after_load(self) -> None:
        # Spike-001 recommendation: enable → load → disable. Verify the
        # final ``enable_load_extension(False)`` call fires on success.
        # Use a ``sqlite3.Connection`` subclass so we can override
        # ``enable_load_extension`` (the base class's method is
        # C-immutable and rejects ``setattr``).

        calls: list[tuple[bool]] = []

        class _TrackingConn(sqlite3.Connection):
            def enable_load_extension(self, value: bool) -> None:  # type: ignore[override]
                calls.append((value,))
                super().enable_load_extension(value)

        raw = sqlite3.connect(":memory:", factory=_TrackingConn)
        try:
            assert try_load_sqlite_vec(raw) is True
            # Enable(True) then Enable(False) in order.
            assert (True,) in calls
            assert (False,) in calls
            assert calls.index((True,)) < calls.index((False,))
        finally:
            raw.close()


# ---------------------------------------------------------------------------
# 6. vec0_version — probe behavior
# ---------------------------------------------------------------------------


class TestVec0Version:
    @_skip_no_extension_loading
    def test_returns_version_string_when_loaded(self, tmp_path: Path) -> None:
        # AC: ``vec0_version(conn)`` returns a version string when loaded.
        conn = open_db(tmp_path / "lcm.db")
        try:
            v = vec0_version(conn)
            assert isinstance(v, str)
            assert v.startswith("v")
        finally:
            close_lcm_db(conn)

    def test_returns_none_when_not_loaded(self) -> None:
        # AC: ``vec0_version(conn)`` returns None when extension not loaded
        # (uses a :memory: connection without enable_load_extension).
        raw = sqlite3.connect(":memory:")
        try:
            assert vec0_version(raw) is None
        finally:
            raw.close()

    def test_returns_none_on_any_exception_path(self) -> None:
        # The probe absorbs any exception class — verify a non-stdlib
        # ExecutionError-shaped exception still maps to None. We use a
        # ``sqlite3.Connection`` subclass to override ``execute`` (the
        # base method is C-immutable so :func:`pytest.monkeypatch.setattr`
        # cannot patch it directly).

        class _BoomingConn(sqlite3.Connection):
            def execute(self, *_a: object, **_kw: object) -> object:  # type: ignore[override]
                raise RuntimeError("simulated apsw ExecutionError")

        raw = sqlite3.connect(":memory:", factory=_BoomingConn)
        try:
            assert vec0_version(raw) is None
        finally:
            raw.close()


# ---------------------------------------------------------------------------
# 7. apsw fallback (gated on HAS_APSW)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_APSW, reason="apsw extra not installed")
class TestApswFallback:
    """Validate the apsw fallback path on environments with the
    ``[apsw]`` extra installed. Per the 05-04 spec the fallback fires
    only when stdlib ``sqlite3.connect`` raises
    :class:`sqlite3.OperationalError`."""

    def test_open_db_apsw_direct_path(self, tmp_path: Path) -> None:
        # AC: ``open_db(path)`` with apsw extra → apsw.Connection (or
        # compatible) with vec0 loaded; vec_version() returns the row.
        conn = connection_mod._open_with_apsw(tmp_path / "apsw.db", "gateway")
        try:
            row = list(conn.execute("SELECT vec_version()"))
            assert row, "vec_version() returned no row"
            assert isinstance(row[0][0], str)
            assert row[0][0].startswith("v")
        finally:
            conn.close()

    def test_apsw_path_sets_busy_timeout_per_role(self, tmp_path: Path) -> None:
        # The apsw branch must honor the same role split — gateway 30 s,
        # worker 5 s.
        for role, expected in (("gateway", 30_000), ("worker", 5_000)):
            conn = connection_mod._open_with_apsw(tmp_path / f"{role}.db", role)  # type: ignore[arg-type]
            try:
                row = list(conn.execute("PRAGMA busy_timeout"))
                assert row[0][0] == expected, f"{role}: expected {expected}, got {row[0][0]}"
            finally:
                conn.close()

    def test_apsw_path_returns_connection_protocol_compatible(self, tmp_path: Path) -> None:
        # apsw.Connection structurally satisfies the Connection Protocol
        # — verify the four contract methods exist.
        conn = connection_mod._open_with_apsw(tmp_path / "apsw.db", "gateway")
        try:
            assert callable(conn.execute)
            assert callable(conn.executemany)
            assert callable(conn.commit)
            assert callable(conn.close)
        finally:
            conn.close()

    def test_apsw_fallback_triggered_by_stdlib_operational_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Simulate stdlib ``sqlite3.connect`` raising
        # :class:`OperationalError` and verify the apsw fallback fires.
        target = tmp_path / "trigger-apsw.db"

        original_connect = sqlite3.connect
        call_count = {"n": 0}

        def boom_then_real(*args: object, **kwargs: object) -> object:
            call_count["n"] += 1
            raise sqlite3.OperationalError("simulated read-only mount")

        monkeypatch.setattr(sqlite3, "connect", boom_then_real)

        conn = open_db(target, role="gateway")
        try:
            # The apsw fallback's PRAGMA busy_timeout = 30000 must be in
            # effect. ``apsw.Connection.execute`` returns a cursor whose
            # iteration yields tuples.
            row = list(conn.execute("PRAGMA busy_timeout"))
            assert row[0][0] == SQLITE_BUSY_TIMEOUT_GATEWAY_MS
            # And we definitely went through the boom-stub.
            assert call_count["n"] == 1
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            # Restore so the autouse fixture's close_lcm_connection() works.
            monkeypatch.setattr(sqlite3, "connect", original_connect)


class TestApswFallbackWithoutExtra:
    """When the apsw extra is NOT installed, ``_open_with_apsw`` raises
    an actionable :class:`ImportError`. The stdlib path's
    ``OperationalError`` propagates unchanged in :func:`open_db`."""

    def test_helper_raises_import_error_without_extra(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Force HAS_APSW=False even if the extra is installed in the
        # test env — exercises the import-error branch.
        monkeypatch.setattr(connection_mod, "HAS_APSW", False)
        monkeypatch.setattr(connection_mod, "_apsw", None)

        with pytest.raises(ImportError, match=r"\[apsw\]"):
            connection_mod._open_with_apsw(tmp_path / "x.db", "gateway")

    def test_open_db_reraises_operational_error_without_apsw(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Stdlib ``sqlite3.connect`` raises ``OperationalError`` AND no
        # apsw extra → re-raise the original error.
        monkeypatch.setattr(connection_mod, "HAS_APSW", False)

        def boom(*args: object, **kwargs: object) -> object:
            raise sqlite3.OperationalError("read-only filesystem")

        monkeypatch.setattr(sqlite3, "connect", boom)

        with pytest.raises(sqlite3.OperationalError, match=r"read-only filesystem"):
            open_db(tmp_path / "ro.db")


# ---------------------------------------------------------------------------
# 8. open_db — sqlite-vec load failure without apsw extra
# ---------------------------------------------------------------------------


@_skip_no_extension_loading
class TestOpenDbSqliteVecLoadFailure:
    """When :func:`sqlite_vec.load` fails AND no apsw extra is installed,
    :func:`open_db` raises a clear :class:`OperationalError` with the
    install hint."""

    def test_raises_operational_error_when_no_apsw(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(connection_mod, "HAS_APSW", False)

        def boom(_conn: object) -> None:
            raise sqlite3.OperationalError("simulated load failure")

        monkeypatch.setattr(connection_mod.sqlite_vec, "load", boom)

        with pytest.raises(sqlite3.OperationalError, match=r"\[apsw\]"):
            open_db(tmp_path / "fail.db")

    def test_no_registry_leak_on_load_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Confirm the half-configured stdlib conn was closed and the
        # registry is empty.
        monkeypatch.setattr(connection_mod, "HAS_APSW", False)

        def boom(_conn: object) -> None:
            raise sqlite3.OperationalError("simulated load failure")

        monkeypatch.setattr(connection_mod.sqlite_vec, "load", boom)

        with pytest.raises(sqlite3.OperationalError):
            open_db(tmp_path / "fail.db")
        with connection_mod._registry_lock:
            assert connection_mod._connections_by_path == {}
            assert connection_mod._connection_index == {}


# ---------------------------------------------------------------------------
# 9. open_db — PRAGMA parity with open_lcm_db
# ---------------------------------------------------------------------------


@_skip_no_extension_loading
class TestOpenDbPragmaParity:
    """Confirm :func:`open_db` applies the same PRAGMA set as
    :func:`open_lcm_db` (modulo the role-based busy_timeout).

    Without this, a caller that swaps :func:`open_lcm_db` for
    :func:`open_db` could silently drop ``foreign_keys = ON`` or
    ``synchronous = NORMAL`` etc., which would be a latent
    correctness/performance bug.
    """

    def test_journal_mode_is_wal(self, tmp_path: Path) -> None:
        conn = open_db(tmp_path / "lcm.db")
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode.lower() == "wal"
        finally:
            close_lcm_db(conn)

    def test_foreign_keys_on(self, tmp_path: Path) -> None:
        conn = open_db(tmp_path / "lcm.db")
        try:
            fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            assert fk == 1
        finally:
            close_lcm_db(conn)

    def test_cache_size_negative_65536(self, tmp_path: Path) -> None:
        conn = open_db(tmp_path / "lcm.db")
        try:
            cache = conn.execute("PRAGMA cache_size").fetchone()[0]
            assert cache == -65536
        finally:
            close_lcm_db(conn)

    def test_synchronous_normal(self, tmp_path: Path) -> None:
        conn = open_db(tmp_path / "lcm.db")
        try:
            sync = conn.execute("PRAGMA synchronous").fetchone()[0]
            assert sync == 1
        finally:
            close_lcm_db(conn)

    def test_temp_store_memory(self, tmp_path: Path) -> None:
        conn = open_db(tmp_path / "lcm.db")
        try:
            ts = conn.execute("PRAGMA temp_store").fetchone()[0]
            assert ts == 2
        finally:
            close_lcm_db(conn)


# Reference unused imports so the lint pass doesn't drop them.
_ = mock  # type: ignore[no-redef]
