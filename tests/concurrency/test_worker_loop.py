"""Tests for :mod:`lossless_hermes.concurrency.worker_loop`.

Ports every case from ``lossless-claw/test/worker-loop.test.ts`` (LCM
commit ``1f07fbd``, 261 LOC TS → ~310 LOC Python) plus a few additional
cases the spec explicitly requires:

* generation-counter behavior across stop/start cycles, and
* ``in_flight_count`` accuracy during a sleeping tick.

All cadences are in seconds (the TS source uses milliseconds; the
Python port stores ``interval_s`` directly). Real-time waits are kept
small (≤200ms in the common path) so the suite stays fast.
"""

from __future__ import annotations

import asyncio

import pytest

from lossless_hermes.concurrency import (
    JobCompleteInfo,
    WorkerJob,
    WorkerLoop,
)


# ---------------------------------------------------------------------------
# Basic lifecycle
# ---------------------------------------------------------------------------


async def test_start_returns_true_first_then_false_when_already_running() -> None:
    """``start()`` is idempotent: returns True first, False on subsequent."""

    async def noop() -> None:
        return None

    loop = WorkerLoop([WorkerJob(kind="embedding-backfill", interval_s=1.0, run=noop)])
    assert loop.start() is True
    assert loop.start() is False  # already running
    assert loop.is_running() is True
    await loop.stop(graceful_timeout_s=1.0)
    assert loop.is_running() is False


async def test_stop_before_start_is_noop() -> None:
    """``stop()`` returns True if the loop was never started."""

    async def noop() -> None:
        return None

    loop = WorkerLoop([WorkerJob(kind="embedding-backfill", interval_s=1.0, run=noop)])
    assert await loop.stop() is True


# ---------------------------------------------------------------------------
# Scheduling cadence
# ---------------------------------------------------------------------------


async def test_runs_at_cadence() -> None:
    """Job fires repeatedly at ~interval_s cadence."""
    count = 0

    async def my_run() -> int:
        nonlocal count
        count += 1
        return count

    loop = WorkerLoop([WorkerJob(kind="embedding-backfill", interval_s=0.03, run=my_run)])
    loop.start()
    await asyncio.sleep(0.12)
    await loop.stop(graceful_timeout_s=1.0)
    # ~3-4 ticks in 120ms (TS test asserts >=2)
    assert count >= 2, f"expected >=2 ticks, got {count}"


async def test_two_jobs_with_different_intervals_dont_interfere() -> None:
    """Distinct kinds fire at their own cadences and don't share state."""
    a_count = 0
    b_count = 0

    async def run_a() -> None:
        nonlocal a_count
        a_count += 1

    async def run_b() -> None:
        nonlocal b_count
        b_count += 1

    loop = WorkerLoop([
        WorkerJob(kind="embedding-backfill", interval_s=0.03, run=run_a),
        WorkerJob(kind="extraction", interval_s=0.06, run=run_b),
    ])
    loop.start()
    await asyncio.sleep(0.15)
    await loop.stop(graceful_timeout_s=1.0)
    assert a_count >= 3, f"a_count expected >=3, got {a_count}"
    assert b_count >= 1, f"b_count expected >=1, got {b_count}"
    # 'a' fires twice as often as 'b'.
    assert a_count > b_count


# ---------------------------------------------------------------------------
# Skip-overlap-on-busy
# ---------------------------------------------------------------------------


async def test_overlapping_ticks_are_skipped_not_queued() -> None:
    """If ``run()`` exceeds ``interval_s``, the next tick is skipped (no queue)."""
    starts = 0
    completions = 0

    async def slow_run() -> None:
        nonlocal starts, completions
        starts += 1
        await asyncio.sleep(0.08)  # exceeds 20ms cadence
        completions += 1

    loop = WorkerLoop([WorkerJob(kind="embedding-backfill", interval_s=0.02, run=slow_run)])
    loop.start()
    await asyncio.sleep(0.15)
    await loop.stop(graceful_timeout_s=0.5)
    # We saw maybe 2-3 starts (each holds the slot for 80ms) — never more
    # than 2-3 simultaneous because dispatcher skips when in-flight. If
    # overlap-protection failed, starts would be >> completions; with it,
    # they're within 1 of each other.
    assert starts <= 4, f"too many starts ({starts}) — overlap protection failed"
    assert completions >= starts - 1, (
        f"completions ({completions}) trails starts ({starts}) by too much"
    )


# ---------------------------------------------------------------------------
# Exception isolation
# ---------------------------------------------------------------------------


