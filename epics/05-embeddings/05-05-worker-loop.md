---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-05] concurrency: port worker-loop.ts → concurrency/worker_loop.py'
labels: 'port, embeddings, concurrency'
---

## Source (TypeScript)
- File: `lossless-claw/src/concurrency/worker-loop.ts`
- Lines: 238
- Function(s)/class(es): `WorkerLoop` class — `start()` (122-158, generation counter + per-job `setInterval`), `stop()` (with `gracefulTimeoutMs`), `runOnce(kind)`, internal in-flight tracking, `WorkerJob` shape, `JobCompleteInfo`.

## Target (Python)
- File: `src/lossless_hermes/concurrency/worker_loop.py`
- Estimated LOC: ~250

## Dependencies
- Depends on: Epic 00 (`pyproject.toml` Python 3.11+ for native asyncio). `src/lossless_hermes/concurrency/model.py` (job-kind enum + concurrency constants) is created in this issue as a peer module.
- Blocks: #05-07 (backfill cron uses the loop), #05-11 (autostart wiring), and transitively Epic 06's hybrid/semantic modes (depend on Epic 05).

## Acceptance criteria

- [ ] **`WorkerJob` dataclass:**
  ```python
  @dataclass
  class WorkerJob:
      kind: WorkerJobKind  # Literal["embedding-backfill", "entity-extraction", "condensation-maintenance", ...]
      interval_s: float
      run: Callable[[], Awaitable[object]]  # Python: connection captured by caller closure, not passed
  ```
- [ ] **`JobCompleteInfo` dataclass:** `kind`, `duration_ms: float`, `result: object | None = None`, `error: BaseException | None = None`. Dispatched via the `on_job_complete` callback after every tick (success or exception).
- [ ] **`WorkerLoop.__init__(jobs, *, on_job_complete=None)`**:
  - Validates: duplicate `kind` → `ValueError`; `interval_s <= 0` → `ValueError`.
  - Initializes `_tasks: list[asyncio.Task] = []`, `_in_flight: dict[WorkerJobKind, asyncio.Task] = {}`, `_running = False`, `_generation = 0`.
- [ ] **`start() -> bool`** (port of `worker-loop.ts:122-158`):
  - Idempotent: returns `False` if already running.
  - Bumps `self._generation`; captures `gen = self._generation` at task-creation time.
  - Creates one `asyncio.create_task(self._run_job(job, gen), name=f"worker-{job.kind}")` per registered job.
  - Returns `True` on first start.
- [ ] **`_run_job(job, my_gen)`** per-task loop:
  - Loop while `self._running and self._generation == my_gen`. The **generation guard** is load-bearing — defends against stale ticks from a previous `start()`/`stop()` cycle (per `worker-loop.ts:107`).
  - Skip-overlap-on-busy: if `self._in_flight[job.kind]` is set and not done, `await asyncio.sleep(job.interval_s)` and continue (no queueing).
  - Create `tick_task = asyncio.create_task(self._invoke_once(job, started_monotonic))`; store in `_in_flight[job.kind]`.
  - `await asyncio.sleep(job.interval_s)` between ticks.
- [ ] **`_invoke_once(job, started)`**: wraps the tick body in `try/except BaseException` (intentional — see TS `worker-loop.ts:166-184` comments; a single bad tick must NOT crash the loop). On success, dispatch `JobCompleteInfo(kind, duration_ms, result=...)`; on exception, dispatch with `error=...` and do NOT re-raise.
- [ ] **`stop(*, graceful_timeout_s=30.0) -> bool`** (port of `worker-loop.ts` `stop`):
  - Idempotent: returns `True` if not running.
  - Sets `self._running = False`; cancels all per-job tasks (`t.cancel()`).
  - `await asyncio.wait_for(asyncio.gather(*in_flight_tasks, return_exceptions=True), timeout=graceful_timeout_s)`.
  - Returns `True` if all in-flight finished within the timeout; `False` on `asyncio.TimeoutError`.
