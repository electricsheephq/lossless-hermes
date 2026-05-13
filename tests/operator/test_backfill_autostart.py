"""Tests for :mod:`lossless_hermes.operator.backfill_autostart` (issue 05-11).

Ports the test list from
``epics/05-embeddings/05-11-autostart-wiring.md`` §"Tests
(``tests/operator/test_backfill_autostart.py``)". Voyage HTTP is NEVER
hit — every test injects a mock ``tick_fn`` that returns canned
:class:`BackfillResult` shapes or raises canned exceptions.

vec0-dependent because the autostart's pre-flight check calls
:func:`~lossless_hermes.embeddings.store.embeddings_table_exists`. The
suite is gated on the extension being loadable via :data:`skip_if_no_vec0`.

Test inventory (matches the issue spec):

* ``VOYAGE_API_KEY`` empty → silent no-op.
* ``VOYAGE_API_KEY`` set + no active profile → no-op + warning log.
* ``VOYAGE_API_KEY`` set + vec0 table missing → no-op + warning log.
* Happy path: short interval + mock tick that returns
  ``BackfillResult(embedded_count=5, ...)``; ``handle.tick_count``
  increases over time.
* Idle drain: 3 consecutive idle ticks → ``is_running() == False``.
* Idle reset: 2 idle then 1 non-idle resets the counter.
* Voyage-failure backoff (via skips): 3 consecutive ticks return
  skipped with voyage reasons → ``is_running() == False``.
* Voyage-failure backoff (via raise): 3 consecutive ticks raise a
  non-auth :class:`VoyageError` → ``is_running() == False``.
* Auth error: a tick raises :class:`VoyageError(kind="auth")` →
  autostart stops immediately (NOT after 3 strikes).
* ``handle.stop()`` is idempotent and stops the underlying worker loop.
* :attr:`AutostartHandle.tick_count`, ``idle_strikes``,
  ``voyage_failure_strikes`` reflect state.

Mirrors the timing pattern from :mod:`tests.concurrency.test_worker_loop`:
``interval_s`` ≈ 0.02 s, ``asyncio.sleep`` ≈ 0.1-0.3 s to let the loop
tick a few times before assertion.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator

import pytest

from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.embeddings.backfill import BackfillResult, BackfillSkippedDoc
from lossless_hermes.embeddings.store import (
    ensure_embeddings_table,
    register_embedding_profile,
)
from lossless_hermes.operator.backfill_autostart import (
    DEFAULT_AUTOSTART_INTERVAL_S,
    AutostartHandle,
    start_embedding_backfill_autostart,
)
from lossless_hermes.voyage.client import VoyageError


# ---------------------------------------------------------------------------
# vec0 availability probe — same as test_store.py / test_backfill.py.
# ---------------------------------------------------------------------------


def _vec0_loadable() -> bool:
    """Return :data:`True` iff ``sqlite_vec.load`` succeeds on this Python.

    Mirrors :func:`tests.embeddings.test_store._vec0_loadable`. The
    probe runs once at module import so the skip decorator has a value
    to consume before any test runs.
    """
    if not hasattr(sqlite3.Connection, "enable_load_extension"):
        return False
    try:
        import sqlite_vec  # local import to keep top-level cheap

        conn = sqlite3.connect(":memory:")
        try:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            return True
        finally:
            conn.close()
    except (AttributeError, sqlite3.OperationalError):
        return False


VEC0_AVAILABLE: bool = _vec0_loadable()


skip_if_no_vec0 = pytest.mark.skipif(
    not VEC0_AVAILABLE,
    reason=(
        "sqlite-vec extension not loadable on this Python build. "
        "Vec0-dependent autostart tests skip cleanly."
    ),
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _setup_db(register_profile: bool = True, ensure_table: bool = True) -> sqlite3.Connection:
    """Open a fresh in-memory DB with vec0 + the v4.1 migrations + a profile.

    Same pattern as :func:`tests.embeddings.test_backfill._setup_db` but
    parameterised so we can verify the no-profile gating path.
    """
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    if VEC0_AVAILABLE:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    run_lcm_migrations(conn, fts5_available=False)
    if register_profile:
        register_embedding_profile(conn, "voyage-4-large", 3)
    if ensure_table and VEC0_AVAILABLE:
        ensure_embeddings_table(conn, "voyage-4-large", 3)
    return conn


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    """In-memory DB with vec0 + migrations + profile + vec0 table."""
    conn = _setup_db()
    try:
        yield conn
    finally:
        conn.close()


class CapturedLogger:
    """An :class:`AutostartLogger`-compatible recorder.

    Captures every message for assertion. The ``warn`` method matches
    the Protocol (TS source uses ``warn`` not ``warning``).
    """

    def __init__(self) -> None:
        self.info_messages: list[str] = []
        self.warn_messages: list[str] = []
        self.error_messages: list[str] = []

    def info(self, msg: str) -> None:
        self.info_messages.append(msg)

    def warn(self, msg: str) -> None:
        self.warn_messages.append(msg)

    def error(self, msg: str) -> None:
        self.error_messages.append(msg)

    def all_messages(self) -> list[str]:
        return self.info_messages + self.warn_messages + self.error_messages


@pytest.fixture
def log() -> CapturedLogger:
    return CapturedLogger()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tick_fn(
    results: list[BackfillResult | BaseException],
    counter: list[int] | None = None,
):
    """Return an async tick function that yields ``results`` in sequence.

    If the list is exhausted, the last entry is repeated forever (so a
    test that wants "always idle" passes one idle result and lets the
    loop tick as many times as it wants).

    Args:
        results: Sequence of either :class:`BackfillResult` (returned)
            or :class:`BaseException` (raised) values.
        counter: Optional list whose ``[0]`` element is incremented each
            invocation. Lets tests assert "tick_fn was called N times".

    Returns:
        An async callable suitable for the ``tick_fn`` parameter.
    """

    async def _tick() -> BackfillResult:
        if counter is not None:
            counter[0] += 1
        idx = min((counter[0] - 1) if counter else 0, len(results) - 1)
        item = results[idx] if idx < len(results) else results[-1]
        if isinstance(item, BaseException):
            raise item
        return item

    return _tick


async def _stop_and_wait(handle: AutostartHandle) -> None:
    """Stop the autostart and drain in-flight ticks."""
    await handle.stop(graceful_timeout_s=2.0)


# ===========================================================================
# Gating
# ===========================================================================


async def test_no_op_when_voyage_api_key_missing(
    db: sqlite3.Connection, log: CapturedLogger
) -> None:
    """``VOYAGE_API_KEY`` empty → silent no-op.

    Returns a handle with ``is_running() == False``. No worker-loop
    started. The mock tick_fn is never called.
    """
    counter = [0]
    tick_fn = _make_tick_fn([BackfillResult(embedded_count=1)], counter)

    handle = start_embedding_backfill_autostart(
        db,
        log=log,
        tick_fn=tick_fn,
        interval_s=0.02,
        env={},
        model_name="voyage-4-large",
    )
    # Give the loop a moment in case it started by mistake.
    await asyncio.sleep(0.1)

    assert handle.is_running() is False
    assert counter[0] == 0
    # The gating log message identifies the reason.
    joined = " ".join(log.info_messages)
    assert "VOYAGE_API_KEY" in joined


@skip_if_no_vec0
async def test_no_op_when_tick_fn_is_none(db: sqlite3.Connection, log: CapturedLogger) -> None:
    """``tick_fn=None`` (integration bug) → no-op + error log.

    The TS source defaults ``tickFn`` to ``tickEmbeddingBackfill`` from
    ``worker-orchestrator.ts``. The Python port doesn't have that
    orchestrator yet (Epic 08), so the caller MUST pass a bound tick
    callable. If they pass ``None``, the autostart logs an error and
    returns a no-op handle.
    """
    handle = start_embedding_backfill_autostart(
        db,
        log=log,
        tick_fn=None,
        interval_s=0.02,
        env={"VOYAGE_API_KEY": "test"},
        model_name="voyage-4-large",
    )
    await asyncio.sleep(0.05)
    assert handle.is_running() is False
    assert any("tick_fn is None" in m for m in log.error_messages)


async def test_no_op_when_voyage_api_key_only_whitespace(
    db: sqlite3.Connection, log: CapturedLogger
) -> None:
    """Whitespace-only API key is treated as empty."""
    counter = [0]
    tick_fn = _make_tick_fn([BackfillResult(embedded_count=1)], counter)

    handle = start_embedding_backfill_autostart(
        db,
        log=log,
        tick_fn=tick_fn,
        interval_s=0.02,
        env={"VOYAGE_API_KEY": "   "},
        model_name="voyage-4-large",
    )
    await asyncio.sleep(0.05)
    assert handle.is_running() is False
    assert counter[0] == 0


async def test_no_op_when_no_active_profile(
    log: CapturedLogger,
) -> None:
    """``VOYAGE_API_KEY`` set + ``model_name`` missing → no-op + warn.

    The TS source resolves the active profile from
    ``lcm_embedding_profile`` via ``getActiveEmbeddingModel``; until 05-08
    ports that helper, the Python equivalent expects ``model_name`` to
    be passed explicitly. Passing ``None`` (or empty) returns a no-op
    handle with a warning log.
    """
    conn = _setup_db(register_profile=False, ensure_table=False)
    try:
        counter = [0]
        tick_fn = _make_tick_fn([BackfillResult(embedded_count=1)], counter)

        handle = start_embedding_backfill_autostart(
            conn,
            log=log,
            tick_fn=tick_fn,
            interval_s=0.02,
            env={"VOYAGE_API_KEY": "test"},
            model_name=None,
        )
        await asyncio.sleep(0.05)
        assert handle.is_running() is False
        assert counter[0] == 0
        assert any("no active embedding profile" in m for m in log.warn_messages)
    finally:
        conn.close()


@skip_if_no_vec0
async def test_no_op_when_vec0_table_missing(
    log: CapturedLogger,
) -> None:
    """``model_name`` is passed but the vec0 table doesn't exist → no-op."""
    conn = _setup_db(register_profile=True, ensure_table=False)
    try:
        counter = [0]
        tick_fn = _make_tick_fn([BackfillResult(embedded_count=1)], counter)

        handle = start_embedding_backfill_autostart(
            conn,
            log=log,
            tick_fn=tick_fn,
            interval_s=0.02,
            env={"VOYAGE_API_KEY": "test"},
            model_name="voyage-4-large",
        )
        await asyncio.sleep(0.05)
        assert handle.is_running() is False
        assert counter[0] == 0
        assert any("embeddings table" in m for m in log.warn_messages)
    finally:
        conn.close()


