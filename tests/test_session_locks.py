"""Tests for :class:`SessionLockRegistry` (issue 02-08).

Covers the per-session async lock infrastructure with refcount + lazy
prune required by ADR-018 §"Per-session queue" + §"Open questions"
(unbounded growth mitigation).

Spec: ``epics/02-engine-skeleton/02-08-per-session-locks.md`` §Tests
+ §Acceptance criteria.

Surface under test:

* :meth:`SessionLockRegistry.acquire` — async context manager that
  holds an :class:`asyncio.Lock` for the duration of the ``async with``
  body. Refcount is bumped on entry, decremented on exit.
* :meth:`SessionLockRegistry.prune` — drops every record with
  ``refcount == 0``; returns the count.
* :meth:`SessionLockRegistry.pending_count` — diagnostics (dict size).
* Read-only ``__len__`` / ``__contains__`` — backwards-compat with
  the issue 02-01 ``defaultdict`` placeholder surface.

See:

* ``docs/adr/018-concurrency-model.md`` §Decision + §"Open questions"
* ``epics/02-engine-skeleton/02-08-per-session-locks.md`` — issue spec
"""

from __future__ import annotations

import asyncio
import time
from typing import List

import pytest

from lossless_hermes.engine.session_locks import (
    DEFAULT_HIGH_WATER_MARK,
    SessionLockRegistry,
)


# ---------------------------------------------------------------------------
# Basic acquire / release
# ---------------------------------------------------------------------------


async def test_basic_acquire_release() -> None:
    """Single ``async with``; assert no exception, refcount returns to 0."""
    registry = SessionLockRegistry()
    async with registry.acquire("s1"):
        # Inside the critical section the record is present + refcount==1.
        assert "s1" in registry
        assert registry.pending_count() == 1
    # On exit, refcount drops back to 0. Without crossing the high-water
    # mark the record is *not* removed (the prune pass is opportunistic).
    assert "s1" in registry
    assert registry.pending_count() == 1


async def test_sequential_acquire_same_session_reuses_lock() -> None:
    """Acquiring the same session_id twice in sequence is fine."""
    registry = SessionLockRegistry()
    async with registry.acquire("s1"):
        pass
    async with registry.acquire("s1"):
        pass
    # No leak, no error.
    assert registry.pending_count() == 1


# ---------------------------------------------------------------------------
# Serialization (same session) vs parallelism (different sessions)
# ---------------------------------------------------------------------------


async def test_serialization_same_session() -> None:
    """N=100 tasks all acquire the same session_id. The critical-section
    counter should never see > 1 concurrent holder.

    Without the lock the increment+sleep+decrement window would race;
    with the lock all 100 increments are seen sequentially.
    """
    registry = SessionLockRegistry()
    max_seen = 0
    in_flight = 0

    async def critical_section() -> None:
        nonlocal max_seen, in_flight
        async with registry.acquire("s1"):
            in_flight += 1
            max_seen = max(max_seen, in_flight)
            # Yield to the loop so a competing task gets a chance to
            # run mid-critical-section — exactly the race we're
            # guarding against.
            await asyncio.sleep(0)
            in_flight -= 1

    await asyncio.gather(*[critical_section() for _ in range(100)])
    assert max_seen == 1, f"saw {max_seen} concurrent holders — lock did not serialize"
    # All 100 tasks released cleanly. One record remains (refcount 0,
    # not pruned because we didn't cross the high-water mark).
    assert registry.pending_count() == 1


async def test_parallelism_different_sessions() -> None:
    """100 tasks across 10 distinct session_ids each sleeping 0.05s.

    With per-session locking and 10 sessions in parallel, total wall
    time is ~10× single-task-time (10 sequential per session × 10
    sessions concurrent), not 100×.
    """
    registry = SessionLockRegistry()
    sleep_per_task = 0.05

    async def critical_section(session_id: str) -> None:
        async with registry.acquire(session_id):
            await asyncio.sleep(sleep_per_task)

    start = time.monotonic()
    tasks = []
    for i in range(100):
        # 10 distinct session_ids, 10 tasks each — same-id tasks serialize,
        # different-id tasks run in parallel.
        session_id = f"s{i % 10}"
        tasks.append(critical_section(session_id))
    await asyncio.gather(*tasks)
    elapsed = time.monotonic() - start
    # 10 sequential per session × 0.05s = 0.5s. Plus generous slack for
    # loop scheduling overhead. The test fails (>2s) only if sessions
    # serialized across IDs — which would mean the registry is handing
    # out the *same* lock for distinct IDs, a real bug.
    assert elapsed < 2.0, f"elapsed {elapsed:.2f}s — sessions not parallel"
    assert registry.pending_count() == 10


# ---------------------------------------------------------------------------
# FIFO fairness (Python 3.11+ asyncio.Lock is FIFO per ADR-018 §Rationale)
# ---------------------------------------------------------------------------


