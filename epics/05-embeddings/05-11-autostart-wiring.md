---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-05] operator: wire backfill cron into operator/backfill_autostart.py'
labels: 'port, embeddings, operator, autostart'
---

## Source (TypeScript)
- File: `lossless-claw/src/operator/backfill-autostart.ts`
- Lines: 264
- Function(s)/class(es): `startEmbeddingBackfillAutostart`, `AutostartOptions`, `AutostartHandle`, `DEFAULT_AUTOSTART_INTERVAL_MS` (5 minutes), 3-strike idle-drain detection, 3-strike Voyage-failure backoff.

This issue is the **minimal wiring** to make backfill ticks auto-fire when `VOYAGE_API_KEY` is present. The full operator surface (the rest of `src/operator/*.ts`) is ported in **Epic 08 (CLI/Ops)** — this issue cross-references that epic and ports only the autostart sub-module.

## Target (Python)
- File: `src/lossless_hermes/operator/backfill_autostart.py`
- Estimated LOC: ~260

## Dependencies
- Depends on: #05-05 (worker loop), #05-06 (worker lock), #05-07 (backfill tick function), #05-08 (`getActiveEmbeddingModel` for the model name lookup). Also depends on Epic 02 (engine lifecycle hooks — `on_session_start` / `on_session_end` for start/stop wiring).
- Blocks: nothing in Epic 05. Epic 08 (CLI/Ops) integrates this autostart module into the broader `/lcm worker` command surface; until Epic 08 lands, autostart is the operator-facing path for getting the corpus embedded.

## Acceptance criteria