# ===========================================================================
# Happy path
# ===========================================================================


@skip_if_no_vec0
async def test_happy_path_ticks_fire(db: sqlite3.Connection, log: CapturedLogger) -> None:
    """Mock ``tick_fn`` returns non-idle results; tick_count increases.

    A "non-idle" result has ``embedded_count > 0`` so neither strike
    counter trips. We let the loop run for ~0.15 s with a 0.02-s interval
    and expect ≥ 3 ticks.
    """
    counter = [0]
    tick_fn = _make_tick_fn([BackfillResult(embedded_count=5, voyage_tokens_consumed=100)], counter)

    handle = start_embedding_backfill_autostart(
        db,
        log=log,
        tick_fn=tick_fn,
        interval_s=0.02,
        env={"VOYAGE_API_KEY": "test"},
        model_name="voyage-4-large",
    )
    try:
        assert handle.is_running() is True
        await asyncio.sleep(0.15)
        assert handle.tick_count >= 3, f"expected >=3 ticks, got {handle.tick_count}"
        assert handle.idle_strikes == 0
        assert handle.voyage_failure_strikes == 0
    finally:
        await _stop_and_wait(handle)


# ===========================================================================
# Idle drain
# ===========================================================================


@skip_if_no_vec0
async def test_idle_drain_stops_after_3_idle_ticks(
    db: sqlite3.Connection, log: CapturedLogger
) -> None:
    """3 consecutive idle ticks (with ``count_pending_docs==0``) stops the loop.

    The DB is empty (no leaves), so ``count_pending_docs`` returns 0 on
    every tick. The mock returns an idle :class:`BackfillResult`
    (``embedded_count=0, skipped=[]``). After 3 ticks ``is_running``
    becomes ``False``.
    """
    tick_fn = _make_tick_fn([BackfillResult(embedded_count=0, skipped=[])])

    handle = start_embedding_backfill_autostart(
        db,
        log=log,
        tick_fn=tick_fn,
        interval_s=0.02,
        env={"VOYAGE_API_KEY": "test"},
        model_name="voyage-4-large",
    )
    try:
        # Wait for at least 4 intervals — guarantees the strike threshold
        # triggers and the stop-task gets scheduled.
        for _ in range(50):
            await asyncio.sleep(0.02)
            if not handle.is_running():
                break
        assert handle.is_running() is False
        assert handle.idle_strikes >= 3
        # Stop-log message present.
        assert any("idle ticks" in m for m in log.info_messages)
    finally:
        await _stop_and_wait(handle)


