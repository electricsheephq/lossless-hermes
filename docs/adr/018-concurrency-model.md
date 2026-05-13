# ADR-018: Concurrency model

**Status:** Accepted
**Date:** 2026-05-13
**Confidence:** 95%
**Supersedes:** —
**Superseded by:** —

## Context

The Python port has three classes of background work that must run concurrently with foreground tool calls:

1. **Embedding backfill** — `src/embeddings/backfill.ts` walks unembedded leaves, packs token-budgeted batches, POSTs to Voyage, writes vec0. Cron-like cadence.
2. **Entity coreference / extraction** — `src/extraction/entity-coreference.ts` drains `lcm_extraction_queue` for new leaves.
3. **Condensation maintenance** — `src/store/compaction-maintenance-store.ts` services the deferred-compaction debt queue when cache-state and activity bands allow.

In TS, each runs as a `setInterval` job under a `WorkerLoop` (one `WorkerLoop` per process, multiple jobs registered). Foreground tool calls share the same SQLite connection registry but never touch the worker loop's state. Cross-process safety (multiple gateway processes against one DB) is via the `lcm_worker_lock` table — TTL=90s, heartbeat every 30s, gateway fallback only after 300s of silence.

The question for the Python port: how do we replicate `setInterval` plus per-conversation mutex plus cross-process lock?

Constraints:
- ADR-017 says DB is sync; the worker tick body calls sync stores.
- Voyage HTTP calls are async (`httpx.AsyncClient`). The tick has at least one `await`.
- apsw spike-001 fallback: a connection can only be used from the thread that created it. Multi-threaded tick bodies are out unless we open a connection per thread.
- The TS code already has the §0 transaction invariant (no LLM/network inside a write tx); the Python port preserves it.

## Options considered

### Option A: `asyncio.Task` per worker kind + `dict[str, asyncio.Lock]` per conversation + `lcm_worker_lock` table for cross-process

