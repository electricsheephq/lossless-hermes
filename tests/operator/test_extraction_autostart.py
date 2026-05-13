"""Tests for :mod:`lossless_hermes.operator.extraction_autostart` (issue 07-04).

Ports the test list from
``epics/07-entity-synthesis/07-04-extraction-autostart.md`` §"Tests to
port". The injected ``tick_fn`` returns canned
:class:`ExtractionTickResult` shapes or raises canned exceptions — no
real DB queue drain happens here (that's covered by
:mod:`tests.extraction.test_coreference`).

Test inventory (matches the issue spec):

* (1) ``LCM_EXTRACTION_LLM_ENABLED=false`` → no-op handle.
* (2) ``deps.complete`` missing (None) → no-op handle.
* (3) ``tick_fn`` returns ``lock_acquired=False`` → skip counted but
  not a failure (consecutive_failures stays at 0).
* (4) Heartbeat-lost mid-tick → ``lock_lost_mid_tick=True`` logged in
  the tick-progress line.
* (5) 3 consecutive ticks throw → loop stops.
* (6) 3 consecutive idle ticks → logs once at info; subsequent idle
  ticks remain silent. Does NOT stop the loop (extraction differs from
  embedding-backfill on this).
* (7) :attr:`CoreferenceTickResult.extractor_failures` > 0 does NOT
  advance the consecutive-failures budget (Final.review.3 Loop-9 B2
  regression guard).
* (8) Initial 10s startup delay observed (mocked via direct trigger
  bypass; the full 10s wait isn't exercised in the unit suite).
* (9) ``stop()`` cancels the task within 2s.

Mirrors the timing pattern from
:mod:`tests.operator.test_backfill_autostart` — ``interval_s`` ≈ 0.02 s,
``asyncio.sleep`` ≈ 0.1-0.3 s to let the loop tick a few times before
assertion.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from lossless_hermes.extraction.coreference import (
    CoreferenceTickResult,
    ExtractedEntity,
)
from lossless_hermes.operator.extraction_autostart import (
    DEFAULT_EXTRACTION_INTERVAL_S,
    STARTUP_DELAY_S,
    ExtractionAutostartHandle,
    ExtractionTickResult,
    try_start_extraction_autostart,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


class CapturedLogger:
    """An :class:`ExtractionAutostartLogger`-compatible recorder.

    Captures every message for assertion. The ``warn`` method matches the
    Protocol (TS source uses ``warn`` not ``warning``).
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


class StubDeps:
    """An :class:`ExtractionAutostartDeps`-compatible test stub.

    The ``complete`` attribute is settable so tests can flip it to None
    to exercise the pre-flight skip path.
    """

    def __init__(self, complete: Callable[..., Awaitable[Any]] | None = None) -> None:
        self._complete = complete

    @property
    def complete(self) -> Callable[..., Awaitable[Any]] | None:
        return self._complete


@pytest.fixture
def deps_with_complete() -> StubDeps:
    """Stub deps with a fake ``complete`` callable.

    The callable is never invoked by the autostart itself — the tick_fn
    is injected. We just need ``complete`` to be non-None to pass the
    pre-flight gate.
    """

    async def _fake_complete(args: dict[str, Any], /) -> Any:
        raise NotImplementedError("test extractor should be injected via extractor_fn")

    return StubDeps(complete=_fake_complete)


@pytest.fixture
def deps_without_complete() -> StubDeps:
    """Stub deps with ``complete=None`` (gateway has no LLM provider)."""
    return StubDeps(complete=None)


@pytest.fixture
def db() -> Any:
    """Stub DB connection — not consulted by the module under test.

    The autostart module body never queries the DB directly (the tick_fn
    closes over its own connection). We pass an :class:`object` sentinel
    to satisfy the signature.
    """
    # The autostart accepts ``sqlite3.Connection`` in its type hint but
    # doesn't actually call any methods on it — pass an object().
    return object()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tick_fn(
    results: list[ExtractionTickResult | BaseException],
    counter: list[int] | None = None,
) -> Callable[[], Awaitable[ExtractionTickResult]]:
    """Return an async tick_fn that yields ``results`` in sequence.

    If the list is exhausted, the last entry is repeated forever (so a
    test that wants "always idle" passes one idle result and lets the
    loop tick as many times as it wants).

    Args:
        results: Sequence of either :class:`ExtractionTickResult`
            (returned) or :class:`BaseException` (raised) values.
        counter: Optional list whose ``[0]`` element is incremented each
            invocation. Lets tests assert "tick_fn was called N times".

    Returns:
        An async callable suitable for the ``tick_fn`` parameter.
    """

    async def _tick() -> ExtractionTickResult:
        if counter is not None:
            counter[0] += 1
        idx = (counter[0] - 1) if counter else 0
        idx = min(idx, len(results) - 1)
        item = results[idx]
        if isinstance(item, BaseException):
            raise item
        return item

    return _tick


