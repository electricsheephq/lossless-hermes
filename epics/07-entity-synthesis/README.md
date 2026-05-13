# Epic 07 — Entity extraction + synthesis dispatch

## Goal

Port LCM's **async, queue-driven entity coreference worker** and its **on-demand tier-dispatched synthesis orchestrator** from TypeScript to Python. Together these two subsystems implement the v4.1 "extract once async, synthesize many times on demand" contract: leaf-writes enqueue background entity work (60s cadence, race-safe upserts against `lcm_entities`), while the synthesis dispatcher serves `lcm_synthesize_around` requests through a 7-field-keyed cache (`lcm_synthesis_cache`) with three supported pass strategies — `single`, `verify_fidelity`, and `best_of_n_judge` — plus a versioned prompt registry (`lcm_prompt_registry`) and forensic audit trail (`lcm_synthesis_audit`). The unifying property is that both halves are **gated on a working LLM client** (Hermes-side adapter over `agent/llm_client.py`), are completely outside the hot path, and both write cache/catalog rows that downstream tools (`lcm_get_entity`, `lcm_search_entities`, `lcm_synthesize_around`) read via suppression-aware CTEs.

## Deliverables

- `src/lossless_hermes/extraction/coreference.py` — `runCoreferenceTick` port with per-row SAVEPOINT discipline (Wave-7), race-safe `INSERT OR IGNORE` (Wave-1), heartbeat-loss break (Wave-4 P0-1), `countPendingExtractions` filter parity (Wave-10 P2).
- `src/lossless_hermes/extraction/llm_extractor.py` — verbatim prompt template (Wave-4 P0-2 prompt-injection defense), tolerant `parseEntityExtractionResponse`, FNV-1a `surface_hash_for_id`, 16k-char input cap, 30s timeout.
- `src/lossless_hermes/operator/extraction_autostart.py` — 60s cooperative-tick autostart that calls through `tickExtraction` (orchestrator) so the cross-process `lcm_worker_lock` is honored (Wave-1 Auditor #6 finding #4).
- `src/lossless_hermes/tools/entity_shared.py` — module-level `VISIBLE_MENTIONS_CTE` string; consumed by both `tools/get_entity.py` and `tools/search_entities.py` (Wave-12 F4 shared-helper decision).
- `src/lossless_hermes/synthesis/dispatch.py` — `SynthesisDispatcher` class with `synthesize(req)` entrypoint, three pass-strategy branches, cache single-flight, audit insert-before-call.
- `src/lossless_hermes/synthesis/prompt_registry.py` — `register_prompt` (`BEGIN IMMEDIATE`, append-only versioning), `get_active_prompt` (NULL-safe `tier_label` normalization), `bump_bundle_version`.
- `src/lossless_hermes/synthesis/seed_prompts.py` — 10 default prompts from architecture-v4.1.md §12 Appendix A; idempotent skip-if-exists; raw `INSERT` so the migration outer txn isn't broken by a nested `BEGIN`.
- **Cache write + invalidation path** — `lcm_synthesis_cache` single-flight via `INSERT OR IGNORE`, `lcm_cache_leaf_refs` populated post-synthesis, explicit `DELETE` on soft-suppression (cascade does not fire on `suppressed_at` set).
- **Tier-to-model routing policy** — [ADR-031](../../docs/adr/031-synthesis-tier-model-routing.md) (issue 07-10, Option A: match TS exactly — single `LCM_SUMMARY_MODEL` env var, NULL `model_recommendation` in seed). Opinionated haiku→sonnet→opus→opus-thinking ladder deferred to Epic 09 eval. Public surface lives in `src/lossless_hermes/synthesis/tier_routing.py` (re-exports the tier-defaults + pass-strategy tables and the public `pick_synthesis_model`).
- **~80 ported pytest cases** across the four contract test files plus the Wave-N regression set.

## Dependencies

- **Epic 04** (compaction) — needs `summarize.py` for the LLM client seam shape (`deps.complete` / Hermes-adapter contract). The synthesis dispatcher's injected `LlmCall` protocol mirrors the one summarize uses; share the adapter.
- **Epic 05** (embeddings) — needs the worker-loop / worker-lock infra (`concurrency/worker_loop.py`, `concurrency/worker_lock.py`, `operator/worker_orchestrator.py`). Entity extraction is the *second* job kind registered against that infrastructure (embedding-backfill is the first).
- **Epic 01** (storage, soft dep) — `lcm_extraction_queue`, `lcm_entities`, `lcm_entity_mentions`, `lcm_entity_type_registry`, `lcm_prompt_registry`, `lcm_synthesis_cache`, `lcm_synthesis_audit`, `lcm_cache_leaf_refs` are all DDL'd in 01-06. This epic wires the seeding callback per ADR-005 option A.

## Blocks

- **Epic 09** (eval) — eval lanes invoke entity-extraction-quality and synthesis-fidelity tests against this epic's outputs; epic 09 cannot run end-to-end without it.

## Critical path

**NO.** The gateway can boot, ingest, summarize, and serve `lcm_get_*` reads with this epic absent — `lcm_get_entity` would return empty, `lcm_synthesize_around` would 5xx with `extraction_disabled`. Useful → not deployable; but unblocks Epic 09 in parallel with Epic 08.

## Estimated total effort

**3 weeks (~50–72 hours).** Breakdown from `docs/porting-guides/entity-extraction.md` (16–24 h) + `docs/porting-guides/synthesis.md` (16–24 h) + ~16–24 h glue (autostart wiring, cache integration tests, audit retention sweep, model-ladder seed):

- Entity extraction core (coref + LLM extractor + prompt + tests): ~14–18 h.
- Extraction autostart wiring + worker-orchestrator tickExtraction: ~4–6 h.
- Synthesis dispatch (3 pass branches + cache single-flight + audit): ~16–22 h.
- Prompt registry + seed (11 prompts + idempotency): ~6–8 h.
- Cache invalidation + leaf-refs + suppression-aware CTE: ~4–6 h.
- Wave-N regression tests + integration smoke: ~6–8 h.

## Confidence

**90%.** One sub-95% item remains:

- **LLM adapter shape:** `synthesis.md` calls out that Hermes's existing client must be adapted into the `LlmCall` protocol. Until Epic 04 lands the adapter, this is a known-unknown — the worker-LLM dispatch and cost-accounting shape might need revision once the real Anthropic SDK token-count payload is in hand.
- ~~**Tier-to-model policy (Open Decision A):**~~ **RESOLVED** per [ADR-031](../../docs/adr/031-synthesis-tier-model-routing.md) (issue 07-10, merged 2026-05-14). Option A: Python port matches TS exactly — single `LCM_SUMMARY_MODEL` env var, NULL `model_recommendation` in seed. Opinionated tier ladder deferred until Epic 09 eval data exists; deferral marker is `lossless_hermes.synthesis.tier_routing.TIER_LADDER_DEFERRED`. Confidence bump from 85% → 90% reflects that this resolution is in the codebase, not deferred.

Everything else — the SQL, the worker-tick algorithm, the cache key composition, the audit-row lifecycle, the prompt-registry append-only contract, the Wave-N fixes — is fully specified in the porting guides and either spike-verified or has direct TS-side test parity.

## Issues

10 issues, port-order-aware (shared helpers + prompt registry first, then entity workers, then synthesis dispatch, then invalidation + audit, then the deferred ADR):

| # | Title | Hours | Confidence |
|---|---|---:|---:|
| 07-01 | Entity shared CTE (`VISIBLE_MENTIONS_CTE`) | 1–2 | 98% |
| 07-02 | Entity coreference worker (498 LOC; Wave-1, Wave-7, Wave-10) | 12–16 | 88% |
| 07-03 | Entity extractor LLM (234 LOC; Wave-4 prompt-injection) | 4–6 | 92% |
| 07-04 | Extraction autostart (214 LOC; worker-loop integration) | 4–6 | 88% |
| 07-05 | Synthesis dispatch (817 LOC; 3 pass kinds + best-of-N) | 14–18 | 82% |
| 07-06 | Synthesis cache-key composition + write path (Wave-10 P1) | 3–4 | 92% |
| 07-07 | Cache invalidation on suppression (`lcm_cache_leaf_refs`) | 2–3 | 95% |
| 07-08 | Prompt registry + seed default prompts (305 + 435 LOC) | 6–8 | 92% |
| 07-09 | Synthesis audit row writes + retention sweep | 3–4 | 92% |
| 07-10 | Tier-to-model routing seed (ADR-? Open Decision A) | 2–4 | 70% |

Approximate total: **51–71 hours** — congruent with the 50–72 h epic estimate.

## ADRs that gate this epic

All accepted at 95%+:

- **ADR-017** (sync vs async DB) — coreference worker calls sync SQLite inside an async task body; no `await` inside transactions.
- **ADR-018** (concurrency model) — cross-process via `lcm_worker_lock` (`job_kind='extraction'`); in-process via the autostart `inFlight` boolean.
- **ADR-020** (worker loop dispatcher) — extraction is the second `asyncio.Task` kind registered against `WorkerLoop` (after embedding-backfill from Epic 05).
- **ADR-024** (project layout) — `extraction/`, `synthesis/`, and `tools/entity_shared.py` paths are pinned.
- **ADR-029** (Wave-N provenance) — every Wave-1, Wave-4, Wave-7, Wave-10 fix in this epic carries the inline `# LCM Wave-N (date): ...` comment.

ADR resolved during epic:

- **[ADR-031](../../docs/adr/031-synthesis-tier-model-routing.md): Tier-to-model routing.** See 07-10. Option A: match TS exactly — single `LCM_SUMMARY_MODEL` env var, NULL `model_recommendation` in seed; opinionated ladder deferred to Epic 09 eval (v0.2 candidate).

## Out of scope for this epic

- **Fuzzy / semantic coreference** — voyage-3-lite entity embeddings for "PR 71676" vs "pull-request 71676". LCM v4.1 explicitly defers; the Python port mirrors that deferral. Tracked as a future ADR placeholder.
- **`/lcm prompts add|list|show|diff` slash-commands** — TS source has none; if added, moves to Epic 08-cli-ops (per `synthesis.md` Open Decisions §4).
- **Honcho dialectic-user-modeling integration** — orthogonal subsystem owned by Hermes-agent itself; no LCM → Honcho bridge in v0.1 (per `entity-extraction.md` cross-reference §).
- **Extended-thinking adapter knobs** — if the model-ladder seed adopts opus-thinking for yearly judge, the `thinking={"type": "enabled", ...}` Anthropic-SDK plumbing lives in Epic 04's adapter, not here.

## Verification gates before close

1. `pytest tests/extraction/` + `pytest tests/synthesis/` green on the macOS + Linux CI matrix.
2. `count_pending_extractions` and `run_coreference_tick`'s selector agree on a synthetic 100-row queue (Wave-10 P2 regression).
3. Cache UNIQUE-index integration test: same leaf set, two `tier` values (`custom` then `filtered`) → two distinct cache rows (Wave-10 P1 regression).
4. Verify-fidelity flag test: `UNSUPPORTED: X\nOK rest` does NOT clear the hallucination flag (Wave-4 P0 regression).
5. Best-of-N cap test: `req.best_of_n=10` clamps to 5 and surfaces `requested=10, capped=true` on the result (Wave-5 P2 regression).
6. Soft-suppress test: suppressing a leaf DELETES dependent cache rows via `lcm_cache_leaf_refs` lookup (Final.review.3 Loop 2 Leak 2.5 regression).
7. `grep -rn "# LCM Wave-" src/lossless_hermes/extraction src/lossless_hermes/synthesis` enumerates ≥ 8 Wave-marked sites (ADR-029 audit trail).
8. `seed_default_prompts(conn)` is idempotent across two consecutive calls; an operator-overridden prompt row is never clobbered.
