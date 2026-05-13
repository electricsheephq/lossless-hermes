# ADR-020: Worker loop dispatcher

**Status:** Accepted
**Date:** 2026-05-13
**Confidence:** 95%
**Supersedes:** —
**Superseded by:** —

## Context

Three classes of background work need cron-like scheduling inside the lossless-hermes plugin (see ADR-018 for the concurrency model context):

- **Embedding backfill** — runs every N seconds, drains unembedded leaves through Voyage.
- **Entity coreference / extraction** — drains `lcm_extraction_queue` for newly-created leaves.
- **Condensation maintenance** — services the deferred-compaction debt queue when cache state and activity band allow.

TS source uses `setInterval` inside a `WorkerLoop` class (`src/concurrency/worker-loop.ts`, 238 LOC). Each job runs at its own cadence; ticks that overlap (a previous tick still running when the next timer fires) are skipped. A generation counter guards against stale ticks after a stop/start cycle. The loop is started during plugin registration and stopped on shutdown.

ADR-018 already chose `asyncio.Task` per worker kind. This ADR pins the lifecycle, the generation guard, and what does NOT live in this layer (external schedulers, cron, apscheduler).

## Options considered

### Option A: `asyncio.create_task` per worker kind, started in `register(ctx)`, generation-counter guard

- Description: `WorkerLoop` class owns one `asyncio.Task` per registered job kind. `register_job(kind, run_fn, interval_s)` adds to the registry. `start()` increments `self._generation`, creates one task per registered job, each running `while self._running and gen == my_gen: try: await self._tick(job); except Exception: log; finally: await asyncio.sleep(interval_s)`. `stop()` sets `self._running = False`, increments the generation, and `await asyncio.gather(*self._tasks, return_exceptions=True)` with a 2s timeout.
- Pros:
  - Direct port of TS `setInterval` semantics. The TS generation counter (`worker-loop.ts:107`) maps to a Python `int` field.
  - Plays cleanly with ADR-018's `lcm_worker_lock` table: each tick acquires the lock for its job kind before doing work. If a peer process holds it, the tick returns immediately.
  - Skip-overlap-on-busy: if a previous tick is still running when the timer fires, the current task is in the middle of its own `await`; the next iteration of the `while` loop hasn't yet been entered. Single-task-per-job structurally prevents overlap.
  - Hermes already uses this exact pattern (`tools/mcp_tool.py:_schedule_tools_refresh`, `plugins/platforms/google_chat/adapter.py:_run_supervisor`).
- Cons:
  - Exception isolation must be explicit: bare `try/except Exception` around `await self._tick(...)`, log, continue. Easy to forget and let an exception kill the task. Mitigated by code review + a test that asserts a `RuntimeError` in one tick doesn't stop the loop.
- Evidence cited:
  - `embeddings.md` §"Worker loop" — `asyncio.create_task` per job, generation counter, skip-overlap.
  - `lcm-source-map.md` — `worker-loop.ts` (238 LOC) → `worker_loop.py` (~250 LOC).
  - `embeddings.md` "ADR-?: Worker loop dispatcher — `asyncio.create_task` vs `apscheduler` vs cron" recommends `asyncio.create_task`.

### Option B: `apscheduler` (AsyncIOScheduler)

- Description: pull in `apscheduler`; register each job as an `IntervalTrigger`.
- Pros: established library; cron-expression support.
- Cons:
  - Adds machinery we don't need (cron syntax, job stores, persistence, multi-process scheduling). LCM is single-process; the `lcm_worker_lock` table handles cross-process coordination.
  - Drifts from the TS source's `setInterval` pattern, complicating side-by-side debugging.
  - One more pinned dep with no offsetting benefit.

### Option C: External cron (os-level cron or systemd timers)

- Description: drop in-process scheduling entirely; let the OS drive ticks via a CLI entry point.
- Pros: zero in-process complexity.
- Cons:
  - Loses lifecycle alignment: the plugin's `on_session_end` and `atexit` hooks can't reach an OS-cron job.
  - Loses in-memory state: per-conversation context cache, token-state cache, generation counter — all evaporate between ticks.
  - Adds an ops surface (cron entries per deployment) the TS source doesn't have.
  - Rejected for the same reasons ADR-018 rejected external scheduling.