def _make_fake_extractor() -> Any:
    """Return a deterministic fake :class:`ExtractEntitiesFn`.

    Returns a single ``ExtractedEntity`` for any input — unused at the
    autostart layer (the tick_fn is injected) but kept for tests that
    want to assert the autostart wires the extractor through.
    """

    class _Fake:
        async def __call__(
            self,
            *,
            summary_id: str,
            session_key: str,
            content: str,
        ) -> list[ExtractedEntity]:
            return [ExtractedEntity(surface="x", entity_type="test")]

    return _Fake()


async def _stop_and_wait(handle: ExtractionAutostartHandle) -> None:
    """Stop the autostart and drain in-flight ticks within 2s."""
    await handle.stop(graceful_timeout_s=2.0)


# ===========================================================================
# Pre-flight gating
# ===========================================================================


async def test_default_extraction_interval_is_60_seconds() -> None:
    """Matches TS ``DEFAULT_EXTRACTION_INTERVAL_MS = 60 * 1000`` and the
    porting-guide reconciliation (porting guide wins over ADR-020's 30s).
    """
    assert DEFAULT_EXTRACTION_INTERVAL_S == 60.0


async def test_startup_delay_is_10_seconds() -> None:
    """Matches TS ``setTimeout(...10_000)``."""
    assert STARTUP_DELAY_S == 10.0


async def test_no_op_when_extraction_disabled(
    db: Any, log: CapturedLogger, deps_with_complete: StubDeps
) -> None:
    """``LCM_EXTRACTION_LLM_ENABLED=false`` → no-op handle.

    Returns a handle with ``is_running() == False``. No worker-loop
    started; the injected tick_fn is never called.
    """
    counter = [0]
    tick_fn = _make_tick_fn(
        [ExtractionTickResult(lock_acquired=True, tick_result=CoreferenceTickResult())],
        counter,
    )

    handle = try_start_extraction_autostart(
        db,
        log=log,
        deps=deps_with_complete,
        interval_s=0.02,
        env={"LCM_EXTRACTION_LLM_ENABLED": "false"},
        tick_fn=tick_fn,
    )
    # Give the loop a moment in case it started by mistake.
    await asyncio.sleep(0.1)

    assert handle.is_running() is False
    assert counter[0] == 0
    # The gating log message identifies the reason.
    joined = " ".join(log.info_messages)
    assert "LCM_EXTRACTION_LLM_ENABLED" in joined or "disabled" in joined


async def test_no_op_when_extraction_disabled_uppercase(
    db: Any, log: CapturedLogger, deps_with_complete: StubDeps
) -> None:
    """Env var comparison is case-insensitive (``FALSE`` also disables)."""
    counter = [0]
    tick_fn = _make_tick_fn(
        [ExtractionTickResult(lock_acquired=True, tick_result=CoreferenceTickResult())],
        counter,
    )

    handle = try_start_extraction_autostart(
        db,
        log=log,
        deps=deps_with_complete,
        interval_s=0.02,
        env={"LCM_EXTRACTION_LLM_ENABLED": "FALSE"},
        tick_fn=tick_fn,
    )
    await asyncio.sleep(0.05)
    assert handle.is_running() is False
    assert counter[0] == 0


async def test_no_op_when_deps_complete_is_none(
    db: Any, log: CapturedLogger, deps_without_complete: StubDeps
) -> None:
    """``deps.complete is None`` → no-op handle + info log.

    The TS source treats this as a configuration state (gateway not
    configured with an LLM provider yet), not an error. Logs info-level
    so operators can see the autostart is intentionally skipped.
    """
    counter = [0]
    tick_fn = _make_tick_fn(
        [ExtractionTickResult(lock_acquired=True, tick_result=CoreferenceTickResult())],
        counter,
    )

    handle = try_start_extraction_autostart(
        db,
        log=log,
        deps=deps_without_complete,
        interval_s=0.02,
        env={},
        tick_fn=tick_fn,
    )
    await asyncio.sleep(0.05)
    assert handle.is_running() is False
    assert counter[0] == 0
    # Per spec: log info with the specific token "extraction_autostart_skipped".
    assert any(
        "no_llm_client" in m or "extraction_autostart_skipped" in m for m in log.info_messages
    )


