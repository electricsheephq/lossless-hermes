# Epic 04: Compaction

**Status: closed** — all 8 issues merged (PRs #70, #73, #75, #81, #83–#84, #91, #93); v0.1.0 release gate.

LCM-style compaction engine: leaf-pass + condensation passes, anti-thrashing guards, fallback model chain, circuit breaker integration, and telemetry write paths. This is the algorithmic heart of LCM — the engine that decides *when* to summarize, runs the summarization safely, and persists the result into the DAG without losing data on auth/timeout/quota failures.

## Goal

Working LCM-style compaction:

- **`evaluate()`** triggers (context-threshold + leaf-trigger) decide if compaction is needed.
- **`leafPass`** collapses one contiguous chunk of raw messages outside the fresh tail into a leaf summary.
- **`condensedPass`** collapses N contiguous same-depth summaries into one depth+1 condensed summary.
- **3 anti-thrashing guards** (per-pass progress, compactUntilUnder bail-out, summarize-escalation "didn't compress") prevent runaway loops.
- **Fallback model chain** (normal → aggressive → deterministic) survives provider outages with Wave-4 deterministic markers always present.
- **Circuit breaker integration** with the LcmContextEngine fields ported in Epic 02 (opens after N consecutive auth failures, cooldown, half-open probe).
- **Telemetry write paths** (`compactionTelemetryStore` updates + `CompactionResult` shape) so Epic 06's `lcm_compact` tool can report status.

## Deliverables

- `src/lossless_hermes/compaction.py` (~1830 LOC target) — ports `lossless-claw/src/compaction.ts` (1831 LOC) verbatim modulo TS→Python translation.
- `src/lossless_hermes/summarize.py` (~1700 LOC target) — ports `lossless-claw/src/summarize.ts` (1696 LOC). Owns prompt construction, provider resolution, auth handling, retries.
- 3 prompt templates ported **verbatim** as Python string-builders: `_build_leaf_prompt`, `_build_condensed_prompt` (D1/D2/D3+ variants), `_build_deterministic_fallback`. The exact string content is load-bearing — operators have tuned prompts against observed compaction quality.
- Full retry/fallback chain: 5-layer provider candidate resolution, exponential backoff (`min(500 * 2^index, 8000)ms`), `skipModelAuth` retry path, `LcmProviderAuthError` propagation, `SummarizerTimeoutError` → deterministic fallback.
- Wave-4/9 deterministic-fallback marker invariant: the `[LCM fallback summary — model unavailable; ...]` prefix is ALWAYS present in fallback output (even when source ≤ char budget) so operators can distinguish "LLM down" from "LLM produced this."
- Per-conversation context-items cache + refcount (port of `withContextCache` from compaction.ts lines 362–403). Cache invalidation on every `replaceContextRangeWithSummary`.
- `CompactionResult` dataclass with `auth_failure: bool` flag — the auth-short-circuit path that avoids persisting fallback summaries during transient provider outages.

## Dependencies

- **Epic 02 (engine-skeleton)** — `LcmContextEngine` shell with circuit-breaker state fields (`_circuit_breaker_states`), session locks, store handles, sync `compress()` ABC entry point. Compaction is invoked from `engine/compact.py`'s `_CompactMixin.compress()` body.
- **Epic 03 (ingest-assembly)** — `ContextAssembler` and `estimate_tokens` must exist so compaction can read `getContextTokenCount`, dispatch on resolved context items, and compute target/input token budgets. Compaction reads `assembler` indirectly via `summaryStore.getContextItems` + `getContextTokenCount`.

## Blocks

- **Epic 06 (tools)** — the `lcm_compact` tool wraps `engine.compact()` and reports `CompactionResult` to the agent. Without this epic, the tool has nothing to call.
- **Epic 07 (entity-synthesis)** — synthesis dispatch reuses `summarize.py`'s prompt-building scaffolding (system prompt, target-token resolution, normalization helpers) for its own LLM calls. The `LcmSummarizer` class is the seam.

## Critical path: YES

Compaction is the load-bearing differentiator of LCM. Without it, the port is "Hermes with a custom retrieval layer" — not LCM. Every wave-N audit fix this epic preserves (Wave-4 markers, Wave-12 progress checks, auth-short-circuit) is scar tissue from real production incidents. Skipping or deferring any guard regresses a known failure.

## Estimated total effort

**~60–90 hours** spread across 8 issues. Breakdown:

| Issue | Hours |
|---|---:|
| 04-01 evaluate() trigger logic | 4–6 |
| 04-02 leaf-pass + select-oldest-chunk | 10–14 |
| 04-03 condensation pass | 10–14 |
| 04-04 anti-thrashing guards | 4–6 |
| 04-05 3 prompt templates verbatim | 6–8 |
| 04-06 summarize fallback chain | 12–16 |
| 04-07 circuit-breaker integration | 6–8 |
| 04-08 telemetry write paths | 4–6 |
| **Total** | **56–78** |

Add ~10% buffer for integration testing + the cache+refcount pattern → 60–90 hours is the planning range.

## Confidence: 90%

The algorithm is well-documented in `docs/porting-guides/assembler-compaction.md` and the TS source has tight test coverage (`compaction-maintenance-store.test.ts`, `summarize.test.ts`, `lcm-summarizer-reasoning.test.ts`). The 10% uncertainty is concentrated in:

1. **Hermes's `auxiliary_client.call_llm` lacks a `reasoning` parameter** — LCM's conservative-retry path (`reasoning: "low"`) must be remapped to `extra_body={"reasoning_effort": "low"}` or accepted as a same-settings retry. See `assembler-compaction.md` Remaining-5%-risk §4.
2. **Sync vs async** (ADR-017) — compaction is sync end-to-end per the decided ADR, but `summarize` is the one call that genuinely benefits from a timeout. Use `concurrent.futures.ThreadPoolExecutor` + `Future.result(timeout=...)` rather than `asyncio.wait_for` in the sync wrapper.
3. **`MessageRecord.tokenCount` divergence** — compaction's running-delta arithmetic depends on `token_count` being populated at insert time (Epic 01/03 contract). If insertion paths leave it zero on some rows, the running-delta optimization degrades but compaction still works.

## Issues

| # | Title | Hours | Confidence |
|---|---|---:|---:|
| [04-01](./04-01-compaction-evaluate.md) | Port `compaction.evaluate()` trigger logic | 4–6 | 95% |
| [04-02](./04-02-compaction-leaf-pass.md) | Port `compactFullSweep` + `runLeafPass` | 10–14 | 90% |
| [04-03](./04-03-compaction-condensation.md) | Port condensation pass (depth N+1) | 10–14 | 90% |
| [04-04](./04-04-anti-thrashing.md) | Port the 3 anti-thrashing guards | 4–6 | 95% |
| [04-05](./04-05-summarize-prompt-templates.md) | Port 3 prompt templates verbatim | 6–8 | 95% |
| [04-06](./04-06-summarize-fallback-chain.md) | Port fallback model chain + auth-failure detection | 12–16 | 85% |
| [04-07](./04-07-circuit-breaker-integration.md) | Wire circuit-breaker fields into summarize.py | 6–8 | 90% |
| [04-08](./04-08-telemetry-write.md) | Port `compactionTelemetryStore` writes + `CompactionResult` | 4–6 | 95% |

## Source of truth

- **Porting guide:** [`docs/porting-guides/assembler-compaction.md`](../../docs/porting-guides/assembler-compaction.md) (sections "Compaction — full algorithm walkthrough", "Summarize — LLM seam")
- **Engine integration:** [`docs/porting-guides/engine.md`](../../docs/porting-guides/engine.md) §"compact(params)" + §"Circuit-breaker logic"
- **ADRs:** [017 sync-vs-async](../../docs/adr/017-sync-vs-async-db.md), [018 concurrency](../../docs/adr/018-concurrency-model.md), [024 layout](../../docs/adr/024-project-layout.md), [027 engine splitting](../../docs/adr/027-engine-splitting.md), [029 wave-fix provenance](../../docs/adr/029-wave-fix-provenance.md)
- **TS source:** `lossless-claw/src/compaction.ts` (pr-613, 1831 LOC), `lossless-claw/src/summarize.ts` (pr-613, 1696 LOC)
- **TS tests:** `test/compaction-maintenance-store.test.ts`, `test/summarize.test.ts`, `test/lcm-summarizer-reasoning.test.ts`, `test/circuit-breaker.test.ts`
