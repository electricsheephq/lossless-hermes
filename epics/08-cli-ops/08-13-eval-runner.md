---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-08] cli-ops: port eval-runner.ts'
labels: 'port, epic-08-cli-ops'
---

## Source (TypeScript)

- File: `src/operator/eval-runner.ts`
- Lines: 193 LOC
- Function(s)/class(es): `runEval(params): EvalResult`, `formatEvalReport(result)`, `EvalRunnerError`, internal `_executeQuerySet`, `_computeRecallAtK`, `_compareToBaseline`

## Target (Python)

- File: `src/lossless_hermes/operator/eval_runner.py`
- Estimated LOC: ~210

## What this issue covers

The runner for `/lcm eval` — executes a query set against the active retrieval surface (FTS-only, semantic-only, or hybrid) and computes recall@K with optional drift comparison to a baseline. The **eval suite itself** (query sets + golden judges) is owned by Epic 09; Epic 08 ships the runner that consumes them.

CLI surface (per plugin-glue.md §"/lcm slash commands — full inventory" line 437):

```
/lcm eval [--baseline] [--mode <fts_only|semantic_only|hybrid>] [--query-set <name>] [--version <int>]
```

Required: `--baseline` OR `--mode`. Default: latest query-set version.

### Modes

- **`fts_only`** — pure SQLite FTS5 + trigram retrieval, no embedding. Cheapest; no LLM/embedding cost.
- **`semantic_only`** — vec0 KNN retrieval, no FTS. **Paid Voyage cost** (embeds the query).
- **`hybrid`** — RRF (reciprocal rank fusion) of FTS + semantic. **Paid Voyage cost** (embeds the query).
- **`--baseline`** mode reads the most recent `lcm_eval_run` row for the same query set and re-runs it for drift comparison.

### Algorithm

1. Resolve query set: load `lcm_eval_query_set` row (Epic 01-06 schema). If `--version` is omitted, use the latest.
2. For each query in the set:
   - Run the active retrieval surface (per `--mode`).
   - Compute recall@K (default K=5) against the query's golden-answer summary IDs.
   - Optionally embed the query (Voyage call) for `semantic_only` / `hybrid` modes.
3. Aggregate metrics: mean recall@K, p50/p95 latency, paid-Voyage-call count, total cost estimate.
4. Write `lcm_eval_run` row with the aggregate + per-query details (`lcm_eval_query_result` table).
5. If `--baseline` provided, read the previous run and compute drift (`current_recall - baseline_recall`).
6. Return `EvalResult` shape:

```python
class EvalResult(BaseModel):
    run_id: str
    mode: Literal["fts_only", "semantic_only", "hybrid"]
    query_set_id: str
    query_set_version: int
    recall_at_5: float
    recall_at_10: float
    p50_latency_ms: float
    p95_latency_ms: float
    voyage_call_count: int
    estimated_cost_usd: float
    drift_recall_at_5: float | None  # only if baseline mode
    drift_recall_at_10: float | None
```

7. `format_eval_report(result)` renders human-readable output:

```
[lcm] eval --mode hybrid --query-set wave12-golden v3
Run id: lcm-eval-9f8a3b
Recall@5:  0.847 (baseline: 0.864, drift -0.017)
Recall@10: 0.912 (baseline: 0.919, drift -0.007)
Latency:   p50=87ms, p95=412ms
Cost:      52 Voyage calls × $0.00012 = $0.0062
Stored: lcm_eval_run.id=lcm-eval-9f8a3b
```

Errors raise `EvalRunnerError`:
- No `--baseline` and no `--mode` (must specify at least one).
- Query set not found.
- `semantic_only` or `hybrid` mode with no Voyage credentials (per ADR-022).

## Dependencies

- Depends on: #08-01 (dispatcher), Epic 01-06 (`lcm_eval_query_set`, `lcm_eval_run`, `lcm_eval_query_result` tables), Epic 05 (semantic-search + hybrid-search surfaces), Epic 06 (tools — `lcm_grep` is the underlying FTS surface), Voyage credentials per ADR-022.
- Blocks: Epic 09 — the eval suite consumes this runner.

## Acceptance criteria

- [ ] `run_eval(params: EvalParams) -> EvalResult` matches the TS signature.
- [ ] Mode required: missing `--baseline` AND missing `--mode` raises `EvalRunnerError`.
- [ ] Query set version: omitted `--version` resolves to the latest row in `lcm_eval_query_set`.
- [ ] `fts_only` mode does NOT call Voyage (`voyage_call_count == 0`).
- [ ] `semantic_only` and `hybrid` modes embed the query once per call (Voyage call count == query count).
- [ ] Recall@K compared against the query's `golden_summary_ids` JSON column.
- [ ] Latency measurements use `time.perf_counter()` per query.
- [ ] `lcm_eval_run` row written with all aggregate fields; per-query details in `lcm_eval_query_result`.
- [ ] `--baseline` mode reads the most recent `lcm_eval_run` for the same query set + version and computes drift.
- [ ] `format_eval_report(result)` matches the TS rendering line-for-line modulo whitespace.
- [ ] All TS test cases in `test/operator-eval-runner.test.ts` have ported pytest equivalents in `tests/operator/test_eval_runner.py`.
- [ ] **New test:** `tests/operator/test_eval_runner.py::test_fts_only_no_voyage` — mock Voyage client, assert never called.
- [ ] **New test:** `tests/operator/test_eval_runner.py::test_baseline_drift_computed` — seed a prior run, run baseline mode, assert drift fields populated.
- [ ] **New test:** `tests/operator/test_eval_runner.py::test_missing_voyage_creds_raises` — `semantic_only` + no creds → `EvalRunnerError`.
- [ ] Function signatures match the spec in [docs/porting-guides/doctor-ops.md](../../docs/porting-guides/doctor-ops.md) §"Operator modules" line 312.
- [ ] `pytest tests/operator/test_eval_runner.py` passes.
- [ ] No new mypy errors (`mypy --strict src/lossless_hermes/operator/eval_runner.py`).
- [ ] PR description cites LCM commit `1f07fbd` (pr-613 head).

## Estimated effort

**6 hours.**

## Confidence

**90%** — well-specified contract; TS has dedicated test coverage. 10% risk lives in coordination with Epic 09 (the eval-suite content) — if Epic 09 reshapes the query-set schema, this runner needs a follow-up.