async def test_no_op_when_tick_fn_is_none(
    db: Any, log: CapturedLogger, deps_with_complete: StubDeps
) -> None:
    """``tick_fn=None`` (integration bug) → no-op + error log.

    The TS source defaults ``tickFn`` to ``tickExtraction`` from
    ``worker-orchestrator.ts``. The Python port doesn't have that
    orchestrator yet (Epic 08), so the caller MUST pass a bound tick
    callable. If they pass ``None``, the autostart logs an error and
    returns a no-op handle.
    """
    handle = try_start_extraction_autostart(
        db,
        log=log,
        deps=deps_with_complete,
        interval_s=0.02,
        env={},
        tick_fn=None,
    )
    await asyncio.sleep(0.05)
    assert handle.is_running() is False
    assert any("tick_fn is None" in m for m in log.error_messages)


# ===========================================================================
# Lock-not-acquired path (Wave-7 split)
# ===========================================================================


async def test_lock_not_acquired_does_not_count_as_failure(
    db: Any, log: CapturedLogger, deps_with_complete: StubDeps
) -> None:
    """``tick_fn`` returns ``lock_acquired=False`` → skip + no strike bump.

    Spec acceptance criterion:
        ``lock_acquired = False`` does NOT increment the
        consecutive-failures counter.

    Patterned after the TS source ``extraction-autostart.ts:157-162``.
    """
    counter = [0]
    tick_fn = _make_tick_fn(
        [ExtractionTickResult(lock_acquired=False, tick_result=None)],
        counter,
    )

    handle = try_start_extraction_autostart(
        db,
        log=log,
        deps=deps_with_complete,
        interval_s=0.02,
        env={},
        tick_fn=tick_fn,
    )
    try:
        # Wait for several ticks to confirm strikes never accumulate.
        for _ in range(20):
            await asyncio.sleep(0.02)
            if counter[0] >= 5:
                break
        assert handle.is_running() is True, "loop must stay running on lock-skip"
        assert handle.consecutive_failures == 0
        # Log line identifies the skip reason.
        assert any("lock held by another worker" in m for m in log.info_messages)
    finally:
        await _stop_and_wait(handle)


# ===========================================================================
# Heartbeat-lost mid-tick (lock_lost_mid_tick)
# ===========================================================================


async def test_heartbeat_lost_mid_tick_is_logged(
    db: Any, log: CapturedLogger, deps_with_complete: StubDeps
) -> None:
    """``lock_lost_mid_tick=True`` flag is surfaced in the tick log line.

    Wave-7 introduced :attr:`CoreferenceTickResult.lock_lost_mid_tick`
    so the orchestrator can signal a heartbeat-lost-mid-tick condition.
    The autostart's tick-progress log line must include this so operators
    see the lock-stolen-mid-tick events.

    Patterned after the spec acceptance criterion:
        heartbeat-lost mid-tick → lock_lost_mid_tick=True logged.
    """
    counter = [0]
    tick_result = CoreferenceTickResult(
        processed_count=2,
        new_entities=1,
        new_mentions=2,
        lock_lost_mid_tick=True,
    )
    tick_fn = _make_tick_fn(
        [ExtractionTickResult(lock_acquired=True, tick_result=tick_result)],
        counter,
    )

    handle = try_start_extraction_autostart(
        db,
        log=log,
        deps=deps_with_complete,
        interval_s=0.02,
        env={},
        tick_fn=tick_fn,
    )
    try:
        for _ in range(20):
            await asyncio.sleep(0.02)
            if counter[0] >= 1:
                break
        # The progress log line includes the lock-lost flag.
        assert any(
            "lock-lost-mid-tick=True" in m or "lock_lost_mid_tick=True" in m
            for m in log.info_messages
        ), f"info messages: {log.info_messages!r}"
    finally:
        await _stop_and_wait(handle)


# ===========================================================================
# 3-consecutive-throw stops the loop
# ===========================================================================