- Description: one `asyncio.create_task` per worker kind (`embedding-backfill`, `entity-extraction`, `condensation-maintenance`), started in `register(ctx)` after engine instantiation. Each task is a `while self._running and gen == my_gen: await asyncio.sleep(interval); await self._tick()` loop. Per-conversation mutex is `defaultdict(asyncio.Lock)` keyed by `conversation_id`, so concurrent calls to `compact(conversation_id=N)` serialize per N but parallelize across different N. Cross-process via the existing `lcm_worker_lock` row with TTL+heartbeat (port `worker-lock.ts` SQL verbatim).
- Pros:
  - Direct port of TS `setInterval` semantics (generation counter, per-tick skip-on-overlap, isolate-exception-per-tick).
  - Single-threaded inside the process: works with apsw fallback (single-thread-per-connection) and avoids `check_same_thread=False` traps for stdlib `sqlite3`.
  - `asyncio.Lock` is reentrant-aware (well, not reentrant — but you can detect re-entry via owner-task tracking; LCM doesn't need reentrancy here).
  - `lcm_worker_lock` table is a verbatim port — same schema (`job_kind TEXT PK, worker_id, acquired_at, expires_at, last_heartbeat_at`), same semantics, same heartbeat task pattern.
- Cons:
  - Lock dict can grow unbounded if many distinct `conversation_id` values are touched. Mitigated: prune entries with no waiters in a low-priority sweep, or accept the O(N) memory (one `asyncio.Lock` is ~200 bytes; 10k conversations is 2MB).
  - Per-tick exceptions must be caught explicitly inside the loop — `asyncio` doesn't auto-restart a task that raises. Mitigated by wrapping the tick body in `try/except Exception: logger.exception(...)` and continuing.
- Evidence cited:
  - `embeddings.md` "Worker loop" section: `asyncio.create_task` per job, generation counter, skip-overlap pattern.
  - `lcm-source-map.md`: `transaction-mutex.ts` (202 LOC) ports to `transaction_mutex.py` (~240 LOC) — `asyncio.Lock` keyed on connection.
  - `embeddings.md` "ADR-?: Worker loop dispatcher — `asyncio.create_task` vs `apscheduler` vs cron" recommends `asyncio.create_task`.

### Option B: `threading.Thread` per worker kind, each with its own DB connection

- Description: spawn a daemon thread per worker; each thread owns its own `sqlite3.Connection`. `threading.Event` for stop signal; `threading.Lock` for per-conversation mutex.
- Pros:
  - True parallelism with the foreground process; no `await` machinery needed.
  - Each thread has its own connection — apsw-compatible without contortions.
- Cons:
  - The foreground itself is async (Voyage `httpx.AsyncClient` and the broader Hermes runtime). Mixing threads + asyncio means the threaded worker can't naturally talk back to async code without `asyncio.run_coroutine_threadsafe` plumbing.
  - Stop semantics are harder: `threading.Event` works but doesn't interrupt an in-flight network call cleanly.
  - Logging, sharing of in-memory caches, and shutdown ordering all become trickier when half the code is async and half threaded.

### Option C: External scheduler (apscheduler / cron / a per-job subprocess)

- Description: don't run workers inside the Python process at all. Let cron or a sidecar process drive them.
- Pros: zero in-process complexity for the worker loop.
- Cons:
  - Requires additional ops surface (cron entries, subprocess management) for what the TS source does in-process.
  - Doesn't match the host's lifecycle: the plugin is supposed to clean up on `on_session_end`; an external scheduler doesn't know about session boundaries.
  - Loses the in-memory caches (per-conversation context items cache, token-state cache) that the TS source relies on for hot paths.
  - LCM is single-process per gateway/worker by design — external scheduling solves a problem we don't have.

## Decision

Chosen: **Option A (`asyncio.Task` per worker kind + `dict[str, asyncio.Lock]` per conversation + `lcm_worker_lock` table for cross-process)**.

## Rationale

- Direct port of TS `setInterval` + generation-counter pattern. Same skip-overlap-on-busy semantics. Same exception isolation. Same start/stop/restart contract.
- Inside-process: `asyncio.Lock` keyed by `conversation_id` (a `defaultdict(asyncio.Lock)`) is the literal Python equivalent of the implicit per-conversation serialization the TS code achieves via single-event-loop scheduling.
- Cross-process: the `lcm_worker_lock` table already exists in the schema (`storage.md` §2.3). Port `worker-lock.ts` verbatim — same SQL, same heartbeat cadence, same 5-minute gateway-fallback soak. SQL is dialect-agnostic; no porting risk.
- apsw fallback compatibility: a single asyncio task body always runs on one thread (the asyncio event-loop thread). All SQL goes through one connection. apsw is happy.
- The TS `transaction-mutex.ts` (202 LOC) ports to a Python class that wraps `asyncio.Lock` per `Connection` (we don't need savepoint-based reentrancy because no caller currently recurses through a transaction; if a future caller does, the savepoint helper is bolted on).

The §0 invariant becomes a static-grep CI check: forbid `await` inside `with conn:` blocks.

## Consequences

- New file: `src/lossless_hermes/concurrency/worker_loop.py` — `WorkerLoop` class with `register_job(kind, run_fn, interval_s)`, `start()`, `stop()`, generation-counter guard.
- New file: `src/lossless_hermes/concurrency/worker_lock.py` — port of `worker-lock.ts` SQL + heartbeat task (single async task per held lock, sleeps `WORKER_HEARTBEAT_S=30`, updates `last_heartbeat_at`).
- New file: `src/lossless_hermes/concurrency/model.py` — `WORKER_LOCK_TTL_S = 90`, `WORKER_HEARTBEAT_S = 30`, `GATEWAY_FALLBACK_SOAK_S = 300`, plus an `assert_no_open_tx(conn)` helper for the §0 check.
- New file: `src/lossless_hermes/transaction_mutex.py` — `TransactionMutex` class with `lock_for(conn) -> AsyncContextManager`. Default mutex map is `dict[int, asyncio.Lock]` keyed by `id(conn)`.
- Per-conversation mutex lives on the engine as `self._per_conv_locks: defaultdict[int, asyncio.Lock]`. Used by `compact(conversation_id=…)` and the worker tick when it picks a conversation.
- Cross-process: every worker tick acquires `lcm_worker_lock` first (SQL pattern from `worker-lock.ts`). If acquisition fails (another worker holds the lock with a non-expired heartbeat), the tick returns immediately with `lockNotAcquired=true`.
- The §0 invariant is enforced by code review + a CI grep test: any `await ` (with trailing space) inside `with self._conn:` or `cursor.execute("BEGIN ")` blocks fails the lint.
- Generation counter: each `start()` increments `self._generation`; in-flight ticks that see `gen != my_gen` skip their body. Prevents stale ticks from a previous `start()`/`stop()` cycle.
- Shutdown: `on_session_end` and `atexit` both call `stop()` which sets `self._running = False` and bumps the generation, then `await asyncio.wait_for(asyncio.gather(*self._tasks, return_exceptions=True), timeout=2.0)` to give in-flight ticks a graceful exit.
- Precludes a future shift to threaded workers without a substantial rework. Acceptable given the trade-off — the in-process asyncio model is uniformly preferable for a single-Python-process gateway.

## Open questions / 5% uncertainty

- **In-flight Voyage call when stop() fires.** `httpx.AsyncClient` doesn't auto-cancel on task cancel unless the request is at an `await` point. Mitigation: each Voyage call uses a per-request timeout (60s default), so worst-case shutdown waits ~60s. Acceptable — the workers don't need fast shutdown.
- **`asyncio.Lock` is not reentrant.** A future caller that tries to acquire a conv-lock it already holds will deadlock. Mitigation: forbid by convention; add a `RuntimeError("re-entrant conv-lock acquisition")` check if the calling task already owns the lock (Python's `asyncio` doesn't expose owner-task tracking natively, but we can wrap it).
- **Cross-process clock skew.** `lcm_worker_lock`'s `expires_at` is stored as an ISO-8601 string from `datetime('now')`. If two processes' clocks drift, a steal-after-TTL might happen sooner or later than intended. Mitigation: use `datetime('now')` consistently in SQL (server-side clock), not Python `datetime.utcnow()` — matches TS behavior exactly.
