# Epic 08 — CLI + Operator Commands + Doctor

## Goal

Full operator command surface for `lossless-hermes`: the `/lcm` slash-command dispatcher (12 owner-gated + 7 open subcommands), the doctor toolkit (read-only scan + apply for both summary repair and DB-wide cleaners), the soft-suppression purge cascade with its 10+ read-path invariants, the worker orchestrator (status + force-tick), the autostart loops for embedding-backfill / entity-extraction / semantic-infra, the eval runner, the DB-backup primitive, and the one-shot `lossless-hermes import-openclaw` CLI for migrating existing OpenClaw users (ADR-025). This is where operators interact with the engine — every command in this epic is the front door for diagnosing, repairing, migrating, and observing LCM state in production.

## Deliverables

- `src/lossless_hermes/plugin/commands.py` — `/lcm` slash-command dispatcher (subcommand router replacing the Epic 02 scaffold), token splitter for `--reason "..."` quoting, alias registration for `/lossless`.
- `src/lossless_hermes/plugin/db_backup.py` — `VACUUM INTO` primitive consumed by `/lcm backup`, `/lcm rotate`, and `applyDoctorCleaners` mandatory-backup step.
- `src/lossless_hermes/doctor/{contract,shared,apply,cleaners}.py` — full doctor contract surface (Pydantic models + marker detector + per-conversation summary repair + DB-wide row deletion).
- `src/lossless_hermes/operator/purge.py` — `runPurge` soft-suppression cascade in one `BEGIN IMMEDIATE` (6-step soft suppression — summaries, condensed contains_suppressed flag, context_items cuts ×2, messages, synthesis cache).
- `src/lossless_hermes/operator/reconcile.py` — `reconcileSessionKeys` list + apply modes with `--allow-main-session` safeguard.
- `src/lossless_hermes/operator/worker_orchestrator.py` — `getWorkerStatusSnapshot`, `tickEmbeddingBackfill`, `tickExtraction`, `forceReleaseLock`, `heartbeatAllHeldLocks` (merged with `worker-llm.ts` adapter per doctor-ops.md table line 314).
- `src/lossless_hermes/operator/{backfill,extraction}_autostart.py` — opt-in/opt-out background loops using ADR-020 worker-loop dispatcher.
- `src/lossless_hermes/operator/eval_runner.py` — `runEval`, `formatEvalReport`, recall@K with hybrid Voyage embedding cost.
- `src/lossless_hermes/operator/semantic_infra.py` — one-time vec0 + embedding-profile registration.
- `src/lossless_hermes/cli/import_openclaw.py` — `lossless-hermes import-openclaw <dir>` per ADR-025.
- **Soft-suppression cascade** wired through 10+ read paths (per doctor-ops.md §"Read paths that filter `suppressed_at IS NULL`"): summary-store (11), conversation-store (5), embeddings backfill/store/semantic-search (2+2+2), entity coreference (3), tools grep/describe/search-entities/synthesize-around/get-entity/entity-shared (4+3+2+2+1+1), and operator/health (1). These read paths are owned by their parent epics — Epic 08 owns the invariant and ships a regression test (`tests/v41/test_suppression_invariants.py`) covering every surface.
- Ported pytest equivalents for `test/operator-{purge,health,reconcile-session-keys,eval-runner,worker-orchestrator}.test.ts`, `test/v41-suppression-{cascade-trigger,fts-filter,invariants}.test.ts`, `test/v41-data-cleanup.test.ts`, plus new dedicated tests for `applyScopedDoctorRepair` and `applyDoctorCleaners` (coverage gap called out in doctor-ops.md §"Test inventory").

## Dependencies

