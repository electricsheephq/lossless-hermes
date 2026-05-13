"""Generic single-process worker loop — LCM v4.1 §0.

Port of ``lossless-claw/src/concurrency/worker-loop.ts`` (LCM commit
``1f07fbd``, 238 LOC TS → ~260 LOC Python).

One Python process running multiple background jobs cooperatively. Each
job has its own cadence (``interval_s``) and runs in turn, single-threaded
on the asyncio event loop, with the cross-process ``lcm_worker_lock``
providing single-flight across processes. The lock is acquired inside
each job's ``run()`` callable; the loop knows nothing about it.

This is intentionally minimal — no thread pool, no cron expressions,
no priority queue. The plugin's worker scheduling needs are simple:

* Run embedding-backfill every ~60s (when there are pending docs)
* Run entity-extraction every ~30s (when there are queued items)
* Run condensation-maintenance periodically (~120s)
* Run theme-consolidation when idle

Each job's ``run()`` returns telemetry (or raises); the loop dispatches
to ``on_job_complete`` callback after each tick — success OR exception.
Loop never logs on its own; callers wire telemetry / logs through the
callback.

### Lifecycle

::

    loop = WorkerLoop(
        jobs=[
            WorkerJob(
                kind="embedding-backfill",
                interval_s=60.0,
                run=lambda: run_backfill_tick(conn, opts),
            ),
            WorkerJob(
                kind="extraction",
                interval_s=30.0,
                run=lambda: run_extraction_tick(conn),
            ),
        ],
        on_job_complete=lambda info: log.info("worker tick", extra=info.__dict__),
    )
    loop.start()
    # ... process runs ...
    await loop.stop(graceful_timeout_s=30.0)

### Single-process model

Per ADR-018 + ADR-020: everything runs in this Python process. We do NOT
spawn ``threading.Thread`` workers (per v4.1.1 A9 the worker_threads
scaffolding for true heartbeat-isolation is a future enhancement; in
Python the asyncio loop has the same property — the heartbeat is just
another task). For now, the loop's ``asyncio.sleep`` + ``create_task``
dispatch is good enough at these cadences.

### Cross-process safety

The per-job ``run()`` MUST acquire its own ``lcm_worker_lock`` row for
its ``WorkerJobKind`` before doing real work. Multiple processes may
run the same WorkerLoop concurrently (e.g. dev box + CI on the same
DB), and the lock prevents double-work. See ``worker_lock.py`` (issue
05-06) for the SQL pattern.

### Exception isolation

Each tick is wrapped in ``try/except BaseException`` — a single bad
tick must NOT crash the loop. The exception is dispatched to
``on_job_complete(error=...)`` and the loop continues. Re-throws are
NOT propagated to the loop; if a job needs to fatally abort, it should
call ``loop.stop()`` explicitly first.

### Skip-overlap-on-busy

If a previous tick for the same kind is still in flight when the
interval fires, the current tick is SKIPPED (not queued). Behavior
matches TS's ``setInterval`` + ``inFlight.has(kind) return`` pattern
in ``worker-loop.ts:130-132``.

### Generation counter

A monotonic ``_generation`` integer is bumped on every ``start()``;
each per-job loop captures its generation at task-creation time and
re-checks it on every iteration. This is the load-bearing defense
against stop()-then-start() races where a stale tick from generation N
fires after generation N+1 has begun (per ``worker-loop.ts:107``).

See:

* ``docs/adr/018-concurrency-model.md`` — Option A (``asyncio.Task`` per
  kind) chosen.
* ``docs/adr/020-worker-loop-dispatcher.md`` — entire ADR pins the
  lifecycle + the generation-counter rationale.
* ``docs/porting-guides/embeddings.md`` §"Worker loop" — the draft
  pattern this module ports verbatim.
* ``lossless-claw/src/concurrency/worker-loop.ts`` — the TS source.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from lossless_hermes.concurrency.model import WorkerJobKind

__all__ = ["JobCompleteInfo", "WorkerJob", "WorkerLoop"]

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkerJob:
    """Registration shape for a single worker job.

    Attributes:
        kind: Job kind matching :data:`WorkerJobKind` — used for telemetry,
            the cross-process lock key, and as the dedup key inside the
            loop.
        interval_s: How often to invoke ``run()``, in seconds. The loop
            schedules with ``asyncio.sleep`` so the actual cadence is
            approximate — drift accumulates if ``run()`` exceeds
            ``interval_s``. Must be ``> 0``.
        run: Coroutine factory invoked once per tick. Should:

            1. Acquire ``lcm_worker_lock`` for ``kind`` (single-flight
               across processes; see issue 05-06).
            2. Do its work.
            3. Release the lock (try/finally).
            4. Return any telemetry; the loop only uses it for the
               ``on_job_complete`` callback.

            If the awaitable raises, the loop captures the exception in
            ``JobCompleteInfo.error`` and the loop continues — a single
            bad tick doesn't crash the loop.

            Unlike the TS source (which passes ``db`` as an argument),
            Python ``run`` takes no arguments — callers close over the
            connection via lambda/closure. Cleaner than passing a stale
            handle, and easier to mock in tests.
    """

    kind: WorkerJobKind
    interval_s: float
    run: Callable[[], Awaitable[object]]


@dataclass
class JobCompleteInfo:
    """Per-tick telemetry payload dispatched to ``on_job_complete``.

    Exactly one of ``result`` / ``error`` is set:

    * ``result`` carries whatever ``WorkerJob.run`` returned (often a
      structured dict — kept as ``object`` here for forward-compat).
    * ``error`` is the captured exception if ``run`` raised.

    Attributes:
        kind: The job kind that just completed.
        duration_ms: Wall-clock duration of the tick in milliseconds
            (uses :func:`time.monotonic` so it's safe under clock-skew).
        result: Return value of ``run()`` on success, else ``None``.
        error: Captured exception on failure, else ``None``.
    """

    kind: WorkerJobKind
    duration_ms: float
    result: object | None = None
    error: BaseException | None = None


class WorkerLoop:
    """Dispatcher for periodic background jobs on the asyncio loop.

    Construct with a list of :class:`WorkerJob` registrations. Call
    :meth:`start` to begin scheduling; call :meth:`stop` to drain. The
    loop never logs on its own — wire telemetry via the optional
    ``on_job_complete`` callback (each tick's success or exception is
    dispatched there).

    See module docstring for the lifecycle + invariants.
    """

    def __init__(
        self,
        jobs: list[WorkerJob],
        *,
        on_job_complete: Callable[[JobCompleteInfo], None] | None = None,
    ) -> None:
        """Initialize the loop and validate job registrations.

        Args:
            jobs: Per-kind job registrations. The list is iterated at
                construction time — duplicates / invalid intervals are
                rejected immediately so the failure is at registration
                rather than at first tick.
            on_job_complete: Optional callback invoked after every tick
                (success or exception). The callback must NOT raise — if
                it does, the exception is logged via ``logging.exception``
                and swallowed (a broken telemetry hook must not kill the
                worker loop). The callback runs synchronously on the
                event loop; long-running work inside it stalls the loop.

        Raises:
            ValueError: ``jobs`` contains a duplicate :data:`WorkerJobKind`,
                or any job has ``interval_s <= 0``.
        """
        self._jobs: list[WorkerJob] = list(jobs)
        self._on_job_complete = on_job_complete
        # Per-kind "outer" tasks: the long-running ``_run_job`` loops. One
        # task per registered kind. Cleared on stop().
        self._tasks: list[asyncio.Task[None]] = []
        # Per-kind "inner" tasks: the most recent in-flight tick. Used
        # both for skip-overlap-on-busy and for the graceful stop drain.
        # Stored as ``Task[Any]`` because the inner coroutine returns the
        # captured ``(result, error)`` tuple (consumed by ``run_once``),
        # but the scheduler in ``_run_job`` doesn't care about the type.
        self._in_flight: dict[
            WorkerJobKind,
            asyncio.Task[tuple[object | None, BaseException | None]],
        ] = {}
        self._running: bool = False
        # Monotonic generation counter (per worker-loop.ts:107). Bumped
        # on every start() so stale ticks from a previous start cycle
        # exit at their next iteration.
        self._generation: int = 0
        self._validate_jobs()

    # ------------------------------------------------------------------
    # Construction-time validation
    # ------------------------------------------------------------------

    def _validate_jobs(self) -> None:
        """Reject duplicate kinds or non-positive intervals up front.

        Mirrors ``worker-loop.ts:validateJobs`` (TS lines 105-116). The
        TS source uses ``Number.isFinite`` to reject NaN/Inf as well —
        we get the same effect by requiring ``interval_s > 0`` (NaN
        comparisons are False; ``+inf`` passes but a never-firing job
        is preferable to a crashing one, matching TS behavior).
        """
        seen: set[WorkerJobKind] = set()
        for job in self._jobs:
            if job.kind in seen:
                raise ValueError(f"[worker-loop] duplicate job kind: {job.kind}")
            seen.add(job.kind)
            if not (job.interval_s > 0):
                raise ValueError(
                    f"[worker-loop] job {job.kind} has invalid interval_s {job.interval_s!r}"
                )

    # ------------------------------------------------------------------
    # Public lifecycle API
    # ------------------------------------------------------------------

    def start(self) -> bool:
        """Begin scheduling. Idempotent — returns ``False`` if already running.

        Bumps :attr:`_generation` and creates one ``asyncio.Task`` per
        registered job, each running the per-job loop captured against
        the new generation. Stale tasks from a previous ``start()``
        cycle (e.g. that survived a cancellation) self-exit at their
        next generation check.

        Returns:
            ``True`` on first start (or after a previous :meth:`stop`),
            ``False`` if the loop was already running.
        """
        if self._running:
            return False
        self._running = True
        self._generation += 1
        gen = self._generation
        for job in self._jobs:
            task = asyncio.create_task(self._run_job(job, gen), name=f"worker-{job.kind}")
            self._tasks.append(task)
        return True

    async def stop(self, *, graceful_timeout_s: float = 30.0) -> bool:
        """Stop scheduling and wait for in-flight ticks to drain.

        Sets :attr:`_running` to ``False``, cancels the per-job outer
        tasks (their ``asyncio.sleep`` raises ``CancelledError`` and
        the loops exit), then waits up to ``graceful_timeout_s`` for
        any in-flight tick tasks to complete. In-flight ticks are NOT
        cancelled — they finish at their own pace. If the wait times
        out, ``False`` is returned and the orphaned tick tasks continue
        running in the background (they self-exit on their next
        generation check after finishing).

        Args:
            graceful_timeout_s: How long to wait for in-flight ticks
                to finish, in seconds. Defaults to 30s (matches the TS
                source ``worker-loop.ts:173``).

        Returns:
            ``True`` if either:

            * the loop wasn't running (no-op), or
            * all in-flight ticks finished within ``graceful_timeout_s``.

            ``False`` if any in-flight tick was still running when the
            timeout expired.
        """
        if not self._running:
            return True
        self._running = False
        # Cancel the per-job loop tasks so their next ``await sleep`` raises
        # CancelledError and they exit cleanly. We do NOT cancel the in-
        # flight tick tasks (they're separate, fire-and-forget) — those
        # are drained below to match TS's "graceful" stop semantics.
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()

        in_flight_tasks = [t for t in self._in_flight.values() if not t.done()]
        if not in_flight_tasks:
            return True

        # asyncio.wait() with timeout does NOT cancel pending tasks on
        # timeout — that's the right semantics here (matches TS's
        # Promise.race + clearTimeout). asyncio.wait_for() WOULD cancel,
        # so use asyncio.wait() directly.
        _done, pending = await asyncio.wait(in_flight_tasks, timeout=graceful_timeout_s)
        return len(pending) == 0

    async def run_once(self, kind: WorkerJobKind) -> object:
        """Invoke a single job immediately, outside the schedule.

        Used by tests, by the ``/lcm worker tick`` operator command
        (Epic 08), and by Epic 03's leaf-write hooks that nudge backfill
        on the same turn as a write. The tick is tracked in
        :attr:`_in_flight` exactly like a scheduled tick — concurrent
        scheduled ticks will skip-on-overlap if they fire while this
        one is in flight.

        Args:
            kind: Which registered job to run.

        Returns:
            Whatever the job's ``run()`` returned.

        Raises:
            ValueError: ``kind`` is not registered.
            RuntimeError: A tick for ``kind`` is already in flight
                (scheduled or via another :meth:`run_once` call).
            BaseException: Any exception raised by ``run()``. The
                exception is dispatched to ``on_job_complete`` before
                being re-raised, so callers that want to swallow it
                can wrap the call in their own ``try/except``.
        """
        job = next((j for j in self._jobs if j.kind == kind), None)
        if job is None:
            raise ValueError(f"[worker-loop] no job kind: {kind}")

        existing = self._in_flight.get(kind)
        if existing is not None and not existing.done():
            raise RuntimeError(f"[worker-loop] job {kind} is already in flight")

        started = time.monotonic()
        # Schedule the run as a task so concurrent ``in_flight_count`` /
        # skip-overlap checks from the scheduler see it. The inner
        # coroutine returns the captured (result, error) — we re-raise
        # the error after the in-flight bookkeeping clears.
        tick_task = asyncio.create_task(self._run_capturing(job, started))
        self._in_flight[kind] = tick_task
        try:
            result, error = await tick_task
        finally:
            # Clear the entry so subsequent run_once() calls aren't
            # blocked. The TS uses .finally(() => inFlight.delete(kind))
            # — same semantics. Pop with default in case stop() already
            # cleared the dict (concurrent stop + run_once).
            if self._in_flight.get(kind) is tick_task:
                self._in_flight.pop(kind, None)
        if error is not None:
            raise error
        return result

    def is_running(self) -> bool:
        """Return whether the loop is currently scheduling new ticks."""
        return self._running

    def in_flight_count(self) -> int:
        """Return the number of job kinds with a tick currently in flight."""
        return sum(1 for t in self._in_flight.values() if not t.done())

    # ------------------------------------------------------------------
    # Internal per-job loop + tick wrapper
    # ------------------------------------------------------------------

    async def _run_job(self, job: WorkerJob, my_gen: int) -> None:
        """Per-job long-running loop. One task per registered kind.

        Sleeps ``interval_s`` first (matching TS ``setInterval`` semantics
        — the first tick fires AFTER the interval, not immediately at
        start). On wake-up, checks generation + running flags + in-flight
        state before spawning the next tick task.

        Args:
            job: The registered :class:`WorkerJob` to dispatch.
            my_gen: The generation counter captured at task-creation
                time. Re-checked on every iteration; a generation
                mismatch (i.e. ``stop()`` then ``start()`` happened
                between iterations) causes the loop to exit so the
                stale task can't fire ticks for the new generation.
        """
        try:
            while self._running and self._generation == my_gen:
                await asyncio.sleep(job.interval_s)
                # Re-check after sleep — both flags may have flipped while
                # we were sleeping (e.g. stop() then start()).
                if not self._running or self._generation != my_gen:
                    return
                # Skip-overlap-on-busy: matches TS line 132 verbatim.
                existing = self._in_flight.get(job.kind)
                if existing is not None and not existing.done():
                    continue
                started = time.monotonic()
                # Fire-and-forget tick. ``_run_capturing`` already swallows
                # every exception and dispatches via ``on_job_complete``,
                # so the un-awaited task can never raise.
                tick_task = asyncio.create_task(self._run_capturing(job, started))
                self._in_flight[job.kind] = tick_task
        except asyncio.CancelledError:
            # stop() cancelled us. Normal shutdown path — exit silently.
            return

    async def _run_capturing(
        self, job: WorkerJob, started: float
    ) -> tuple[object | None, BaseException | None]:
        """Invoke a single tick, dispatch telemetry, return captured outcome.

        Wraps ``job.run()`` in ``try/except BaseException`` (intentional
        — see TS lines 142-148 + worker-loop.ts comments: a single bad
        tick must NOT crash the per-job loop). On success, dispatches
        :class:`JobCompleteInfo` with ``result``; on exception,
        dispatches with ``error`` and does NOT re-raise. Returns the
        outcome as a tuple so :meth:`run_once` can re-raise to its own
        caller (the TS ``runOnce`` contract: return on success, throw
        on error). The scheduler path in :meth:`_run_job` discards the
        return value (it's already been dispatched via the callback).

        Args:
            job: The :class:`WorkerJob` whose ``run()`` to invoke.
            started: ``time.monotonic()`` snapshot at tick start — used
                to compute ``duration_ms``.

        Returns:
            ``(result, None)`` on success or ``(None, exception)`` on
            failure. Exactly one element is non-``None``.
        """
        try:
            result = await job.run()
            duration_ms = (time.monotonic() - started) * 1000.0
            self._dispatch_complete(
                JobCompleteInfo(kind=job.kind, duration_ms=duration_ms, result=result)
            )
            return result, None
        except BaseException as exc:  # noqa: BLE001 — intentional (TS lines 142-148)
            duration_ms = (time.monotonic() - started) * 1000.0
            self._dispatch_complete(
                JobCompleteInfo(kind=job.kind, duration_ms=duration_ms, error=exc)
            )
            # Do NOT re-raise from the scheduler path — the loop must
            # continue even if a single tick fails. run_once() reads
            # the return tuple and re-raises explicitly.
            return None, exc

    def _dispatch_complete(self, info: JobCompleteInfo) -> None:
        """Invoke ``on_job_complete`` and swallow any exception it raises.

        A broken telemetry hook must NOT kill the loop. The exception
        is logged via :func:`logging.Logger.exception` and dropped.
        Matches the TS source's implicit behavior — TS uses ``?.()`` so
        a thrown callback would propagate, but in practice the production
        callback is wrapped in a try/catch by the caller. We make the
        protective wrap explicit here.
        """
        if self._on_job_complete is None:
            return
        try:
            self._on_job_complete(info)
        except BaseException:  # noqa: BLE001 — intentional (see docstring)
            _log.exception("[worker-loop] on_job_complete callback raised")
