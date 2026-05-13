"""Per-session async lock registry with reference counting + lazy pruning.

Implements the per-session :class:`asyncio.Lock` infrastructure mandated
by ADR-018 §"Per-session queue", extending the simpler
``defaultdict[str, asyncio.Lock]`` placeholder introduced at issue 02-01.

The TypeScript source (``lossless-claw/src/engine.ts``
``sessionOperationQueues``, lines 1761–1764 + ``withSessionQueue`` lines
2038–2084) uses a FIFO promise chain with a refcount field; the chain
is cleaned up when no callers are queued. Python's :class:`asyncio.Lock`
provides FIFO fairness as of 3.11 (per ADR-018 §Rationale), so we don't
need to re-implement the promise chain — but we *do* need the refcount
+ cleanup pass so the ``_session_locks`` dict cannot grow without bound
when a long-running gateway sees many distinct ``session_id``\\ s
(ADR-018 §"Open questions" line 96–97).

Design contract (per issue 02-08):

* :class:`SessionLockRegistry` owns the lock dict and a dict-protecting
  :class:`asyncio.Lock` that serializes get-or-create + refcount mutations.
* :meth:`acquire` returns an async context manager that holds the
  per-session lock for the duration of the ``async with`` body. The
  refcount is incremented on entry and decremented on exit.
* Exit-side: when the refcount drops to 0, the entry is *eligible* for
  removal — we do **not** remove on release because a concurrent
  ``acquire(same_session_id)`` could race the deletion (cf. spec line
  103–104). Instead, a lazy prune runs whenever the dict size crosses
  a configurable high-water mark.
* :meth:`prune` is also callable explicitly (e.g. from a low-priority
  sweep task, or from ``on_session_end``) — it iterates the dict and
  drops every record whose refcount is currently 0.
* :meth:`pending_count` returns the number of entries currently in the
  registry (for diagnostics + tests).

Re-entrancy: per ADR-018 §"Open questions" line 96, :class:`asyncio.Lock`
is **not** reentrant. A task that already holds the lock for
``session_id`` and tries to acquire it again from the same task body
will deadlock. The spec calls this out (Test ``test_no_reentrancy_deadlock``)
and we preserve the behavior — the registry does not add owner-task
tracking.

See:

* ``docs/adr/018-concurrency-model.md`` §Decision + §"Open questions"
* ``epics/02-engine-skeleton/02-08-per-session-locks.md`` — full spec
* ``lossless-claw/src/engine.ts`` lines 1761–1764, 2038–2084 — TS source
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict

__all__ = ["DEFAULT_HIGH_WATER_MARK", "SessionLockRegistry"]

logger = logging.getLogger("lossless_hermes.engine.session_locks")


# Heuristic prune trigger: the dict can hold up to this many entries
# before an acquire-side prune sweep fires. 50 is well below the 10 000
# / 2 MB upper bound ADR-018 §Option A "Cons" notes. The exact value is
# not load-bearing — tests can override via the ``high_water_mark`` kwarg.
DEFAULT_HIGH_WATER_MARK: int = 50


class _SessionLockRecord:
    """Per-session lock + reference count.

    The reference count tracks "how many tasks currently have an
    outstanding :meth:`SessionLockRegistry.acquire` context manager on
    this ``session_id``". When the count drops to 0, the record is
    eligible for cleanup by the next prune pass.

    Attributes:
        lock: The :class:`asyncio.Lock` guarding the critical section.
        refcount: Integer count of in-flight acquires (waiters + holder).
    """

    __slots__ = ("lock", "refcount")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.refcount = 0


class SessionLockRegistry:
    """Refcounted per-session lock registry with lazy pruning.

    The registry replaces the bare ``defaultdict[str, asyncio.Lock]``
    placeholder from issue 02-01 with a structure that:

    * Returns the same :class:`asyncio.Lock` for repeated calls on the
      same ``session_id`` (so concurrent acquires on the same session
      serialize FIFO).
    * Hands out distinct locks for distinct ``session_id``\\ s (so
      acquires on different sessions parallelize).
    * Tracks an in-flight refcount per ``session_id`` and lazily prunes
      records with refcount==0 once the dict size exceeds a high-water
      mark — preventing unbounded growth in long-running gateways that
      see many distinct sessions.

    Backwards-compat: a few read-only methods on the placeholder API
    surface remain available so callers reading ``len(self._session_locks)``
    or iterating membership still work. See :meth:`__len__` /
    :meth:`__contains__`.

    Maps to ``lossless-claw/src/engine.ts`` ``sessionOperationQueues``
    (lines 1761–1764) + ``withSessionQueue`` (lines 2038–2084).
    """

    __slots__ = ("_records", "_dict_lock", "_high_water_mark")

    def __init__(self, *, high_water_mark: int = DEFAULT_HIGH_WATER_MARK) -> None:
        """Initialize an empty registry.

        Args:
            high_water_mark: Dict size at which the acquire path will
                run an opportunistic prune sweep. Defaults to
                :data:`DEFAULT_HIGH_WATER_MARK`. Tests pass a smaller
                value to exercise the prune path deterministically.
        """
        self._records: Dict[str, _SessionLockRecord] = {}
        # Protects ``_records`` against torn reads during get-or-create
        # + refcount mutations. Held only for the constant-time book-
        # keeping; the per-session :class:`asyncio.Lock` itself is
        # acquired *outside* this dict-lock so dict-lock contention does
        # not block parallel critical sections on different sessions.
        self._dict_lock = asyncio.Lock()
        self._high_water_mark = high_water_mark

    @asynccontextmanager
    async def acquire(self, session_id: str) -> AsyncIterator[None]:
        """Acquire the per-session lock for ``session_id``.

        Usage::

            async with registry.acquire("sess-1"):
                # critical section — only one task at a time per session_id
                ...

        Maps to ``engine.ts:withSessionQueue`` (lines 2038–2084). The TS
        source uses a chained-promise FIFO queue; Python's
        :class:`asyncio.Lock` provides FIFO fairness as of 3.11 (per
        ADR-018 §Rationale).

        Implementation notes:

        * The refcount is incremented under ``_dict_lock`` *before* we
          attempt to acquire ``record.lock`` — this ensures a concurrent
          :meth:`prune` cannot delete the record while we're queued.
        * The refcount is decremented (also under ``_dict_lock``) in
          ``finally``, so cancellation / exception inside the ``async
          with`` body still releases the refcount correctly.
        * On exit, if the dict size crosses ``_high_water_mark``, an
          opportunistic prune sweep runs. This is a *best-effort* pass —
          the dict may still grow above the mark briefly if many
          acquires fire concurrently before the next prune.

        Args:
            session_id: The logical session identifier. Any string;
                typically a Hermes session_id.

        Yields:
            ``None`` — the value is unused; callers care about the
            critical-section guarantee.
        """
        record = await self._acquire_record(session_id)
        try:
            async with record.lock:
                yield
        finally:
            await self._release_record(session_id)

    async def _acquire_record(self, session_id: str) -> _SessionLockRecord:
        """Get-or-create the record + increment refcount atomically."""
        async with self._dict_lock:
            record = self._records.get(session_id)
            if record is None:
                record = _SessionLockRecord()
                self._records[session_id] = record
            record.refcount += 1
            return record

    async def _release_record(self, session_id: str) -> None:
        """Decrement refcount; opportunistically prune past high-water.

        Per the spec (line 103–104), we do NOT delete the record here
        even when ``refcount == 0`` — a concurrent
        :meth:`_acquire_record` for the same ``session_id`` could be
        queued behind us on ``_dict_lock``, and racing the deletion
        would leak the lock entirely. Instead, the prune sweep below
        runs only when the dict size has crossed the high-water mark,
        and only over *all* zero-refcount records (with a final
        re-check immediately before each ``del`` to handle the same
        race).
        """
        async with self._dict_lock:
            record = self._records.get(session_id)
            if record is not None:
                record.refcount -= 1
            # Opportunistic prune: only when the dict has actually grown
            # past the high-water mark. We're already holding the
            # dict-lock so we can safely scan + delete in this block.
            if len(self._records) > self._high_water_mark:
                self._prune_locked()

    def _prune_locked(self) -> int:
        """Remove every record with ``refcount == 0``.

        Must be called with ``_dict_lock`` held. Returns the number
        removed (for diagnostics).
        """
        # Snapshot keys to mutate the dict during iteration without
        # ``RuntimeError: dictionary changed size during iteration``.
        keys_to_remove = [k for k, v in self._records.items() if v.refcount == 0]
        for k in keys_to_remove:
            # Re-check refcount immediately before delete — even with the
            # dict-lock held, the snapshot above is a frozen view; a
            # belt-and-suspenders re-check makes the assertion locally
            # obvious to future readers.
            rec = self._records.get(k)
            if rec is not None and rec.refcount == 0:
                del self._records[k]
        if keys_to_remove:
            logger.debug(
                "session_locks: pruned %d idle records (size now %d)",
                len(keys_to_remove),
                len(self._records),
            )
        return len(keys_to_remove)

    async def prune(self) -> int:
        """Explicitly run a prune sweep; return the number removed.

        Safe to call any time. Acquires ``_dict_lock`` for the duration
        of the sweep — no caller is blocked on a per-session lock by
        this method (only the dict-lock, which is constant-time).

        Returns:
            The number of records dropped.
        """
        async with self._dict_lock:
            return self._prune_locked()

    def pending_count(self) -> int:
        """Return the number of records currently in the registry.

        Snapshot only — value can change immediately after the call. For
        diagnostics + tests; production callers should not branch on
        this.
        """
        return len(self._records)

    # ------------------------------------------------------------------
    # Backwards-compat read-only surface
    # ------------------------------------------------------------------
    # Issue 02-01 exposed ``_session_locks`` as a ``defaultdict[str,
    # asyncio.Lock]``. The placeholder permitted callers to do
    # ``len(self._session_locks)`` and ``"sess" in self._session_locks``.
    # The Epic 03/04 callers that will *use* this surface haven't landed
    # yet (per spec line 26: "Other epic issues … will *use* the lock");
    # we preserve the minimal read-only surface so any tooling that
    # introspects the dict (e.g. tests, future debug commands) still
    # works without a churn-y rename.

    def __len__(self) -> int:
        """Number of records currently in the registry — same as :meth:`pending_count`."""
        return len(self._records)

    def __contains__(self, session_id: object) -> bool:
        """Membership test — ``"sess" in registry``.

        Snapshot only; value can change immediately after the call.
        """
        return session_id in self._records
