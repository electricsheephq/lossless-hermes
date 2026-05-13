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

Sync surface (added at issue 03-02): PR #34 (merged 2026-05-13)
converted the Hermes hook callback surface from ``async def`` →
``def``. Hermes's ``PluginManager.invoke_hook``
(``hermes_cli/plugins.py:1218-1232``) invokes callbacks via
``ret = cb(**kwargs)`` with no ``await`` — so the Epic 03 ingest path
needs a **sync** acquire path. This module ships ``acquire_sync`` /
``_acquire_sync_record`` / ``_release_sync_record`` / ``prune_sync`` /
``pending_count_sync`` as the sync mirror of the async surface, each
backed by a :class:`threading.Lock` (NOT :class:`threading.RLock` — the
non-reentrant invariant is preserved across both surfaces). The two
surfaces do NOT share record state: the sync ``_sync_records`` dict
and the async ``_records`` dict are independent (the in-process
serialization guarantees are surface-local; the only cross-surface
coordination is cross-process, via SQLite WAL + ``lcm_worker_lock`` per
ADR-018 §Decision).

See:

* ``docs/adr/018-concurrency-model.md`` §Decision + §"Open questions"
* ``epics/02-engine-skeleton/02-08-per-session-locks.md`` — full spec
* ``epics/03-ingest-assembly/03-02-ingest-diff-on-turn.md`` — sync
  surface motivation
* ``lossless-claw/src/engine.ts`` lines 1761–1764, 2038–2084 — TS source
"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import asynccontextmanager, contextmanager
from typing import AsyncIterator, Dict, Iterator

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