@skip_if_no_vec0
async def test_idle_strikes_reset_on_non_idle_tick(
    db: sqlite3.Connection, log: CapturedLogger
) -> None:
    """After 2 idle ticks, a non-idle tick resets the counter to 0."""
    counter = [0]
    results: list[BackfillResult | BaseException] = [
        BackfillResult(embedded_count=0, skipped=[]),  # idle 1
        BackfillResult(embedded_count=0, skipped=[]),  # idle 2
        BackfillResult(embedded_count=5),  # non-idle reset
        # Subsequent idle ticks should NOT have triggered the threshold,
        # because the strikes were just reset.
        BackfillResult(embedded_count=0, skipped=[]),  # idle 1 (post-reset)
        BackfillResult(embedded_count=0, skipped=[]),  # idle 2 (post-reset)
    ]
    tick_fn = _make_tick_fn(results, counter)

    handle = start_embedding_backfill_autostart(
        db,
        log=log,
        tick_fn=tick_fn,
        interval_s=0.02,
        env={"VOYAGE_API_KEY": "test"},
        model_name="voyage-4-large",
    )
    try:
        # Wait long enough for ~5 ticks but not enough for another full
        # 3-strike trip after the reset.
        for _ in range(40):
            await asyncio.sleep(0.02)
            if counter[0] >= 5:
                break
        # After the 5th tick the strike count should be 2 (post-reset),
        # not the 5 it would have been without the reset.
        assert handle.is_running() is True, "loop should not have stopped"
        assert handle.idle_strikes <= 2
        # Stay below 3 strikes — never hit the threshold.
    finally:
        await _stop_and_wait(handle)