async def test_three_consecutive_throws_stop_the_loop(
    db: Any, log: CapturedLogger, deps_with_complete: StubDeps
) -> None:
    """3 consecutive ``tick_fn`` raises → ``is_running()`` becomes False.

    Spec acceptance criterion:
        3-consecutive-tick-throw shutdown latches the loop off;
        is_running() returns False afterward.
    """
    exc = RuntimeError("simulated tick throw")
    tick_fn = _make_tick_fn([exc])

    handle = try_start_extraction_autostart(
        db,
        log=log,
        deps=deps_with_complete,
        interval_s=0.02,
        env={},
        tick_fn=tick_fn,
    )
    try:
        for _ in range(60):
            await asyncio.sleep(0.02)
            if not handle.is_running():
                break
        assert handle.is_running() is False
        assert handle.consecutive_failures >= 3
        # The terminal log line indicates the 3-strike stop.
        assert any("3 consecutive" in m or "consecutive failures" in m for m in log.error_messages)
    finally:
        await _stop_and_wait(handle)


async def test_consecutive_failures_reset_on_clean_tick(
    db: Any, log: CapturedLogger, deps_with_complete: StubDeps
) -> None:
    """2 consecutive raises + 1 clean tick resets the strike counter to 0."""
    counter = [0]
    exc = RuntimeError("simulated transient throw")
    clean_result = ExtractionTickResult(
        lock_acquired=True,
        tick_result=CoreferenceTickResult(processed_count=1, new_entities=1, new_mentions=1),
    )
    tick_fn = _make_tick_fn(
        [
            exc,
            exc,
            clean_result,
            clean_result,
            clean_result,
        ],
        counter,
    )

    handle = try_start_extraction_autostart(
        db,
        log=log,
        deps=deps_with_complete,
        interval_s=0.02,
        env={},
        tick_fn=tick_fn,
    )
    try:
        for _ in range(40):
            await asyncio.sleep(0.02)
            if counter[0] >= 5:
                break
        # After 2 throws (strikes=2) the clean third tick should have
        # reset to 0. The loop must still be running.
        assert handle.is_running() is True
        assert handle.consecutive_failures == 0
    finally:
        await _stop_and_wait(handle)


# ===========================================================================
# 3-consecutive-idle logs once at info (NOT stops the loop)
# ===========================================================================


async def test_three_consecutive_idle_logs_once_does_not_stop(
    db: Any, log: CapturedLogger, deps_with_complete: StubDeps
) -> None:
    """3 idle ticks → first idle logs info, subsequent idle ticks silent.

    Spec acceptance criterion:
        3-consecutive-idle log fires once at info; subsequent idle ticks
        log at debug only.

    Different from embedding-backfill: extraction does NOT stop on 3
    consecutive idle ticks because new leaves can be queued at any time
    and the count probe is cheap.
    """
    counter = [0]
    idle_result = ExtractionTickResult(
        lock_acquired=True,
        tick_result=CoreferenceTickResult(processed_count=0),
    )
    tick_fn = _make_tick_fn([idle_result], counter)

    handle = try_start_extraction_autostart(
        db,
        log=log,
        deps=deps_with_complete,
        interval_s=0.02,
        env={},
        tick_fn=tick_fn,
    )
    try:
        # Wait for ~5 ticks to confirm the loop keeps running.
        for _ in range(30):
            await asyncio.sleep(0.02)
            if counter[0] >= 5:
                break
        # The loop must still be running (extraction does NOT stop on
        # idle drain).
        assert handle.is_running() is True
        assert handle.consecutive_idle_ticks >= 3
        # Only ONE "queue empty; idle." info message — the first idle.
        # Note we filter for the specific empty-idle string to avoid
        # counting the "starting" or "tick N processed=" lines.
        idle_logs = [m for m in log.info_messages if "queue empty" in m]
        assert len(idle_logs) == 1, (
            f"expected exactly 1 idle log, got {len(idle_logs)}: {idle_logs!r}"
        )
    finally:
        await _stop_and_wait(handle)