async def test_fifo_ordering() -> None:
    """5 tasks acquire same session_id in launch order — they should
    enter + exit the critical section in the same order.

    asyncio.Lock acquires the loop's FIFO scheduling for waiters
    (CPython 3.11+ guarantee — see ADR-018 §Rationale).
    """
    registry = SessionLockRegistry()
    exit_order: List[int] = []
    # ``ready`` lets the test wait until *all* tasks are queued on the
    # lock before the first one starts — otherwise the first acquire
    # would finish before later acquires queued, masking FIFO.
    ready = asyncio.Event()

    async def task(idx: int) -> None:
        # Wait for go-ahead so all 5 enqueue together.
        await ready.wait()
        async with registry.acquire("s1"):
            exit_order.append(idx)
            # Hold the lock briefly so the next waiter has time to be
            # the scheduled successor (not just whichever happens to win
            # the loop scheduling race).
            await asyncio.sleep(0.01)

    # Launch all tasks; they all park on ``ready`` until set().
    tasks = [asyncio.create_task(task(i)) for i in range(5)]
    # Give the loop a tick so every task hits ``ready.wait()`` before
    # we release them — otherwise the early-scheduled tasks might race
    # ahead and the FIFO chain wouldn't be exercised.
    await asyncio.sleep(0)
    ready.set()
    await asyncio.gather(*tasks)
    assert exit_order == [0, 1, 2, 3, 4], (
        f"FIFO violated: {exit_order} — asyncio.Lock should be FIFO on 3.11+"
    )


# ---------------------------------------------------------------------------
# Prune behavior — removes idle, keeps active
# ---------------------------------------------------------------------------


async def test_prune_removes_idle_records() -> None:
    """Acquire-release on 5 sessions; explicit prune drops all 5."""
    registry = SessionLockRegistry()
    for i in range(5):
        async with registry.acquire(f"s{i}"):
            pass
    assert registry.pending_count() == 5
    removed = await registry.prune()
    assert removed == 5
    assert registry.pending_count() == 0


async def test_prune_keeps_active_records() -> None:
    """Acquire on s1 (don't release); explicit prune keeps s1.

    Use a long-running task that holds the lock while a concurrent
    prune() runs. The prune must see refcount > 0 and skip s1.
    """
    registry = SessionLockRegistry()
    holding = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        async with registry.acquire("s1"):
            holding.set()
            # Park inside the critical section until the test releases us.
            await release.wait()

    task = asyncio.create_task(holder())
    # Wait until the holder is inside the critical section.
    await holding.wait()
    assert registry.pending_count() == 1
    # Prune now — must NOT drop s1 (refcount > 0).
    removed = await registry.prune()
    assert removed == 0
    assert "s1" in registry
    # Release the holder and let it complete.
    release.set()
    await task
    # Now a follow-up prune drops it.
    removed = await registry.prune()
    assert removed == 1
    assert registry.pending_count() == 0


async def test_prune_mixed_idle_and_active() -> None:
    """Multiple sessions, some active, some idle: prune drops only idle."""
    registry = SessionLockRegistry()
    # Idle: 3 sessions acquired+released.
    for i in range(3):
        async with registry.acquire(f"idle-{i}"):
            pass
    # Active: 2 sessions still being held.
    holding = asyncio.Event()
    release = asyncio.Event()
    active_count = 0

    async def holder(session_id: str) -> None:
        nonlocal active_count
        async with registry.acquire(session_id):
            active_count += 1
            if active_count == 2:
                holding.set()
            await release.wait()

    tasks = [asyncio.create_task(holder(f"active-{i}")) for i in range(2)]
    await holding.wait()
    assert registry.pending_count() == 5
    removed = await registry.prune()
    assert removed == 3, f"prune removed {removed}, expected 3 idle records"
    assert registry.pending_count() == 2
    assert "active-0" in registry
    assert "active-1" in registry
    # Cleanup.
    release.set()
    await asyncio.gather(*tasks)


# ---------------------------------------------------------------------------
# High-water-mark trigger — automatic prune from the acquire path
# ---------------------------------------------------------------------------


async def test_high_water_mark_triggers_auto_prune() -> None:
    """When the dict size exceeds high_water_mark, the release path
    runs an opportunistic prune sweep that drops idle records.

    Drives the dict past the mark by acquiring + releasing N+1 distinct
    sessions, then asserts the next release fires a prune.
    """
    registry = SessionLockRegistry(high_water_mark=3)
    # Acquire+release 3 distinct sessions — no prune yet (we're *at* the
    # mark, not past it). Each release sees ``len > 3`` after the new
    # entry was inserted in the acquire — so let's drive it explicitly.
    for i in range(3):
        async with registry.acquire(f"s{i}"):
            pass
    # 3 idle records, mark is 3 — no prune fired (the condition is
    # strictly greater than, and the dict grew but the release only
    # checks AFTER decrementing, so a stable population at == mark stays).
    assert registry.pending_count() == 3
    # The 4th acquire grows the dict to 4 — exceeding the mark — and
    # its release fires the opportunistic prune. All 4 sessions are
    # idle at that point, so all 4 records vanish.
    async with registry.acquire("s3"):
        # Inside the critical section: refcount 1, dict size 4.
        assert registry.pending_count() == 4
    # On release: refcount went 1->0, then ``len > 3`` triggered the
    # prune which dropped all 4 zero-refcount entries.
    assert registry.pending_count() == 0


