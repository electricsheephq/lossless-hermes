---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-02] engine: implement per-session asyncio.Lock infrastructure per ADR-018'
labels: 'port, epic-02'
---

## Source (TypeScript)
- File: `src/engine.ts`
- Lines:
  - `sessionOperationQueues` (1761–1764) — `Map<string, {promise, refCount}>` field
  - `withSessionQueue` (2038–2084) — the per-session FIFO mutex chain implementation
  - `resolveSessionQueueKey` (helper)
- Function(s)/class(es): per-session async queue infrastructure

## Target (Python)
- File: `src/lossless_hermes/engine/__init__.py` (state field + acquire/release helpers)
- Estimated LOC: ~80

## Summary

Implement the **per-session async lock** infrastructure per ADR-018: a `defaultdict[str, asyncio.Lock]` keyed by `session_id`, plus an `async_session_lock(session_id)` context manager that acquires the lock for the duration of a critical section.

The TS source uses a FIFO promise chain with a refCount (so the lock cleans up when no one is waiting). Python's `asyncio.Lock` provides FIFO fairness as of 3.11. The Python port adds a **refcount + cleanup pass** so the lock dict doesn't grow without bound (per ADR-018 Open Questions line 96–97).

This issue ships the **infrastructure only**. Other epic issues (02-03 lifecycle, Epic 03 ingest, Epic 04 compact) will *use* the lock for their critical sections.

## Implementation

```python
# src/lossless_hermes/engine/__init__.py

from collections import defaultdict
from contextlib import asynccontextmanager
import asyncio
import logging
from typing import AsyncIterator

logger = logging.getLogger("lcm.engine.locks")


class _SessionLockRecord:
    """Per-session lock + reference count.

    Reference count tracks "how many tasks have an outstanding async_session_lock
    context on this session_id". When refcount drops to 0, the lock is eligible
    for cleanup by the next prune pass.
    """
    __slots__ = ("lock", "refcount")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.refcount = 0


# In LCMEngine._init_state_fields (issue 02-02 adds this):
# self._session_locks: dict[str, _SessionLockRecord] = {}
# self._session_locks_dict_lock = asyncio.Lock()  # protects _session_locks itself


@asynccontextmanager
async def async_session_lock(self, session_id: str) -> AsyncIterator[None]:
    """Per-session FIFO mutex per ADR-018. Acquire the lock for `session_id`
    for the duration of the `async with` block.

    Maps to engine.ts:withSessionQueue (2038-2084). The TS source uses a
    promise chain; Python's asyncio.Lock provides FIFO fairness as of 3.11.

    Usage:
        async with self.async_session_lock(session_id):
            # critical section — only one task at a time per session_id
            ...

    Cleanup: when refcount drops to 0, the lock entry is eligible for removal
    by the next prune_session_locks() call. We do NOT remove on release
    because re-acquiring within the same task would race with the cleanup.
    """
    record = await self._acquire_session_lock_record(session_id)
    try:
        async with record.lock:
            yield
    finally:
        await self._release_session_lock_record(session_id, record)


async def _acquire_session_lock_record(self, session_id: str) -> _SessionLockRecord:
    """Get-or-create the lock record + increment refcount atomically."""
    async with self._session_locks_dict_lock:
        record = self._session_locks.get(session_id)
        if record is None:
            record = _SessionLockRecord()
            self._session_locks[session_id] = record
        record.refcount += 1
        return record


async def _release_session_lock_record(
    self, session_id: str, record: _SessionLockRecord
) -> None:
    """Decrement refcount; do NOT remove (next prune pass handles cleanup)."""
    async with self._session_locks_dict_lock:
        record.refcount -= 1
        # NB: don't `del self._session_locks[session_id]` here — a concurrent
        # acquire could be racing. Cleanup happens lazily in prune_session_locks.


def prune_session_locks(self) -> int:
    """Remove lock records with refcount==0. Returns the number removed.

    Call opportunistically (e.g., from on_session_end, or from a low-priority
    sweep task — out of scope for Epic 02; Epic 04's deferred-debt drain can
    hook this in).

    SAFETY: must NOT be called inside an `async with self.async_session_lock(...)`
    block — the dict-lock acquire would deadlock.
    """
    removed = 0
    # No async lock needed: this is a sync inspection of the dict snapshot.
    # The defaultdict semantics mean a concurrent acquire could race, but the
    # acquire path will re-create the record if it's gone — idempotent.
    keys_to_remove = [k for k, v in self._session_locks.items() if v.refcount == 0]
    for k in keys_to_remove:
        # Re-check refcount in case it bumped between snapshot and removal.
        rec = self._session_locks.get(k)
        if rec is not None and rec.refcount == 0:
            del self._session_locks[k]
            removed += 1
    return removed
```