- **Epic 02 (engine skeleton)** — `/lcm` slash-command scaffold (single-handler stub that returns "help"); Epic 08 replaces this stub with the full dispatch table. Engine instance + `current_session_id` accessor; circuit-breaker state for `/lcm health`.
- **Epic 04 (compaction)** — `LcmSummarizer` for `applyScopedDoctorRepair` (the doctor-apply path re-runs the leaf/condensed summarizer on rows with fallback/truncated markers; needs the same prompt-build + provider-resolution + fallback-chain machinery).
- **Epic 05 (worker infra)** — `lcm_worker_lock` + `WORKER_JOB_KINDS` + `acquire_lock` / `release_lock` / `heartbeat_lock` (per doctor-ops.md "Remaining 5% risk" #3). The worker orchestrator (08-10) and autostart loops (08-11/12) consume this infra; they cannot land without it.
- **Storage stores (Epic 01)** — `summaries`, `conversations`, `messages`, `context_items`, `summary_messages`, `summary_parents`, `lcm_synthesis_cache`, `lcm_cache_leaf_refs`, plus the `suppressed_at` columns + `lcm_embed_suppress_<slug>` triggers (already created in 01-06 per doctor-ops.md §"Schema additions to support suppression").

## Blocks

**None.** Parallel with Epic 07 (entity synthesis) and Epic 09 (eval suite). The eval runner (08-13) ships the *runner*; the eval suite (queries, golden judges) is Epic 09.

## Critical path

**NO.** v0.1.0 can ship without the operator surface — the `ContextEngine` ABC + per-turn ingest/assembly/compaction (Epics 02–05) deliver the user-facing functionality. Epic 08 is what makes production operations possible: without `/lcm health` operators can't observe; without `/lcm doctor apply` they can't repair; without `/lcm purge` and `/lcm reconcile-session-keys` they can't recover from data corruption; without `import-openclaw` existing OpenClaw users can't migrate. Treat Epic 08 as a hard requirement for v0.1.0 GA, but not a blocker for v0.1.0-alpha.

## Estimated total effort

**3–4 weeks (~70–90 hours)** across 17 issues. Breakdown:

- Dispatcher + token splitter + aliases (08-01): ~6 h
- Read-only commands `status`/`health`/`worker status` (08-02, 08-03, 08-17 read path): ~10 h
- Doctor shared + apply + cleaners (08-06/07/08): ~20 h
- Purge soft-suppression cascade (08-04): ~10 h
- Reconcile (08-05): ~6 h
- DB backup (08-09): ~4 h
- Worker orchestrator + worker tick (08-10, 08-17 tick path): ~8 h
- Autostart loops backfill/extraction (08-11/12): ~10 h
- Eval runner (08-13): ~6 h
- Semantic-infra init (08-14): ~3 h
- Rotate (08-16): ~4 h
- `import-openclaw` CLI (08-15): ~6 h
- Buffer (cross-issue integration + suppression-invariant regression matrix): ~5 h

## Confidence

**90%.** The cleaners, integrity checks, purge cascade, doctor-apply ordering, eval-runner contract, and reconcile semantics are all well-specified in `docs/porting-guides/doctor-ops.md` (5,363 source LOC catalogued; every TS function has a Python target). The 10% residual lives in:

1. **Doctor-apply LLM coupling** (doctor-ops.md "Remaining 5% risk" #2) — `applyScopedDoctorRepair` pulls in `createLcmSummarizeFromLegacyParams` + `LcmDependencies`; the Hermes equivalent depends on how Epic 04's `LcmSummarizer` shapes its DI surface. Mitigated by sequencing 08-07 after Epic 04.
2. **`PluginCommandContext.sessionId` divergence** (plugin-glue.md "Remaining 5% risk" #1) — TS handlers read `ctx.sessionId`; Hermes handlers receive only `raw_args: str`. Engine-internal `current_session_id` covers `/lcm status` and `/lcm rotate`, but verifying every subcommand is dry-run against an OpenClaw lcm.db copy.
3. **Owner-gating is upstream** (ADR-013) — destructive subcommands do NOT check `is_owner` themselves. Operators MUST set `allow_admin_from` in `config.yaml`. Documented + a startup warning if unset, but a configuration hazard remains.
4. **Soft-suppression invariant surface** — 45 occurrences of `suppressed_at IS NULL` across the TS source must be mirrored in the Python ports owned by other epics; the test in Epic 08 (`test_suppression_invariants.py`) catches regressions but cannot prevent them at write time.
5. **Voyage vs alternative embedder** (doctor-ops.md ADR-? line 448) — backfill autostart is hardcoded to `VOYAGE_API_KEY`. If Hermes later abstracts the embedder, 08-11 needs a provider-shaped refactor.

## Issues

| # | Title | Hours | Confidence | Depends on |
|---|---|---:|---:|---|
| [08-01](./08-01-slash-command-router.md) | `/lcm` subcommand dispatch table replacing Epic 02 scaffold | 6 | 95% | Epic 02 scaffold |
| [08-02](./08-02-status.md) | `/lcm status` — info-level health snapshot | 4 | 95% | 08-01, Epic 02 engine |
| [08-03](./08-03-health.md) | `/lcm health` — detailed v4.1 health probe | 6 | 90% | 08-01, Epic 05 workers |
| [08-04](./08-04-purge-soft-suppression.md) | `runPurge` + 6-step soft-suppression cascade | 10 | 90% | 08-01, Epic 01 schema |
| [08-05](./08-05-reconcile-session-keys.md) | `reconcileSessionKeys` list + apply | 6 | 92% | 08-01 |
| [08-06](./08-06-doctor-shared.md) | Doctor contract surface — markers + targets + stats | 4 | 95% | 08-01 |
| [08-07](./08-07-doctor-apply.md) | `applyScopedDoctorRepair` — per-conversation summary repair | 10 | 85% | 08-06, Epic 04 summarizer |
| [08-08](./08-08-doctor-cleaners.md) | `applyDoctorCleaners` + 3 predefined predicates | 8 | 90% | 08-06, 08-09 backup |
| [08-09](./08-09-backup.md) | `/lcm backup` — `VACUUM INTO` primitive | 4 | 95% | 08-01 |
| [08-10](./08-10-worker-orchestrator.md) | Worker orchestrator (merging `worker-llm.ts`) | 6 | 90% | 08-01, Epic 05 workers |
| [08-11](./08-11-backfill-autostart.md) | Embedding-backfill autostart loop | 5 | 88% | 08-10, ADR-020 |
| [08-12](./08-12-extraction-autostart.md) | Entity-extraction autostart loop | 5 | 90% | 08-10, ADR-020, Epic 07-04 |
| [08-13](./08-13-eval-runner.md) | `/lcm eval` runner (recall@K + drift) | 6 | 90% | 08-01 |
| [08-14](./08-14-semantic-infra-init.md) | One-time vec0 + embedding-profile init | 3 | 92% | 08-01 |
| [08-15](./08-15-import-openclaw-cli.md) | `lossless-hermes import-openclaw` CLI per ADR-025 | 6 | 90% | 08-01 |
| [08-16](./08-16-rotate.md) | `/lcm rotate` — force DB rotation if applicable | 4 | 90% | 08-09 backup |
| [08-17](./08-17-worker-status.md) | `/lcm worker status` (open) + `/lcm worker tick` (gated) | 4 | 92% | 08-10 |

Approximate total: **97 hours** — within the 70–90 h planning range after accounting for ~10% scope overlap (08-09 ⇄ 08-08 backup, 08-10 ⇄ 08-17 status/tick).

## ADRs that gate this epic

All accepted at 90%+:

- **ADR-013** (owner-gating) — pure upstream gate via `gateway/slash_access.SlashAccessPolicy`; no per-handler `is_owner` checks; startup warning if `allow_admin_from` is unset.
- **ADR-020** (worker-loop dispatcher) — `asyncio.create_task` per worker kind, generation-counter guard, no apscheduler/cron.
- **ADR-023** (config delivery) — `lossless_hermes.*` namespace in `~/.hermes/config.yaml`, snake_case keys, pydantic v2 validation. Worker-interval overrides via `lossless_hermes.workers.<kind>.interval_s`.
- **ADR-024** (project layout) — `src/lossless_hermes/operator/` peer of `doctor/` (the latter promoted out of `plugin/`); `commands.py` under `plugin/`.
- **ADR-025** (OpenClaw migration) — explicit `lossless-hermes import-openclaw` CLI; default `~/.openclaw` source; refuses without `--force` if destination exists; `shutil.copy2` + idempotent migration + identity-hash sample validation.

## Out of scope for this epic

- **Hard-delete drainer** (TS `mode='immediate'`) — removed in the first-principles pass (2026-05-06) per doctor-ops.md §"Prune cascade"; `runPurge` always returns `mode: "soft"`. Byte-level GDPR erasure stays out-of-band (raw `DELETE` + `VACUUM`).
- **JSONL transcript-rewrite branch** of `transcript_repair.py` — Hermes is SQLite-only; the on-disk rewrite path drops (engine.md §"State owned by LcmContextEngine").
- **Eval query set + golden judges** — Epic 09 owns these. Epic 08 ships the runner that consumes them.
- **Hermes-cron-based autostarts** — ADR-020 chose in-process `asyncio.create_task` over apscheduler/external cron; no cron entries are added by this epic.
- **Per-subcommand `request_context` thread-local** — ADR-013 chose pure upstream gating. If Hermes core adds `request_context` later, defense-in-depth checks can be added in a follow-up.
- **`lcm_doctor_audit` table** (doctor-ops.md ADR-? line 445) — keep doctor logs ephemeral (option (a) parity with TS) until operators ask for retroactive forensics.

## Verification gates before close

1. `pytest tests/operator/` and `tests/v41/` green on all CI matrix cells.
2. `pytest tests/commands/test_owner_gating.py` — mocked `SlashAccessPolicy.deny()` causes every destructive subcommand to return the upstream-rejection text and never invoke the handler body.
3. `pytest tests/v41/test_suppression_invariants.py` — every read surface (summary-store, conversation-store, embeddings, semantic-search, entity-coreference, tools, health) excludes `suppressed_at IS NOT NULL` rows by default; the `include_suppressed: true` opt-out works on integrity, compaction, and doctor.
4. `pytest tests/commands/test_purge.py::test_cascade_full_six_steps` — runs `runPurge` against a seeded fixture and asserts each of the six cascade steps (per doctor-ops.md §"runPurge SOFT SUPPRESSION") fired correctly in one `BEGIN IMMEDIATE`.
5. **OpenClaw migration smoke** — `lossless-hermes import-openclaw --from tests/fixtures/openclaw-mini --validate-rows 100` against a 100-conv fixture: schema migrates, identity-hash sample validates 100/100 matched, `state_meta.lcm_db_imported_at` is written.
6. **Doctor-apply LLM seam** — `tests/doctor/test_apply.py::test_leaves_first_then_condensed` confirms the override-map ordering: condensed re-summarization reads its leaf children's (possibly rewritten) content from the in-memory `overrides` map.
7. **Worker-orchestrator tick budget** — `tests/operator/test_worker_orchestrator.py::test_backfill_tick_processes_200` confirms the 200-embedding-per-tick budget bound from TS `worker-orchestrator.ts:tickEmbeddingBackfill`.
8. `mypy --strict src/lossless_hermes/operator src/lossless_hermes/doctor src/lossless_hermes/plugin/commands.py src/lossless_hermes/cli` passes.

## Source of truth

- **Porting guide:** [`docs/porting-guides/doctor-ops.md`](../../docs/porting-guides/doctor-ops.md) (the full 5,363-LOC operator + doctor map)
- **Plugin-glue cross-reference:** [`docs/porting-guides/plugin-glue.md`](../../docs/porting-guides/plugin-glue.md) §"/lcm slash commands — full inventory", §"Owner-gating in Hermes"
- **ADRs:** [013 owner-gating](../../docs/adr/013-owner-gating.md), [020 worker-loop](../../docs/adr/020-worker-loop-dispatcher.md), [023 config delivery](../../docs/adr/023-config-delivery.md), [024 project layout](../../docs/adr/024-project-layout.md), [025 OpenClaw migration](../../docs/adr/025-openclaw-migration.md)
- **TS source:** `lossless-claw/src/plugin/lcm-command.ts` (2884 LOC), `src/plugin/lcm-doctor-{shared,apply,cleaners}.ts` (270+541+641 LOC), `src/plugin/lcm-db-backup.ts` (82 LOC), `src/operator/` (eight files, ~2517 LOC)
- **TS tests:** `test/operator-{purge,health,reconcile-session-keys,eval-runner,worker-orchestrator}.test.ts`, `test/v41-{suppression-cascade-trigger,suppression-fts-filter,suppression-invariants,data-cleanup,authorization-invariants}.test.ts`
