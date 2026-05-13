---
name: Port issue
about: Reproduce +52.5pp Voyage paraphrastic uplift on Python
title: '[epic-09] eval: reproduce +52.5pp Voyage paraphrastic uplift on Python'
labels: 'port, epic-09-eval, benchmark'
---

## Source (TypeScript)

- **Measurement:** `docs/v4.1/PR_DESCRIPTION.md` lines 325–334 (in `lossless-claw`) — Phase A spike result table:

  | Stratum | n | FTS-only | Hybrid (Voyage rerank-2.5) | Lift |
  |---|---|---|---|---|
  | FTS-easy | 14 | 40.5% | **69.0%** | +28.5pp |
  | FTS-medium | 9 | not graded | not graded | — |
  | Paraphrastic | 8 | 5.0% | **57.5%** | **+52.5pp** |

  Spike cost: $0.58 total (one-time eval).
- **Code under test (TS):** `src/embeddings/hybrid-search.ts` + `src/embeddings/semantic-search.ts` + `src/voyage/client.ts`. The +52.5pp number is the **threshold** that justified shipping Voyage in v4.1 (decision gate ≥30pp). If Python reproduces it within ±5pp, the port is faithful; if it doesn't, the port has a defect somewhere along the embed → KNN → rerank → recall path.
- **Spike validation:** `docs/spike-results/004-voyage-python-client.md` already confirmed the Python Voyage client gives unit-normalized vectors and semantically-correct rerank ordering on a 3-doc sanity check. This issue is the next layer: 31 queries against a real corpus.

## Target (Python)

- File: `docs/benchmarks/voyage-recall-2026-q2.md` (~150 LOC of markdown — published reproduction report).
- Supporting code: `scripts/benchmark_voyage_recall.py` (~250 LOC) — the runnable reproduction. Calls into already-ported modules; ships no production code itself.

## What this issue covers

A reproducible benchmark that **measures the recall lift of hybrid Voyage retrieval vs FTS-only on `eva-baseline-v2`**, on Python, against a fixed corpus, and publishes the result. Output is a checked-in markdown report with a per-stratum table, methodology, cost, and a baseline-vs-port delta.

**Concrete steps:**