## State field updates (in issue 02-02)

Add to `_init_state_fields`:
```python
self._session_locks: dict[str, _SessionLockRecord] = {}
self._session_locks_dict_lock = asyncio.Lock()
```

(Replaces the `defaultdict[str, asyncio.Lock]` placeholder issue 02-02 declares.)

## Dependencies
- Depends on: 02-01 (constructor), 02-02 (state field declaration)
- Blocks: Epic 03 (ingest critical sections), Epic 04 (compact critical sections), any future code that needs per-session serialization

## Acceptance criteria
- [ ] `async with engine.async_session_lock("s1"): pass` runs without error
- [ ] Concurrent acquisitions of the same `session_id` serialize (FIFO ordering); concurrent acquisitions of different `session_id`s parallelize
- [ ] After `async with engine.async_session_lock("s1")` exits, `engine._session_locks["s1"].refcount == 0`
- [ ] `engine.prune_session_locks()` removes entries with `refcount == 0` and returns the count
- [ ] Stress test: 100 tasks acquiring `async_session_lock("s1")` in parallel produces exactly 100 sequential entries to a shared counter (no race)
- [ ] Different `session_id`s: 100 tasks across 10 distinct ids — completion time bounded by `~10× single_task_time` (parallelism preserved)
- [ ] `pytest tests/test_session_locks.py` passes

## Tests
- `tests/test_session_locks.py::test_basic_acquire_release` — single `async with`; assert no exception, refcount returns to 0
- `tests/test_session_locks.py::test_serialization_same_session` — N=100 tasks all `async with self.async_session_lock("s1"): counter += 1; counter -= 1`. With no lock this would race; with the lock, counter never exceeds 1.
- `tests/test_session_locks.py::test_parallelism_different_sessions` — 100 tasks across 10 distinct session_ids each sleeping 0.1s. Total time should be ~1s (10 concurrent × 10 sequential), not 10s.
- `tests/test_session_locks.py::test_fifo_ordering` — Python 3.11+ asyncio.Lock is FIFO. Acquire 5 tasks in order; assert they exit in the same order.
- `tests/test_session_locks.py::test_prune_removes_idle_records` — acquire-release on 5 sessions; call `prune_session_locks()`; assert 5 removed and dict is empty
- `tests/test_session_locks.py::test_prune_keeps_active_records` — acquire on s1 (don't release); call `prune_session_locks()`; assert s1 still present
- `tests/test_session_locks.py::test_no_reentrancy_deadlock` — assert that re-acquiring the same lock from the same task deadlocks (asyncio.Lock is not reentrant by design per ADR-018 Open Questions); the test should `await asyncio.wait_for(..., timeout=0.5)` and expect `TimeoutError`

## Estimated effort
6 hours

## Confidence
95% — the pattern is standard. The only minor risk is the cleanup-on-prune ordering: if a task is mid-`yield` when `prune` runs, the record's refcount > 0 so prune skips it. The implementation handles that correctly via the dict_lock around acquire/release.
