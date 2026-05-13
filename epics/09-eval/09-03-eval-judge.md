---
name: Port issue
about: Port `src/eval/judge.ts` to Python
title: '[epic-09] eval: port judge.ts → eval/judge.py'
labels: 'port, epic-09-eval'
---

## Source (TypeScript)

- File: `src/eval/judge.ts` (`pr-613` HEAD `1f07fbd`)
- Lines: 191 LOC
- Function(s)/class(es): `runQualityEval(queries, candidatesByQuery, judges)`. Internal helpers: `callOneJudge`, `isFiniteNumber`. Public types: `JudgeCallArgs`, `JudgeCallResult`, `JudgeCall`, `JudgeEntry`, `PerJudgeScore`, `QualityResult`, `QualityReport`.

## Target (Python)

- File: `src/lossless_hermes/eval/judge.py`
- Estimated LOC: ~200

## Background

LLM-as-judge ensemble harness. Per architecture-v4.1 §11, the production gate uses **3 different model families** voting — but the module accepts any 1..N `JudgeEntry` list and aggregates accordingly. Same `LlmCall`-style injection pattern as `synthesis/dispatch.ts` — production wires real model calls, tests inject deterministic mocks. **No model wiring lives here.**

**Judge-failure tolerance contract:**

- A judge can return a finite-number score → counted toward `mean_score`.
- A judge can return `null`/missing score → counted as a failure (judge "couldn't decide").
- A judge can raise an exception → also counted as a failure; `reason` captures the error message; `score=None`.
- A judge can return a non-finite number (`NaN`, `inf`, `-inf`) → invalidated to `None` with `reason=f"invalid_score: {value}"`.

`mean_score` per query is the mean of **non-None** judge scores. If every judge fails for a query, `mean_score = None` and the query is excluded from the overall mean. The aggregate `judge_failures` counts **total failure events** across `(queries × judges)`, NOT the count of queries that had ≥1 failure.

**Concurrency contract:** within a single query, all judges fire in parallel via `asyncio.gather` (independent calls to different external services). Across queries, sequential — intra-set batching is the caller's call.

**Range non-enforcement:** TS comment notes "we don't enforce 1..5 on the judge return value" because the rubric scale may evolve. Port keeps that lenience — only `Number.isFinite` is checked.

## Python public API

```python
from dataclasses import dataclass
from typing import Protocol
from lossless_hermes.eval.query_set import QueryRecord

@dataclass(frozen=True)
class JudgeCallArgs:
    query: str
    candidate: str
    reference: str | None = None

@dataclass(frozen=True)
class JudgeCallResult:
    score: float | None
    reason: str

class JudgeCall(Protocol):
    async def judge(self, args: JudgeCallArgs) -> JudgeCallResult: ...

@dataclass(frozen=True)
class JudgeEntry:
    judge_id: str  # opaque (typically model family, e.g. "claude-opus-4-7", "gpt-5.4")
    call: JudgeCall

@dataclass(frozen=True)
class PerJudgeScore:
    judge_id: str
    score: float | None
    reason: str

@dataclass(frozen=True)
class QualityResult:
    query_id: str
    candidate: str
    per_judge_scores: list[PerJudgeScore]
    mean_score: float | None

@dataclass(frozen=True)
class QualityOverall:
    mean_score: float          # 0.0 if no successful queries
    n: int                      # queries with ≥1 successful judge
    judge_failures: int         # total failure events across (queries × judges)

@dataclass(frozen=True)
class QualityReport:
    per_query: list[QualityResult]
    overall: QualityOverall

async def run_quality_eval(
    queries: list[QueryRecord],
    candidates_by_query: dict[str, str],
    judges: list[JudgeEntry],
) -> QualityReport: ...
```

## Dependencies

- **Depends on:** #09-01 (`QueryRecord` type), Epic 07-entity-synthesis (the `LlmCall` adapter that lives at `synthesis/llm_adapter.py` — wraps Anthropic/OpenAI/etc. into the protocol). The judge port itself doesn't ship any LLM wiring; that lands in #09-07 / #09-08 callers.
- **Blocks:** #09-04 (run.py records `QualityReport.overall.mean_score`), #09-08 (benchmark uses an ensemble to score paraphrastic recall hits).

## Acceptance criteria

- [ ] `run_quality_eval` raises `ValueError("requires at least one judge")` if `judges` is empty.
- [ ] Queries missing from `candidates_by_query` are SKIPPED (no per-query result, no contribution to `n`).
- [ ] Per-query `mean_score` is the mean of non-`None` scores; `None` if every judge failed.
- [ ] Overall `mean_score` is the mean over queries with `mean_score is not None`; `0.0` if zero such queries.
- [ ] `judge_failures` counts **total** failure events across `(queries × judges)`, not the count of failing queries.
- [ ] Judge exceptions are caught and converted to `PerJudgeScore(score=None, reason=f"judge_error: {err_msg}")`. The exception type does not leak; reason is a string.
- [ ] Non-finite scores (`NaN`, `inf`, `-inf`) are invalidated to `None` with `reason=f"invalid_score: {value}"`.
- [ ] Within a query, judges fire in parallel — verified by a fixture with 3 judges each sleeping 100 ms; total wall time < 300 ms (i.e., overlapped).
- [ ] Across queries, sequential — verified by a fixture with 3 queries each with 1 judge sleeping 100 ms; total wall time ≥ 300 ms (i.e., serialized).
- [ ] All TS unit tests in `test/eval-judge.test.ts` (~30 cases) have ported pytest equivalents in `tests/eval/test_judge.py`.
- [ ] Function signatures match the spec above; `mypy --strict src/lossless_hermes/eval/judge.py` passes.
- [ ] `pytest tests/eval/test_judge.py` passes locally + on GitHub CI.
- [ ] PR description cites LCM commit `1f07fbd`.

## Tests

Port `test/eval-judge.test.ts` line-for-line to `tests/eval/test_judge.py`. Mandatory cases:

- Empty `judges` list raises.
- Empty `candidates_by_query` returns empty `per_query` and `overall.n == 0`.
- Single judge, single query, score=4.5 → `overall.mean_score == 4.5`.
- Ensemble of 3 judges (5, 3, 4) → `mean_score == 4.0` for that query.
- One judge returns `null` → counted as failure; remaining two judges average into `mean_score`; `judge_failures == 1`.
- All judges fail for a query → `mean_score is None`; query excluded from overall; `judge_failures == 3`.
- Judge raises `RuntimeError("oops")` → captured as `score=None`, `reason="judge_error: oops"`; eval continues.
- Judge returns `float("nan")` → invalidated to None with `invalid_score: nan` reason.
- Judge returns `float("inf")` → invalidated.
- Query in `queries` but missing from `candidates_by_query` → skipped, no per-query result.
- `reference_summary` on the QueryRecord is forwarded to `JudgeCallArgs.reference` when set; omitted (None) otherwise.
- Concurrency: 3-judge ensemble each sleeping 100 ms — wall time < 300 ms.
- Sequentiality: 3 queries each with a 100 ms judge — wall time ≥ 300 ms.

## Estimated effort

**4–6 hours.**

## Confidence

**92%** — pure-function port. Slight risk: matching TS's `Number.isFinite` semantics with Python's `math.isfinite` (Python distinguishes `float` vs `int`; the check should accept `int` and `float` but reject `nan/inf` regardless of type). Cover with explicit fixtures.
