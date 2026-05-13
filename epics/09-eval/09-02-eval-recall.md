---
name: Port issue
about: Port `src/eval/recall.ts` to Python
title: '[epic-09] eval: port recall.ts → eval/recall.py'
labels: 'port, epic-09-eval'
---

## Source (TypeScript)

- File: `src/eval/recall.ts` (`pr-613` HEAD `1f07fbd`)
- Lines: 236 LOC
- Function(s)/class(es): `runRecallEval(queries, adapter, opts)`. Internal helpers: `computePerQuery`, `emptyAggregate`, `aggregate`. Public types: `RecallSearchAdapter`, `RecallResult`, `RecallStratumAggregate`, `RecallReport`, `RecallEvalOptions`. Default K values: `[1, 5, 10, 20, 50]`.

## Target (Python)

- File: `src/lossless_hermes/eval/recall.py`
- Estimated LOC: ~240 (Python is similar — straight function port + a `Protocol` for the adapter)

## Background

Pure metric module — **no LLM calls, no SQL writes**. Given `list[QueryRecord]` (each with optional `expected_summary_ids`) and an injected `RecallSearchAdapter`, computes recall@K for each K in `kValues` plus reciprocal rank, then aggregates per stratum and overall.

**Recall@K convention:** `|hits[:K] ∩ expected| / |expected|`. Queries with no `expected_summary_ids` are SKIPPED from recall aggregates (their map is empty and they don't contribute to mean) — but they can still feed quality eval downstream.

**Two wave-fixes the port MUST preserve:**

1. **Wave-4 Auditor #15 P1 — per-query timeout.** Without it a hung adapter (network stall, vec0 deadlock) hangs the whole eval. Default 30 s; tests assert behavior at 100 ms (minimum) and 5 minutes (clamped max).
2. **Wave-9 Agent #10 P1 — timer cleanup.** The TS `Promise.race(adapter, timeout)` leaked timers for the timeout duration after each adapter resolution. Port via `asyncio.wait_for(...)` — Python's native timeout handles this cleanly (no leak possible), but write a regression test that runs 100 fast queries and asserts no pending tasks remain on `asyncio.all_tasks()`.

**Determinism note:** the module is deliberately sequential across queries — most retrieval surfaces aren't safe to parallelize against the same SQLite connection. Within a query the adapter is opaque (can do whatever it wants). Adapter exceptions bubble (TS comment: "silently dropping a failed query would skew the aggregate") — port that contract.

## Python public API

```python
from typing import Protocol, TypedDict
from dataclasses import dataclass
from lossless_hermes.eval.query_set import QueryRecord, Stratum

class RecallSearchAdapter(Protocol):
    async def search(self, query: QueryRecord) -> list[str]: ...

@dataclass(frozen=True)
class RecallResult:
    query_id: str
    hits: list[str]
    expected: list[str]
    recall_at_k: dict[int, float]
    reciprocal_rank: float

@dataclass(frozen=True)
class RecallStratumAggregate:
    mean_recall_at_k: dict[int, float]
    mean_rr: float
    n: int

@dataclass(frozen=True)
class RecallReport:
    per_query: list[RecallResult]
    by_stratum: dict[str, RecallStratumAggregate]
    overall: RecallStratumAggregate

class RecallEvalOptions(TypedDict, total=False):
    k_values: list[int]
    per_query_timeout_ms: int

DEFAULT_K_VALUES = (1, 5, 10, 20, 50)

async def run_recall_eval(
    queries: list[QueryRecord],
    adapter: RecallSearchAdapter,
    opts: RecallEvalOptions | None = None,
) -> RecallReport: ...
```

## Dependencies

- **Depends on:** #09-01 (`QueryRecord`, `Stratum` types).
- **Blocks:** #09-04 (run.py builds the `PerQueryScoresEnvelope` from `RecallReport.per_query[i].reciprocal_rank`), #09-08 (benchmark calls `run_recall_eval` directly).

## Acceptance criteria

- [ ] `run_recall_eval` returns a `RecallReport` with `per_query` in input order, `by_stratum` keyed by stratum name with one aggregate per non-empty stratum, and `overall` aggregating across scored (expected-bearing) queries only.
- [ ] Default `k_values` is `[1, 5, 10, 20, 50]`; caller-provided values are sorted ASC and validated (positive integers; empty list raises).
- [ ] Recall@K dedupes the `hits[:k]` window — an adapter that returns the same ID twice cannot push recall above 1.0 (asserted via fixture).
- [ ] Reciprocal rank uses 1-based rank of the first expected hit anywhere in the full `hits` list (not truncated). 0 if no expected ID found.
- [ ] Queries with empty `expected_summary_ids` produce `recall_at_k == {}` and `reciprocal_rank == 0` and are EXCLUDED from `by_stratum` + `overall` aggregates.
- [ ] **Wave-4 timeout:** default 30 s; `per_query_timeout_ms < 100` falls back to 30 s default; `> 300_000` (5 min) clamps to 5 min; on timeout the query's `hits` is treated as empty and the eval continues without raising.
- [ ] **Wave-9 timer-cleanup regression:** after running 100 queries, `len(asyncio.all_tasks()) == 0` outside the test's own task (assert via `asyncio.gather` boundary).
- [ ] Adapter exceptions are NOT swallowed — `run_recall_eval` propagates the first `Exception` raised by `adapter.search`.
- [ ] All TS unit tests in `test/eval-recall.test.ts` (~30 cases) have ported pytest equivalents in `tests/eval/test_recall.py`.
- [ ] Function signatures match the spec above; `mypy --strict src/lossless_hermes/eval/recall.py` passes.
- [ ] `pytest tests/eval/test_recall.py` passes locally + on GitHub CI.
- [ ] PR description cites LCM commit `1f07fbd`.

## Tests

Port `test/eval-recall.test.ts` line-for-line to `tests/eval/test_recall.py` plus the regression tests for both wave-fixes:

- Recall@K for K ∈ {1, 5, 10}: hits exactly matching expected (1.0), all missing (0.0), partial overlap (0.5, 0.333…).
- Recall@K with duplicate hit IDs: recall capped at 1.0.
- Reciprocal rank: first hit at position 1 → 1.0; position 5 → 0.2; not found → 0.0.
- Query with no `expected_summary_ids`: contributes nothing to aggregates.
- Per-stratum aggregation: 3 fts-easy queries + 2 paraphrastic + 1 fts-medium produces 3 stratum keys with correct n=.
- Empty `k_values` raises.
- Non-integer or zero K raises with offending value in the message.
- **Wave-4 timeout** — synthetic adapter that hangs `asyncio.sleep(60)`; assert finishes within 1 s of the timeout and zero recall for that query.
- **Wave-4 clamp** — `per_query_timeout_ms=0` and `=10` both fall back to default 30 s.
- **Wave-4 clamp** — `per_query_timeout_ms=600_000` clamps to 5 min.
- **Wave-9 leak** — 100 queries with 50 ms `asyncio.sleep` each; assert `len(asyncio.all_tasks()) == 1` (the test's own task) at exit.
- Adapter raises: `run_recall_eval` propagates the exception.

## Estimated effort

**5–7 hours.**

## Confidence

**95%** — pure metric port with explicit wave-fix provenance. Python's `asyncio.wait_for` is a cleaner primitive than the TS `Promise.race + setTimeout + finally clearTimeout` dance, so the Wave-9 leak class is effectively impossible in Python — keep the regression test anyway as a contract assertion.