# ===========================================================================
# Voyage-failure backoff
# ===========================================================================


@skip_if_no_vec0
async def test_voyage_failure_backoff_via_raise(
    db: sqlite3.Connection, log: CapturedLogger
) -> None:
    """3 consecutive non-auth :class:`VoyageError` raises stop the loop.

    The mock tick raises ``VoyageError(kind="server_error", ...)`` every
    invocation. After 3 strikes ``is_running == False`` and the
    documented log line is present.
    """
    err = VoyageError("server_error", "voyage 503 outage", status=503)
    tick_fn = _make_tick_fn([err])

    handle = start_embedding_backfill_autostart(
        db,
        log=log,
        tick_fn=tick_fn,
        interval_s=0.02,
        env={"VOYAGE_API_KEY": "test"},
        model_name="voyage-4-large",
    )
    try:
        for _ in range(50):
            await asyncio.sleep(0.02)
            if not handle.is_running():
                break
        assert handle.is_running() is False
        assert handle.voyage_failure_strikes >= 3
        assert any("3 consecutive" in m or "back off" in m for m in log.error_messages)
    finally:
        await _stop_and_wait(handle)


@skip_if_no_vec0
async def test_voyage_failure_backoff_via_skipped_voyage_reasons(
    db: sqlite3.Connection, log: CapturedLogger
) -> None:
    """3 consecutive ticks with ``embedded_count=0`` + voyage skips stop the loop.

    Ports the Wave-12 / Loop 7 B5 strike-on-allSkipped behavior from the
    TS source (``backfill-autostart.ts:204-226``).
    """
    skipped_result = BackfillResult(
        embedded_count=0,
        skipped=[
            BackfillSkippedDoc(summary_id="leaf_a", reason="voyage_other"),
            BackfillSkippedDoc(summary_id="leaf_b", reason="voyage_other"),
        ],
    )
    tick_fn = _make_tick_fn([skipped_result])

    handle = start_embedding_backfill_autostart(
        db,
        log=log,
        tick_fn=tick_fn,
        interval_s=0.02,
        env={"VOYAGE_API_KEY": "test"},
        model_name="voyage-4-large",
    )
    try:
        for _ in range(50):
            await asyncio.sleep(0.02)
            if not handle.is_running():
                break
        assert handle.is_running() is False
        assert handle.voyage_failure_strikes >= 3
    finally:
        await _stop_and_wait(handle)