async def test_exception_in_one_tick_does_not_stop_the_loop() -> None:
    """Thrown error reported via on_job_complete; subsequent ticks still run."""
    runs = 0
    errors: list[BaseException] = []
    successes: list[object] = []

    async def my_run() -> int:
        nonlocal runs
        runs += 1
        if runs == 1:
            raise RuntimeError("boom!")
        return runs

    def on_complete(info: JobCompleteInfo) -> None:
        if info.error is not None:
            errors.append(info.error)
        else:
            successes.append(info.result)

    loop = WorkerLoop(
        [WorkerJob(kind="embedding-backfill", interval_s=0.02, run=my_run)],
        on_job_complete=on_complete,
    )
    loop.start()
    await asyncio.sleep(0.08)
    await loop.stop(graceful_timeout_s=1.0)
    assert runs >= 2, "second tick should still have fired after first raised"
    assert len(errors) == 1, f"expected 1 captured error, got {len(errors)}"
    assert isinstance(errors[0], RuntimeError)
    assert str(errors[0]) == "boom!"
    assert len(successes) >= 1


async def test_broken_on_job_complete_callback_does_not_kill_loop() -> None:
    """A raising telemetry callback is swallowed; the loop keeps ticking."""
    runs = 0

    async def my_run() -> None:
        nonlocal runs
        runs += 1

    def broken_callback(info: JobCompleteInfo) -> None:
        raise RuntimeError("telemetry blew up")

    loop = WorkerLoop(
        [WorkerJob(kind="embedding-backfill", interval_s=0.02, run=my_run)],
        on_job_complete=broken_callback,
    )
    loop.start()
    await asyncio.sleep(0.08)
    await loop.stop(graceful_timeout_s=1.0)
    assert runs >= 2, "loop should have continued past the broken callback"


# ---------------------------------------------------------------------------
# Graceful stop
# ---------------------------------------------------------------------------


async def test_stop_waits_for_inflight_tick_to_finish() -> None:
    """stop(graceful_timeout_s=N) waits up to N seconds for in-flight to drain."""
    completed = False

    async def slow_run() -> None:
        nonlocal completed
        await asyncio.sleep(0.06)
        completed = True

    loop = WorkerLoop([WorkerJob(kind="embedding-backfill", interval_s=0.01, run=slow_run)])
    loop.start()
    # Let one tick get into its sleep.
    await asyncio.sleep(0.025)
    assert loop.in_flight_count() == 1
    stopped = await loop.stop(graceful_timeout_s=0.5)
    assert stopped is True
    assert completed is True


async def test_stop_returns_false_on_graceful_timeout() -> None:
    """stop() returns False when in-flight tick exceeds graceful_timeout_s."""

    async def very_slow_run() -> None:
        await asyncio.sleep(0.5)  # well above the graceful timeout below

    loop = WorkerLoop([WorkerJob(kind="embedding-backfill", interval_s=0.01, run=very_slow_run)])
    loop.start()
    await asyncio.sleep(0.025)
    stopped = await loop.stop(graceful_timeout_s=0.05)
    assert stopped is False
    # Cleanup: wait for the orphan tick to finish so the test doesn't
    # leak a pending task between tests.
    await asyncio.sleep(0.5)


# ---------------------------------------------------------------------------
# run_once
# ---------------------------------------------------------------------------


async def test_run_once_returns_run_result() -> None:
    """run_once invokes the job and returns its value."""
    count = 0

    async def my_run() -> int:
        nonlocal count
        count += 1
        return count

    loop = WorkerLoop(
        # Far cadence so scheduled ticks don't interfere with the manual run.
        [WorkerJob(kind="embedding-backfill", interval_s=60.0, run=my_run)]
    )
    result = await loop.run_once("embedding-backfill")
    assert result == 1
    assert count == 1


async def test_run_once_raises_on_unknown_kind() -> None:
    """ValueError if the job kind isn't registered."""

    async def my_run() -> None:
        return None

    loop = WorkerLoop([WorkerJob(kind="embedding-backfill", interval_s=1.0, run=my_run)])
    with pytest.raises(ValueError, match="no job kind"):
        await loop.run_once("extraction")


async def test_run_once_raises_when_already_in_flight() -> None:
    """RuntimeError if a tick for the same kind is already in flight."""
    release_block: asyncio.Event = asyncio.Event()

    async def blocked_run() -> None:
        await release_block.wait()

    loop = WorkerLoop([WorkerJob(kind="embedding-backfill", interval_s=60.0, run=blocked_run)])
    first = asyncio.create_task(loop.run_once("embedding-backfill"))
    # Yield enough for the first run_once to register in _in_flight.
    await asyncio.sleep(0.01)
    with pytest.raises(RuntimeError, match="already in flight"):
        await loop.run_once("embedding-backfill")
    # Unblock the first call.
    release_block.set()
    await first