## Decision

Chosen: **Option A (`asyncio.create_task` per worker kind, generation-counter guard, no external scheduler)**.

## Rationale

- Matches TS `WorkerLoop` 1:1. Same scheduling primitives, same skip-overlap-on-busy semantics, same generation-counter guard against stop/start races.
- Cleanly composes with ADR-018's `lcm_worker_lock` cross-process gate: each tick body acquires its job-kind lock first, does the work, releases on exit (try/finally). Cross-process safety lives in SQL; in-process scheduling lives in asyncio.
- Cleanly composes with ADR-019's `httpx.AsyncClient`: tick body `await`s on Voyage calls without blocking the event loop.
- No external scheduler dependency. Reduces the runtime surface area.

The generation counter is the load-bearing detail. TS's `worker-loop.ts:107` bumps the generation on `start()`. Each tick captures the current generation at task-creation time; if the field has changed by the time the timer fires (i.e. `stop()` then `start()` was called), the stale tick exits the loop. Port verbatim.

## Consequences

- New file: `src/lossless_hermes/concurrency/worker_loop.py`. `WorkerLoop` class with:
  - `register_job(kind: str, run_fn: Callable[[], Awaitable[None]], interval_s: float) -> None`
  - `start() -> None` — increments `self._generation`, sets `self._running = True`, creates one `asyncio.Task` per registered job, names it `f"worker-{kind}"` for debugging.
  - `stop() -> None` — sets `self._running = False`, increments `self._generation`, awaits all tasks with `asyncio.wait_for(asyncio.gather(*self._tasks, return_exceptions=True), timeout=2.0)`.
  - Internal `_run_job(job, gen)` — the per-task loop. Catches exceptions per-tick, logs, sleeps, continues.
- Lifecycle integration:
  - **Started:** on first session via `LCMEngine.register(ctx)` after engine instantiation. The engine constructor sets up the `WorkerLoop` but doesn't `start()`; the first `register(ctx)` call does. Subsequent sessions reuse the running loop.
  - **Stopped:** on plugin shutdown (`on_session_end` for the last session, or `atexit` for process-level shutdown). Both call `WorkerLoop.stop()`.
- Cross-process coordination: handled by ADR-018's `lcm_worker_lock`. The worker-loop dispatcher does NOT know about cross-process; each tick body acquires its own lock.
- Per-job interval defaults (ported verbatim from TS where defined):
  - `embedding-backfill`: 60s.
  - `entity-extraction`: 30s.
  - `condensation-maintenance`: 120s.
  Operators override via `context.lossless_hermes.workers.<kind>.interval_s` (ADR-023).
- A test (`tests/concurrency/test_worker_loop.py`) ports `worker-loop.test.ts` fixture-for-fixture: assert overlapping ticks skip, generation-counter guard works, exception in one tick doesn't kill the loop, `stop()` waits up to 2s then proceeds.
- Precludes a future shift to cron/apscheduler without rewriting this layer. Acceptable — the asyncio model fits the host runtime.

## Open questions / 5% uncertainty

- **In-flight tick when `stop()` fires.** The tick task is at some `await` point (most likely inside `httpx` waiting for Voyage). Cancellation via `task.cancel()` will raise `CancelledError` at that point; the tick body's `try/finally` releases the worker lock. Worst-case: a tick was mid-write to the DB; the WAL handles rollback. Verify in `test_worker_loop.py`.
- **Long-running tick stalling shutdown.** If a tick body takes 10s to finish, `stop()` waits 2s then proceeds — leaves an orphan task. Mitigation: the generation-counter guard means the orphan exits at its next loop iteration; meanwhile, the lock TTL (90s per ADR-018) ensures another process can step in eventually.
- **Reload-during-development.** Hot reload of the plugin would create a second WorkerLoop. The generation counter saves us: stale loops detect the change and exit. But two WorkerLoop instances can't both hold the cross-process `lcm_worker_lock`; the second one fails fast and waits for the first to time out. Acceptable for dev; document it.
- **Memory: defaultdict of locks (per conv) doesn't auto-prune.** Per ADR-018, the lock dict grows with the count of distinct conversations touched. Worst-case (10k conversations) is ~2MB. Not addressed here; revisit if it ever shows up in memory profiling.