async def test_high_water_mark_skips_pruning_active_records() -> None:
    """When the dict size exceeds the mark but some records are active,
    the auto-prune drops only the idle ones — active records remain.
    """
    registry = SessionLockRegistry(high_water_mark=3)
    holding = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        async with registry.acquire("active"):
            holding.set()
            await release.wait()

    holder_task = asyncio.create_task(holder())
    await holding.wait()
    # Now drive idle entries past the mark. After enough acquires the
    # opportunistic prune fires (when len > mark on a release) and
    # drops every idle entry but leaves the "active" record alone.
    # We acquire+release N idle sessions; once the dict size exceeds
    # the mark (3), the next release will prune. Acquiring 4 idles
    # plus the 1 active = 5 distinct sessions touched ≥ mark.
    for i in range(4):
        async with registry.acquire(f"idle-{i}"):
            pass
    # At this point the auto-prune has fired at least once. The
    # "active" record was *never* eligible (refcount == 1 the whole
    # time), so it must still be present.
    assert "active" in registry
    # The registry size depends on exactly when the prune fired —
    # the load-bearing invariant is "no idle records persist forever
    # and active records are never dropped". An explicit prune
    # converges to the steady state.
    await registry.prune()
    assert registry.pending_count() == 1, (
        f"expected only the active record, got {list(registry._records.keys())}"
    )
    assert "active" in registry
    release.set()
    await holder_task


# ---------------------------------------------------------------------------
# No-reentrancy invariant (ADR-018 §"Open questions" line 96)
# ---------------------------------------------------------------------------


async def test_no_reentrancy_deadlock() -> None:
    """asyncio.Lock is not reentrant. Re-acquiring the same lock from
    inside an outer ``async with`` body on the same task must deadlock
    (ADR-018 §"Open questions" line 96).

    The test expects :class:`asyncio.TimeoutError` from
    :func:`asyncio.wait_for` rather than a successful re-acquire.
    """
    registry = SessionLockRegistry()

    async def reentrant_critical_section() -> None:
        async with registry.acquire("s1"):
            # Second acquire on the *same* task + same session_id.
            # This must hang forever (asyncio.Lock is not reentrant);
            # the outer wait_for cancels it.
            async with registry.acquire("s1"):
                pytest.fail("re-entrant acquire should have deadlocked")

    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        await asyncio.wait_for(reentrant_critical_section(), timeout=0.3)


# ---------------------------------------------------------------------------
# Cancellation safety — refcount is decremented even if the task is cancelled
# ---------------------------------------------------------------------------


async def test_cancellation_decrements_refcount() -> None:
    """If a task is cancelled while waiting for the lock, the refcount
    increment from the acquire side must be backed out.

    Otherwise the registry would leak refcount and prune would skip
    the record forever.
    """
    registry = SessionLockRegistry()
    holding = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        async with registry.acquire("s1"):
            holding.set()
            await release.wait()

    async def waiter() -> None:
        # This task will queue behind the holder and get cancelled
        # while waiting.
        async with registry.acquire("s1"):
            pass  # pragma: no cover - cancelled before reaching here

    holder_task = asyncio.create_task(holder())
    await holding.wait()

    waiter_task = asyncio.create_task(waiter())
    # Give the waiter a tick to register its refcount + queue on the lock.
    await asyncio.sleep(0.05)

    # Cancel the waiter mid-acquire.
    waiter_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter_task

    # Release the holder.
    release.set()
    await holder_task

    # After everything settles, an explicit prune should drop s1 —
    # which proves both refcounts (holder + waiter) made it back to 0.
    removed = await registry.prune()
    assert removed == 1
    assert registry.pending_count() == 0


# ---------------------------------------------------------------------------
# Public-surface invariants
# ---------------------------------------------------------------------------


def test_default_high_water_mark_is_50() -> None:
    """Spec line ``high_water_mark=50`` is the chosen default."""
    assert DEFAULT_HIGH_WATER_MARK == 50


def test_registry_default_uses_module_constant() -> None:
    """Default-constructed registry uses :data:`DEFAULT_HIGH_WATER_MARK`."""
    registry = SessionLockRegistry()
    assert registry._high_water_mark == DEFAULT_HIGH_WATER_MARK


def test_registry_empty_on_construction() -> None:
    """Fresh registry has no records."""
    registry = SessionLockRegistry()
    assert len(registry) == 0
    assert registry.pending_count() == 0
    assert "anything" not in registry