async def test_run_once_propagates_exception() -> None:
    """run_once re-raises the job's exception to its caller."""

    async def failing_run() -> None:
        raise ValueError("something went wrong")

    callbacks: list[JobCompleteInfo] = []

    def on_complete(info: JobCompleteInfo) -> None:
        callbacks.append(info)

    loop = WorkerLoop(
        [WorkerJob(kind="embedding-backfill", interval_s=60.0, run=failing_run)],
        on_job_complete=on_complete,
    )
    with pytest.raises(ValueError, match="something went wrong"):
        await loop.run_once("embedding-backfill")
    # Telemetry was still dispatched.
    assert len(callbacks) == 1
    assert isinstance(callbacks[0].error, ValueError)


# ---------------------------------------------------------------------------
# Construction-time validation
# ---------------------------------------------------------------------------


def test_duplicate_kind_raises() -> None:
    """Two jobs with the same kind: ValueError at construction."""

    async def r() -> None:
        return None

    with pytest.raises(ValueError, match="duplicate job kind"):
        WorkerLoop([
            WorkerJob(kind="embedding-backfill", interval_s=1.0, run=r),
            WorkerJob(kind="embedding-backfill", interval_s=2.0, run=r),
        ])


def test_invalid_interval_raises() -> None:
    """interval_s <= 0 (or NaN): ValueError at construction."""
    import math

    async def r() -> None:
        return None

    with pytest.raises(ValueError, match="invalid interval_s"):
        WorkerLoop([WorkerJob(kind="embedding-backfill", interval_s=0.0, run=r)])
    with pytest.raises(ValueError, match="invalid interval_s"):
        WorkerLoop([WorkerJob(kind="embedding-backfill", interval_s=-0.5, run=r)])
    # NaN comparison is False everywhere, so the validator should reject it.
    with pytest.raises(ValueError, match="invalid interval_s"):
        WorkerLoop([WorkerJob(kind="embedding-backfill", interval_s=math.nan, run=r)])


# ---------------------------------------------------------------------------
# Generation counter — load-bearing across stop/start cycles
# ---------------------------------------------------------------------------


async def test_generation_guard_abandons_stale_ticks_after_stop_start() -> None:
    """Stop → start increments generation; stale ticks self-exit."""
    runs: list[int] = []

    async def my_run() -> None:
        runs.append(1)

    loop = WorkerLoop([WorkerJob(kind="embedding-backfill", interval_s=0.03, run=my_run)])
    loop.start()
    gen_1 = loop._generation  # noqa: SLF001 — testing internal invariant
    await asyncio.sleep(0.08)  # ~2-3 ticks
    runs_before_stop = len(runs)
    await loop.stop(graceful_timeout_s=1.0)
    # Start again — generation must increment.
    loop.start()
    gen_2 = loop._generation  # noqa: SLF001 — testing internal invariant
    assert gen_2 > gen_1
    await asyncio.sleep(0.08)  # ~2-3 more ticks under the new generation
    await loop.stop(graceful_timeout_s=1.0)
    # Total ticks should be roughly 2× the first run, NOT 4× (which would
    # indicate stale-tick double-firing from generation 1).
    runs_after_second_stop = len(runs)
    assert runs_after_second_stop > runs_before_stop
    # The conservative invariant: total ticks should be in a sensible
    # range, not multiplied by stale-task double-fires.
    assert runs_after_second_stop <= runs_before_stop * 2 + 4, (
        f"too many ticks ({runs_after_second_stop}) — generation guard "
        f"may not be abandoning stale tasks"
    )


# ---------------------------------------------------------------------------
# is_running / in_flight_count helpers
# ---------------------------------------------------------------------------


async def test_is_running_and_in_flight_count_accuracy() -> None:
    """is_running tracks lifecycle; in_flight_count counts active ticks."""
    block: asyncio.Event = asyncio.Event()

    async def blocked_run() -> None:
        await block.wait()

    loop = WorkerLoop([WorkerJob(kind="embedding-backfill", interval_s=60.0, run=blocked_run)])
    assert loop.is_running() is False
    assert loop.in_flight_count() == 0

    # Start a run_once that blocks indefinitely.
    pending = asyncio.create_task(loop.run_once("embedding-backfill"))
    await asyncio.sleep(0.01)  # let it register
    assert loop.in_flight_count() == 1

    # Unblock and confirm count drops back to 0.
    block.set()
    await pending
    # Give the finally-block one tick to clear _in_flight.
    await asyncio.sleep(0.01)
    assert loop.in_flight_count() == 0
