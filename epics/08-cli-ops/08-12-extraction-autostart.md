---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-08] cli-ops: port extraction-autostart.ts (entity coreference loop)'
labels: 'port, epic-08-cli-ops'
---

## Source (TypeScript)

- File: `src/operator/extraction-autostart.ts`
- Lines: 214 LOC
- Function(s)/class(es): `tryStartExtractionAutostart(db, deps)`, internal `_extractionTickLoop`, `_isExtractionEnabled`, `_handleExtractionFailure`

## Target (Python)

- File: `src/lossless_hermes/operator/extraction_autostart.py`
- Estimated LOC: ~230

## What this issue covers

The entity-coreference autostart loop — best-effort background drainer that calls `tick_extraction` (from 08-10) every 30s by default (per ADR-020 §"Per-job interval defaults"; TS source uses 60s). Per plugin-glue.md §"Env vars consumed by the plugin" line 614: "default on, opt-out via `LCM_EXTRACTION_LLM_ENABLED=false`".

### Behavior (from doctor-ops.md §"Background workers" line 322)

- Fires once at plugin load via `register(ctx)` body (per plugin-glue.md "Plugin registration sequence" item 11).
- Default cadence: **30s** per ADR-020. Operators override via `lossless_hermes.workers.entity_extraction.interval_s`.
- **Default ON.** Operators opt OUT via `LCM_EXTRACTION_LLM_ENABLED=false` (env-var-only kill switch — no YAML field, matching TS).
- **Auto-stop on 3 consecutive failures** (LLM errors). Operator must manually restart via `/lcm worker tick entity-extraction` once resolved.
- **No auto-stop on idle** (unlike backfill at 08-11) — extraction queue can be empty for long stretches; staying running lets new leaves get extracted promptly.
- All ticks acquire the `entity-extraction` worker lock (ADR-018); peer holds the lock → tick is a no-op.

### Coordination with Epic 07-04

The task brief notes: "Cross-reference Epic 07-04." Epic 07-04 (the entity-coreference module) provides the actual extraction logic (`coreference.py`, `llm_extractor.py`). This issue (08-12) wraps that logic in the autostart-loop scaffolding. The seam is `tick_extraction(deps)` — defined in 08-10's `worker_orchestrator.py`, which delegates to Epic 07-04's `coreference.process_one_batch(...)`.

If Epic 07-04 lands later than this issue, deliver this issue's loop scaffolding with a placeholder `tick_extraction` that no-ops (returns `processed=0`); Epic 07-04 then wires the real implementation into the orchestrator.

### Per ADR-020 (worker-loop dispatcher)

Same pattern as 08-11 — registers a job with the central `WorkerLoop`:

```python
def try_start_extraction_autostart(loop: WorkerLoop, deps: ExtractionDeps) -> ExtractionAutostartHandle | None:
    if not _is_extraction_enabled(deps):
        log_startup_banner_once("extraction-disabled", "[lcm] Entity extraction autostart disabled (LCM_EXTRACTION_LLM_ENABLED=false)")
        return None

    state = _ExtractionAutostartState(consecutive_failures=0)

    async def tick() -> None:
        if state.stopped:
            return
        try:
            result = await tick_extraction(deps)
            if result.processed > 0:
                state.consecutive_failures = 0
        except Exception as exc:
            _handle_extraction_failure(state, exc)

    loop.register_job(kind="entity-extraction", run_fn=tick, interval_s=deps.interval_s)
    return ExtractionAutostartHandle(state=state)
```

## Dependencies

- Depends on: #08-10 (worker orchestrator — provides `tick_extraction`), Epic 05 (worker lock + `WorkerLoop`), **Epic 07-04** (the entity-coreference logic this loop drives).
- Blocks: nothing.

## Acceptance criteria

- [ ] `try_start_extraction_autostart(loop, deps) -> ExtractionAutostartHandle | None` returns `None` when `LCM_EXTRACTION_LLM_ENABLED=false`; logs a banner once.
- [ ] When enabled (default), registers a job on the `WorkerLoop` with kind `"entity-extraction"` and default `interval_s=30`.
- [ ] Operators override interval via `lossless_hermes.workers.entity_extraction.interval_s`.
- [ ] 3 consecutive failures sets `state.stopped = True`.
- [ ] A successful tick resets `consecutive_failures` to 0.
- [ ] No auto-stop on idle — `processed=0` does NOT increment any counter (idle is the steady state).
- [ ] Lock contention from peer process is a no-op (does not count as failure).
- [ ] `ExtractionAutostartHandle.stop()` flips `state.stopped = True`.
- [ ] No dedicated TS test file for extraction-autostart (exercised via `test/operator-worker-orchestrator.test.ts` indirectly).
- [ ] **New test:** `tests/operator/test_extraction_autostart.py::test_default_on` — env var unset → handle returned, task registered.
- [ ] **New test:** `tests/operator/test_extraction_autostart.py::test_opt_out_via_env` — `LCM_EXTRACTION_LLM_ENABLED=false` → None returned, no task.
- [ ] **New test:** `tests/operator/test_extraction_autostart.py::test_three_failures_stops` — three raises, then `state.stopped`.
- [ ] **New test:** `tests/operator/test_extraction_autostart.py::test_idle_does_not_stop` — 100 consecutive `processed=0` ticks, still running.
- [ ] **New test:** `tests/operator/test_extraction_autostart.py::test_success_resets_failures` — fail, fail, succeed, fail → not stopped.
- [ ] Function signatures match the spec in [docs/porting-guides/doctor-ops.md](../../docs/porting-guides/doctor-ops.md) §"Operator modules" line 311.
- [ ] `pytest tests/operator/test_extraction_autostart.py` passes.
- [ ] No new mypy errors (`mypy --strict src/lossless_hermes/operator/extraction_autostart.py`).
- [ ] PR description cites LCM commit `1f07fbd` (pr-613 head).

## Estimated effort

**5 hours.**

## Confidence

**90%** — mirror of 08-11's structure with one difference (no idle auto-stop). The dependency on Epic 07-04 is sequenced via the orchestrator seam (`tick_extraction`), so this issue can land in parallel with a placeholder.