1. **Seed the corpus.** Restore or rebuild the same DB the TS-baseline measurement ran against. Two routes (mirror #09-05 Path A/B):
   - **Path A:** restore Eva's snapshot DB (or sanitized export). Backfills already exist; just re-run.
   - **Path B:** seed `v41-test-corpus.ts`'s Python port + embed all summaries via `embeddings/backfill.py`.
2. **Seed the query set.** `register_query_set(db, EVA_BASELINE_V2_IDENTITY, build_eva_baseline_v2())` (from #09-05).
3. **Define two adapters:**
   - `fts_only_adapter` — wraps Epic 06 `lcm_grep`'s FTS5 query path.
   - `hybrid_adapter` — wraps Epic 05 `runHybridSearch` (FTS + semantic union → Voyage rerank-2.5).
4. **Run recall eval twice** (FTS-only and hybrid) via `run_recall_eval(eva_queries, adapter, opts={k_values: [1,5,10,20,50]})`.
5. **Record both runs** via `record_eval_run` with `mode="fts_only"` and `mode="hybrid"` respectively.
6. **Compute the per-stratum delta** between the two reports — NOT via `compute_drift` (which compares same-mode runs across time) but via a direct subtraction (`hybrid.by_stratum[s].mean_recall_at_k[5] - fts.by_stratum[s].mean_recall_at_k[5]`).
7. **Publish the report.** `docs/benchmarks/voyage-recall-2026-q2.md` carries the result table, methodology, cost breakdown, and an explicit per-stratum lift number.
8. **Acceptance:** paraphrastic lift is in `[+47.5pp, +57.5pp]` (TS-baseline ±5pp tolerance). If outside, file a defect issue against either the embeddings port (#05-*) or the Voyage client (`voyage/client.py`) and DO NOT merge this issue until the gap is explained.

## Why ±5pp tolerance, not exact reproduction

Reproducing **exactly** +52.5pp requires byte-identical embeddings + identical rerank scoring, which is impossible across:

- Voyage model-version drift (Voyage may update `voyage-4-large` / `rerank-2.5` weights between the TS spike and the Python re-run).
- `float32` vs JSON-parsed `float64` precision in embedding storage (Spike 004 §"Remaining 5% risk" #1).
- Tokenizer drift if Voyage updates the tokenizer.
- Stochasticity in the Voyage API itself (the docs don't guarantee determinism across requests, though our empirical experience is that it IS deterministic for the same input).

±5pp accommodates the noise floor while still being tight enough to catch a real port defect.

## Python orchestration script outline

```python
# scripts/benchmark_voyage_recall.py
import asyncio, json, sqlite3, time
from pathlib import Path
from lossless_hermes.eval.query_set import register_query_set, get_query_set
from lossless_hermes.eval.recall import run_recall_eval
from lossless_hermes.eval.run import record_eval_run, EvalRunRecord
from tests.fixtures.eva_baseline_v2 import (
    build_eva_baseline_v2, EVA_BASELINE_V2_IDENTITY,
)
from lossless_hermes.embeddings.hybrid_search import build_hybrid_adapter
from lossless_hermes.embeddings.semantic_search import build_fts_only_adapter

async def main(db_path: Path, out_md: Path) -> None:
    conn = open_lcm_db(db_path)
    register_query_set(conn, EVA_BASELINE_V2_IDENTITY, build_eva_baseline_v2())
    queries = get_query_set(conn, EVA_BASELINE_V2_IDENTITY).queries

    fts_adapter = build_fts_only_adapter(conn)
    hybrid_adapter = build_hybrid_adapter(conn, voyage_client=...)  # Epic 05 wiring

    fts_report = await run_recall_eval(queries, fts_adapter, {"k_values": [1, 5, 10, 20, 50]})
    hybrid_report = await run_recall_eval(queries, hybrid_adapter, {"k_values": [1, 5, 10, 20, 50]})

    fts_run_id = record_eval_run(conn, EvalRunRecord(
        query_set_identity=EVA_BASELINE_V2_IDENTITY, mode="fts_only",
        recall_report=fts_report, trigger="manual", notes="benchmark/baseline",
    ))
    hybrid_run_id = record_eval_run(conn, EvalRunRecord(
        query_set_identity=EVA_BASELINE_V2_IDENTITY, mode="hybrid",
        recall_report=hybrid_report, trigger="manual", notes="benchmark/hybrid",
    ))

    per_stratum_delta = compute_lift(fts_report, hybrid_report)  # local helper, see below
    write_report(out_md, fts_report, hybrid_report, per_stratum_delta, fts_run_id, hybrid_run_id)

def compute_lift(fts, hybrid):
    """Return {stratum: {k: hybrid.recall@k - fts.recall@k}}."""
    out = {}
    for stratum in hybrid.by_stratum:
        out[stratum] = {
            k: hybrid.by_stratum[stratum].mean_recall_at_k[k]
                - fts.by_stratum.get(stratum, _empty).mean_recall_at_k.get(k, 0.0)
            for k in hybrid.by_stratum[stratum].mean_recall_at_k
        }
    return out
```

## Dependencies

- **Depends on:** #09-01..#09-06 (all eval modules), #09-05 (the fixture is required — paraphrastic queries with `expected_summary_ids` are the +52.5pp line), Epic 05 (hybrid + FTS-only retrieval adapters, Voyage client port), Epic 06 (`lcm_grep` FTS5 path — the FTS-only adapter wraps this).
- **Blocks:** none — terminal artifact. The result gates the v0.1.0 release tag.

## Acceptance criteria

- [ ] `scripts/benchmark_voyage_recall.py --db <path> --out docs/benchmarks/voyage-recall-2026-q2.md` runs end-to-end with `VOYAGE_API_KEY` set.
- [ ] Run completes in < 5 minutes wall-clock on `ubuntu-latest` (31 queries × 2 modes × ≤ 1 s/query rerank cap).
- [ ] Run cost is < $0.50 (asserted by sum of Voyage `total_tokens` × per-token rate; expected actual: ~$0.03).
- [ ] **Paraphrastic lift is in [+47.5pp, +57.5pp]** (TS-baseline 52.5pp ±5pp). If outside, the issue cannot close — file a child defect issue and address it first.
- [ ] **FTS-easy lift is in [+23.5pp, +33.5pp]** (TS-baseline 28.5pp ±5pp).
- [ ] Both fts_only and hybrid runs are recorded in `lcm_eval_run`; `compute_drift` of the hybrid run against any prior hybrid run completes without error.
- [ ] Published markdown report (`docs/benchmarks/voyage-recall-2026-q2.md`) contains:
  - The per-stratum table (TS-baseline, Python-port, delta).
  - Methodology: corpus source, fixture identity, adapter wiring, K values, timeout, judge ensemble (if any).
  - Cost breakdown: embedding cost, rerank cost, judge cost (if quality eval ran).
  - Reproduction recipe: exact command + env-var requirements.
  - Date of the run + Voyage model + rerank model + Python version + Voyage-client version.
  - Section explaining ANY non-zero delta vs +52.5pp baseline (model drift? tokenizer drift? float precision?).
- [ ] `tests/benchmarks/test_voyage_recall_benchmark.py` — mocked smoke test that runs the orchestration logic without live API; asserts the report is generated with all required sections (skips the actual measurement check).
- [ ] PR description cites: TS commit `1f07fbd`, Voyage client version (Spike 004 sketch's `httpx` pin), and the date the live benchmark ran.

## Tests

`tests/benchmarks/test_voyage_recall_benchmark.py` (mocked, runs in regular CI):

- Mock `voyage.client.embed_texts` to return 1024-dim unit-normalized vectors.
- Mock `voyage.client.rerank_candidates` to return scores that favor the paraphrastic ground truth.
- Run the script; assert the markdown report exists, contains the required sections, and the per-stratum delta column is filled.
- Assert `compute_lift` correctly subtracts FTS-only from hybrid on the same stratum keys.

The **live** measurement against real Voyage is NOT part of `pytest -q`. It runs only via the `scripts/benchmark_voyage_recall.py` invocation (manually or via #09-07's CI workflow once promoted to production).

## Estimated effort

**8–10 hours.**

Breakdown: 2 h corpus prep (depends on #09-05 Path A/B), 2 h script + adapter wiring, 1 h cost accounting, 1 h report-template authoring, 2 h actual measurement + result write-up (if the number lands in range), or 4-8 h debugging if it doesn't.

## Confidence

**85%.** Lowest-confidence issue **after** #09-05 because the result either lands or doesn't:

- **Confidence in the eval harness:** ≥95% (mechanical port, already tested in #09-01..09-04).
- **Confidence in the Voyage client primitives:** ≥95% (Spike 004 validated).
- **Confidence in the Python-Voyage-rerank reproducing TS-Voyage-rerank:** ~85%. Voyage's rerank-2.5 is a third-party model that we can't reproduce locally; if Voyage updated the weights between TS-baseline and this run, we can't tell.
- **Confidence in the corpus reproducibility:** ~80% (per #09-05 Path A availability).

**Mitigation if the number misses:** the report should explicitly document the deviation and explain it (model version, tokenizer drift, corpus difference). A +30pp lift on Python with a +52.5pp upstream baseline is still well above the original decision gate (≥30pp) — the v0.1.0 release can proceed if the lift is ≥30pp on paraphrastic, even if it's not exactly +52.5pp. This is a discussion the maintainers should have when the actual number comes in.
