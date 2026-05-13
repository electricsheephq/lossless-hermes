"""Backfill auto-start — LCM v4.1 Wire-2 + issue 05-11.

Ports ``lossless-claw/src/operator/backfill-autostart.ts`` (LCM commit
``1f07fbd`` on branch ``pr-613``, 264 LOC TS → ~280 LOC Python).

Plugin lifecycle hook that auto-runs the embedding-backfill cron in the
background until the corpus is fully embedded. **Operator opt-in:** the
presence of a non-empty ``VOYAGE_API_KEY`` env var. Without it, this
module is a silent no-op (returns a stub :class:`AutostartHandle` whose
``stop()`` is a no-op).

Once started, runs a tick every :data:`DEFAULT_AUTOSTART_INTERVAL_S`
(default 5 minutes). Each tick processes up to ``per_tick_limit=200``
docs inside :func:`~lossless_hermes.embeddings.backfill.tick_embedding_backfill`
(issue 05-07). Stops automatically when:

* :attr:`BackfillResult.embedded_count` ``== 0`` and
  :attr:`BackfillResult.skipped` ``== []`` and ``count_pending_docs == 0``
  for 3 consecutive ticks (idle drain — corpus fully embedded).
* The tick raises a non-auth exception 3 consecutive times, OR returns a
  :class:`BackfillResult` with ≥ 1 ``voyage_5xx`` / ``voyage_other`` skip
  3 consecutive times (Voyage-failure backoff — operator must intervene).
* A :class:`VoyageError` with ``kind="auth"`` propagates out of the tick
  (immediate stop; not subject to the 3-strike threshold).

Why auto-opt-in instead of always-on:

* Costs Voyage tokens (~$1 for Eva's 4187-leaf corpus first run).
* Operators in dev environments may not want background API calls.
* ``VOYAGE_API_KEY`` presence is a clear "I want this" signal that
  matches the TS canonical (per the TS module docstring).

NOT auto-started (per the TS source docstring — deferred to Epic 07
cycle-2):

* Entity coreference (needs LLM injection through plugin lifecycle).
* Procedure mining.
* Themes consolidation.

Manual ``/lcm worker tick embedding-backfill`` still works (Epic 08
surfaces this). Autostart just makes it unnecessary in the typical case.

### Wiring (Epic 02 / Epic 08 — out of scope for issue 05-11)

This module exposes :func:`start_embedding_backfill_autostart` as a
standalone callable. The decision on *where* to call it (e.g. inside
:class:`LCMEngine.on_session_start` for the first session, or as part of
``/lcm worker`` boot) is left to Epic 02. Callers are responsible for
constructing the per-tick callable (typically a closure over a long-lived
:class:`~lossless_hermes.voyage.client.VoyageClient` and the active
embedding profile) and passing it as the ``tick_fn`` argument.

See:

* ``epics/05-embeddings/05-11-autostart-wiring.md`` — full spec.
* ``lossless-claw/src/operator/backfill-autostart.ts`` — TS source.
* ``src/lossless_hermes/embeddings/backfill.py`` — the
  :func:`~lossless_hermes.embeddings.backfill.tick_embedding_backfill`
  invoked by every tick.
* ``src/lossless_hermes/concurrency/worker_loop.py`` — the
  :class:`~lossless_hermes.concurrency.worker_loop.WorkerLoop` that
  provides the cadence engine for this autostart loop.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Final, Protocol

from lossless_hermes.concurrency.worker_loop import (
    JobCompleteInfo,
    WorkerJob,
    WorkerLoop,
)
from lossless_hermes.embeddings.backfill import (
    BackfillResult,
    count_pending_docs,
)
from lossless_hermes.embeddings.store import (
    EmbeddedKind,
    embeddings_table_exists,
)
from lossless_hermes.voyage.client import VoyageError

__all__ = [
    "DEFAULT_AUTOSTART_INTERVAL_S",
    "AutostartHandle",
    "AutostartLogger",
    "start_embedding_backfill_autostart",
]


# ---------------------------------------------------------------------------
# Constants (verbatim port of ``backfill-autostart.ts:42``)
# ---------------------------------------------------------------------------

#: Default cadence in seconds. Matches TS ``DEFAULT_AUTOSTART_INTERVAL_MS = 5 * 60 * 1000``.
DEFAULT_AUTOSTART_INTERVAL_S: Final[float] = 5 * 60.0

#: Idle-drain threshold. After this many consecutive ticks return no
#: progress AND ``count_pending_docs == 0``, the autostart loop stops
#: (corpus is fully embedded). Matches TS line 161.
_IDLE_STRIKE_THRESHOLD: Final[int] = 3

#: Voyage-failure backoff threshold. After this many consecutive ticks
#: raise a non-auth exception OR return ≥ 1 ``voyage_*`` skip, the
#: autostart loop stops (operator intervention required). Matches TS
#: lines 218 + 232.
_VOYAGE_FAILURE_STRIKE_THRESHOLD: Final[int] = 3


# ---------------------------------------------------------------------------
# Logger protocol (mirrors ``backfill-autostart.ts:45-49``)
# ---------------------------------------------------------------------------


class AutostartLogger(Protocol):
    """Caller-supplied logger interface.

    Mirrors ``backfill-autostart.ts:45-49`` ``AutostartLogger``. The
    autostart module never holds a global logger — callers (engine,
    plugin lifecycle) pass their own so log routing is decided at the
    edge.

    Implementers must accept :class:`str` messages on ``info`` / ``warn``
    / ``error``. Exceptions raised from a logger call are caught and
    dropped at the autostart boundary (a broken logger must not crash
    the autostart loop).
    """

    def info(self, msg: str) -> None: ...

    def warn(self, msg: str) -> None: ...

    def error(self, msg: str) -> None: ...


# ---------------------------------------------------------------------------
# Public handle (port of ``backfill-autostart.ts:77-84``)
# ---------------------------------------------------------------------------


@dataclass
class AutostartHandle:
    """Handle returned by :func:`start_embedding_backfill_autostart`.

    Ports ``backfill-autostart.ts:77-84`` ``AutostartHandle``. Callers use
    this to (a) stop the loop on gateway shutdown via :meth:`stop` and
    (b) introspect tick state via :attr:`tick_count`, :attr:`idle_strikes`,
    and :attr:`voyage_failure_strikes` for ``/lcm health`` and tests.

    Mutable so the per-tick callback can record state in-place (each
    field is updated under the asyncio event loop's single-threaded
    invariant — no locks needed).
    """

    #: How many ticks have completed since :func:`start_embedding_backfill_autostart`.
    tick_count: int = 0

    #: Count of consecutive ticks that reported no progress AND no
    #: pending docs. Stops the loop when this hits :data:`_IDLE_STRIKE_THRESHOLD`.
    idle_strikes: int = 0

    #: Count of consecutive ticks that raised a non-auth exception OR
    #: returned ≥ 1 ``voyage_*`` skip. Stops the loop when this hits
    #: :data:`_VOYAGE_FAILURE_STRIKE_THRESHOLD`.
    voyage_failure_strikes: int = 0

    #: The underlying :class:`WorkerLoop`. ``None`` for the no-op handle
    #: (gating failed — ``VOYAGE_API_KEY`` missing, vec0 not loaded, or
    #: no active profile). Tests inspect this to verify gating short-
    #: circuited cleanly.
    _loop: WorkerLoop | None = field(default=None, repr=False)

    #: Flag set when ``stop()`` runs to make subsequent calls idempotent.
    _stopped: bool = field(default=False, repr=False)

    def is_running(self) -> bool:
        """Return whether the autostart is currently scheduling ticks.

        ``False`` if either (a) the handle is the no-op gating handle, or
        (b) ``stop()`` has been called, or (c) the underlying
        :class:`WorkerLoop` finished on its own (idle drain / failure
        backoff stops it from within the per-tick callback).
        """
        if self._loop is None or self._stopped:
            return False
        return self._loop.is_running()

    async def stop(self, *, graceful_timeout_s: float = 30.0) -> None:
        """Stop the autostart loop. Idempotent.

        Ports ``backfill-autostart.ts:253-259`` ``handle.stop``. Calls
        :meth:`WorkerLoop.stop` on the underlying loop and marks the
        handle stopped so :meth:`is_running` returns ``False`` from this
        point. For the no-op handle (gating failed) this is a no-op.

        Args:
            graceful_timeout_s: Forwarded to :meth:`WorkerLoop.stop`.
                Default 30 s (matches the TS source — see
                ``backfill-autostart.ts:258`` clearTimeout).
        """
        if self._stopped:
            return
        self._stopped = True
        if self._loop is not None:
            await self._loop.stop(graceful_timeout_s=graceful_timeout_s)


# ---------------------------------------------------------------------------
# Per-tick callable type (matches ``backfill-autostart.ts:60-65``)
# ---------------------------------------------------------------------------


#: The user-supplied per-tick callable. Returns a :class:`BackfillResult`
#: or raises. The autostart loop wraps invocation in its own try/except
#: so exceptions propagate as Voyage-failure strikes (or, if
#: :class:`VoyageError(kind="auth")`, as an immediate stop).
#:
#: Why no-args: in the TS source the tick takes ``(db, TickArgs)`` but the
#: Python equivalent has too many keyword arguments (``voyage`` instance,
#: ``model_name``, ``input_type``, ``voyage_max_retries``, etc.). Asking
#: callers to bind those into a closure once is cleaner than passing a
#: large blob through the autostart layer; also matches the Python
#: :class:`WorkerJob.run` shape from issue 05-05.
TickFn = Callable[[], Awaitable[BackfillResult]]


# ---------------------------------------------------------------------------
# Internal helper — schedule the WorkerLoop.stop() coroutine without awaiting.
# ---------------------------------------------------------------------------


def _schedule_loop_stop(handle: AutostartHandle) -> None:
    """Schedule :meth:`WorkerLoop.stop` on the running event loop.

    Called from inside a tick when a stop condition is reached (auth
    error, idle drain, 3-strike failure). Avoids awaiting because the
    caller is the in-flight tick itself — awaiting the loop's stop
    would deadlock (``WorkerLoop.stop`` waits on the in-flight ticks
    to drain). Fire-and-forget is safe: the loop's per-job task is in
    its ``asyncio.sleep`` cycle and will see ``_running=False`` on its
    next wake-up.

    No-op if the handle has no underlying loop (gating path returned
    early; the no-op handle shouldn't be in a position to call this).
    """
    if handle._loop is None:
        return
    asyncio.create_task(handle._loop.stop())


# ---------------------------------------------------------------------------
# Public entry point (port of ``backfill-autostart.ts:104-264``)
# ---------------------------------------------------------------------------


def start_embedding_backfill_autostart(
    db: sqlite3.Connection,
    *,
    log: AutostartLogger,
    tick_fn: TickFn | None,
    interval_s: float = DEFAULT_AUTOSTART_INTERVAL_S,
    env: Mapping[str, str] | None = None,
    model_name: str | None = None,
    embedded_kind: EmbeddedKind = "summary",
) -> AutostartHandle:
    """Try to start the backfill autostart loop.

    Ports ``backfill-autostart.ts:104-264`` ``tryStartBackfillAutostart`` to
    Python. The function performs three pre-flight checks and returns a
    no-op :class:`AutostartHandle` if any fail (silent gating — the
    operator gets a single info-level log line explaining the reason).

    Pre-flight order (matches TS lines 111-134):

    1. ``VOYAGE_API_KEY`` is present and non-empty in ``env`` (defaults to
       :func:`os.environ`).
    2. The per-model vec0 table exists for ``model_name`` (defaults to
       resolving the active profile via the caller-supplied ``model_name``
       argument; until issue 05-08 ports ``getActiveEmbeddingModel``, the
       caller MUST pass ``model_name`` explicitly).
    3. ``tick_fn`` is supplied. If ``None`` is supplied (programmer error
       in the integration code), the function logs an error and returns a
       no-op handle — better than crashing the engine at boot.

    On success:

    * Creates a :class:`WorkerLoop` with a single registered
      :class:`WorkerJob` of kind ``"embedding-backfill"``,
      ``interval_s=interval_s``, ``run=`` an internal closure that wraps
      :paramref:`tick_fn` with the strike-counter post-processing.
    * Starts the loop and returns an :class:`AutostartHandle` whose
      :meth:`AutostartHandle.is_running` returns ``True``.

    The internal closure handles three failure modes:

    * **Auth error** (:class:`VoyageError` with ``kind="auth"``) — stops
      the autostart loop immediately (not subject to 3-strike); logs
      an actionable error message.
    * **Other exception or skipped Voyage 5xx/other** — increments
      :attr:`AutostartHandle.voyage_failure_strikes`. After
      :data:`_VOYAGE_FAILURE_STRIKE_THRESHOLD` consecutive failures, the
      loop stops and a clear "back off; manual intervention" message is
      logged.
    * **Idle tick** (``embedded_count == 0 AND skipped == [] AND
      count_pending_docs == 0``) — increments
      :attr:`AutostartHandle.idle_strikes`. After
      :data:`_IDLE_STRIKE_THRESHOLD` consecutive idle ticks the loop
      stops cleanly (corpus is fully embedded).

    Both strike counters reset to 0 on the FIRST opposing tick (e.g. a
    clean tick resets ``voyage_failure_strikes``; a non-idle tick resets
    ``idle_strikes``).

    Args:
        db: Open :class:`sqlite3.Connection`. Used only for the
            :func:`~lossless_hermes.embeddings.backfill.count_pending_docs`
            check inside the strike-evaluation logic. The connection must
            survive for the lifetime of the autostart loop — closing it
            mid-tick will surface as a Voyage-failure strike via the
            sqlite3 exception path.
        log: Caller's logger (see :class:`AutostartLogger`).
        tick_fn: Async no-arg callable that runs one backfill tick and
            returns a :class:`BackfillResult`. Callers (Epic 02 / Epic 08)
            bind this closure once at boot — typically over a long-lived
            :class:`~lossless_hermes.voyage.client.VoyageClient`, the
            active embedding profile, and the per-tick caps (token limit,
            etc.). Tests inject a mock that returns canned results.
        interval_s: Cadence in seconds. Default
            :data:`DEFAULT_AUTOSTART_INTERVAL_S` (5 min). Tests pass a
            small value (e.g. ``0.05``) to exercise the strike logic
            quickly.
        env: Override environment for the ``VOYAGE_API_KEY`` gating
            check. Default :func:`os.environ`. Tests pass ``{}`` to
            verify the silent no-op gating path, and pass
            ``{"VOYAGE_API_KEY": "test"}`` to verify the start path.
        model_name: Embedding model name to use when querying
            :func:`count_pending_docs` AND for the
            :func:`embeddings_table_exists` pre-flight check. Until
            issue 05-08 ports ``getActiveEmbeddingModel``, the caller
            MUST pass this explicitly; future-port wiring will resolve
            it from ``lcm_embedding_profile.active=1``.
        embedded_kind: Kind argument forwarded to
            :func:`count_pending_docs`. Default ``"summary"``.

    Returns:
        An :class:`AutostartHandle`. ``is_running()`` is ``True`` if the
        autostart loop was started; ``False`` if gating short-circuited
        (no-op handle).
    """
    resolved_env: Mapping[str, str] = env if env is not None else os.environ

    # Pre-flight 1: ``VOYAGE_API_KEY`` presence (matches TS lines 111-117).
    api_key = resolved_env.get("VOYAGE_API_KEY", "").strip()
    if not api_key:
        log.info(
            "[backfill-autostart] VOYAGE_API_KEY not set — semantic retrieval "
            "will use FTS-only until you set it (or run /lcm worker tick "
            "embedding-backfill manually)."
        )
        return AutostartHandle()

    # Pre-flight 2: per-model vec0 table exists (matches TS lines 119-134).
    # The TS source also checks ``vec0Version(db)`` and ``getActiveEmbeddingModel(db)``
    # at this point. Issue 05-08 ports the active-model lookup; until then,
    # the caller passes ``model_name`` explicitly. We still defend against a
    # missing vec0 table here because the next per-tick ``count_pending_docs``
    # call would raise ``sqlite3.OperationalError`` otherwise.
    if model_name is None or not model_name.strip():
        log.warn(
            "[backfill-autostart] no active embedding profile registered. "
            "INSERT a row into lcm_embedding_profile (e.g. voyage-4-large "
            "dim=1024) and pass model_name=<that-row>. Will NOT auto-start "
            "until the active profile is wired in (issue 05-08)."
        )
        return AutostartHandle()
    if not embeddings_table_exists(db, model_name):
        log.warn(
            f"[backfill-autostart] embeddings table for {model_name!r} "
            "doesn't exist (sqlite-vec extension not loaded, or "
            "ensure_embeddings_table() was never called). Will NOT auto-start."
        )
        return AutostartHandle()

    # Pre-flight 3: tick_fn supplied (integration-side programmer-error
    # guard — TS gets this for free because ``tickEmbeddingBackfill`` is
    # the default; Python's port doesn't have a worker-orchestrator yet
    # so the caller MUST wire one in via ``tick_fn``).
    if tick_fn is None:
        log.error(
            "[backfill-autostart] tick_fn is None — integration bug. The "
            "caller (Epic 02 engine lifecycle or Epic 08 /lcm worker boot) "
            "must bind a tick callable before invoking this function."
        )
        return AutostartHandle()

    log.info(f"[backfill-autostart] starting (model={model_name} interval={interval_s}s)")

    handle = AutostartHandle()
    # The closure below mutates ``handle``; this works because each tick
    # runs on the asyncio event loop's single-threaded scheduler (no
    # locks needed). ``count_pending_docs`` is also serialized through
    # the same connection used by the tick path.
    on_complete_log = log
    handle_ref = handle
    inner_tick = tick_fn

    async def _run_one_tick() -> BackfillResult | None:
        """Run a single backfill tick + post-process the strike counters.

        Returns the :class:`BackfillResult` on success, or ``None`` on
        auth error (so ``_on_job_complete`` can stop the loop and log a
        terminal error). All other exceptions become Voyage-failure
        strikes; if the strike count crosses the threshold, the loop is
        stopped from within this callback.
        """
        # Early-return guard: stop() may have been scheduled by a prior
        # tick (auth error, idle-drain, or 3-strike) but the worker
        # loop's next ``asyncio.sleep`` may have raced ahead of the
        # stop-task. Short-circuit cleanly so no further state mutates
        # after the terminal log message has fired.
        if handle_ref._stopped:
            return None
        try:
            result = await inner_tick()
        except VoyageError as exc:
            if exc.kind == "auth":
                # Immediate stop on auth (NOT 3-strike — operator must
                # fix the key before any retry makes sense). Ports the
                # TS module's docstring contract: "auth re-throw → stops
                # autostart immediately." The TS source does this
                # implicitly because ``tickEmbeddingBackfill`` re-throws
                # auth errors and the outer try/catch in TS records the
                # error + stops at strike 3 — but our spec mandates
                # immediate stop on auth specifically.
                on_complete_log.error(
                    f"[backfill-autostart] Voyage auth error — stopping "
                    f"autostart immediately. Set VOYAGE_API_KEY correctly "
                    f"and restart. Error: {exc}"
                )
                # Synchronously flip the stopped flag so any concurrently-
                # scheduled tick sees it and short-circuits via the early-
                # return guard. Then schedule the actual loop stop on the
                # event loop (avoid awaiting it here — that would mean the
                # tick is waiting on the loop that is waiting on the tick
                # to drain).
                handle_ref._stopped = True
                _schedule_loop_stop(handle_ref)
                return None
            # Non-auth VoyageError counts as a failure strike.
            handle_ref.voyage_failure_strikes += 1
            on_complete_log.warn(
                f"[backfill-autostart] tick raised VoyageError "
                f"(kind={exc.kind}, consecutive={handle_ref.voyage_failure_strikes}): {exc}"
            )
            if handle_ref.voyage_failure_strikes >= _VOYAGE_FAILURE_STRIKE_THRESHOLD:
                on_complete_log.error(
                    "[backfill-autostart] 3 consecutive Voyage failures; "
                    "backing off — set VOYAGE_API_KEY or check status. "
                    "Run /lcm worker tick embedding-backfill manually "
                    "after the issue is resolved."
                )
                handle_ref._stopped = True
                _schedule_loop_stop(handle_ref)
            return None
        except Exception as exc:  # noqa: BLE001 — strike-bookkeeping path
            # Any other exception (sqlite3.OperationalError, KeyError in
            # caller's tick_fn, etc.) is a failure strike.
            handle_ref.voyage_failure_strikes += 1
            on_complete_log.error(
                f"[backfill-autostart] tick raised "
                f"{type(exc).__name__} "
                f"(consecutive={handle_ref.voyage_failure_strikes}): {exc}"
            )
            if handle_ref.voyage_failure_strikes >= _VOYAGE_FAILURE_STRIKE_THRESHOLD:
                on_complete_log.error(
                    "[backfill-autostart] 3 consecutive failures; "
                    "stopping autostart. Run /lcm worker tick "
                    "embedding-backfill manually after fixing the "
                    "underlying issue."
                )
                handle_ref._stopped = True
                _schedule_loop_stop(handle_ref)
            return None

        # Tick returned normally — post-process for strike counters.
        handle_ref.tick_count += 1

        if result.lock_not_acquired:
            # Another worker held the lock; not a failure, not idle.
            # Reset neither strike counter — this tick was a no-op
            # neither side claims. Ports TS lines 193-198.
            on_complete_log.info(
                "[backfill-autostart] lock held by another worker; ticking again next interval."
            )
            return result

        # 3-strike Voyage failure: any non-empty skip list with reasons
        # ``voyage_400`` / ``voyage_other`` (or ``lock_stolen_mid_embed``)
        # counts as a failed tick. Ports TS lines 210-226 (the ``allSkipped``
        # branch) — generalized here to count ANY tick that wrote 0 + had
        # skips as a failure, because the Python port may surface partial
        # successes differently and the strict ``embedded_count==0 AND
        # pending > 0`` condition leaves operator alerts in a blind spot
        # when Voyage 500s strike just a few rows.
        had_voyage_skips = result.embedded_count == 0 and any(
            s.reason in ("voyage_400", "voyage_other", "lock_stolen_mid_embed")
            for s in result.skipped
        )
        if had_voyage_skips:
            handle_ref.voyage_failure_strikes += 1
            sample = ", ".join(s.reason for s in result.skipped[:3])
            on_complete_log.warn(
                f"[backfill-autostart] tick {handle_ref.tick_count} returned "
                f"0 embedded with {len(result.skipped)} skipped "
                f"(consecutive={handle_ref.voyage_failure_strikes}); "
                f"sample reasons: {sample}"
            )
            if handle_ref.voyage_failure_strikes >= _VOYAGE_FAILURE_STRIKE_THRESHOLD:
                on_complete_log.error(
                    "[backfill-autostart] 3 consecutive Voyage failures; "
                    "backing off — set VOYAGE_API_KEY or check status. "
                    "Run /lcm worker tick embedding-backfill manually "
                    "after the issue is resolved."
                )
                handle_ref._stopped = True
                _schedule_loop_stop(handle_ref)
            # Idle strikes reset because this tick wasn't idle.
            handle_ref.idle_strikes = 0
            return result

        # Reset voyage_failure_strikes on a clean tick (matches TS line
        # 224-226 — ``else { consecutiveFailures = 0; }``).
        handle_ref.voyage_failure_strikes = 0

        # Idle-drain check: 0 embedded + 0 skipped + no pending docs.
        # Pending count is queried separately (matches TS line 152
        # ``countPendingDocs(db, ...)``).
        is_idle = (
            result.embedded_count == 0
            and len(result.skipped) == 0
            and count_pending_docs(db, model_name=model_name, embedded_kind=embedded_kind) == 0
        )
        if is_idle:
            handle_ref.idle_strikes += 1
            on_complete_log.info(
                f"[backfill-autostart] idle tick "
                f"{handle_ref.tick_count} (consecutive_idle="
                f"{handle_ref.idle_strikes}); corpus fully embedded."
            )
            if handle_ref.idle_strikes >= _IDLE_STRIKE_THRESHOLD:
                on_complete_log.info(
                    "[backfill-autostart] corpus drained after 3 consecutive idle ticks; stopping."
                )
                handle_ref._stopped = True
                _schedule_loop_stop(handle_ref)
            return result

        # Non-idle tick: reset idle strikes + log progress.
        handle_ref.idle_strikes = 0
        on_complete_log.info(
            f"[backfill-autostart] tick {handle_ref.tick_count} "
            f"embedded={result.embedded_count} "
            f"tokens={result.voyage_tokens_consumed} "
            f"skipped={len(result.skipped)} "
            f"duration={result.duration_ms}ms"
        )
        return result

    def _on_complete(info: JobCompleteInfo) -> None:  # noqa: ARG001 — telemetry hook stub
        """Receive per-tick telemetry from the worker loop.

        The autostart's strike-bookkeeping lives inside ``_run_one_tick``
        (the closure passed to :class:`WorkerJob`), not here, because
        the per-job-complete callback runs *after* the tick has fully
        returned (including ``stop()`` scheduling). The bookkeeping
        callback used to live here in an earlier draft; moving it
        inside the tick closure simplifies the control flow.
        """
        return

    loop = WorkerLoop(
        jobs=[
            WorkerJob(
                kind="embedding-backfill",
                interval_s=interval_s,
                run=_run_one_tick,
            )
        ],
        on_job_complete=_on_complete,
    )
    handle._loop = loop
    loop.start()
    return handle