- [ ] **`start_embedding_backfill_autostart(db, *, log, interval_s=300, env=None, tick_fn=None) -> AutostartHandle`** (port of `backfill-autostart.ts:startEmbeddingBackfillAutostart`):
  - **Opt-in gating:** if `(env or os.environ).get("VOYAGE_API_KEY", "").strip()` is empty → silent no-op; return a stub `AutostartHandle` that does nothing on `stop()`. (Future: also check the three-tier resolver per #05-02, but the explicit env-var presence is the operator-clear "I want this" signal per the TS `backfill-autostart.ts` module docstring.)
  - **Default interval:** 5 minutes (`DEFAULT_AUTOSTART_INTERVAL_S = 5 * 60` — matches TS `DEFAULT_AUTOSTART_INTERVAL_MS / 1000`).
  - **Per-tick body:** call `tick_fn or tick_embedding_backfill` from #05-07. Pass `model_name` resolved from `getActiveEmbeddingModel(db)`. If no active profile is registered → log a warning and return a no-op handle.
  - **Worker-loop integration:** start a `WorkerLoop` (from #05-05) with a single registered job kind `"embedding-backfill"`, `interval_s=interval_s`, `run=` a closure that wraps the tick call.
- [ ] **3-strike idle drain** (port of `backfill-autostart.ts`):
  - After each tick, if `BackfillResult.embedded_count == 0 AND BackfillResult.skipped == [] AND count_pending_docs(db) == 0`, increment `idle_strikes`.
  - When `idle_strikes >= 3`, stop the autostart loop (the corpus is fully embedded).
  - Reset to 0 on any non-empty tick.
- [ ] **3-strike Voyage-failure backoff** (port of `backfill-autostart.ts`):
  - After each tick, if the tick raised a non-auth exception or `BackfillResult` had ≥ 1 voyage_5xx/voyage_other skips, increment `voyage_failure_strikes`.
  - When `voyage_failure_strikes >= 3`, stop the autostart loop and log a clear "back off; manual intervention" message. Operator can `/lcm worker tick embedding-backfill` once Voyage is healthy.
  - Reset to 0 on any clean tick.
  - **Auth errors are NOT covered by the 3-strike** — they raise out of the tick and stop the autostart loop immediately (the operator must fix `VOYAGE_API_KEY` before any retry makes sense).
- [ ] **`AutostartHandle`** dataclass with:
  - `stop() -> None` — idempotent; stops the underlying `WorkerLoop`.
  - `is_running() -> bool` — for `/lcm health` and tests.
  - `tick_count: int` — how many ticks have fired (diagnostic).
  - `idle_strikes: int`, `voyage_failure_strikes: int` — diagnostic.
- [ ] **Lifecycle integration:** the autostart is started from `LCMEngine.on_session_start` (or equivalent) ONLY on the first session and ONLY if the gating env var is set. Stopped via `LCMEngine.on_session_end` for the last session OR `atexit`. Both paths call `handle.stop()`.
- [ ] **Cross-process safety:** the autostart's tick acquires the same `lcm_worker_lock` row as a manual `/lcm worker tick embedding-backfill` would (via #05-06). If two gateway processes both auto-start, only one's ticks do work — the other gets `lock_not_acquired=True` and silently skips.
- [ ] **NOT auto-started (out of scope per TS source docstring):**
  - Entity coreference (needs LLM injection through plugin lifecycle — deferred to Epic 07 / cycle-2).
  - Procedure mining (Epic 07).
  - Themes consolidation (Epic 07).
- [ ] **Manual `/lcm worker tick embedding-backfill`** still works (Epic 08 surfaces this). Autostart just makes it unnecessary in the typical case.
- [ ] **Logging:** info-level on start ("[backfill-autostart] starting; interval=300s"), on idle-drain stop ("[backfill-autostart] corpus drained after 3 consecutive idle ticks; stopping"), on Voyage-failure stop ("[backfill-autostart] 3 consecutive Voyage failures; backing off — set VOYAGE_API_KEY or check status"), on each successful tick (`embedded=N tokens=K skipped=M`).
- [ ] `mypy --strict` and `ty check` pass.
- [ ] Test coverage with mocked tick function (no real Voyage calls).

## Tests (`tests/operator/test_backfill_autostart.py`)

- **`VOYAGE_API_KEY` empty → silent no-op.** `start_embedding_backfill_autostart(db, env={})` returns a handle with `is_running()=False`. No worker-loop started.
- **`VOYAGE_API_KEY` set + no active profile → no-op + warning log.**
- **Happy path:** stage a corpus, set the env var, register a profile; start autostart with a short interval (e.g. `interval_s=0.1`); inject a mock `tick_fn` that returns `BackfillResult(embedded_count=5, ...)`; verify `handle.tick_count` increases over time.
- **Idle drain:** 3 consecutive ticks return `embedded_count=0, skipped=[], pending=0`; verify `handle.is_running()` becomes `False`.
- **Idle reset:** after 2 idle ticks, a non-idle tick resets the counter (verify via injecting tick results).
- **Voyage failure backoff:** 3 consecutive ticks raise a non-auth exception (or return skipped with reason="voyage_5xx"); verify `handle.is_running()` becomes `False` and the documented log line fires.
- **Auth error:** a tick raises `VoyageError(kind="auth")`; verify autostart stops immediately (NOT after 3 strikes) and the auth-error message is logged.
- **Cross-process:** two `start_embedding_backfill_autostart` calls in the same process (simulating two gateways) — verify both start, but only one's ticks acquire the lock (the other ticks see `lock_not_acquired=True`).
- **`handle.stop()` is idempotent** and stops the underlying worker loop.
- **`AutostartHandle.tick_count, idle_strikes, voyage_failure_strikes`** reflect state.

## Estimated effort
3 hours

## Confidence
90% — the TS source is a self-contained 264-LOC module with clear gating + strike logic. The cross-references to #05-05 / #05-06 / #05-07 are mechanical. Residual 10%:
- The 3-strike thresholds (idle + Voyage-failure) are TS magic numbers; port verbatim. If operators report different drainage profiles, tune in a follow-up.
- The lifecycle wiring (where exactly to call `start` and `stop`) depends on Epic 02's engine lifecycle. Coordinate with Epic 02's `LCMEngine.on_session_start` / `on_session_end` shape; until that's pinned, expose `start_embedding_backfill_autostart` as a callable that's wired in by Epic 02 / Epic 08 rather than by this issue.
- **Cross-references Epic 08 for full operator port:** this issue ports only the autostart sub-module. The rest of `src/operator/*.ts` (`purge.ts`, `health.ts`, `reconcile.ts`, `worker_orchestrator.ts`, `eval_runner.ts`, `semantic_infra.ts`, `worker_llm.ts`, `extraction_autostart.ts`) lands in Epic 08.

## Files to read before starting
- `docs/porting-guides/embeddings.md` (no dedicated autostart section, but the backfill section §"Backfill cron" gives the tick contract this autostart wraps)
- TS source: `lossless-claw/src/operator/backfill-autostart.ts` (entire — 264 LOC)
- TS source: `lossless-claw/src/operator/worker-orchestrator.ts` (the `tickEmbeddingBackfill` callable this issue invokes — defined in Epic 08, but the signature is fixed by #05-07)
