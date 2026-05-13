---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-08] cli-ops: port backfill-autostart.ts (embedding backfill loop)'
labels: 'port, epic-08-cli-ops'
---

## Source (TypeScript)

- File: `src/operator/backfill-autostart.ts`
- Lines: 264 LOC
- Function(s)/class(es): `tryStartBackfillAutostart(db)`, internal `_backfillTickLoop`, `_isBackfillEnabled`, `_handleBackfillFailure`

## Target (Python)

- File: `src/lossless_hermes/operator/backfill_autostart.py`
- Estimated LOC: ~280

## What this issue covers

The embedding-backfill autostart loop — best-effort background drainer that calls `tick_embedding_backfill` (from 08-10) every 5 minutes by default. Opt-in via `VOYAGE_API_KEY` env var (per plugin-glue.md §"Env vars consumed by the plugin" line 615) — without the key, the loop never starts. Default OFF (operator opt-in).

### Behavior (from doctor-ops.md §"Background workers" lines 320–322)

- Fires once at plugin load via `register(ctx)` body (per plugin-glue.md "Plugin registration sequence" item 10).
- Spawns a worker-loop dispatcher task per ADR-020 (`asyncio.create_task` per worker kind, generation-counter guard).
- Default cadence: **60s** per ADR-020 §"Per-job interval defaults" (TS source uses 5min `setInterval`; ADR-020 reduces this to 60s for finer-grained drain. Operators override via `lossless_hermes.workers.embedding_backfill.interval_s` per ADR-023.).
- **Auto-stop on 3 consecutive idle ticks** (no work to do — backfill is caught up).
- **Auto-stop on 3 consecutive failures** (Voyage 401 / 429 / 5xx). Operator must manually restart via `/lcm worker tick embedding-backfill` once the issue is resolved.
- All ticks acquire the `embedding-backfill` worker lock (ADR-018); if a peer process holds it, the tick is a no-op and counts as idle.

### Per ADR-020 (worker-loop dispatcher)

This issue does NOT implement the loop dispatcher itself — that's owned by Epic 05 / ADR-020 (`src/lossless_hermes/concurrency/worker_loop.py`). This issue registers a job with that dispatcher:

```python
def try_start_backfill_autostart(loop: WorkerLoop, deps: BackfillDeps) -> BackfillAutostartHandle | None:
    if not _is_backfill_enabled(deps):
        log_startup_banner_once("backfill-disabled", "[lcm] Embedding backfill autostart disabled (VOYAGE_API_KEY not set)")
        return None

    state = _BackfillAutostartState(consecutive_idle=0, consecutive_failures=0)

    async def tick() -> None:
        if state.stopped:
            return
        try:
            result = await tick_embedding_backfill(deps)
            if result.processed == 0:
                state.consecutive_idle += 1
                if state.consecutive_idle >= 3:
                    state.stopped = True
                    log.info("[lcm] backfill autostart stopped (3 consecutive idle ticks)")
            else:
                state.consecutive_idle = 0
                state.consecutive_failures = 0
        except Exception as exc:
            _handle_backfill_failure(state, exc)

    loop.register_job(kind="embedding-backfill", run_fn=tick, interval_s=deps.interval_s)
    return BackfillAutostartHandle(state=state)
```

### Voyage-specific dependency call-out

Per doctor-ops.md "Operator modules" line 310: "Re-evaluate — depends on whether Hermes embeds via Voyage." This issue assumes Voyage is the embedder (matching the TS source and Epic 05's stance). If Hermes later abstracts the embedder (pgvector + OpenAI embeddings, Qdrant + local model, etc.), this module needs a follow-up to consume a provider-shaped abstraction; track as ADR-? per doctor-ops.md line 448.

## Dependencies

- Depends on: #08-10 (worker orchestrator — provides `tick_embedding_backfill`), Epic 05 (worker lock infra + `WorkerLoop` dispatcher per ADR-020), Voyage credentials per ADR-022.
- Blocks: nothing — failure to start is a no-op.

## Acceptance criteria

- [ ] `try_start_backfill_autostart(loop, deps) -> BackfillAutostartHandle | None` returns `None` when `VOYAGE_API_KEY` is not set; logs a startup banner once.
- [ ] When enabled, registers a job on the `WorkerLoop` with kind `"embedding-backfill"` and default `interval_s=60`.
- [ ] Operators override interval via `lossless_hermes.workers.embedding_backfill.interval_s` (ADR-023 contract).
- [ ] 3 consecutive idle ticks (`processed == 0`) sets `state.stopped = True` and logs the stop.
- [ ] 3 consecutive failures sets `state.stopped = True` and logs the stop.
- [ ] A `processed > 0` tick resets BOTH `consecutive_idle` and `consecutive_failures` to 0.
- [ ] Lock contention from a peer process (acquire returns None) is treated as idle (counts toward the 3-idle stop).
- [ ] `BackfillAutostartHandle.stop()` flips `state.stopped = True`; the next tick exits early.
- [ ] All TS test cases in `test/backfill-autostart.test.ts` (if present; the TS source doesn't have a dedicated test file but the behavior is exercised via `test/operator-worker-orchestrator.test.ts`) have ported pytest equivalents in `tests/operator/test_backfill_autostart.py`.
- [ ] **New test:** `tests/operator/test_backfill_autostart.py::test_disabled_without_voyage_key` — env var unset → `None` returned, no task created.
- [ ] **New test:** `tests/operator/test_backfill_autostart.py::test_three_consecutive_idle_stops` — mock `tick_embedding_backfill` to return `processed=0` three times, assert `state.stopped`.
- [ ] **New test:** `tests/operator/test_backfill_autostart.py::test_three_consecutive_failures_stops` — mock to raise three times, assert `state.stopped`.
- [ ] **New test:** `tests/operator/test_backfill_autostart.py::test_success_resets_counters` — pattern: fail, fail, succeed, fail, fail → not stopped (counter reset on succeed).
- [ ] Function signatures match the spec in [docs/porting-guides/doctor-ops.md](../../docs/porting-guides/doctor-ops.md) §"Operator modules" line 310.
- [ ] `pytest tests/operator/test_backfill_autostart.py` passes.
- [ ] No new mypy errors (`mypy --strict src/lossless_hermes/operator/backfill_autostart.py`).
- [ ] PR description cites LCM commit `1f07fbd` (pr-613 head).

## Estimated effort

**5 hours.**

## Confidence

**88%** — well-defined behavior; the auto-stop counters and lock-handoff semantics port cleanly. 12% risk: ADR-020's worker-loop dispatcher must land first (Epic 05); the Voyage-vs-other-embedder question (doctor-ops.md line 448) is a follow-up if Hermes abstracts the embedder.
