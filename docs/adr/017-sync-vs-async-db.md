# ADR-017: Sync vs async DB layer

**Status:** Accepted
**Date:** 2026-05-13
**Confidence:** 95%
**Supersedes:** —
**Superseded by:** —

## Context

The TS source's stores (`conversation-store.ts`, `summary-store.ts`, etc.) are declared `async` because `node:sqlite` wraps its sync C API in Promises at the binding boundary. The actual SQLite work is synchronous; the `await` is fictional. Hermes is a Python codebase that runs sync code in its existing `ContextEngine` abstract base class (`agent/context_engine.py:77` defines `compress(messages, current_tokens, focus_topic) -> List[dict]` synchronously). LCM has hard contracts about transactions — assembler-compaction.md §0 invariant says no LLM/network call may live inside a SQLite write transaction, and embeddings.md "Concurrency contract" repeats that.

The question: should the Python port wrap every store call in `async def` (`await self._conn.execute(...)`), or use plain blocking `sqlite3.Connection` calls?

Constraints:
- `node:sqlite` is sync under the hood; the TS `async` is decorative. Mapping `await` → `await` would be a literal port that preserves nothing real.
- Hermes's `_content_length_for_budget`, `_compress`, and the ContextEngine ABC are all sync. Bridging async stores into sync `compress()` requires `asyncio.run()` per call (creates a fresh loop, loses connection pool) or a long-lived background loop (more architecture for no benefit).
- The §0 invariant means LLM calls must already be outside transactions. So we don't need transaction-aware async machinery to coordinate them — the discipline is enforced at a higher layer.
- `apsw` (the spike-001 fallback for sqlite-vec) requires single-thread-per-connection. Async with a connection pool is incompatible with apsw; sync is fine.

## Options considered

### Option A: Synchronous stores everywhere; async only at the IO boundary (Voyage HTTP, optional LLM)

- Description: `ConversationStore`, `SummaryStore`, etc. expose plain `def` methods that call `self._conn.execute(...)` directly. The async boundary lives at `httpx.AsyncClient.post` in `voyage/client.py` and inside the worker loop's `asyncio.create_task` machinery. Compaction's `summarize` callback is async-capable but invoked at a clean boundary where no transaction is open.
- Pros:
  - 1:1 with the TS source's actual semantics (sync DB, async network) — drops the decorative `await`s.
  - Matches Hermes's `ContextEngine.compress` ABC exactly; no bridging code.
  - Matches `hermes_state.py` (also sync) — the host runtime expects sync persistence.
  - Works with both `sqlite3` (CPython stdlib, default) and `apsw` (sqlite-vec fallback per spike-001); apsw requires single-thread-per-connection and is hostile to async pools.
  - Less surface area for the §0 invariant to be violated — there's no `async def` for a junior contributor to drop an `await voyage.embed(...)` into mid-transaction.