@skip_if_no_vec0
async def test_voyage_failure_strikes_reset_on_clean_tick(
    db: sqlite3.Connection, log: CapturedLogger
) -> None:
    """2 consecutive raises + 1 clean tick resets the strike counter."""
    counter = [0]
    err = VoyageError("server_error", "voyage 503", status=503)
    results: list[BackfillResult | BaseException] = [
        err,
        err,
        BackfillResult(embedded_count=3),
        # After reset, expect the strikes to be back to 0 and we're
        # mid-recovery — supply more results to keep the loop alive.
        BackfillResult(embedded_count=3),
        BackfillResult(embedded_count=3),
    ]
    tick_fn = _make_tick_fn(results, counter)

    handle = start_embedding_backfill_autostart(
        db,
        log=log,
        tick_fn=tick_fn,
        interval_s=0.02,
        env={"VOYAGE_API_KEY": "test"},
        model_name="voyage-4-large",
    )
    try:
        for _ in range(40):
            await asyncio.sleep(0.02)
            if counter[0] >= 5:
                break
        # The first 2 ticks each bumped voyage_failure_strikes, then the
        # clean third tick reset it to 0. Even if we ran more ticks after
        # the reset, the strike count should stay below 3.
        assert handle.voyage_failure_strikes == 0
        assert handle.is_running() is True
    finally:
        await _stop_and_wait(handle)


# ===========================================================================
# Auth error — immediate stop, NOT 3-strike
# ===========================================================================


@skip_if_no_vec0
async def test_auth_error_stops_immediately(db: sqlite3.Connection, log: CapturedLogger) -> None:
    """A single :class:`VoyageError(kind="auth")` stops the loop on the first tick.

    The 3-strike threshold is NOT consulted for auth errors. The
    operator gets one terminal error log line.
    """
    counter = [0]
    auth_err = VoyageError("auth", "bad api key", status=401)
    # A few non-auth filler results to verify the auth error short-circuits.
    tick_fn = _make_tick_fn([auth_err, BackfillResult(embedded_count=5)], counter)

    handle = start_embedding_backfill_autostart(
        db,
        log=log,
        tick_fn=tick_fn,
        interval_s=0.02,
        env={"VOYAGE_API_KEY": "test"},
        model_name="voyage-4-large",
    )
    try:
        for _ in range(50):
            await asyncio.sleep(0.02)
            if not handle.is_running():
                break
        assert handle.is_running() is False
        # The strike counter should NOT have ticked to 3 — auth stops
        # immediately.
        assert handle.voyage_failure_strikes < 3
        assert any("Voyage auth error" in m for m in log.error_messages)
    finally:
        await _stop_and_wait(handle)


# ===========================================================================
# Stop idempotency
# ===========================================================================


@skip_if_no_vec0
async def test_stop_is_idempotent(db: sqlite3.Connection, log: CapturedLogger) -> None:
    """Calling ``stop()`` twice is a no-op the second time."""
    tick_fn = _make_tick_fn([BackfillResult(embedded_count=5)])
    handle = start_embedding_backfill_autostart(
        db,
        log=log,
        tick_fn=tick_fn,
        interval_s=0.02,
        env={"VOYAGE_API_KEY": "test"},
        model_name="voyage-4-large",
    )
    assert handle.is_running() is True

    await handle.stop(graceful_timeout_s=1.0)
    assert handle.is_running() is False

    # Second stop is a no-op; doesn't raise.
    await handle.stop(graceful_timeout_s=1.0)
    assert handle.is_running() is False


