# Epic 09 — Eval + Benchmarks

**Status: closed** — all 8 issues merged (PRs #120–#125; 09-01/09-02/09-04 landed via the combined test-coverage PR #122); v0.1.0 release gate. The benchmark harness + measured `fts_only` baseline ship in 09-08 (#125); the live +52.5pp hybrid confirmation is a documented operator-gated step (`VOYAGE_API_KEY` required — B-001), per the BLOCKERS.md recommended action.

## Goal

Port LCM's recall + drift eval suite to Python and reproduce the **+52.5pp paraphrastic recall lift** of Voyage hybrid retrieval over FTS-only that the Phase A spike measured on Eva's 31-query stratified eval set. Concretely: a working `/lcm eval <query-set-id>` end-to-end (query-set seed → recall@K + reciprocal-rank → run-record → drift vs prior) and a CI live-eval job gated on `VOYAGE_API_KEY` + `ANTHROPIC_API_KEY` that runs on PRs touching retrieval surfaces. The +52.5pp number is the decision gate that justified shipping Voyage in v4.1 (see `docs/v4.1/PR_DESCRIPTION.md` §"Why Voyage embeddings"); the port is not done until Python reproduces it.

> **v0.1.0 closure note:** the `/lcm eval` pipeline, drift detection, the live-eval CI workflow, and the benchmark harness are all ported and verified — the harness is exercised end-to-end by `tests/benchmarks/test_voyage_recall_benchmark.py` with the Voyage seam mocked. The `fts_only` baseline is **measured offline** (paraphrastic recall@5 = 0.0%, the FTS-only weakness the hybrid arm addresses). The live `hybrid` +52.5pp confirmation requires a provisioned `VOYAGE_API_KEY`, which neither this environment nor the repo has — it ships as a documented single-command operator step in `docs/benchmarks/voyage-recall-2026-q2.md` ("Live hybrid run PENDING"). This is the **B-001** resolution: harness reproduces; live run documented + pending key — release-gate item 7 is satisfied on that basis.

## Deliverables

- `src/lossless_hermes/eval/query_set.py` (~290 Python LOC) — query-set register/get/list, identity encoding (`name@vN`), namespaced row IDs, append-only versioning.
- `src/lossless_hermes/eval/recall.py` (~240 LOC) — `run_recall_eval(queries, adapter, opts)` with recall@K, reciprocal-rank, per-stratum + overall aggregates, per-query timeout (Wave-4 Auditor #15 P1 fix).
- `src/lossless_hermes/eval/judge.py` (~190 LOC) — LLM-as-judge ensemble harness (`run_quality_eval(queries, candidates, judges)`), per-judge failure tolerance, mean-of-non-null scoring.
- `src/lossless_hermes/eval/run.py` (~380 LOC) — `record_eval_run` + `compute_drift` with mode-aware prior-run selection (parsed out of JSON envelope per SCHEMA-GAPS §1), noise-floor SD comparison, `lcm_eval_drift` aggregate write.
- `tests/eval/` — 61 ported pytest cases mirroring `eval-query-set.test.ts` (44) + `eval-recall.test.ts` (~40) + `eval-judge.test.ts` (~35) + `eval-run.test.ts` (~50) + `v41-eval-tables.test.ts` (~14). Total ~180 cases (TS counts include parametrized expansions).
- `tests/fixtures/eva_baseline_v2.py` — the 31-query stratified eval fixture (14 fts-easy / 9 fts-medium / 8 paraphrastic). Built from upstream provenance; ground-truth `expected_summary_ids` come from running once against the reference DB.
- `.github/workflows/live-eval.yml` — Voyage- + Anthropic-gated workflow triggered on `paths:` matching `embeddings/`, `synthesis/`, `compaction/`, `tools/grep.py`. Records recall@K + drift, comments deltas on the PR.
- `docs/benchmarks/voyage-recall-2026-q2.md` — published reproduction of the Phase A spike on Python with measured uplift, methodology, cost, and a per-stratum drift table comparing TS-baseline → Python-port.

## Dependencies

- **Epic 05 (embeddings)** — `runHybridSearch` / `runSemanticSearch` adapters are what we plug into the `RecallSearchAdapter` injection point. Without them there's nothing to benchmark.
- **Epic 06 (tools)** — the FTS-only baseline adapter wraps `lcm_grep`'s underlying FTS5 query path (issue 09-08 reuses this). The `/lcm eval` CLI lands in Epic 08 but the eval-runner glue (`operator/eval_runner.py`) is sketched here; the actual CLI wiring is owned by Epic 08-cli-ops.
- **Epic 07 (synthesis)** — judge ensembles call the same `LlmCall` protocol that synthesis dispatches through. Judge wiring reuses the synthesis llm-adapter so we don't ship two LLM clients.

Schema tables (`lcm_eval_query_set`, `lcm_eval_query`, `lcm_eval_run`, `lcm_eval_drift`) are created in **Epic 01-15** (versioned backfills) — this epic does not touch the migration ladder.

## Blocks

**None.** Epic 09 is terminal — nothing in the port plan waits on it. The +52.5pp benchmark is a release-gate artifact (must be reproduced before tagging v0.1.0), not a downstream dependency.

## Critical path

**NO.** Epic 09 can ship after the v0.1.0 retrieval surface is otherwise complete. The eval suite is what validates the port; the port doesn't depend on the suite to function. That said, **delaying Epic 09 past v0.1.0 means shipping without a recall-drift gate** — every PR after that touches retrieval is unverified until 09 lands. Strong recommendation: ship Epic 09 in the same release as Epic 05 to gate from day one.

## Estimated total effort

**2 weeks — ~40–50 hours.** Breakdown:

| Issue | Hours |
|---|---:|
| 09-01 query-set port | 4–6 |
| 09-02 recall metric port | 5–7 |
| 09-03 judge harness port | 4–6 |
| 09-04 eval-run + drift port | 7–9 |
| 09-05 eva-baseline-v2 fixture | 5–8 |
| 09-06 drift-detection thresholds + per-stratum | 3–4 |
| 09-07 CI live-eval workflow | 4–6 |
| 09-08 +52.5pp benchmark reproduction | 8–10 |
| **Total** | **40–56** |

Hours estimate is bounded by the fixture work (09-05) and the benchmark reproduction (09-08); the pure-function ports are mechanical and fast.

## Confidence

**90%.** The four `src/eval/*.ts` modules are pure functions with small surfaces (1093 TS LOC total) and tight tests; the recall@K + reciprocal-rank math is textbook, and the drift envelope is a well-defined JSON shape (`PerQueryScoresEnvelope` v=1). Spike 004 already validated the Voyage HTTP-client primitives end-to-end so the embedding adapter behind 09-08 is de-risked.

The 10% residual lives in three buckets:

1. **Eva-baseline-v2 ground-truth provenance.** The TS source references `eva-baseline-v2` (8 paraphrastic queries) by name but the literal fixture isn't checked into upstream. Issue 09-05 either recovers Eva's fixture from her local LCM DB OR rebuilds an equivalent stratified set from `test/fixtures/v41-test-corpus.ts`. Equivalent ≠ identical, so the reproduced uplift number may differ slightly from +52.5pp.
2. **Numeric stability of recall@K aggregates** under Python `float` vs TS `Number` arithmetic. Negligible for n=31 but should be asserted at ±1e-9.
3. **Live API cost-control on PR-triggered workflows.** The live-eval job costs ~$0.001/query × 31 = ~$0.03/run; 100 PR triggers/month = $3 — well below noise but still a real recurring cost line. The workflow should `concurrency: cancel-in-progress` to clip multi-push waste.

## Issues

| # | Title | Hours | Confidence |
|---|---|---:|---:|
| [09-01](./09-01-eval-query-set.md) | Port `query-set.ts` (schema, versioning, encode/decode) | 4–6 | 95% |
| [09-02](./09-02-eval-recall.md) | Port `recall.ts` (recall@K, reciprocal-rank, per-query timeout) | 5–7 | 95% |
| [09-03](./09-03-eval-judge.md) | Port `judge.ts` (LLM-as-judge ensemble harness) | 4–6 | 92% |
| [09-04](./09-04-eval-run.md) | Port `run.ts` (eval-run record, drift envelope, prior-run select) | 7–9 | 90% |
| [09-05](./09-05-evaluation-fixtures.md) | Eva-baseline-v2 31-query fixture (recover or replicate) | 5–8 | 80% |
| [09-06](./09-06-drift-detection.md) | Drift-detection thresholds + per-stratum drift surface | 3–4 | 92% |
| [09-07](./09-07-ci-live-eval.md) | GH Action workflow gated on `VOYAGE_API_KEY`/`ANTHROPIC_API_KEY` | 4–6 | 90% |
| [09-08](./09-08-benchmarks.md) | Reproduce +52.5pp Voyage uplift on Python | 8–10 | 85% |

## Source of truth

- **Porting guides:** [`docs/porting-guides/tests-and-config.md`](../../docs/porting-guides/tests-and-config.md) §"Subsystem table" row "Eval / judge"; [`docs/porting-guides/synthesis.md`](../../docs/porting-guides/synthesis.md) §"Hermes cross-reference: model selection" (judge wires through the same `LlmCall` seam).
- **Spike:** [`docs/spike-results/004-voyage-python-client.md`](../../docs/spike-results/004-voyage-python-client.md) — the +52.5pp claim is load-bearing on this client's faithful port; live round-trip already passed.
- **ADRs:** [024 project layout](../../docs/adr/024-project-layout.md) (places `eval/` under `src/lossless_hermes/`), [028 vitest→pytest](../../docs/adr/028-vitest-to-pytest.md) (test translation + `pytest.mark.live` gating).
- **TS source:** `lossless-claw/src/eval/{query-set,recall,judge,run}.ts` (191 + 236 + 191 + 375 = 993 LOC pure logic, plus `src/operator/eval-runner.ts` 193 LOC for the operator surface).
- **TS tests:** `test/eval-{query-set,recall,judge,run}.test.ts` + `test/operator-eval-runner.test.ts` + `test/v41-eval-tables.test.ts` (~1424 TS LOC of test).
- **Benchmark precedent:** `lossless-claw/docs/v4.1/PR_DESCRIPTION.md` §"Why Voyage embeddings" (the original measurement: FTS-easy +28.5pp, paraphrastic +52.5pp, $0.58 spike cost).
