"""Extraction worker autostart — LCM v4.1 cycle-2 + issue 07-04.

Ports ``lossless-claw/src/operator/extraction-autostart.ts`` (LCM commit
``1f07fbd`` on branch ``pr-613``, 214 LOC TS → ~360 LOC Python with
prose docstrings + Wave-N comments).

Plugin lifecycle hook that auto-runs the entity-coreference worker
(:func:`~lossless_hermes.extraction.coreference.run_coreference_tick`)
in the background at a 60-second cadence. The autostart is the in-process
scheduler; the cross-process locking discipline lives in
:func:`~lossless_hermes.operator.worker_orchestrator.tick_extraction`
(Epic 08 — not yet ported). Until that orchestrator lands, this module
accepts a caller-supplied ``tick_fn`` callable mirroring
``tick_extraction(db, *, extractor, ...)``. Callers MUST wire the
tick_fn through the orchestrator (NOT through ``run_coreference_tick``
directly) so two gateways booting simultaneously can't double-process
the queue (Wave-1 Auditor #6 finding #4).

Operator gating:

* **Opt-out:** ``LCM_EXTRACTION_LLM_ENABLED=false`` (default ON —
  extraction is intrinsic to v4.1, not opt-in like embeddings which
  costs Voyage tokens). When opt-out, returns a no-op handle.
* **Pre-flight:** ``deps.complete`` must be callable (gateway has at
  least one LLM provider configured). If absent, returns a no-op
  handle and logs info-level.
* **Per-tick guard:** ``in_flight`` (asyncio task) skips overlapping
  ticks. The :class:`WorkerLoop` already implements this via its
  skip-overlap-on-busy contract (ADR-020); we don't reimplement here.

Auto-stop conditions:

* **3 consecutive tick-throw failures** → log error + stop the loop,
  require gateway restart. Per-extractor failures are absorbed into
  :attr:`CoreferenceTickResult.extractor_failures` and do NOT burn the
  consecutive-failures budget.
* **Outer-tick body throws** (e.g. DB closed mid-tick during shutdown)
  also count as consecutive failure. This is the LCM v4.1 Final.review.3
  Loop-9 B2 HIGH fix: extraction was modeled on backfill but lost the
  outer try/catch in cycle-2.
* **Lock skip:** if ``tick_fn`` returns a result with
  ``lock_acquired = False``, log info + skip; do NOT burn the
  consecutive-failures budget. This lets a sibling gateway hold the
  lock without us treating it as a failure.

3-consecutive-idle behavior (DIFFERENT from backfill — see TS source
``extraction-autostart.ts:120-125``): the first idle tick logs info,
subsequent idle ticks continue cheaply (the count probe is one SQL
query). Unlike embedding-backfill, we do NOT stop on 3 idle ticks —
new leaves can be queued any time, and the cost of polling is trivial
(unlike Voyage tokens for embedding-backfill).

Initial 10s startup delay before first tick — matches TS source line
194 ``setTimeout(...10_000)``. Prevents a thundering herd on gateway
boot when many sessions start at once.

See:

* ``epics/07-entity-synthesis/07-04-extraction-autostart.md`` — full spec.
* ``lossless-claw/src/operator/extraction-autostart.ts`` — TS source.
* ``src/lossless_hermes/extraction/coreference.py`` — the
  :func:`~lossless_hermes.extraction.coreference.run_coreference_tick`
  invoked indirectly by every tick (via the orchestrator).
* ``src/lossless_hermes/extraction/extractor.py`` — the
  :func:`~lossless_hermes.extraction.extractor.create_entity_extractor_llm`
  factory called once at autostart-start.
* ``src/lossless_hermes/concurrency/worker_loop.py`` — the
  :class:`~lossless_hermes.concurrency.worker_loop.WorkerLoop` that
  provides the cadence engine for this autostart loop.
* ``src/lossless_hermes/operator/backfill_autostart.py`` — the
  sibling autostart this module is patterned after (issue 05-11 / PR #58).
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from lossless_hermes.concurrency.worker_loop import (
    JobCompleteInfo,
    WorkerJob,
    WorkerLoop,
)
from lossless_hermes.extraction.coreference import (
    CoreferenceTickResult,
    ExtractEntitiesFn,
)
from lossless_hermes.extraction.extractor import (
    LlmCompleteFn,
    create_entity_extractor_llm,
)

__all__ = [
    "DEFAULT_EXTRACTION_INTERVAL_S",
    "STARTUP_DELAY_S",
    "ExtractionAutostartDeps",
    "ExtractionAutostartHandle",
    "ExtractionAutostartLogger",
    "ExtractionTickFn",
    "ExtractionTickResult",
    "try_start_extraction_autostart",
]


# ---------------------------------------------------------------------------
# Constants (verbatim port of ``extraction-autostart.ts:42, 194``)
# ---------------------------------------------------------------------------


#: Default cadence in seconds. Matches TS
#: ``DEFAULT_EXTRACTION_INTERVAL_MS = 60 * 1000`` at ``extraction-autostart.ts:42``.
#:
#: Note: ADR-020 currently lists ``entity-extraction: 30s`` in its job-cadence
#: table; the issue spec (07-04) and TS source agree on 60s. The porting
#: guide wins per the spec ("reconcile with porting-guide's 60s — the
#: porting guide wins"). Future ADR addendum should align ADR-020 with this.
DEFAULT_EXTRACTION_INTERVAL_S: Final[float] = 60.0


#: Initial delay before the first tick fires, in seconds. Matches TS
#: ``setTimeout(...10_000)`` at ``extraction-autostart.ts:194-196``.
#: Prevents thundering-herd on gateway boot when many sessions start at
#: once.
STARTUP_DELAY_S: Final[float] = 10.0


#: Consecutive-tick-throw threshold. After this many consecutive ticks
#: raise an exception (or report outer-body failure), the autostart
#: stops and requires gateway restart. Matches TS line 147.
_FAILURE_STRIKE_THRESHOLD: Final[int] = 3


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


class ExtractionAutostartLogger(Protocol):
    """Caller-supplied logger interface.

    Mirrors ``extraction-autostart.ts:44-48`` ``ExtractionAutostartLogger``.
    Same shape as :class:`~lossless_hermes.operator.backfill_autostart.AutostartLogger`
    for cross-module consistency. Implementers must accept :class:`str`
    messages on ``info`` / ``warn`` / ``error``. Exceptions raised from a
    logger call are caught and dropped at the autostart boundary.
    """

    def info(self, msg: str) -> None: ...

    def warn(self, msg: str) -> None: ...

    def error(self, msg: str) -> None: ...


class ExtractionAutostartDeps(Protocol):
    """Narrow slice of LcmDependencies consumed at autostart pre-flight.

    Mirrors the ``LcmDependencies.complete`` field at
    ``lossless-claw/src/types.ts`` (the full TS interface — Python port
    pending Epic 04). The ``complete`` callable is the LLM-call adapter
    that the entity-extractor binds over (see
    :class:`~lossless_hermes.extraction.extractor.LlmCompleteFn`). If
    the gateway has no LLM provider configured, ``complete`` is ``None``
    and the autostart returns a no-op handle.

    This Protocol intentionally widens beyond the existing narrow
    :class:`~lossless_hermes.tools.conversation_scope.LcmDependencies`
    (which only has ``resolve_session_id_from_session_key``) because the
    extraction autostart needs a different slice. Once Epic 04 lands the
    full :class:`LcmDependencies` dataclass, this Protocol can be a
    structural subset of it.
    """

    @property
    def complete(self) -> LlmCompleteFn | None: ...


@dataclass(frozen=True)
class ExtractionTickResult:
    """Per-tick result returned by ``tick_fn``.

    Mirrors the TS ``CoreferenceTickResult & { lockAcquired: boolean }``
    intersection type at ``extraction-autostart.ts:129``. The orchestrator
    (Epic 08 — :func:`tick_extraction`) wraps the bare
    :class:`~lossless_hermes.extraction.coreference.CoreferenceTickResult`
    with the ``lock_acquired`` boolean and surfaces it here. Until the
    orchestrator ports, tests supply this dataclass directly.

    Attributes:
        lock_acquired: ``True`` if the orchestrator's cross-process worker
            lock was held for the entire tick. ``False`` if the initial
            acquire failed (sibling gateway holds the lock) OR the
            heartbeat was lost mid-tick (Wave-7 sets this to False inside
            the orchestrator when the heartbeat check returns False).
        tick_result: The :class:`CoreferenceTickResult` from the underlying
            :func:`~lossless_hermes.extraction.coreference.run_coreference_tick`.
            ``None`` when ``lock_acquired=False`` because no work was
            attempted.
    """

    lock_acquired: bool
    tick_result: CoreferenceTickResult | None = None


#: The user-supplied per-tick callable. The Python signature mirrors the
#: TS ``tickExtraction(db, opts)`` shape but accepts a bound closure so
#: callers can pre-bind ``extractor``, ``passId``, ``perTickLimit``, etc.
#: Why no-args: the autostart layer doesn't know about the extractor or
#: per-tick limits; the caller (Epic 02 engine lifecycle or Epic 08
#: ``/lcm worker`` boot) binds those into the closure once.
#:
#: The callable returns :class:`ExtractionTickResult`. Exceptions
#: propagate up; the autostart's outer try/except counts them as
#: consecutive-failure strikes.
ExtractionTickFn = Callable[[], Awaitable[ExtractionTickResult]]


# ---------------------------------------------------------------------------
# Public handle (port of ``extraction-autostart.ts:60-70``)
# ---------------------------------------------------------------------------


@dataclass
class ExtractionAutostartHandle:
    """Handle returned by :func:`try_start_extraction_autostart`.

    Ports ``extraction-autostart.ts:60-70`` ``ExtractionAutostartHandle``.
    Callers use this to (a) stop the loop on gateway shutdown via
    :meth:`stop` and (b) introspect tick state via :attr:`tick_count` and
    :attr:`consecutive_failures` for ``/lcm health`` and tests.

    Mutable so the per-tick callback can record state in-place (each
    field is updated under the asyncio event loop's single-threaded
    invariant — no locks needed).
    """

    #: How many ticks have completed since :func:`try_start_extraction_autostart`.
    #: Matches TS ``totalTicks`` at line 103.
    tick_count: int = 0

    #: Count of consecutive ticks that raised an exception OR reported
    #: an outer-body failure. Stops the loop when this hits
    #: :data:`_FAILURE_STRIKE_THRESHOLD`. Reset to 0 on any clean tick.
    #: Matches TS ``consecutiveFailures`` at line 102.
    consecutive_failures: int = 0

    #: Count of consecutive idle ticks (queue empty at tick start). The
    #: first idle tick logs info; subsequent idle ticks are silent. Unlike
    #: embedding-backfill, this does NOT stop the loop — new leaves can
    #: be queued any time and the count probe is cheap.
    #: Matches TS ``consecutiveIdleTicks`` at line 101.
    consecutive_idle_ticks: int = 0

    #: The underlying :class:`WorkerLoop`. ``None`` for the no-op handle
    #: (gating failed — ``LCM_EXTRACTION_LLM_ENABLED=false`` OR
    #: ``deps.complete`` was None). Tests inspect this to verify gating
    #: short-circuited cleanly.
    _loop: WorkerLoop | None = field(default=None, repr=False)

    #: Flag set when ``stop()`` runs to make subsequent calls idempotent.
    _stopped: bool = field(default=False, repr=False)

    #: Initial-delay task. We use the WorkerLoop's regular interval
    #: mechanism for the 10s startup delay — by scheduling the first job
    #: tick after a 10s sleep we get the same effect without a separate
    #: asyncio task. This field is for parity with the TS source's
    #: ``initialDelay`` timer ref; kept None on the Python port because
    #: the WorkerLoop handles the cadence.
    _initial_delay_task: asyncio.Task[None] | None = field(default=None, repr=False)

    def is_running(self) -> bool:
        """Return whether the autostart is currently scheduling ticks.

        ``False`` if either (a) the handle is the no-op gating handle, or
        (b) ``stop()`` has been called, or (c) the underlying
        :class:`WorkerLoop` finished on its own (3-strike failure stop).
        """
        if self._loop is None or self._stopped:
            return False
        return self._loop.is_running()

    async def stop(self, *, graceful_timeout_s: float = 30.0) -> None:
        """Stop the autostart loop. Idempotent.

        Ports ``extraction-autostart.ts:202-209`` ``handle.stop``. Calls
        :meth:`WorkerLoop.stop` on the underlying loop and marks the
        handle stopped so :meth:`is_running` returns ``False`` from this
        point. For the no-op handle (gating failed) this is a no-op.

        Args:
            graceful_timeout_s: Forwarded to :meth:`WorkerLoop.stop`.
                Default 30 s (matches the TS source — see
                ``backfill-autostart.ts:258`` clearTimeout pattern).
        """
        if self._stopped:
            return
        self._stopped = True
        if self._initial_delay_task is not None and not self._initial_delay_task.done():
            self._initial_delay_task.cancel()
        if self._loop is not None:
            await self._loop.stop(graceful_timeout_s=graceful_timeout_s)


# ---------------------------------------------------------------------------
# Internal helper — schedule the WorkerLoop.stop() coroutine without awaiting.
# ---------------------------------------------------------------------------


def _schedule_loop_stop(handle: ExtractionAutostartHandle) -> None:
    """Schedule :meth:`WorkerLoop.stop` on the running event loop.

    Called from inside a tick when the 3-strike failure threshold trips.
    Avoids awaiting because the caller is the in-flight tick itself —
    awaiting the loop's stop would deadlock (``WorkerLoop.stop`` waits
    on the in-flight ticks to drain). Fire-and-forget is safe: the
    loop's per-job task is in its ``asyncio.sleep`` cycle and will see
    ``_running=False`` on its next wake-up.

    Mirrors :func:`~lossless_hermes.operator.backfill_autostart._schedule_loop_stop`.
    Kept private to this module so the two autostarts evolve independently.
    """
    if handle._loop is None:
        return
    asyncio.create_task(handle._loop.stop())


# ---------------------------------------------------------------------------
# Public entry point (port of ``extraction-autostart.ts:72-214``)
# ---------------------------------------------------------------------------


def try_start_extraction_autostart(
    db: sqlite3.Connection,
    *,
    log: ExtractionAutostartLogger,
    deps: ExtractionAutostartDeps,
    interval_s: float = DEFAULT_EXTRACTION_INTERVAL_S,
    env: Mapping[str, str] | None = None,
    extractor_fn: ExtractEntitiesFn | None = None,
    tick_fn: ExtractionTickFn | None = None,
) -> ExtractionAutostartHandle:
    """Try to start the extraction autostart loop.

    Ports ``extraction-autostart.ts:72-214`` ``tryStartExtractionAutostart``
    to Python. The function performs two pre-flight checks and returns a
    no-op :class:`ExtractionAutostartHandle` if either fails (silent
    gating — the operator gets a single info-level log line explaining
    the reason).

    Pre-flight order (matches TS lines 79-94):

    1. ``LCM_EXTRACTION_LLM_ENABLED`` env var is NOT set to ``"false"``
       (default ON — extraction is intrinsic to v4.1, not opt-in like
       embeddings which costs Voyage tokens).
    2. ``deps.complete`` is a callable (gateway has at least one LLM
       provider configured). If ``None``, returns a no-op handle.

    On success:

    * Constructs the entity extractor via
      :func:`~lossless_hermes.extraction.extractor.create_entity_extractor_llm`
      (or accepts an explicit ``extractor_fn`` override for tests).
    * Creates a :class:`WorkerLoop` with a single registered
      :class:`WorkerJob` of kind ``"extraction"``, ``interval_s=interval_s``,
      ``run=`` an internal closure that wraps ``tick_fn`` with the
      consecutive-failure post-processing.
    * Starts the loop and returns an :class:`ExtractionAutostartHandle`
      whose :meth:`is_running` returns ``True``.

    The internal closure handles three result modes (matches TS lines
    117-191):

    * **Idle** (``tick_result.processed_count == 0`` with no items
      drained, or the underlying queue had no pending items at tick start):
      bumps :attr:`consecutive_idle_ticks`. First idle logs info;
      subsequent idle ticks are silent. Does NOT stop the loop.

    * **Lock not acquired** (``lock_acquired=False``): logs info + skips.
      Does NOT increment :attr:`consecutive_failures`. The TS source
      treats this as a no-op tick that "wasn't ours to do" — a sibling
      gateway is holding the lock. Idle counter is NOT reset because no
      work was actually attempted.

    * **Tick throws / outer-body throws**: bumps
      :attr:`consecutive_failures`. After :data:`_FAILURE_STRIKE_THRESHOLD`
      consecutive failures, the loop stops and a clear "require gateway
      restart" message is logged. Per-extractor failures (where
      :attr:`CoreferenceTickResult.extractor_failures` > 0 but the tick
      itself returned normally) do NOT increment this counter — the
      queue items just aren't marked completed so they retry next tick
      (Wave-4 dead-letter handles persistent failures via the
      ``attempts < MAX_ATTEMPTS`` gate).

    * **Clean tick** (``lock_acquired=True``, ``tick_result.processed_count > 0``):
      resets :attr:`consecutive_failures` and :attr:`consecutive_idle_ticks`
      to 0. Logs an info line with the per-tick counters.

    Initial delay: the first tick fires after :data:`STARTUP_DELAY_S`
    seconds (default 10s) — implemented via an asyncio task that sleeps
    then triggers an immediate :meth:`WorkerLoop.run_once`. After the
    initial run, the :class:`WorkerLoop` resumes its regular cadence.

    Args:
        db: Open :class:`sqlite3.Connection`. Currently unused by this
            module's body — the per-tick callable closes over its own
            DB connection. Kept in the signature for parity with the TS
            source and the backfill-autostart sibling, and to leave room
            for future direct-SQL probes (e.g. inline
            :func:`count_pending_extractions` calls if the porting
            guide's "cheap polling" optimization moves here).
        log: Caller's logger (see :class:`ExtractionAutostartLogger`).
        deps: Narrow LcmDependencies slice (see
            :class:`ExtractionAutostartDeps`). Only ``complete`` is
            consulted for the pre-flight gate.
        interval_s: Cadence in seconds. Default
            :data:`DEFAULT_EXTRACTION_INTERVAL_S` (60 s). Tests pass a
            small value (e.g. ``0.05``) to exercise the failure logic
            quickly. Note that the WorkerLoop uses ``asyncio.sleep``
            BEFORE the first tick, so the first tick is fired manually
            here after :data:`STARTUP_DELAY_S` (which is also reduced in
            tests via the ``startup_delay_s`` argument — TODO future).
        env: Override environment for the ``LCM_EXTRACTION_LLM_ENABLED``
            gating check. Default :func:`os.environ`. Tests pass
            ``{"LCM_EXTRACTION_LLM_ENABLED": "false"}`` to verify the
            silent no-op gating path.
        extractor_fn: Optional override for the entity extractor. When
            ``None`` (the default), the autostart constructs one via
            :func:`create_entity_extractor_llm` over ``deps.complete``.
            Tests inject a deterministic fake to avoid LLM calls.
        tick_fn: Optional override for the per-tick callable. When
            ``None`` (the default), this module would call
            :func:`~lossless_hermes.operator.worker_orchestrator.tick_extraction`
            from Epic 08 — but that orchestrator isn't ported yet. Until
            it lands, callers MUST pass ``tick_fn`` explicitly. The
            callable's body should:

            1. Acquire the ``lcm_worker_lock`` row for kind ``"extraction"``
               (single-flight across processes; see issue 05-06).
            2. Call :func:`run_coreference_tick` with the bound extractor.
            3. Release the lock (try/finally).
            4. Return an :class:`ExtractionTickResult` wrapping the
               :class:`CoreferenceTickResult` + the ``lock_acquired``
               flag.

            Passing ``tick_fn=None`` returns a no-op handle and logs an
            error (better than crashing the engine at boot).

    Returns:
        An :class:`ExtractionAutostartHandle`. ``is_running()`` is
        ``True`` if the autostart loop was started; ``False`` if gating
        short-circuited (no-op handle).
    """
    resolved_env: Mapping[str, str] = env if env is not None else os.environ

    # Pre-flight 1: ``LCM_EXTRACTION_LLM_ENABLED`` not set to "false"
    # (matches TS lines 79-85). Default is ON — extraction is intrinsic
    # to v4.1, not opt-in like embedding which costs Voyage tokens.
    flag = resolved_env.get("LCM_EXTRACTION_LLM_ENABLED", "").strip().lower()
    if flag == "false":
        log.info(
            "[extraction-autostart] disabled via LCM_EXTRACTION_LLM_ENABLED=false. "
            "Extraction queue will accumulate; manually drain via the "
            "run_coreference_tick service."
        )
        return ExtractionAutostartHandle()

    # Pre-flight 2: ``deps.complete`` is callable (matches TS lines 87-93).
    # If the gateway has no LLM provider configured, extraction can't run.
    # We log info-level (NOT warn) per the spec's "log info" requirement
    # — this is an expected configuration state, not an error.
    complete = deps.complete
    if complete is None or not callable(complete):
        log.info(
            "extraction_autostart_skipped: no_llm_client (deps.complete not "
            "available — gateway must be configured with at least one LLM "
            "provider). Disabling extraction autostart."
        )
        return ExtractionAutostartHandle()

    # Pre-flight 3: tick_fn supplied (integration-side programmer-error
    # guard — TS gets this for free because ``tickExtraction`` is the
    # default; Python's port doesn't have a worker-orchestrator yet, so
    # the caller MUST wire one in via ``tick_fn``). Same pattern as
    # backfill_autostart line 394-400.
    if tick_fn is None:
        log.error(
            "[extraction-autostart] tick_fn is None — integration bug. The "
            "caller (Epic 02 engine lifecycle or Epic 08 /lcm worker boot) "
            "must bind a tick_extraction closure before invoking this "
            "function. Until worker_orchestrator.tick_extraction is ported "
            "(Epic 08), this autostart cannot self-default."
        )
        return ExtractionAutostartHandle()

    # Construct (or accept override of) the entity extractor. The TS source
    # at line 100 does this once at autostart-start; we do the same so the
    # extractor's per-call closure (model resolution, fence-token logic) is
    # warm. Tests override with deterministic fakes to avoid LLM calls.
    extractor: ExtractEntitiesFn
    if extractor_fn is not None:
        extractor = extractor_fn
    else:
        # Bind the extractor over ``deps.complete``. The ``complete`` callable
        # is the LlmCompleteFn from Epic 04; the extractor's per-call body
        # invokes it with the prompt + model config.
        extractor = create_entity_extractor_llm(llm_complete=complete)

    log.info(
        f"[extraction-autostart] starting (interval={interval_s}s, "
        f"startup_delay={STARTUP_DELAY_S}s, perTickLimit=50)"
    )

    handle = ExtractionAutostartHandle()
    # The closure below mutates ``handle``; this works because each tick
    # runs on the asyncio event loop's single-threaded scheduler (no
    # locks needed).
    on_complete_log = log
    handle_ref = handle
    inner_tick = tick_fn

    async def _run_one_tick() -> ExtractionTickResult | None:
        """Run a single extraction tick + post-process the strike counters.

        Returns the :class:`ExtractionTickResult` on success, or ``None``
        on failure (so the worker-loop's ``on_job_complete`` sees a clean
        result/error split). The autostart's bookkeeping (consecutive
        failures, idle counters) lives in this closure, NOT in the
        per-job-complete callback — by the time the callback runs the
        tick is fully resolved and we want the bookkeeping to influence
        the next iteration synchronously.

        LCM v4.1 Final.review.3 fix (Loop 9 B2 HIGH): the outer try/catch
        wraps the ENTIRE tick body, not just the ``inner_tick`` call.
        Without this, any throw before line N (e.g. ``deps.complete``
        becoming None during shutdown, log calls themselves throwing)
        becomes an unhandled promise rejection from the worker-loop's
        ``await job.run()``. Backfill-autostart already had this pattern;
        extraction was modeled on backfill but lost the outer catch in
        cycle-2.
        Original: ``lossless-claw/src/operator/extraction-autostart.ts:117-191``.
        """
        # Early-return guard: stop() may have been scheduled by a prior
        # tick (3-strike failure) but the worker loop's next
        # ``asyncio.sleep`` may have raced ahead of the stop-task.
        # Short-circuit cleanly so no further state mutates after the
        # terminal log message has fired.
        if handle_ref._stopped:
            return None
        try:
            # Inner try: catches exceptions from ``inner_tick`` itself.
            try:
                result = await inner_tick()
            except BaseException as exc:  # noqa: BLE001 — strike-bookkeeping path
                # Inner-tick throw counts as a consecutive failure (Wave-1
                # Auditor #6 finding #4 → use orchestrator; here we just
                # count the throws that escaped it).
                # Original: ``extraction-autostart.ts:142-154``.
                handle_ref.consecutive_failures += 1
                exc_msg = str(exc) if not isinstance(exc, str) else exc
                on_complete_log.error(
                    f"[extraction-autostart] tick threw "
                    f"(consecutive={handle_ref.consecutive_failures}): {exc_msg}"
                )
                if handle_ref.consecutive_failures >= _FAILURE_STRIKE_THRESHOLD:
                    on_complete_log.error(
                        f"[extraction-autostart] {_FAILURE_STRIKE_THRESHOLD} "
                        "consecutive failures — stopping. Inspect /lcm health "
                        "worker status; restart gateway after fixing the "
                        "underlying issue."
                    )
                    handle_ref._stopped = True
                    _schedule_loop_stop(handle_ref)
                return None

            # Lock-skip path: sibling gateway holds the lock. Log + skip.
            # Do NOT bump consecutive_failures (lock contention isn't a
            # failure — Wave-7 specifically split this out so the autostart
            # doesn't stop just because another worker is running).
            # Original: ``extraction-autostart.ts:157-162``.
            if not result.lock_acquired:
                on_complete_log.info(
                    "[extraction-autostart] lock held by another worker; skipping this tick."
                )
                return result

            # We had the lock and the tick ran. Bump the tick count.
            handle_ref.tick_count += 1
            tick_result = result.tick_result
            # If lock_acquired=True the orchestrator MUST also have
            # surfaced a CoreferenceTickResult — defensive None-check so a
            # malformed tick_fn doesn't crash the bookkeeping.
            if tick_result is None:
                # Malformed result — treat as a tick failure. This is a
                # programmer-error path (the orchestrator contract is
                # "lock_acquired=True → tick_result not None"), but we
                # surface it via the strike counter rather than crashing.
                handle_ref.consecutive_failures += 1
                on_complete_log.error(
                    f"[extraction-autostart] tick {handle_ref.tick_count} "
                    f"returned lock_acquired=True but tick_result=None "
                    f"(consecutive={handle_ref.consecutive_failures}) — "
                    "orchestrator contract violation."
                )
                if handle_ref.consecutive_failures >= _FAILURE_STRIKE_THRESHOLD:
                    handle_ref._stopped = True
                    _schedule_loop_stop(handle_ref)
                return result

            # Idle path: queue had no pending items at tick start
            # (processed_count == 0). Bump idle counter; first idle logs
            # info, subsequent idle ticks are silent.
            # Original: ``extraction-autostart.ts:118-125`` — but note that
            # the TS source logs on the FIRST idle and breaks out via
            # ``return``; we do the same here.
            if tick_result.processed_count == 0:
                handle_ref.consecutive_idle_ticks += 1
                if handle_ref.consecutive_idle_ticks == 1:
                    on_complete_log.info("[extraction-autostart] queue empty; idle.")
                # Per-tick extractor failures aren't fatal — the queue
                # items just aren't marked completed so they retry next
                # tick. Only count tick-level throws as "consecutive
                # failures" (Wave-4 dead-letter handles persistent
                # extractor failures via attempts < MAX_ATTEMPTS).
                handle_ref.consecutive_failures = 0
                return result

            # Non-idle clean tick: reset both counters + log progress.
            # Original: ``extraction-autostart.ts:164-174``.
            handle_ref.consecutive_idle_ticks = 0
            handle_ref.consecutive_failures = 0
            on_complete_log.info(
                f"[extraction-autostart] tick {handle_ref.tick_count} "
                f"processed={tick_result.processed_count} "
                f"entities={tick_result.new_entities} "
                f"mentions={tick_result.new_mentions} "
                f"extractor-failures={tick_result.extractor_failures} "
                f"lock-lost-mid-tick={tick_result.lock_lost_mid_tick}"
            )
            return result
        except BaseException as outer_exc:  # noqa: BLE001 — outer catch (Loop 9 B2)
            # Outer catch — anything before/after the inner_tick call
            # (count_pending probe, log calls themselves, etc.) doesn't
            # escape. This is the LCM v4.1 Final.review.3 Loop-9 B2 HIGH
            # fix: extraction was modeled on backfill but lost the outer
            # try/catch in cycle-2.
            # Original: ``lossless-claw/src/operator/extraction-autostart.ts:175-188``.
            handle_ref.consecutive_failures += 1
            exc_msg = str(outer_exc) if not isinstance(outer_exc, str) else outer_exc
            on_complete_log.error(
                f"[extraction-autostart] outer tick body threw "
                f"(consecutive={handle_ref.consecutive_failures}): {exc_msg}"
            )
            if handle_ref.consecutive_failures >= _FAILURE_STRIKE_THRESHOLD:
                on_complete_log.error(
                    f"[extraction-autostart] {_FAILURE_STRIKE_THRESHOLD} "
                    "consecutive outer-tick failures — stopping. Likely "
                    "gateway shutdown closed the DB mid-tick; restart "
                    "gateway after diagnosing."
                )
                handle_ref._stopped = True
                _schedule_loop_stop(handle_ref)
            return None

    def _on_complete(info: JobCompleteInfo) -> None:  # noqa: ARG001 — telemetry hook stub
        """Receive per-tick telemetry from the worker loop.

        The autostart's strike-bookkeeping lives inside ``_run_one_tick``
        (the closure passed to :class:`WorkerJob`), not here. Same
        pattern as :func:`~lossless_hermes.operator.backfill_autostart.start_embedding_backfill_autostart`.
        """
        return

    loop = WorkerLoop(
        jobs=[
            WorkerJob(
                kind="extraction",
                interval_s=interval_s,
                run=_run_one_tick,
            )
        ],
        on_job_complete=_on_complete,
    )
    handle._loop = loop
    loop.start()

    # LCM (TS source line 194-196): schedule the first tick after a 10s
    # initial delay. The WorkerLoop's normal cadence sleeps ``interval_s``
    # BEFORE the first tick (per worker_loop.py:411 ``await asyncio.sleep``
    # at top of _run_job), but the TS source uses ``setTimeout(10_000)``
    # then ``setInterval(intervalMs)``. We approximate by spawning an
    # asyncio task that sleeps STARTUP_DELAY_S then triggers one
    # ``run_once`` — after which the WorkerLoop continues its cadence.
    #
    # Note: for tests that want a small startup delay, this would need to
    # be parameterized. The spec mandates 10s as a constant; tests rely on
    # the regular interval (e.g. interval_s=0.05) to exercise the loop
    # rather than waiting 10s.
    async def _trigger_initial_tick() -> None:
        try:
            await asyncio.sleep(STARTUP_DELAY_S)
        except asyncio.CancelledError:
            return
        if handle_ref._stopped:
            return
        # Use run_once so the in-flight bookkeeping in WorkerLoop tracks
        # the manual tick the same as a scheduled one. If a scheduled
        # tick has already fired by now (e.g. interval_s < STARTUP_DELAY_S
        # in tests), run_once raises RuntimeError — catch + ignore.
        try:
            await loop.run_once("extraction")
        except RuntimeError:
            # Already in flight from the scheduled cadence — fine.
            pass
        except BaseException:  # noqa: BLE001 — already counted as a strike
            # The _run_one_tick closure already dispatched the error and
            # incremented consecutive_failures. We don't want to crash
            # the initial-delay task on top of that.
            pass

    handle._initial_delay_task = asyncio.create_task(
        _trigger_initial_tick(), name="extraction-autostart-initial-delay"
    )

    return handle


# ---------------------------------------------------------------------------
# Convenience for type-checker pinning (mirrors backfill_autostart).
# ---------------------------------------------------------------------------


# Awaitable-Callable convenience alias pinned for ty. Kept private so it
# doesn't widen the module's public surface.
_AwaitableTick = Callable[..., Awaitable[Any]]