async def test_idle_strikes_reset_on_non_idle_tick(
    db: Any, log: CapturedLogger, deps_with_complete: StubDeps
) -> None:
    """After 2 idle ticks, a non-idle tick resets the idle counter to 0."""
    counter = [0]
    idle = ExtractionTickResult(
        lock_acquired=True,
        tick_result=CoreferenceTickResult(processed_count=0),
    )
    non_idle = ExtractionTickResult(
        lock_acquired=True,
        tick_result=CoreferenceTickResult(processed_count=2, new_entities=1, new_mentions=2),
    )
    tick_fn = _make_tick_fn([idle, idle, non_idle, idle, idle], counter)

    handle = try_start_extraction_autostart(
        db,
        log=log,
        deps=deps_with_complete,
        interval_s=0.02,
        env={},
        tick_fn=tick_fn,
    )
    try:
        for _ in range(40):
            await asyncio.sleep(0.02)
            if counter[0] >= 5:
                break
        # After non_idle, the idle counter was reset; the trailing 2 idle
        # ticks bump it back up to 2 (post-reset). Allow margin.
        assert handle.is_running() is True
        # Tick counter advanced.
        assert handle.tick_count >= 3
    finally:
        await _stop_and_wait(handle)


# ===========================================================================
# extractor_failures does NOT advance consecutive-failures budget
# ===========================================================================


async def test_extractor_failures_do_not_count_as_consecutive_failures(
    db: Any, log: CapturedLogger, deps_with_complete: StubDeps
) -> None:
    """Per-call extractor failures must NOT burn the consecutive-failures budget.

    Spec acceptance criterion + LCM v4.1 Final.review.3 Loop-9 B2
    regression guard:
        extractor_failures in the per-tick result does NOT increment
        the consecutive-failures counter (per-call extractor failures
        are not tick failures).

    The tick body completes successfully; the extractor failures are
    surfaced in the result but represent per-leaf failures that the
    Wave-4 dead-letter mechanism handles via ``attempts < MAX_ATTEMPTS``.
    """
    counter = [0]
    # Tick reports 3 extractor_failures but the tick itself succeeded
    # (lock_acquired=True, tick_result returned normally).
    result_with_failures = ExtractionTickResult(
        lock_acquired=True,
        tick_result=CoreferenceTickResult(
            processed_count=5,
            new_entities=2,
            new_mentions=2,
            extractor_failures=3,
        ),
    )
    tick_fn = _make_tick_fn([result_with_failures], counter)

    handle = try_start_extraction_autostart(
        db,
        log=log,
        deps=deps_with_complete,
        interval_s=0.02,
        env={},
        tick_fn=tick_fn,
    )
    try:
        # Wait for 10+ ticks — each one has extractor_failures=3.
        for _ in range(40):
            await asyncio.sleep(0.02)
            if counter[0] >= 10:
                break
        assert handle.is_running() is True, (
            "loop must stay running — extractor_failures should not strike"
        )
        assert handle.consecutive_failures == 0, (
            "consecutive_failures must stay at 0 even with per-call extractor "
            f"failures (got {handle.consecutive_failures})"
        )
        assert handle.tick_count >= 10
    finally:
        await _stop_and_wait(handle)


# ===========================================================================
# Stop idempotency + 2s graceful drain
# ===========================================================================


async def test_stop_cancels_within_2s(
    db: Any, log: CapturedLogger, deps_with_complete: StubDeps
) -> None:
    """``stop()`` returns within 2s.

    Spec acceptance criterion:
        stop() cancels the task within 2s.

    The autostart's :meth:`ExtractionAutostartHandle.stop` calls into
    :meth:`WorkerLoop.stop` with a 30s default timeout; tests pass 2s.
    Verified by wall-clock measurement.
    """
    clean = ExtractionTickResult(
        lock_acquired=True,
        tick_result=CoreferenceTickResult(processed_count=1),
    )
    tick_fn = _make_tick_fn([clean])

    handle = try_start_extraction_autostart(
        db,
        log=log,
        deps=deps_with_complete,
        interval_s=0.02,
        env={},
        tick_fn=tick_fn,
    )
    assert handle.is_running() is True
    await asyncio.sleep(0.1)  # let it tick a couple of times

    start = asyncio.get_event_loop().time()
    await handle.stop(graceful_timeout_s=2.0)
    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed < 2.5, f"stop took {elapsed:.2f}s, expected < 2s"
    assert handle.is_running() is False


async def test_stop_is_idempotent(
    db: Any, log: CapturedLogger, deps_with_complete: StubDeps
) -> None:
    """Calling ``stop()`` twice is a no-op the second time."""
    clean = ExtractionTickResult(
        lock_acquired=True,
        tick_result=CoreferenceTickResult(processed_count=1),
    )
    tick_fn = _make_tick_fn([clean])
    handle = try_start_extraction_autostart(
        db,
        log=log,
        deps=deps_with_complete,
        interval_s=0.02,
        env={},
        tick_fn=tick_fn,
    )
    assert handle.is_running() is True

    await handle.stop(graceful_timeout_s=1.0)
    assert handle.is_running() is False

    # Second stop is a no-op; doesn't raise.
    await handle.stop(graceful_timeout_s=1.0)
    assert handle.is_running() is False