class _SyncSessionLockRecord:
    """Per-session :class:`threading.Lock` + reference count.

    Issue 03-02 sibling of :class:`_SessionLockRecord` for the **sync**
    Hermes hook surface. PR #34 (merged 2026-05-13) converted the hook
    callback surface from ``async def`` → ``def`` because Hermes's
    ``PluginManager.invoke_hook`` (``hermes_cli/plugins.py:1218-1232``)
    calls callbacks via ``ret = cb(**kwargs)`` with no ``await`` —
    forcing the Epic 03 ingest path to a ``with self._session_locks
    .acquire_sync(session_id): ...`` surface.

    The :class:`threading.Lock` here is **in-process** only; cross-
    process serialization of ingest vs compact vs assemble across
    multiple gateway workers continues to ride on SQLite WAL +
    ``lcm_worker_lock`` (per ADR-018 §Decision). The role of this lock
    is exactly what the async one does for asyncio tasks: serialize
    ingest/assemble/compact critical sections within ONE process for
    a single ``session_id`` while parallelizing across distinct
    ``session_id``\\ s.

    Attributes:
        lock: The :class:`threading.Lock` guarding the critical section.
        refcount: Integer count of in-flight acquires (waiters + holder).
    """

    __slots__ = ("lock", "refcount")

    def __init__(self) -> None:
        self.lock = threading.Lock()
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

    The registry exposes both an **async** surface
    (:meth:`acquire` / :meth:`prune` / :meth:`pending_count`) and a
    **sync** surface (:meth:`acquire_sync` / :meth:`prune_sync` /
    :meth:`pending_count_sync`) added at issue 03-02 for the Hermes
    hook path post-PR #34. The two surfaces are independent: distinct
    record dicts, distinct dict-locks, distinct refcount + prune
    semantics. Callers pick the surface that matches their concurrency
    model.
    """

    __slots__ = (
        "_records",
        "_dict_lock",
        "_high_water_mark",
        "_sync_records",
        "_sync_dict_lock",
    )

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

        # 03-02 sync surface (PR #34 converted the Hermes hook callbacks
        # from async to sync; ingest now runs through ``acquire_sync``).
        # The sync surface is independent of the async one — the
        # :class:`threading.Lock` records don't share state with the
        # :class:`asyncio.Lock` records because the two surfaces guard
        # disjoint critical sections (the async surface for future
        # worker-loop callers, the sync surface for the Hermes hook
        # path). Sharing one record would either require an
        # ``asyncio.Lock`` + ``threading.Lock`` pair per record (more
        # memory) or a complex cross-surface coordination protocol with
        # no concrete caller demanding it (per ADR-018 §"Open questions"
        # — only the ingest path lives in sync land at v0.1).
        self._sync_records: Dict[str, _SyncSessionLockRecord] = {}
        # Protects ``_sync_records``. ``threading.Lock`` (not RLock) —
        # the dict-lock is held only for the constant-time book-keeping
        # and never recursively (the per-session lock is acquired
        # OUTSIDE this dict-lock, mirroring the async surface above).
        self._sync_dict_lock = threading.Lock()

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
    # Sync surface (issue 03-02 — Hermes hook path post-PR #34)
    # ------------------------------------------------------------------
    # PR #34 converted Hermes's hook callbacks from async to sync; the
    # ingest path (Epic 03) now lives in sync land. The sync surface
    # mirrors the async one method-for-method: a context-managed
    # ``acquire_sync(session_id)`` that holds a per-session
    # :class:`threading.Lock` for the duration of the ``with`` body,
    # with the same refcount + lazy-prune semantics.

    @contextmanager
    def acquire_sync(self, session_id: str) -> Iterator[None]:
        """Acquire the per-session sync lock for ``session_id``.

        Usage::

            with registry.acquire_sync("sess-1"):
                # critical section — one thread at a time per session_id
                ...

        Sibling to :meth:`acquire` (the async variant). PR #34 forced
        the ingest hook callback surface to be synchronous, so Epic 03's
        ``_on_post_llm_call`` body needs a sync acquire path. The
        in-process serialization guarantee is identical to the async
        surface; cross-process serialization (multiple gateway workers
        against one DB) continues to ride on SQLite WAL +
        ``lcm_worker_lock`` per ADR-018 §Decision.

        Implementation notes:

        * The refcount is incremented under ``_sync_dict_lock`` *before*
          we attempt to acquire ``record.lock`` — this ensures a
          concurrent sync-prune cannot delete the record while another
          thread is queued.
        * The refcount is decremented (also under ``_sync_dict_lock``)
          in ``finally``, so any exception inside the ``with`` body
          still releases the refcount correctly.
        * On exit, if the sync-records dict crosses ``_high_water_mark``,
          an opportunistic prune sweep runs.

        Args:
            session_id: The logical session identifier (typically a
                Hermes session_id).

        Yields:
            ``None`` — the value is unused; callers care about the
            critical-section guarantee.
        """
        record = self._acquire_sync_record(session_id)
        try:
            with record.lock:
                yield
        finally:
            self._release_sync_record(session_id)

    def _acquire_sync_record(self, session_id: str) -> _SyncSessionLockRecord:
        """Get-or-create the sync record + increment refcount atomically."""
        with self._sync_dict_lock:
            record = self._sync_records.get(session_id)
            if record is None:
                record = _SyncSessionLockRecord()
                self._sync_records[session_id] = record
            record.refcount += 1
            return record

    def _release_sync_record(self, session_id: str) -> None:
        """Decrement refcount; opportunistically prune past high-water.

        Sync mirror of :meth:`_release_record`. Same deletion-race
        rationale (do NOT delete on refcount==0; let prune sweep
        handle it).
        """
        with self._sync_dict_lock:
            record = self._sync_records.get(session_id)
            if record is not None:
                record.refcount -= 1
            if len(self._sync_records) > self._high_water_mark:
                self._prune_sync_locked()

    def _prune_sync_locked(self) -> int:
        """Remove every sync record with ``refcount == 0``.

        Must be called with ``_sync_dict_lock`` held. Returns the
        number removed.
        """
        keys_to_remove = [k for k, v in self._sync_records.items() if v.refcount == 0]
        for k in keys_to_remove:
            rec = self._sync_records.get(k)
            if rec is not None and rec.refcount == 0:
                del self._sync_records[k]
        if keys_to_remove:
            logger.debug(
                "session_locks: sync-pruned %d idle records (size now %d)",
                len(keys_to_remove),
                len(self._sync_records),
            )
        return len(keys_to_remove)

    def prune_sync(self) -> int:
        """Explicitly run a sync prune sweep; return the number removed.

        Safe to call any time. Acquires ``_sync_dict_lock`` for the
        duration of the sweep — no caller is blocked on a per-session
        lock by this method.
        """
        with self._sync_dict_lock:
            return self._prune_sync_locked()

    def pending_count_sync(self) -> int:
        """Return the number of sync records in the registry.

        Snapshot only — value can change immediately after the call.
        Diagnostics + tests only.
        """
        return len(self._sync_records)

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