async def test_stop_on_no_op_handle_is_safe(
    log: CapturedLogger,
) -> None:
    """Stopping the gating no-op handle (no underlying loop) is safe.

    The handle returned when ``VOYAGE_API_KEY`` is missing has no
    underlying :class:`WorkerLoop`. ``stop()`` must not crash.
    """
    conn = _setup_db(register_profile=False, ensure_table=False)
    try:
        tick_fn = _make_tick_fn([BackfillResult(embedded_count=1)])
        handle = start_embedding_backfill_autostart(
            conn,
            log=log,
            tick_fn=tick_fn,
            interval_s=0.02,
            env={},
            model_name="voyage-4-large",
        )
        assert handle.is_running() is False
        # Must not raise.
        await handle.stop()
    finally:
        conn.close()


# ===========================================================================
# Diagnostic fields
# ===========================================================================


@skip_if_no_vec0
async def test_diagnostic_fields_reflect_state(db: sqlite3.Connection, log: CapturedLogger) -> None:
    """``tick_count``, ``idle_strikes``, ``voyage_failure_strikes`` track state."""
    counter = [0]
    # One non-idle, then one idle, then one voyage skip — verifies each
    # counter moves in its own dimension.
    results: list[BackfillResult | BaseException] = [
        BackfillResult(embedded_count=5),
        BackfillResult(embedded_count=0, skipped=[]),
        BackfillResult(
            embedded_count=0,
            skipped=[BackfillSkippedDoc(summary_id="a", reason="voyage_other")],
        ),
    ]
    tick_fn = _make_tick_fn(results, counter)

    handle = start_embedding_backfill_autostart(
        db,
        log=log,
        tick_fn=tick_fn,
        interval_s=0.02,
        env={"VOYAGE_API_KEY": "test"},
        model_name="voyage-4-large",
    )
    try:
        for _ in range(20):
            await asyncio.sleep(0.02)
            if counter[0] >= 3:
                break
        # After 3 ticks: tick_count >= 3; idle_strikes saw 1 then was
        # reset by the voyage-skip tick (the skip resets idle_strikes
        # too, since the tick wasn't idle); voyage_failure_strikes is 1.
        assert handle.tick_count >= 3
        # The idle strike from tick 2 was reset by tick 3 (the
        # voyage-failure path resets idle_strikes too).
        assert handle.idle_strikes == 0
        # The voyage-failure-via-skips bumped this once.
        assert handle.voyage_failure_strikes >= 1
    finally:
        await _stop_and_wait(handle)


# ===========================================================================
# Default interval
# ===========================================================================


def test_default_autostart_interval_is_5_minutes() -> None:
    """Matches TS ``DEFAULT_AUTOSTART_INTERVAL_MS = 5 * 60 * 1000``."""
    assert DEFAULT_AUTOSTART_INTERVAL_S == 5 * 60.0


# ===========================================================================
# Generic-exception path (not just VoyageError)
# ===========================================================================


@skip_if_no_vec0
async def test_generic_exception_counts_as_voyage_strike(
    db: sqlite3.Connection, log: CapturedLogger
) -> None:
    """An arbitrary exception (e.g. a programmer bug in ``tick_fn``) counts as a strike.

    Ports the TS ``catch (e: unknown)`` branch from
    ``backfill-autostart.ts:227-237``: any exception ticks the consecutive
    failure counter; 3 in a row stops the loop.
    """
    tick_fn = _make_tick_fn([RuntimeError("simulated tick bug")])

    handle = start_embedding_backfill_autostart(
        db,
        log=log,
        tick_fn=tick_fn,
        interval_s=0.02,
        env={"VOYAGE_API_KEY": "test"},
        model_name="voyage-4-large",
    )
    try:
        for _ in range(50):
            await asyncio.sleep(0.02)
            if not handle.is_running():
                break
        assert handle.is_running() is False
        assert handle.voyage_failure_strikes >= 3
    finally:
        await _stop_and_wait(handle)
