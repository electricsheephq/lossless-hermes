---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-08] cli-ops: port worker-orchestrator.ts (merging worker-llm.ts)'
labels: 'port, epic-08-cli-ops'
---

## Source (TypeScript)

- File: `src/operator/worker-orchestrator.ts` (250 LOC) + `src/operator/worker-llm.ts` (167 LOC, merged per doctor-ops.md table line 314)
- Function(s)/class(es): `getWorkerStatusSnapshot(db)`, `tickEmbeddingBackfill(deps)`, `tickExtraction(deps)`, `forceReleaseLock(db, kind, host?)`, `heartbeatAllHeldLocks(db, host)`, `createWorkerLlmCall(deps)` (from worker-llm.ts)

## Target (Python)

- File: `src/lossless_hermes/operator/worker_orchestrator.py`
- Estimated LOC: ~440 (combined)

## What this issue covers

The thin coordinator over the cross-process worker lock surface — powers `/lcm worker status` (read-only) and `/lcm worker tick <kind>` (owner-gated forced tick). Per doctor-ops.md table line 315: this module imports `acquireLock`, `releaseLock`, `heartbeatLock`, `lockInfo`, `generateWorkerId` from `src/concurrency/worker-lock.js` plus `WORKER_JOB_KINDS` from `src/concurrency/model.js` (Epic 05 dependency — see doctor-ops.md "Remaining 5% risk" #3).

### Merging `worker-llm.ts`

Per doctor-ops.md table line 314: "Adapter that wraps `deps.complete` into the `LlmCall` signature consumed by `dispatchSynthesis`. Merge into `operator/worker_orchestrator.py` or `synthesis/dispatch.py` (167 LOC, no independent state)."

This issue takes the merge — `worker_orchestrator.py` exports `create_worker_llm_call(deps)` alongside the orchestrator surfaces. The adapter is small (~60 LOC after stripping TS-specific wrappers), shares lifecycle with the worker tick paths, and avoids creating a 167-LOC standalone module.

### Surfaces

1. **`get_worker_status_snapshot(db) -> WorkerStatusSnapshot`** — read-only. Returns:
   ```python
   class WorkerStatusSnapshot(BaseModel):
       workers: list[WorkerStatus]
       pending_embedding_backfill: int
       pending_entity_extraction: int
       pending_condensation_maintenance: int

   class WorkerStatus(BaseModel):
       kind: str  # "embedding-backfill" | "entity-extraction" | "condensation-maintenance"
       held: bool
       held_by_host: str | None
       held_by_worker_id: str | None
       started_at: str | None
       last_heartbeat_at: str | None
       stale: bool  # last_heartbeat_at older than ttl
   ```

2. **`tick_embedding_backfill(deps) -> TickResult`** — force one tick of the embedding backfill worker. Per plugin-glue.md line 430: "200 paid Voyage embeddings per call. Owner-gated because of paid quota burn." Returns `{"processed": N, "skipped_reason": <if no work> | None}`.

3. **`tick_extraction(deps) -> TickResult`** — force one tick of the entity-extraction worker. Drains `lcm_extraction_queue` for newly-created leaves.

4. **`force_release_lock(db, kind, host=None) -> ReleaseResult`** — admin escape hatch when a worker crashed without releasing its lock. If `host` is provided, only releases if the lock is held by that host (defense against accidentally releasing another node's active lock).

5. **`heartbeat_all_held_locks(db, host) -> HeartbeatResult`** — periodic refresh from inside the worker loop dispatcher (ADR-020). Updates `last_heartbeat_at` for every lock currently held by `host`.

6. **`create_worker_llm_call(deps) -> LlmCall`** — adapter that wraps `deps.complete(prompt, model)` into the signature expected by `dispatchSynthesis` (Epic 07-04). The adapter handles provider resolution, timeout, fallback chain — same shape as Epic 04-06's summarize fallback but for synthesis-side calls.

### Cross-process lock semantics (from ADR-018, brought in by Epic 05)

- Each tick path: `lock = acquire_lock(db, kind, host, worker_id, ttl=90)` → if `None`, return early ("lock held by peer; skipping"); otherwise do work, then `release_lock(lock)` in `finally`.
- Heartbeat every 30s (per ADR-018 TTL=90s, 3× headroom).
- Stale detection: lock with `last_heartbeat_at` older than `ttl` is considered abandoned; `force_release_lock` cleans it up; status snapshot flags it `stale: true`.

## Dependencies

- Depends on: #08-01 (dispatcher), Epic 05 (`lcm_worker_lock` table + `acquire_lock` / `release_lock` / `heartbeat_lock` / `lock_info` / `generate_worker_id` — per doctor-ops.md "Remaining 5% risk" #3).
- Blocks: #08-11 (backfill autostart consumes `tick_embedding_backfill`), #08-12 (extraction autostart consumes `tick_extraction`), #08-17 (worker status + tick handlers).

## Acceptance criteria

- [ ] `get_worker_status_snapshot(db)` returns a pydantic model with the four pending counts + one entry per `WORKER_JOB_KINDS`.
- [ ] Stale detection: a lock with `last_heartbeat_at` older than ttl is flagged `stale=True`.
- [ ] `tick_embedding_backfill(deps)` processes ≤200 embeddings per call (per plugin-glue.md line 430 contract).
- [ ] `tick_extraction(deps)` drains the queue in batches; respects circuit-breaker state.
- [ ] `force_release_lock(db, kind, host=None)` only releases if the lock is held by `host` (when `host` is given) or unconditionally otherwise; returns `{released: bool, reason: str}`.
- [ ] `heartbeat_all_held_locks(db, host)` updates `last_heartbeat_at` for every lock held by `host`.
- [ ] `create_worker_llm_call(deps)` returns a callable matching Epic 07-04's `LlmCall` signature.
- [ ] All TS test cases in `test/operator-worker-orchestrator.test.ts` have ported pytest equivalents in `tests/operator/test_worker_orchestrator.py`.
- [ ] **New test:** `tests/operator/test_worker_orchestrator.py::test_backfill_tick_processes_200` (per Epic README "Verification gates" #7) — 250 unembedded leaves, single tick processes exactly 200.
- [ ] **New test:** `tests/operator/test_worker_orchestrator.py::test_tick_skipped_when_lock_held_by_peer` — second tick with `acquire_lock` returning None returns early.
- [ ] **New test:** `tests/operator/test_worker_orchestrator.py::test_force_release_host_guard` — `force_release_lock(kind, host="other-host")` doesn't release a lock held by `"this-host"`.
- [ ] Function signatures match the spec in [docs/porting-guides/doctor-ops.md](../../docs/porting-guides/doctor-ops.md) §"Operator modules" line 315.
- [ ] `pytest tests/operator/test_worker_orchestrator.py` passes.
- [ ] No new mypy errors (`mypy --strict src/lossless_hermes/operator/worker_orchestrator.py`).
- [ ] PR description cites LCM commit `1f07fbd` (pr-613 head).

## Estimated effort

**6 hours.**

## Confidence

**90%** — well-tested in TS (`test/operator-worker-orchestrator.test.ts`). The merger of `worker-llm.ts` is straightforward (167 LOC adapter; no independent state). 10% risk is Epic 05's lock-API surface stability — if `acquire_lock`'s signature drifts, this issue needs a small adapter pass.