- Cons:
  - SQLite reads block the event loop when called from an `async def`. Mitigated: SQLite calls are microseconds on a local WAL-mode DB (the embeddings.md "Concurrency contract" notes gateway `busy_timeout = 30s` precisely because the rare contention case dominates wall-time). For long batches (compaction's leaf-pass), the work is bounded and runs in a worker context that already isn't on the request hot path.
- Evidence cited:
  - `assembler-compaction.md` "Async vs sync" ADR section recommends sync for exactly these reasons.
  - `lcm-source-map.md`: "TS uses `node:sqlite` (`DatabaseSync`). Python uses stdlib `sqlite3` OR `apsw`."
  - Hermes precedent: `hermes_state.py` is sync.

### Option B: Async stores end-to-end via `aiosqlite`

- Description: every store method is `async def`, every `execute` is `await self._conn.execute(...)`. Compaction and assembler stay `async`. Hermes's `compress()` wrapper calls `asyncio.run(self._async_compress(...))` to bridge.
- Pros: literal port of TS shape.
- Cons:
  - `aiosqlite` is just a thread-pool wrapper around stdlib `sqlite3` — the "async" is a thread hop, not real async I/O. No throughput win for a single-process local DB.
  - Incompatible with the apsw spike-001 fallback (apsw doesn't pretend to be async).
  - `asyncio.run` inside `compress()` creates a fresh event loop per call. Connections, locks, and worker tasks can't survive across calls. Either we add a long-lived background loop (more architecture) or we accept per-call setup overhead.
  - Doesn't fix anything: the §0 invariant still has to be enforced manually. Async syntax doesn't help.

### Option C: Hybrid — sync stores, async assembler/compaction (using thread executor for store calls)

- Description: stores stay sync; assembler is `async def` and uses `loop.run_in_executor(None, store.method, ...)` for DB calls.
- Pros: keeps async fluency at the consumer layer.
- Cons:
  - The executor hop adds latency (~50µs per call) that dominates the actual SQLite work for microsecond-scale reads.
  - Forces every call site to deal with thread-safety on the connection object. `sqlite3.Connection` is not safe to share across threads without `check_same_thread=False` plus app-level locking. apsw is straight-up single-thread-per-connection.
  - Solves no real problem — the consumers don't need to interleave SQL with non-SQL `await`s.

## Decision

Chosen: **Option A (synchronous SQLite end-to-end; async only at IO boundary)**.

## Rationale

Three convergent inputs from the porting guides:
1. **`storage.md`** treats stores as a sync layer over `sqlite3.Connection`/`apsw.Connection`; the PRAGMA application path and connection registry assume single-thread-per-connection semantics that are incompatible with async pools.
2. **`assembler-compaction.md` "Async vs sync" ADR section** explicitly recommends sync as Option 1, citing that SQLite calls are microseconds and LLM calls run via Hermes's existing sync `auxiliary_client.call_llm`.
3. **`engine.md`** treats the engine as sync at its public surface to match the `ContextEngine` ABC.

The §0 invariant (no LLM/network inside a transaction) is already enforced as a code discipline in the LCM source — adding async syntax around the DB doesn't enforce it any harder. Conversely, sync DB code makes it trivially obvious where transactions begin and end (a plain `with conn:` block), so the invariant gets a visual aid.

The apsw fallback dictates single-thread-per-connection. Async would force us to drop apsw or layer locking on top. Sync sidesteps the choice.

Where async genuinely earns its keep: the Voyage HTTP client (`httpx.AsyncClient`), the worker-loop dispatcher (`asyncio.create_task` per worker kind per ADR-020), and any future LLM client that exposes a streaming interface. These are IO-bound and benefit from cooperative scheduling. The DB doesn't.

## Consequences

- Stores expose `def` methods. No `async`, no `await`, no `aiosqlite`.
- `ContextAssembler.assemble`, `CompactionEngine.compact`, `CompactionEngine.evaluate` are all sync. They match Hermes's `ContextEngine.compress` ABC directly.
- The worker-loop tasks (per ADR-020) are `async def` because they wrap the Voyage HTTP client. Inside the task body, when it needs to write to the DB, it calls sync store methods directly. The `await` only appears around `httpx` calls and `asyncio.sleep`.
- The §0 invariant becomes a static-analysis target: no `await` may appear inside a `with conn:` (or explicit `BEGIN`/`COMMIT`) block. A simple grep test in CI catches violations.
- Transaction handling: `with conn:` for autocommit-rollback. For nested savepoints (`transaction-mutex.ts` semantics), port to a custom context manager around `SAVEPOINT`/`RELEASE`/`ROLLBACK TO`.
- Worker-lock acquisition (`lcm_worker_lock` table per ADR-018) is sync SQL inside the async worker task. The pattern is `lock = acquire_worker_lock(conn, job_kind, worker_id); try: ...; finally: release_worker_lock(conn, job_kind, worker_id)` — no `await` in the acquire/release path.
- Precludes: switching to a true async-native DB driver later without a substantial rewrite. Acceptable given the trade-off; SQLite is the storage choice for the lifetime of this project.

## Open questions / 5% uncertainty

- If lossless-hermes ever needs to serve concurrent gateway calls from one Python process (multi-tenant Hermes), sync DB calls will block the event loop. Mitigation: at that point, run multiple worker processes (one per tenant or per CPU core) rather than going async. SQLite's WAL mode supports many readers + one writer per file, which scales with process count, not thread count.
- `asyncio.to_thread(store.method, ...)` is available as an escape hatch if a particular caller measures a real latency problem. It's not the default and shouldn't proliferate.
- apsw vs `sqlite3` choice is decided by sqlite-vec availability (spike-001), not by this ADR. Both are sync; this ADR works either way.
