---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-07] operator: port extraction-autostart (214 LOC, worker-loop integration)'
labels: 'port, epic-07'
---

## Source (TypeScript)

- File: `src/operator/extraction-autostart.ts`
- Lines: 214 LOC
- Function(s)/class(es): `tryStartExtractionAutostart(db, opts) → ExtractionAutostartHandle`, `ExtractionAutostartHandle { stop, isRunning, tickCount }`

## Target (Python)

- File: `src/lossless_hermes/operator/extraction_autostart.py`
- Estimated LOC: ~240

## What this issue covers

The 60-second cooperative-tick autostart loop that drives `run_coreference_tick` (from 07-02) at a steady cadence. This module is the in-process scheduler; the worker-loop infrastructure (`concurrency/worker_loop.py` from Epic 05, ADR-020) is the multi-job dispatcher this hooks into.

The autostart **must** call through `tick_extraction` (in `operator/worker_orchestrator.py`, ported in Epic 05), NOT `run_coreference_tick` directly. The Wave-1 Auditor #6 finding #4 was: bypassing the worker-lock orchestration causes two gateways booting simultaneously to double-process the queue.

Lifecycle contract:

- **Cadence:** 60s default (`DEFAULT_EXTRACTION_INTERVAL_S = 60.0`). Operator override via `context.lossless_hermes.workers.entity-extraction.interval_s` (ADR-023). Note that ADR-020 currently defaults entity-extraction to 30s; reconcile with porting-guide's 60s — the porting guide wins (matches TS `DEFAULT_EXTRACTION_INTERVAL_MS = 60_000`).
- **Initial delay:** 10s after gateway boot (`STARTUP_DELAY_S = 10.0`).
- **Opt-out:** `LCM_EXTRACTION_LLM_ENABLED=false` (default ON — extraction is intrinsic, not opt-in like embeddings which costs Voyage tokens). When opt-out, return a no-op handle (stop=noop, is_running=False, tick_count=0).
- **Pre-flight:** `deps.complete` must be available (gateway has at least one LLM provider configured). If absent, return a no-op handle and log info-level.
- **Per-tick guard:** `in_flight` boolean (or `asyncio.Lock`) drops overlapping ticks. ADR-018's generation-counter mechanism also applies if running under `WorkerLoop`.

Auto-stop conditions:

- **3 consecutive idle ticks** (queue empty) → log once at info, KEEP polling cheaply. Idle means `count_pending_extractions == 0` at tick start.
- **3 consecutive tick-throw failures** → log error, stop, require gateway restart. Per-extractor failures are absorbed into `result.extractor_failures` and do NOT burn the consecutive-failures budget.
- **Outer-tick body throws** (e.g. DB closed mid-tick during shutdown) — also count as consecutive failure. This was the v4.1 Final.review.3 Loop-9 B2 HIGH fix (extraction was modeled on backfill but lost the outer try/catch in cycle-2).
- **Lock skip:** if `tick_extraction` returns `lock_acquired = False` (initial acquire failed OR heartbeat lost mid-tick → flipped to False by Wave-7), log debug and skip. This lets a sibling gateway hold the lock without us treating it as failure.

## Dependencies

- Depends on: 07-02 (`run_coreference_tick` is what `tick_extraction` invokes under the lock), 07-03 (`create_entity_extractor_llm` factory called once at autostart-start), Epic 05 (`worker_loop.py`, `worker_lock.py`, `operator/worker_orchestrator.py::tick_extraction`), Epic 04 (`deps.complete` adapter)
- Blocks: nothing (terminal node for this epic's entity-extraction half)

## Acceptance criteria

- [ ] `try_start_extraction_autostart(db, *, log, deps, interval_s=60.0, env=None, extractor_fn=None) → ExtractionAutostartHandle` signature matches the porting-guide skeleton
- [ ] When `LCM_EXTRACTION_LLM_ENABLED=false`, returns a no-op handle (`stop` is callable noop, `is_running()` returns False)
- [ ] When `deps.complete` is None, returns a no-op handle and logs info `"extraction_autostart_skipped: no_llm_client"`
- [ ] Initial 10s startup delay before first tick
- [ ] Tick interval respects the constructor's `interval_s`; override via env var documented but not implemented here (Hermes config delivery handles that — ADR-023)
- [ ] Calls through `tick_extraction(db, kind='entity', ...)` — NOT `run_coreference_tick` directly (Wave-1 Auditor #6 finding #4)
- [ ] 3-consecutive-tick-throw shutdown latches the loop off; `is_running()` returns False afterward
- [ ] 3-consecutive-idle log fires once at info; subsequent idle ticks log at debug only
- [ ] `lock_acquired = False` does NOT increment the consecutive-failures counter
- [ ] `extractor_failures` in the per-tick result does NOT increment the consecutive-failures counter (per-call extractor failures are not tick failures)
- [ ] `pytest tests/operator/test_extraction_autostart.py` passes (5+ cases)
- [ ] No new mypy errors with strict mode

## Tests to port

| Source | LOC | Cases |
|---|---:|---|
| `test/v41-wiring.test.ts` (relevant subset) | ~30 | (1) `LCM_EXTRACTION_LLM_ENABLED=false` → no-op handle; (2) `deps.complete` missing → no-op handle |
| `test/operator-worker-orchestrator.test.ts` (relevant subset) | ~50 | (3) `tick_extraction` returns `lock_acquired=False` → skip counted but not failure; (4) heartbeat-lost mid-tick → `lock_lost_mid_tick=True` logged |
| New tests this issue adds | — | (5) 3-consecutive-throw stops the loop; (6) 3-consecutive-idle logs once; (7) extractor-failure does NOT advance consecutive-failures budget (Final.review.3 Loop-9 B2 regression); (8) initial 10s delay observed; (9) `stop()` cancels the task within 2s |

## Estimated effort

**4–6 hours.** Hermes already uses the `asyncio.create_task` + `while running and gen == my_gen` pattern (`tools/mcp_tool.py:_schedule_tools_refresh`, `plugins/platforms/google_chat/adapter.py:_run_supervisor`); this is a direct application. Most cost is in the test harness around `asyncio.sleep` (use `pytest-asyncio` + `FakeClock` or `freezegun`).

## Confidence

**88%.** Residual risk:

- **ADR-020 vs porting-guide cadence mismatch.** ADR-020 specifies `entity-extraction: 30s` as the default; the porting guide and TS source both specify 60s. Resolve to 60s and update ADR-020's table or open an ADR addendum. Caught by the lifecycle smoke test if mis-set.
- **Multi-session reuse.** Hermes plugins are session-scoped but the worker loop is process-scoped. The first session starts the loop; subsequent sessions must NOT restart it (the generation-counter protects against this, but the autostart's `is_running` check needs to return True early). Caught by a multi-session integration test in Epic 02.
