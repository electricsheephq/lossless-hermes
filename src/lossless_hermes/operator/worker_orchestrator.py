"""Worker orchestrator — LCM v4.1 cycle-2 + issue 08-10.

Ports ``lossless-claw/src/operator/worker-orchestrator.ts`` (LCM commit
``1f07fbd`` on branch ``pr-613``, 250 LOC TS) AND merges
``lossless-claw/src/operator/worker-llm.ts`` (167 LOC TS, no independent
state — per ``docs/porting-guides/doctor-ops.md`` table line 314).

### What this module is

A thin coordinator over the cross-process worker-lock surface
(:mod:`lossless_hermes.concurrency.worker_lock`). Two callers:

* ``/lcm worker status`` (read-only) — calls
  :func:`get_worker_status_snapshot` and renders the per-kind lock state
  + pending counts.
* ``/lcm worker tick <kind>`` (owner-gated forced tick) — calls
  :func:`tick_embedding_backfill` or :func:`tick_extraction` to drain
  one batch on demand.

Plus two admin escape hatches:

* :func:`force_release_lock` — clears a stuck lock when a worker
  crashed without releasing (TTL+heartbeat is the SAFE recovery path;
  this is the override).
* :func:`heartbeat_all_held_locks` — periodic TTL refresh from the
  worker-loop dispatcher (ADR-020).

And one adapter (merged from ``worker-llm.ts``):

* :func:`create_worker_llm_call` — wraps a generic LLM-complete callable
  into the :class:`~lossless_hermes.synthesis.dispatch.LlmCall` Protocol
  signature consumed by :func:`~lossless_hermes.synthesis.dispatch.dispatch_synthesis`.

### What this module is NOT

* The actual job functions (those live in
  :mod:`lossless_hermes.embeddings.backfill` and
  :mod:`lossless_hermes.extraction.coreference`).
* The cross-process worker_lock primitives (those live in
  :mod:`lossless_hermes.concurrency.worker_lock` and are imported here).
* The :class:`~lossless_hermes.concurrency.worker_loop.WorkerLoop`
  scheduling (separate concern; the orchestrator's :func:`tick_*`
  surfaces can be the ``run=`` callable passed to
  :class:`~lossless_hermes.concurrency.worker_loop.WorkerJob`).

Design choice: **thin coordinator, thick injectables.** Makes
``/lcm worker tick <kind>`` easy to wire (one switch over kind → call
the right orchestrator method) without forcing the orchestrator to
know about every job's specific dependencies (Voyage client, embedding
profile, LLM completion adapter, …). Callers bind those in once at the
edge.

### Cross-process lock semantics (ADR-018, brought in by Epic 05)

* Each tick path: ``lock = acquire_lock(db, kind, worker_id, ttl_s=90)``
  → if ``False``, return early ("lock held by peer; skipping"); otherwise
  do work, then ``release_lock(...)`` in ``finally``.
* Heartbeat every 30 s (per ADR-018 TTL=90s, 3× headroom).
* Stale detection: lock with ``last_heartbeat_at`` older than ttl is
  considered abandoned; :func:`force_release_lock` cleans it up; status
  snapshot flags it ``stale: True``.

### Load-bearing Wave-N fixes preserved (per ADR-029)

This module port preserves four Wave-N fixes from the TS source:

* **Wave-4 Auditor #12 P0-1 + #13 P1** (:func:`tick_extraction`): the
  orchestrator wraps the tick with an ``on_item_heartbeat`` closure so
  :func:`~lossless_hermes.extraction.coreference.run_coreference_tick`
  can extend the worker-lock TTL between items. Without this, a 50-item
  tick × 30 s/item = 25 min would blow past the 90 s
  :data:`~lossless_hermes.concurrency.model.WORKER_LOCK_TTL_S`, allowing
  a second gateway's autostart to GC + re-acquire and double-process the
  queue — directly causing the duplicate-mention scenario the
  ``INSERT OR IGNORE`` path was fixed for in Wave-1.
* **Wave-7 Auditor #4/13 P1** (:func:`tick_extraction`): surface
  ``lock_lost_mid_tick``. The W4 fix wired the heartbeat callback +
  result field, but ``tick_extraction`` returned ``lock_acquired=True``
  regardless — collapsing "lost lock partway" into "ran cleanly". Now
  if heartbeat returned ``False`` mid-tick, ``lock_acquired`` flips to
  ``False`` so callers (autostart) can pace down + treat as soft failure.
* **Wave-4 Auditor #13 P1** (:func:`force_release_lock`): when
  ``expected_worker_id`` is provided, scope the DELETE to that worker so
  an operator slip doesn't evict a healthy holder mid-tick (which would
  cascade into double-processing the queue exactly as the Wave-1
  ``INSERT OR IGNORE`` protections aim to prevent). When omitted,
  behavior matches the legacy escape-hatch semantic (delete by kind
  only) — caller takes responsibility.
* **Wave-4 Auditor #13 P1** (:func:`heartbeat_all_held_locks`):
  previously returned only a count, throwing away the per-kind boolean.
  A worker that lost its lock between ticks (stolen during a long LLM
  call) saw ``refreshed=0`` indistinguishable from "we never held it."
  Now returns per-kind status so callers can detect lock-loss and abort
  the in-flight tick. Also wraps each call in ``try/except`` so one
  failed heartbeat doesn't abort the loop.

### Source map

* TS canonical:
  - ``lossless-claw/src/operator/worker-orchestrator.ts`` lines 1-250
    (commit ``1f07fbd``).
  - ``lossless-claw/src/operator/worker-llm.ts`` lines 1-167 (commit
    ``1f07fbd``) — merged here per the porting guide.
* Porting guide: ``docs/porting-guides/doctor-ops.md`` §"Operator
  modules" line 314 (worker-llm) + 315 (worker-orchestrator).
* Issue spec: ``epics/08-cli-ops/08-10-worker-orchestrator.md``.
* ADR-018: ``docs/adr/018-concurrency-model.md`` — concurrency model.
* ADR-020: ``docs/adr/020-worker-loop-dispatcher.md`` — heartbeat
  cadence + dispatcher semantics.
* ADR-029: ``docs/adr/029-wave-fix-provenance.md`` — Wave-N comment
  format.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Final, Literal, Protocol, cast

from lossless_hermes.concurrency.model import (
    WORKER_JOB_KINDS,
    LockInfo,
    WorkerJobKind,
)
from lossless_hermes.concurrency.worker_lock import (
    acquire_lock,
    generate_worker_id,
    heartbeat_lock,
    lock_info,
    release_lock,
)
from lossless_hermes.embeddings.backfill import (
    BackfillResult,
    count_pending_docs,
    tick_embedding_backfill as _tick_backfill_inner,
)
from lossless_hermes.extraction.coreference import (
    CoreferenceTickOptions,
    CoreferenceTickResult,
    ExtractEntitiesFn,
    PerItemDetail,
    count_pending_extractions,
    run_coreference_tick,
)
from lossless_hermes.synthesis.dispatch import (
    LlmCall,
    LlmCallArgs,
    LlmCallResult,
)

__all__ = [
    "DEFAULT_WORKER_LLM_TIMEOUT_S",
    "ExtractionTickArgs",
    "ExtractionTickResultWithLock",
    "ForceReleaseResult",
    "HeartbeatLossKind",
    "HeartbeatResult",
    "LlmCompleteCallable",
    "LlmCompleteResultLike",
    "PendingCounts",
    "WorkerLlmConfig",
    "WorkerStatusSnapshot",
    "create_worker_llm_call",
    "force_release_lock",
    "get_worker_status_snapshot",
    "heartbeat_all_held_locks",
    "tick_embedding_backfill",
    "tick_extraction",
]


_log = logging.getLogger("lossless_hermes.operator.worker_orchestrator")


# ---------------------------------------------------------------------------
# get_worker_status_snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PendingCounts:
    """Pending queue counts for the workers that have queryable backlogs.

    Mirrors the TS inline ``pending`` object at
    ``worker-orchestrator.ts:61-69``. Counts that aren't directly
    queryable (e.g. procedure-mining's "corpus size + cadence" trigger
    has no queue) are reported as ``-1`` — a sentinel rather than
    :data:`None` so the JSON serializer in ``/lcm worker status`` can
    emit a single shape regardless of subsystem availability.

    Note on ``embedding_backfill``: the count depends on the active
    embedding model, which this module does NOT resolve (deferred to the
    caller per the TS source's "modelName argument; orchestrator doesn't
    know the active one" contract). When :func:`get_worker_status_snapshot`
    is called with ``model_name=None`` (the default), this counter is
    ``-1``.

    The same shape is reused by the AC-mandated alternative-named
    properties ``pending_embedding_backfill`` /
    ``pending_entity_extraction`` /
    ``pending_condensation_maintenance`` on
    :class:`WorkerStatusSnapshot` — the spec keeps the per-counter names
    aligned with the per-kind ``WORKER_JOB_KINDS`` literals.
    """

    embedding_backfill: int
    """Count of leaves still pending embedding for the active model, or
    ``-1`` if no ``model_name`` was supplied (per the TS contract)."""

    extraction_queue: int
    """Count of ``lcm_extraction_queue`` rows the next
    :func:`~lossless_hermes.extraction.coreference.run_coreference_tick`
    would process (matches the selector parity established in Wave-10
    P2 — see
    :func:`~lossless_hermes.extraction.coreference.count_pending_extractions`)."""

    procedure_mining: int = -1
    """Always ``-1`` at v4.1 — procedure mining was removed in the
    first-principles pass (2026-05-06) and has no on-disk queue.
    Preserved as a field so future Epics that revive procedure mining
    can populate it without changing the snapshot's typed surface."""


@dataclass(frozen=True, slots=True)
class WorkerStatusSnapshot:
    """Snapshot of all worker lock state + pending backlog counts.

    Ports the TS ``WorkerStatusSnapshot`` interface at
    ``worker-orchestrator.ts:58-69``. The snapshot is a pure-read-only
    projection of :sql:`lcm_worker_lock` + the per-kind pending probes;
    it never mutates state and never raises (probes that hit a missing
    table return ``-1``).

    Caller usage::

        snap = get_worker_status_snapshot(db, model_name="voyage-3")
        for kind, info in snap.locks.items():
            if info is None:
                print(f"{kind}: idle")
            else:
                marker = " (stale)" if info.is_stale else ""
                print(f"{kind}: held by {info.lock.worker_id}{marker}")

    The ``locks`` mapping is keyed by every
    :data:`~lossless_hermes.concurrency.model.WORKER_JOB_KINDS` literal
    — kinds with no lock row have a value of ``None``. The
    spec-mandated alternative read-paths (``workers``,
    ``pending_embedding_backfill``, etc.) are exposed via properties
    so callers can use either API shape.
    """

    locks: dict[str, "WorkerLockSnapshot | None"]
    """Per-kind lock snapshot, or :data:`None` for idle kinds.

    Keys are the values in
    :data:`~lossless_hermes.concurrency.model.WORKER_JOB_KINDS` — every
    kind is present in the mapping (no missing keys), so callers can
    iterate without ``.get()`` defensiveness."""

    pending: PendingCounts
    """Per-subsystem pending counts (see :class:`PendingCounts`)."""

    @property
    def workers(self) -> list["WorkerLockSnapshot | None"]:
        """List form of :attr:`locks`, ordered by
        :data:`~lossless_hermes.concurrency.model.WORKER_JOB_KINDS`.

        Convenience alias matching the AC-mandated
        ``WorkerStatusSnapshot.workers`` field shape — same data as
        :attr:`locks`, just iterable without dict access.
        """
        return [self.locks[kind] for kind in WORKER_JOB_KINDS]

    @property
    def pending_embedding_backfill(self) -> int:
        """Alias for ``self.pending.embedding_backfill`` (per AC)."""
        return self.pending.embedding_backfill

    @property
    def pending_entity_extraction(self) -> int:
        """Alias for ``self.pending.extraction_queue`` (per AC)."""
        return self.pending.extraction_queue

    @property
    def pending_condensation_maintenance(self) -> int:
        """Condensation maintenance backlog — not directly queryable at
        v4.1 (the condensation worker reads ``summaries.kind='leaf'`` +
        depth thresholds rather than a queue table). Returned as ``-1``
        for forward-compat parity with the other pending counts."""
        return -1


@dataclass(frozen=True, slots=True)
class WorkerLockSnapshot:
    """A :class:`~lossless_hermes.concurrency.model.LockInfo` plus the
    derived ``is_stale`` flag.

    The bare :class:`LockInfo` carries the raw ``expires_at`` /
    ``last_heartbeat_at`` timestamps; the snapshot wraps it with the
    ``is_stale`` computation so callers don't have to re-derive it. A
    lock is **stale** when :attr:`LockInfo.last_heartbeat_at` is older
    than the TTL window — that's the signature of a crashed worker
    that didn't release its lock. The autostart's strike counter and
    the ``/lcm worker status`` renderer both consume this flag.
    """

    lock: LockInfo
    """The raw lock row data — kind, worker_id, timestamps, metadata."""

    is_stale: bool
    """``True`` if :attr:`LockInfo.last_heartbeat_at` is older than
    :data:`~lossless_hermes.concurrency.model.WORKER_LOCK_TTL_S` (90 s
    by default). Stale locks are candidates for
    :func:`force_release_lock`."""

    @property
    def kind(self) -> str:
        """Convenience alias for ``self.lock.job_kind``."""
        return self.lock.job_kind

    @property
    def held(self) -> bool:
        """A :class:`WorkerLockSnapshot` only exists for held locks, so
        this is always ``True``. Kept as a property so the spec-stated
        ``WorkerStatus.held`` access pattern works on the snapshot AND
        on :data:`None` (caller can ``getattr(s, 'held', False)``)."""
        return True

    @property
    def worker_id(self) -> str:
        """Convenience alias for ``self.lock.worker_id`` (spec parity
        with ``WorkerStatus.held_by_worker_id`` access)."""
        return self.lock.worker_id


def _is_lock_stale(info: LockInfo, *, now_iso: str, ttl_s: float) -> bool:
    """Return ``True`` if ``info.last_heartbeat_at`` is older than ``ttl_s``.

    Comparison uses ISO-8601 string lexicographic ordering because the
    timestamps come from SQL ``datetime('now')`` (fixed-width
    ``YYYY-MM-DD HH:MM:SS`` form). To compute the "older than ttl"
    threshold we ask SQLite — we already have a connection in the
    snapshot path — so the comparison is consistent with the
    server-side clock that issued the original timestamps (ADR-018
    "Cross-process clock skew").

    Args:
        info: The lock row to inspect.
        now_iso: SQL ``datetime('now')`` from the same connection that
            holds the lock row. Caller resolves this once per snapshot
            so all kinds compare against the same reference instant.
        ttl_s: TTL window in seconds — typically
            :data:`~lossless_hermes.concurrency.model.WORKER_LOCK_TTL_S`.

    Returns:
        ``True`` if ``info.last_heartbeat_at`` is more than ``ttl_s``
        seconds before ``now_iso`` (lock holder has missed the
        heartbeat → crashed worker candidate).
    """
    # Defensive: if either timestamp is None / empty (shouldn't happen
    # for a valid row, but lcm_worker_lock allows NULL last_heartbeat_at
    # historically), treat as non-stale so we don't false-positive on
    # transient inserts mid-tick.
    if not info.last_heartbeat_at or not now_iso:
        return False
    # Compute the threshold instant via SQL so the comparison uses the
    # same clock the row's timestamps came from. Caller is responsible
    # for passing a ``now_iso`` from that same connection's
    # ``datetime('now')``.
    # The cheap arithmetic in Python: parse "YYYY-MM-DD HH:MM:SS" → epoch
    # → subtract ttl → re-format. We use lexicographic comparison
    # against the formatted result, matching the lock-table's
    # comparison convention. (This is faster than parsing each row's
    # timestamp into a datetime, and consistent with worker_lock.py's
    # use of TEXT comparison.)
    try:
        threshold_struct = time.strptime(now_iso, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return False
    threshold_epoch = time.mktime(threshold_struct) - ttl_s
    threshold_iso = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(threshold_epoch))
    return info.last_heartbeat_at < threshold_iso


def get_worker_status_snapshot(
    db: sqlite3.Connection,
    *,
    model_name: str | None = None,
    ttl_s: float | None = None,
) -> WorkerStatusSnapshot:
    """Read-only snapshot of all worker state.

    Ports ``worker-orchestrator.ts:78-98`` ``getWorkerStatusSnapshot`` to
    Python. Iterates every value in
    :data:`~lossless_hermes.concurrency.model.WORKER_JOB_KINDS` to build
    the ``locks`` mapping, then probes the per-subsystem pending
    counters. The function is pure-read-only — safe to call at any
    latency budget, no LLM calls, no DB writes.

    Args:
        db: Open :class:`sqlite3.Connection` with the v4.1 migration
            ladder applied. The connection's ``isolation_level`` doesn't
            matter (the probes are all SELECTs). Closing the connection
            mid-call would surface as a :class:`sqlite3.Error` from one
            of the underlying probes; the snapshot path does NOT catch
            those (programmer error to call with a closed connection).
        model_name: Active embedding model name. When ``None`` (the
            default), the ``pending.embedding_backfill`` counter is
            ``-1`` — matches the TS contract at line 86. Callers
            (``/lcm worker status``, ``/lcm health``) typically resolve
            this from the active
            :sql:`lcm_embedding_profile` row before calling.
        ttl_s: TTL window for the staleness check. Defaults to
            :data:`~lossless_hermes.concurrency.model.WORKER_LOCK_TTL_S`
            (90 s). Tests that want to deterministically force a stale
            row pass a small value (e.g. ``0.01``) after seeding a lock.

    Returns:
        A :class:`WorkerStatusSnapshot` keyed by every
        :data:`WORKER_JOB_KINDS` literal — every kind appears in
        :attr:`WorkerStatusSnapshot.locks` (with value :data:`None` for
        idle kinds), so callers don't need ``.get()`` defensiveness.

    Raises:
        sqlite3.Error: SQL failure on the underlying probes
            (e.g. ``lcm_worker_lock`` missing because migrations
            weren't run). Programmer-error path; never silently swallowed.
    """
    from lossless_hermes.concurrency.model import WORKER_LOCK_TTL_S

    effective_ttl_s = WORKER_LOCK_TTL_S if ttl_s is None else ttl_s

    # Resolve "now" once for consistent staleness checks across all kinds.
    now_row = db.execute("SELECT datetime('now')").fetchone()
    now_iso: str = now_row[0] if now_row and now_row[0] is not None else ""

    locks: dict[str, WorkerLockSnapshot | None] = {}
    for kind in WORKER_JOB_KINDS:
        info = lock_info(db, kind)
        if info is None:
            locks[kind] = None
        else:
            stale = _is_lock_stale(info, now_iso=now_iso, ttl_s=effective_ttl_s)
            locks[kind] = WorkerLockSnapshot(lock=info, is_stale=stale)

    # Pending counts. Each probe is tolerant of missing tables; if the
    # underlying tables aren't migrated, the probes raise — caller's
    # programmer error. The TS source has the same shape (no try/catch).
    embedding_pending: int
    if model_name is not None and model_name.strip():
        embedding_pending = count_pending_docs(db, model_name=model_name)
    else:
        embedding_pending = -1

    extraction_pending = count_pending_extractions(db)

    return WorkerStatusSnapshot(
        locks=locks,
        pending=PendingCounts(
            embedding_backfill=embedding_pending,
            extraction_queue=extraction_pending,
            procedure_mining=-1,  # removed in first-principles pass (2026-05-06)
        ),
    )


# ---------------------------------------------------------------------------
# tick_embedding_backfill
# ---------------------------------------------------------------------------


async def tick_embedding_backfill(
    db: sqlite3.Connection,
    *,
    worker_id: str | None = None,
    **tick_kwargs: Any,
) -> BackfillResult:
    """Manual backfill tick. Wraps
    :func:`~lossless_hermes.embeddings.backfill.tick_embedding_backfill`
    with a stable ``worker_id`` if the caller doesn't provide one.

    Ports ``worker-orchestrator.ts:110-116`` ``tickEmbeddingBackfill``.
    Used by ``/lcm worker tick embedding-backfill``.

    The embedding-backfill tick handles its own cross-process worker
    lock (see :mod:`lossless_hermes.embeddings.backfill` — the tick
    body acquires ``embedding-backfill`` for ``WORKER_LOCK_TTL_S`` and
    releases it in ``finally``). The orchestrator's role is just to
    supply a sensible ``worker_id`` and pass through the caller's
    keyword arguments verbatim.

    Per-tick contract (per
    ``epics/08-cli-ops/08-10-worker-orchestrator.md`` AC + plugin-glue.md
    line 430): ≤200 paid Voyage embeddings per call. Default
    ``per_tick_limit=200`` lives in
    :mod:`lossless_hermes.embeddings.backfill`; this orchestrator does
    not override it.

    Args:
        db: Open :class:`sqlite3.Connection`. Passed through.
        worker_id: Optional override for the lock's worker_id. Default:
            generated via :func:`generate_worker_id` with the role
            ``"orchestrator-backfill"`` so operators can identify
            orchestrator-driven ticks in the lock table.
        **tick_kwargs: All other keyword args forwarded verbatim to
            :func:`~lossless_hermes.embeddings.backfill.tick_embedding_backfill`.
            See that function's docstring for the full signature —
            typically ``voyage``, ``voyage_model``, ``model_name``,
            ``input_type``, etc.

    Returns:
        The :class:`~lossless_hermes.embeddings.backfill.BackfillResult`
        from the inner tick.

    Raises:
        Whatever the inner tick raises (typically auth
        :class:`~lossless_hermes.voyage.client.VoyageError` only —
        per-batch failures are surfaced via
        :attr:`BackfillResult.skipped`).
    """
    resolved_worker_id = (
        worker_id if worker_id is not None else generate_worker_id("orchestrator-backfill")
    )
    return await _tick_backfill_inner(db, worker_id=resolved_worker_id, **tick_kwargs)


# ---------------------------------------------------------------------------
# tick_extraction
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExtractionTickArgs:
    """Optional knobs for :func:`tick_extraction`.

    Mirrors the TS ``RunCoreferenceTickArgs`` shape at
    ``worker-orchestrator.ts:118-122`` minus the always-injected
    ``extractor`` (which we pass as a positional arg to
    :func:`tick_extraction` for type clarity).

    The dataclass is frozen so the orchestrator can't mutate the
    caller's args mid-tick.
    """

    pass_id: str | None = None
    """Optional pass ID for audit/telemetry. Default: generated as
    ``"tick-<unix_ms>"`` per the TS source line 159."""

    per_tick_limit: int | None = None
    """Optional override for
    :attr:`~lossless_hermes.extraction.coreference.CoreferenceTickOptions.per_tick_limit`.
    When :data:`None`, uses the
    :data:`~lossless_hermes.extraction.coreference.DEFAULT_PER_TICK_LIMIT`
    (50)."""


@dataclass(frozen=True, slots=True)
class ExtractionTickResultWithLock:
    """A :class:`CoreferenceTickResult` augmented with ``lock_acquired``.

    Mirrors the TS ``CoreferenceTickResult & { lockAcquired: boolean }``
    intersection type returned from
    ``worker-orchestrator.ts:tickExtraction``. The autostart
    (:func:`~lossless_hermes.operator.extraction_autostart.try_start_extraction_autostart`)
    consumes this surface — its
    :class:`~lossless_hermes.operator.extraction_autostart.ExtractionTickResult`
    is the same shape under a different name.

    Attributes:
        lock_acquired: ``True`` if the cross-process worker lock for
            ``"extraction"`` was held for the entire tick. ``False``
            if the initial :func:`acquire_lock` failed (sibling
            gateway holds it) OR the heartbeat returned ``False``
            mid-tick (Wave-7 fix — see module docstring).
        processed_count: From :class:`CoreferenceTickResult.processed_count`.
        new_entities: From :class:`CoreferenceTickResult.new_entities`.
        new_mentions: From :class:`CoreferenceTickResult.new_mentions`.
        extractor_failures: From :class:`CoreferenceTickResult.extractor_failures`.
        lock_lost_mid_tick: From :class:`CoreferenceTickResult.lock_lost_mid_tick`.
        per_item: From :class:`CoreferenceTickResult.per_item`.
    """

    lock_acquired: bool
    processed_count: int = 0
    new_entities: int = 0
    new_mentions: int = 0
    extractor_failures: int = 0
    lock_lost_mid_tick: bool = False
    per_item: list[PerItemDetail] = field(default_factory=list)


def _empty_extraction_result(lock_acquired: bool) -> ExtractionTickResultWithLock:
    """Build the "no work attempted" result returned when the lock is held by a peer."""
    return ExtractionTickResultWithLock(lock_acquired=lock_acquired)


def _make_pass_id() -> str:
    """Generate the default ``pass_id`` for a tick.

    Matches the TS source line 159 (literal template string
    ``tick-${Date.now()}``) — millisecond-precision Unix timestamp
    prefixed with ``"tick-"``.
    """
    return f"tick-{int(time.time() * 1000)}"


async def tick_extraction(
    db: sqlite3.Connection,
    extractor: ExtractEntitiesFn,
    args: ExtractionTickArgs | None = None,
) -> ExtractionTickResultWithLock:
    """Manual entity-coreference tick. Wraps the worker-lock acquire /
    heartbeat / release dance around
    :func:`~lossless_hermes.extraction.coreference.run_coreference_tick`.

    Ports ``worker-orchestrator.ts:133-175`` ``tickExtraction``. Used by
    ``/lcm worker tick extraction`` and by the extraction autostart loop
    (:func:`~lossless_hermes.operator.extraction_autostart.try_start_extraction_autostart`).

    Why the orchestrator wraps the lock (rather than letting
    ``run_coreference_tick`` do it internally like backfill does):
    extraction has per-leaf LLM-call latency variance that can blow past
    the lock TTL on a slow item. Wrapping here lets us inject an
    ``on_item_heartbeat`` closure into the tick so the lock TTL is
    extended between items — see the Wave-4 Auditor #12 P0-1 fix in the
    module docstring.

    Args:
        db: Open :class:`sqlite3.Connection` with the v4.1 schema. The
            connection's transaction state matters — the inner tick
            uses ``BEGIN IMMEDIATE`` per item, so the connection must
            be in autocommit (``isolation_level=None``) mode.
        extractor: The injected
            :class:`~lossless_hermes.extraction.coreference.ExtractEntitiesFn`.
            Production wires
            :func:`~lossless_hermes.extraction.extractor.create_entity_extractor_llm`
            with the gateway's LLM-complete callable; tests inject a
            deterministic fake.
        args: Optional :class:`ExtractionTickArgs` knobs. When ``None``
            (the default), the tick uses generated ``pass_id`` and the
            default per-tick limit (50).

    Returns:
        :class:`ExtractionTickResultWithLock`:

        * ``lock_acquired=False`` with zero counts when the lock was
          held by a peer at acquire time. No extractor invocation
          happened; the caller (autostart) treats this as a no-op.
        * ``lock_acquired=False`` with the lock-lost flag set when the
          heartbeat returned ``False`` mid-tick (Wave-7 fix — see
          module docstring). Partial progress is committed before
          returning.
        * ``lock_acquired=True`` with the full
          :class:`CoreferenceTickResult` fields when the tick ran
          cleanly.

    Raises:
        Never. Inner-tick exceptions are absorbed by
        :func:`~lossless_hermes.extraction.coreference.run_coreference_tick`
        and surfaced via the result's ``extractor_failures`` /
        ``per_item.error`` fields. The orchestrator's only failure
        mode is "couldn't acquire the lock" → ``lock_acquired=False``.

        If a lock-release failure occurs in the ``finally`` block, it
        is logged but not re-raised — the inner tick result is the
        truth of the matter.
    """
    resolved_args = args if args is not None else ExtractionTickArgs()
    worker_id = generate_worker_id("orchestrator-extraction")
    pass_id = resolved_args.pass_id or _make_pass_id()

    # Acquire the cross-process lock. If a peer holds it, return early
    # with a zero-count "not ours to do" result — matches TS line 138-148.
    acquired = acquire_lock(
        db,
        "extraction",
        worker_id=worker_id,
        job_metadata="tick_extraction",
    )
    if not acquired:
        return _empty_extraction_result(lock_acquired=False)

    try:
        # LCM Wave-4 Auditor #12 P0-1 + #13 P1 fix: pass an on_item_heartbeat
        # closure so run_coreference_tick can extend the worker lock TTL
        # between items. Without this, a 50-item tick × 30s/item = 25 min
        # would blow past the 90s WORKER_LOCK_TTL_S, allowing a second
        # gateway's autostart to GC + re-acquire and double-process the
        # queue — directly causing the duplicate-mention scenario the
        # INSERT OR IGNORE path was fixed for in Wave-1.
        # Original: lossless-claw/src/operator/worker-orchestrator.ts:151-161.
        def _on_heartbeat() -> bool:
            return heartbeat_lock(db, "extraction", worker_id)

        tick_kwargs: dict[str, Any] = {
            "pass_id": pass_id,
            "on_item_heartbeat": _on_heartbeat,
        }
        if resolved_args.per_tick_limit is not None:
            tick_kwargs["per_tick_limit"] = resolved_args.per_tick_limit

        tick_result: CoreferenceTickResult = await run_coreference_tick(
            db,
            extractor,
            CoreferenceTickOptions(**tick_kwargs),
        )

        # LCM Wave-7 Auditor #4/13 P1 fix: surface lockLostMidTick. The
        # W4 fix wired the heartbeat callback + result field, but
        # tickExtraction returned `lockAcquired: true` regardless —
        # collapsing "lost lock partway" into "ran cleanly". Now if
        # heartbeat returned false mid-tick, lockAcquired flips to false
        # so callers (autostart) can pace down + treat as soft failure.
        # Original: lossless-claw/src/operator/worker-orchestrator.ts:163-172.
        lock_acquired = not tick_result.lock_lost_mid_tick

        return ExtractionTickResultWithLock(
            lock_acquired=lock_acquired,
            processed_count=tick_result.processed_count,
            new_entities=tick_result.new_entities,
            new_mentions=tick_result.new_mentions,
            extractor_failures=tick_result.extractor_failures,
            lock_lost_mid_tick=tick_result.lock_lost_mid_tick,
            per_item=list(tick_result.per_item),
        )
    finally:
        # Always release, even on inner-tick exception. If release fails
        # (e.g. DB closed mid-shutdown), log but don't mask the tick's
        # outcome — the lock will GC at TTL expiry.
        try:
            release_lock(db, "extraction", worker_id)
        except sqlite3.Error:
            _log.exception(
                "[worker-orchestrator] release_lock failed for extraction worker_id=%s",
                worker_id,
            )


# ---------------------------------------------------------------------------
# force_release_lock
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ForceReleaseResult:
    """Result of :func:`force_release_lock`.

    The AC requires returning ``{released: bool, reason: str}``; this
    dataclass formalizes that contract. The TS source returns a bare
    ``bool``; we add the ``reason`` discriminator so callers (the
    ``/lcm worker`` command renderer) can show a precise explanation
    for the ``False`` case.

    Attributes:
        released: ``True`` if a row matched and was deleted. ``False``
            if no row existed, OR if the host/worker guard didn't
            match the current holder.
        reason: Human-readable explanation. One of:

            * ``"released"`` — a row matched and was deleted.
            * ``"no_lock_held"`` — no row exists for this kind (with
              the supplied ``expected_worker_id`` if given).
            * ``"guard_mismatch"`` — a row exists but its
              ``worker_id`` doesn't match ``expected_worker_id``.
              The current holder's worker_id is included in the
              reason for operator diagnostic.
    """

    released: bool
    reason: str


def force_release_lock(
    db: sqlite3.Connection,
    job_kind: WorkerJobKind | str,
    *,
    expected_worker_id: str | None = None,
) -> ForceReleaseResult:
    """Force-release a stuck lock. Operator escape hatch.

    Ports ``worker-orchestrator.ts:191-212`` ``forceReleaseLock``. Used
    by ``/lcm worker force-release <kind>`` (owner-gated) when a worker
    crashed without releasing — see the lock-table TTL + heartbeat
    mechanism in :mod:`lossless_hermes.concurrency.worker_lock` for the
    SAFE recovery path; this is the override for cases where heartbeat
    is broken (e.g. DST clock jump, NTP correction).

    USE WITH CAUTION — if the original holder is still alive, releasing
    its lock causes a race where two workers may end up doing the same
    job (one succeeded, one inserts duplicates). The
    ``expected_worker_id`` guard is the defense: only release if the
    lock is currently held by the worker the operator expects.

    Args:
        db: Open :class:`sqlite3.Connection`.
        job_kind: One of :data:`WorkerJobKind` (accepts ``str`` for
            forward-compat).
        expected_worker_id: Optional guard. When provided, the DELETE
            is scoped to ``WHERE job_kind = ? AND worker_id = ?`` —
            so an operator slip doesn't evict a healthy holder
            mid-tick (which would cascade into double-processing the
            queue exactly as the Wave-1 ``INSERT OR IGNORE``
            protections aim to prevent). When omitted, behavior
            matches the legacy escape-hatch semantic (delete by kind
            only) — caller takes responsibility for any race.

    Returns:
        :class:`ForceReleaseResult` with the ``released`` flag + a
        human-readable ``reason``. See the class docstring for the
        possible reasons.

    Note on the spec's "host" naming:
        The issue spec phrases this as ``force_release_lock(kind,
        host=None)`` — "host" is the spec's intent-name for what the
        lock table calls ``worker_id``. Generated worker IDs are of the
        form ``<role>-<pid>-<startMs>-<nonce>``, so the leading role
        token IS effectively a per-host identifier when operators want
        to release "all locks held by this gateway." For exact-match
        semantics the operator passes the full worker_id; for "any
        worker from this host" matching, the caller iterates locks via
        :func:`get_worker_status_snapshot` and filters by prefix
        before calling :func:`force_release_lock`. The ``host``
        parameter name in the spec maps to ``expected_worker_id``
        here.
    """
    # LCM Wave-4 Auditor #13 P1 fix: when expected_worker_id is provided,
    # scope the DELETE to that worker so an operator slip doesn't evict a
    # healthy holder mid-tick (which would cascade into double-processing
    # the queue exactly as the Wave-1 INSERT OR IGNORE protections aim
    # to prevent). When omitted, behavior matches the legacy escape-hatch
    # semantic (delete by kind only) — caller takes responsibility.
    # Original: lossless-claw/src/operator/worker-orchestrator.ts:202-212.
    if expected_worker_id is not None:
        # First check whether a row exists at all so we can distinguish
        # "no row" from "row but guard mismatch" in the result.reason.
        current = lock_info(db, job_kind)
        if current is None:
            return ForceReleaseResult(released=False, reason="no_lock_held")
        if current.worker_id != expected_worker_id:
            return ForceReleaseResult(
                released=False,
                reason=(
                    f"guard_mismatch: expected_worker_id={expected_worker_id!r} "
                    f"but current holder is {current.worker_id!r}"
                ),
            )
        cur = db.execute(
            "DELETE FROM lcm_worker_lock WHERE job_kind = ? AND worker_id = ?",
            (job_kind, expected_worker_id),
        )
        db.commit()
        if cur.rowcount > 0:
            return ForceReleaseResult(released=True, reason="released")
        # The row disappeared between our SELECT and DELETE (another
        # caller raced us). Treat as no-op — the lock IS gone, just not
        # by us.
        return ForceReleaseResult(released=False, reason="no_lock_held")

    # Unguarded path: legacy escape-hatch semantic.
    cur = db.execute(
        "DELETE FROM lcm_worker_lock WHERE job_kind = ?",
        (job_kind,),
    )
    db.commit()
    if cur.rowcount > 0:
        return ForceReleaseResult(released=True, reason="released")
    return ForceReleaseResult(released=False, reason="no_lock_held")


# ---------------------------------------------------------------------------
# heartbeat_all_held_locks
# ---------------------------------------------------------------------------


HeartbeatLossKind = Literal["ok", "lost", "skipped"]
"""Per-kind heartbeat outcome.

* ``"ok"`` — heartbeat succeeded; lock TTL extended.
* ``"lost"`` — heartbeat returned ``False`` (lock was stolen by a peer
  or expired before we got there). Caller MUST abort any in-flight
  tick for this kind.
* ``"skipped"`` — no ``worker_id`` supplied in the input map for this
  kind. The caller doesn't claim to hold this lock.
"""


@dataclass(frozen=True, slots=True)
class HeartbeatResult:
    """Result of :func:`heartbeat_all_held_locks`.

    Ports the TS ``{ refreshed, perKind }`` return shape from
    ``worker-orchestrator.ts:222-250`` ``heartbeatAllHeldLocks``.

    Attributes:
        refreshed: Number of kinds where the heartbeat succeeded
            (``"ok"`` count in :attr:`per_kind`).
        per_kind: Per-kind outcome map. Every kind in the input
            ``worker_ids_by_kind`` mapping has an entry; kinds NOT in
            the input map have ``"skipped"``. Caller can iterate to
            find ``"lost"`` kinds and abort their in-flight ticks.
    """

    refreshed: int
    per_kind: dict[str, HeartbeatLossKind]


def heartbeat_all_held_locks(
    db: sqlite3.Connection,
    worker_ids_by_kind: dict[str, str],
) -> HeartbeatResult:
    """Heartbeat every lock the caller claims to hold.

    Ports ``worker-orchestrator.ts:222-250`` ``heartbeatAllHeldLocks``.
    Called by the WorkerLoop dispatcher (ADR-020) to extend TTL on every
    lock held by this gateway in one DB round-trip per kind.

    For each kind in :data:`WORKER_JOB_KINDS`:

    1. Look up the caller's worker_id for that kind in
       ``worker_ids_by_kind``.
    2. If absent, record ``"skipped"`` in the per-kind map.
    3. Otherwise call :func:`heartbeat_lock` (which returns ``False`` if
       the lock was stolen / expired). Record ``"ok"`` or ``"lost"``.

    Each call is wrapped in try/except so a single SQL failure doesn't
    abort the loop — that kind is recorded ``"lost"`` and the other
    kinds proceed (Wave-4 Auditor #13 P1 — see module docstring).

    Args:
        db: Open :class:`sqlite3.Connection`.
        worker_ids_by_kind: Map from job_kind → the worker_id the caller
            currently holds for that kind. Kinds NOT in the map are
            recorded ``"skipped"``.

    Returns:
        :class:`HeartbeatResult` with the count + per-kind status map.
        The result is deterministic — same input always produces the
        same outcome (assuming no concurrent lock manipulation).
    """
    # LCM Wave-4 Auditor #13 P1 fix: previously returned only a count,
    # throwing away the per-kind boolean. A worker that lost its lock
    # between ticks (stolen during a long LLM call) saw `refreshed=0`
    # indistinguishable from "we never held it." Now returns per-kind
    # status so callers can detect lock-loss and abort the in-flight
    # tick. Also wraps each call in try/except so one failed heartbeat
    # doesn't abort the loop.
    # Original: lossless-claw/src/operator/worker-orchestrator.ts:230-250.
    refreshed = 0
    per_kind: dict[str, HeartbeatLossKind] = {}
    for kind in WORKER_JOB_KINDS:
        wid = worker_ids_by_kind.get(kind)
        if not wid:
            per_kind[kind] = "skipped"
            continue
        try:
            ok = heartbeat_lock(db, kind, wid)
        except sqlite3.Error:
            # DB closed mid-shutdown or transient SQLite error; treat
            # as lost so the caller aborts the in-flight tick.
            _log.exception(
                "[worker-orchestrator] heartbeat SQL error for kind=%s worker=%s",
                kind,
                wid,
            )
            per_kind[kind] = "lost"
            continue
        per_kind[kind] = "ok" if ok else "lost"
        if ok:
            refreshed += 1
    return HeartbeatResult(refreshed=refreshed, per_kind=per_kind)


# ---------------------------------------------------------------------------
# create_worker_llm_call — merged from worker-llm.ts
# ---------------------------------------------------------------------------


#: Default per-call LLM timeout in seconds — matches the TS source
#: ``DEFAULT_TIMEOUT_MS = 60_000`` at ``worker-llm.ts:37``. Per-call
#: hard cap so a stuck LLM doesn't block the worker loop's heartbeat.
DEFAULT_WORKER_LLM_TIMEOUT_S: Final[float] = 60.0


class LlmCompleteResultLike(Protocol):
    """The shape returned by the injected LLM-complete callable.

    Mirrors the TS ``CompletionResult`` shape that ``deps.complete``
    returns. Defensive: we read ``output`` (preferred) AND fall back to
    ``text`` / ``content`` if the adapter uses a different field name,
    matching the TS source's
    :func:`~lossless_hermes.operator.worker_orchestrator._extract_text`
    fallback chain (``worker-llm.ts:130-150``).
    """

    @property
    def output(self) -> str: ...


class LlmCompleteCallable(Protocol):
    """The injected LLM-complete callable.

    Mirrors ``deps.complete`` from the TS plugin's ``LcmDependencies``
    interface — the gateway-side LLM provider adapter (anthropic /
    openai / mock). The Python port stays vendor-agnostic; concrete
    Hermes wiring lives elsewhere (Epic 04 cycle).

    The callable accepts a dict of arguments (model, prompt,
    max_output_tokens, …) — matches the Python-side
    :class:`~lossless_hermes.extraction.extractor.LlmCompleteFn` Protocol
    so a single Hermes adapter can satisfy both.
    """

    async def __call__(self, args: dict[str, Any], /) -> LlmCompleteResultLike: ...


@dataclass(frozen=True, slots=True)
class WorkerLlmConfig:
    """Config for :func:`create_worker_llm_call`.

    Mirrors the TS ``WorkerLlmConfig`` interface at
    ``worker-llm.ts:22-35``.

    Attributes:
        complete: The injected LLM-complete callable. Required —
            without it the adapter has no way to dispatch.
        default_model: Default model identifier when the per-call
            :attr:`LlmCallArgs.model` is empty. Falls back to
            ``LCM_SUMMARY_MODEL`` env (operator's chosen default,
            matching the leaf-summarizer convention in
            :mod:`lossless_hermes.summarize`) with a ``"gpt-5.4-mini"``
            fallback if env unset.
        timeout_s: Per-attempt timeout in seconds. Default 60. Worker
            task budget is hard-capped so a stuck LLM doesn't block
            the worker loop's heartbeat.
    """

    complete: LlmCompleteCallable
    default_model: str | None = None
    timeout_s: float = DEFAULT_WORKER_LLM_TIMEOUT_S


def _resolve_worker_llm_default_model(config_default: str | None) -> str:
    """Resolve the default model from config / env / hardcoded fallback.

    Mirrors the TS expression
    ``process.env.LCM_SUMMARY_MODEL?.trim() || "gpt-5.4-mini"`` at
    ``worker-llm.ts:41``, generalized so the config can override the
    env-driven value.

    Precedence:

    1. ``config.default_model`` if non-None / non-empty.
    2. ``LCM_SUMMARY_MODEL`` env var (stripped).
    3. ``"gpt-5.4-mini"`` hardcoded fallback.
    """
    import os

    if config_default and config_default.strip():
        return config_default.strip()
    env_value = os.environ.get("LCM_SUMMARY_MODEL", "").strip()
    if env_value:
        return env_value
    return "gpt-5.4-mini"


def _extract_text(response: Any) -> str | None:
    """Tolerant extraction of the LLM response's text payload.

    Ports ``worker-llm.ts:130-150`` ``extractText``. Tries the
    ``output`` attribute (Python-port canonical), then falls back to
    ``text`` (TS ``CompletionResult``-shape) and ``content`` (alternative
    adapters). For list-shaped ``content``, joins string parts with
    newlines (matches the TS source's array handling).

    Returns:
        The extracted text, or :data:`None` if no recognizable text
        field is present (caller raises a clear error in that case).
    """
    if response is None:
        return None
    # Python-port canonical: an object with an ``output`` attribute.
    out = getattr(response, "output", None)
    if isinstance(out, str):
        return out
    # TS-shape fallback: ``text`` attribute / key.
    text = getattr(response, "text", None)
    if isinstance(text, str):
        return text
    # Dict-shaped response (some adapters return plain dicts).
    if isinstance(response, dict):
        d_out = response.get("output")
        if isinstance(d_out, str):
            return d_out
        d_text = response.get("text")
        if isinstance(d_text, str):
            return d_text
        d_content = response.get("content")
        if isinstance(d_content, str):
            return d_content
        if isinstance(d_content, list):
            parts: list[str] = []
            for c in d_content:
                if isinstance(c, str):
                    parts.append(c)
                elif isinstance(c, dict):
                    inner = c.get("text")
                    if isinstance(inner, str):
                        parts.append(inner)
            if parts:
                return "\n".join(parts)
    # Object-shaped ``content``.
    content = getattr(response, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts2: list[str] = []
        for c in content:
            if isinstance(c, str):
                parts2.append(c)
            else:
                inner = getattr(c, "text", None)
                if isinstance(inner, str):
                    parts2.append(inner)
        if parts2:
            return "\n".join(parts2)
    return None


def create_worker_llm_call(config: WorkerLlmConfig) -> LlmCall:
    """Build a :class:`~lossless_hermes.synthesis.dispatch.LlmCall` from
    an injected ``deps.complete`` callable.

    Ports ``worker-llm.ts:52-126`` ``createWorkerLlmCall`` to Python.
    Merged into this module per the porting guide (167 LOC adapter, no
    independent state — see module docstring).

    The returned callable is suitable for injection into
    :func:`~lossless_hermes.synthesis.dispatch.dispatch_synthesis` (Group
    D synthesis) or any other worker that needs a generic LLM-call
    surface.

    The adapter:

    * Resolves the per-call model from
      :attr:`LlmCallArgs.model` falling back to
      :attr:`WorkerLlmConfig.default_model` falling back to env / hard
      fallback (see :func:`_resolve_worker_llm_default_model`).
    * Measures latency in milliseconds around the call (used for audit).
    * Cost is NOT computed (no token-cost calculator wired); the
      returned :attr:`LlmCallResult.cost_cents` stays :data:`None` →
      recorded as ``NULL`` in :sql:`lcm_synthesis_audit`.
    * Catches the configured ``timeout_s`` via
      :func:`asyncio.wait_for` — a stuck LLM doesn't block the worker
      loop's heartbeat.

    Args:
        config: :class:`WorkerLlmConfig` carrying the injected
            ``complete`` callable + per-call defaults.

    Returns:
        An ``async`` callable matching the
        :class:`~lossless_hermes.synthesis.dispatch.LlmCall` Protocol
        (``async (LlmCallArgs) -> LlmCallResult``).

    The returned closure is safe to share across ticks (no internal
    mutable state; ``config.complete`` is the only injected reference
    and the caller controls its lifecycle).
    """
    import asyncio

    timeout_s = config.timeout_s
    complete = config.complete
    config_default = config.default_model

    async def _call(args: LlmCallArgs) -> LlmCallResult:
        """Inner adapter — matches :class:`LlmCall` shape."""
        started_at = time.monotonic()
        # Resolve model. The synthesis dispatcher always supplies a
        # non-empty ``args.model``; this fallback is defense-in-depth
        # for callers (best-of-N candidates with an override).
        model = args.model.strip() if args.model else ""
        if not model:
            model = _resolve_worker_llm_default_model(config_default)

        # Build the kwargs payload. Match the TS source's shape — fields
        # the underlying provider doesn't recognize are typically
        # ignored, so passing the same shape regardless of provider is
        # safe (and the TS source does too).
        payload: dict[str, Any] = {
            "model": model,
            "prompt": args.prompt,
            "pass_kind": args.pass_kind,
            "max_output_tokens": (
                args.max_output_tokens if args.max_output_tokens is not None else 1024
            ),
            # The TS source also sends a ``system`` field on every call —
            # matches ``worker-llm.ts:75-80``. We preserve the literal
            # text so /tests can assert it deterministically and so the
            # worker prompt-format stays consistent across the port.
            "system": (
                "You are a worker process for the LCM (Lossless Context "
                "Management) plugin. You handle structured tasks like entity "
                "extraction, procedure judging, theme naming, and synthesis. "
                "Follow the user prompt's exact contract — output formats matter "
                "for downstream parsing."
            ),
            # Reasoning hint: low for short-output tasks (judges, names),
            # medium for longer (synthesis). Matches TS line 84-85.
            "reasoning_if_supported": ("low" if args.pass_kind == "best_of_n_judge" else "medium"),
        }

        # Apply the timeout. asyncio.wait_for is the Python equivalent
        # of the TS source's hand-rolled withTimeout helper at
        # ``worker-llm.ts:152-167``. The label is embedded in the
        # exception message — match the TS form so log scrapers work.
        try:
            response = await asyncio.wait_for(complete(payload), timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                f"[worker-llm] timeout after {int(timeout_s * 1000)}ms "
                f"(worker-llm:{args.pass_kind}:{model})"
            ) from exc
        except Exception as exc:  # noqa: BLE001 — re-raise so dispatch logs the audit row
            # Mirror TS line 90-99: log warn + re-raise so dispatch can
            # update the audit row to 'failed' and surface a typed
            # SynthesisDispatchError("llm_failure").
            _log.warning(
                "[worker-llm] LLM call failed (model=%s pass_kind=%s): %s",
                model,
                args.pass_kind,
                exc,
            )
            raise

        # Extract text. Mirror TS line 104-109: defensive shape check;
        # surface a clear error rather than returning an empty string.
        text = _extract_text(response)
        if text is None:
            raise RuntimeError(f"[worker-llm] LLM response had no text content (model={model})")

        latency_ms = (time.monotonic() - started_at) * 1000.0

        # Mirror TS line 117-119: providers may populate a ``model``
        # field on the response (some pi-ai providers do; the typed
        # surface doesn't expose it). Read it tolerantly and fall
        # back to the requested model.
        actual_model: str | None = None
        responded_model = getattr(response, "actual_model", None) or getattr(
            response, "model", None
        )
        if isinstance(responded_model, str) and responded_model:
            actual_model = responded_model
        elif isinstance(response, dict):
            # Cast to Any so ty's narrowing of the Protocol intersection
            # doesn't fix the dict key type to Never. The runtime check
            # above guarantees ``response`` is a mapping at this point.
            response_dict: dict[str, Any] = cast("dict[str, Any]", response)
            d_actual = response_dict.get("actual_model") or response_dict.get("model")
            if isinstance(d_actual, str) and d_actual:
                actual_model = d_actual

        return LlmCallResult(
            output=text,
            latency_ms=latency_ms,
            cost_cents=None,  # no token-cost calculator wired
            actual_model=actual_model or model,
        )

    return _call


# ---------------------------------------------------------------------------
# Convenience: re-export the LlmCompleteFn alias for type-checker pinning.
# ---------------------------------------------------------------------------

# Awaitable-Callable convenience alias pinned for ty. Kept private so it
# doesn't widen the module's public surface.
_AwaitableLlm = Callable[[dict[str, Any]], Awaitable[Any]]