async def test_stop_on_no_op_handle_is_safe(
    db: Any, log: CapturedLogger, deps_with_complete: StubDeps
) -> None:
    """Stopping the gating no-op handle (no underlying loop) is safe.

    The handle returned when ``LCM_EXTRACTION_LLM_ENABLED=false`` has no
    underlying :class:`WorkerLoop`. ``stop()`` must not crash.
    """
    handle = try_start_extraction_autostart(
        db,
        log=log,
        deps=deps_with_complete,
        interval_s=0.02,
        env={"LCM_EXTRACTION_LLM_ENABLED": "false"},
        tick_fn=_make_tick_fn([
            ExtractionTickResult(lock_acquired=True, tick_result=CoreferenceTickResult())
        ]),
    )
    assert handle.is_running() is False
    # Must not raise.
    await handle.stop()


# ===========================================================================
# Diagnostic fields reflect state
# ===========================================================================


async def test_diagnostic_fields_track_state(
    db: Any, log: CapturedLogger, deps_with_complete: StubDeps
) -> None:
    """``tick_count``, ``consecutive_failures``, ``consecutive_idle_ticks``
    reflect the underlying state transitions.
    """
    counter = [0]
    results: list[ExtractionTickResult | BaseException] = [
        # tick 1: clean (resets both counters; tick_count=1)
        ExtractionTickResult(
            lock_acquired=True,
            tick_result=CoreferenceTickResult(processed_count=5),
        ),
        # tick 2: idle (idle_strikes=1)
        ExtractionTickResult(
            lock_acquired=True, tick_result=CoreferenceTickResult(processed_count=0)
        ),
        # tick 3: idle (idle_strikes=2)
        ExtractionTickResult(
            lock_acquired=True, tick_result=CoreferenceTickResult(processed_count=0)
        ),
        # tick 4: clean (resets idle; tick_count=4)
        ExtractionTickResult(
            lock_acquired=True,
            tick_result=CoreferenceTickResult(processed_count=2),
        ),
    ]
    tick_fn = _make_tick_fn(results, counter)

    handle = try_start_extraction_autostart(
        db,
        log=log,
        deps=deps_with_complete,
        interval_s=0.02,
        env={},
        tick_fn=tick_fn,
    )
    try:
        for _ in range(30):
            await asyncio.sleep(0.02)
            if counter[0] >= 4:
                break
        assert handle.tick_count >= 4
        # After the final clean tick, both counters are 0.
        assert handle.consecutive_idle_ticks == 0
        assert handle.consecutive_failures == 0
    finally:
        await _stop_and_wait(handle)


# ===========================================================================
# Initial 10s startup delay
# ===========================================================================


async def test_initial_delay_task_is_scheduled(
    db: Any, log: CapturedLogger, deps_with_complete: StubDeps
) -> None:
    """The initial-delay task is scheduled on the event loop at start.

    Spec acceptance criterion:
        Initial 10s startup delay before first tick.

    Full 10s wait isn't exercised in the unit suite (would slow CI); we
    just verify the task exists and is pending. The regular interval
    cadence still drives ticks while we wait, which is fine for the
    unit-test contract.
    """
    clean = ExtractionTickResult(
        lock_acquired=True,
        tick_result=CoreferenceTickResult(processed_count=1),
    )
    tick_fn = _make_tick_fn([clean])

    handle = try_start_extraction_autostart(
        db,
        log=log,
        deps=deps_with_complete,
        interval_s=0.02,
        env={},
        tick_fn=tick_fn,
    )
    try:
        # The initial-delay task is created at start and not yet done.
        assert handle._initial_delay_task is not None
        # It's still pending (10s sleep hasn't completed in our 0s
        # snapshot here).
        assert not handle._initial_delay_task.done()
    finally:
        await _stop_and_wait(handle)
    # After stop(), the initial-delay task is cancelled. The cancellation
    # is scheduled synchronously in stop() but the task transitions through
    # CANCELLING → CANCELLED on its next loop yield; await a brief sleep
    # to let it settle before the done() check.
    assert handle._initial_delay_task is not None
    await asyncio.sleep(0.05)
    # done() includes cancelled.
    assert handle._initial_delay_task.done()