- [ ] **`run_once(kind) -> object`** (port of `worker-loop.ts` `runOnce`): invoke a specific job immediately outside the schedule. Used by tests, by `/lcm worker tick` (Epic 08), and by Epic 03's leaf-write nudges. Raises `ValueError` for unknown kind; `RuntimeError` if the job is already in flight.
- [ ] **`is_running() -> bool`** and **`in_flight_count() -> int`** helpers for `/lcm health`.
- [ ] **`_dispatch_complete`** swallows callback exceptions (logs `exception(...)`). A broken `on_job_complete` must NOT kill the loop.
- [ ] **Concurrency model peer module:** create `src/lossless_hermes/concurrency/model.py` (~150 LOC):
  ```python
  from typing import Literal

  WorkerJobKind = Literal[
      "condensation",
      "extraction",
      "embedding-backfill",
      "profile-rebuild",
      "theme-consolidation",
      "eval",
  ]

  GATEWAY_BUSY_TIMEOUT_MS = 30_000
  WORKER_BUSY_TIMEOUT_MS = 5_000
  WORKER_HEARTBEAT_MS = 30_000
  WORKER_LOCK_TTL_MS = 90_000
  GATEWAY_FALLBACK_SOAK_MS = 300_000

  # §0 invariant helper for CI grep + runtime asserts
  def assert_no_open_tx(conn) -> None:
      """Raise if a write transaction is open. Used to enforce §0:
      no LLM/network calls inside SQLite write transactions."""
      ...
  ```
  Constants ported verbatim from `concurrency/model.ts:55-87`.
- [ ] **§0 invariant** ("no LLM/network call inside any SQLite write transaction") is enforced by code review + a CI grep test that forbids `await ` (with trailing space) inside `with conn:` / `BEGIN`-bracketed regions. The `assert_no_open_tx(conn)` helper is the runtime defense.
- [ ] `mypy --strict` and `ty check` pass.
- [ ] All 261 LOC of `test/worker-loop.test.ts` ported to `tests/concurrency/test_worker_loop.py`.

## Tests (`tests/concurrency/test_worker_loop.py`)

Cases from `test/worker-loop.test.ts` (261 LOC):

- `start()` returns `True` first; subsequent `start()` returns `False`.
- `stop()` before start is a no-op (returns `True`).
- Two jobs at different intervals don't interfere; each fires on its own cadence.
- Overlapping ticks are skipped (not queued): one job with `interval_s=0.05`, tick body sleeps 0.2s — verify it doesn't fire 4× during a 0.4s window.
- **Exception in one tick captured in `on_job_complete.error`; loop continues.** Stage a tick that raises `RuntimeError`; verify the next tick still runs.
- `stop(graceful_timeout_s=1.0)` waits for in-flight; returns `True` if all finish.
- `stop(graceful_timeout_s=0.05)` with a long-running tick returns `False`.
- `run_once(kind)` returns the result of the registered run function.
- `run_once(kind)` raises `ValueError` on unknown kind.
- `run_once(kind)` raises `RuntimeError` if the job is already in flight (started via `start()`, still running).
- `WorkerLoop([WorkerJob("x", 1.0, run1), WorkerJob("x", 1.0, run2)])` raises on duplicate kind.
- `WorkerLoop([WorkerJob("x", 0.0, run)])` raises on invalid interval.
- **Generation guard:** start → stop → start; verify the stale task from the first generation doesn't fire any more ticks after the second start.
- `is_running()` reflects state; `in_flight_count()` is accurate during a sleeping tick.

## Estimated effort
5 hours

## Confidence
95% — ADR-018 + ADR-020 pin the design exhaustively. The TS pattern (`setInterval` + generation counter + skip-overlap) maps cleanly to `asyncio.create_task` + `asyncio.sleep`. Hermes already uses this pattern in 5+ plugin adapters per ADR-020 §Option-A "Evidence cited". Residual 5%:
- In-flight tick during `stop()`: the task is at some `await` (most likely inside `httpx`). `task.cancel()` raises `CancelledError`; the tick body's `try/finally` releases the worker lock. Worst-case: a mid-write task — WAL handles rollback. Verify in test.
- Long-running tick stalling shutdown: if a tick takes 10s and `graceful_timeout_s=2.0`, the orphan exits at its next loop iteration (generation guard); meanwhile, the lock TTL (90s per #05-06) ensures another process can step in.

## Files to read before starting
- `docs/porting-guides/embeddings.md` §"Worker loop" (lines 562-746)
- `docs/adr/018-concurrency-model.md` (Option A chosen — `asyncio.Task` per kind)
- `docs/adr/020-worker-loop-dispatcher.md` (entire — pins the lifecycle + generation-counter rationale)
- TS source: `lossless-claw/src/concurrency/worker-loop.ts` (entire — 238 LOC)
- TS source: `lossless-claw/src/concurrency/model.ts` (entire — 147 LOC; the constants module)
- TS tests: `lossless-claw/test/worker-loop.test.ts` (entire — 261 LOC)
