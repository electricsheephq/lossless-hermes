---
name: Port issue
about: Port `src/eval/run.ts` to Python
title: '[epic-09] eval: port run.ts → eval/run.py (eval-run record + drift envelope)'
labels: 'port, epic-09-eval'
---

## Source (TypeScript)

- File: `src/eval/run.ts` (`pr-613` HEAD `1f07fbd`)
- Lines: 375 LOC
- Function(s)/class(es): `recordEvalRun(db, record) → runId`, `computeDrift(db, runId) → DriftSummary`. Internal helpers: `buildEnvelope`, `generateRunId`, `generateDriftId`, `selectPriorRun`, `pickComparableScore`, `judgeModelsFromQualityReport`. Public types: `EvalTrigger`, `EvalRunRecord`, `DriftDetail`, `DriftSummary`, `PerQueryScoresEnvelope` (private).

## Target (Python)

- File: `src/lossless_hermes/eval/run.py`
- Estimated LOC: ~380

## Background

This is the **persistence + drift comparison** half of the eval suite. `record_eval_run` inserts one row into `lcm_eval_run` per `(query_set_id, mode)` triple; `compute_drift` selects the prior run of the same `(query_set_id, mode)` and aggregates per-query deltas, writing the cumulative delta to `lcm_eval_drift`.

**Four documented schema gaps the port MUST preserve verbatim** (TS source comments lines 9–35):

1. **No `mode` column on `lcm_eval_run`.** Mode is serialized into `per_query_scores` JSON envelope as `{"v": 1, "mode": "fts_only" | "hybrid" | ..., "perQuery": {...}}`. `select_prior_run` parses the envelope of every candidate row to filter by mode. (A v4.2 schema migration could add an indexed column; not in this epic.)
2. **`lcm_eval_drift` is aggregate-only.** Per-query drift detail is in the return value (`DriftSummary.details`); only `cumulative_delta` + `window_runs=2` is persisted.
3. **`prompt_bundle_version` is NOT NULL, no schema default.** Caller passes a positive integer; the port defaults to `1` if omitted, matching TS.
4. **`retrieval_recall_score` and `synthesis_quality_score` are both NOT NULL.** If caller passes only one of `recall_report` / `quality_report`, the missing side is recorded as `0` and the envelope flags `has_recall` / `has_quality` so consumers know which side is real.

**Drift threshold rule** (per architecture-v4.1 §11.1): if `noise_floor_sd` is non-null on the current run, threshold = `2 × noise_floor_sd`. Otherwise any non-zero delta counts. The `2×` factor is empirical — covered by `eval-run.test.ts`.

**Score preference for drift comparison** (per TS `pickComparableScore`):
1. Both runs have `qualityScore` → diff those.
2. Both runs have `recallRR` → diff those.
3. Otherwise, surface whichever scalar is available on each side as `prior_score` / `current_score`, but `delta=None` (excluded from drift count).

## Python public API

```python
from dataclasses import dataclass, field
from typing import Literal, TypedDict
import sqlite3
from lossless_hermes.eval.query_set import QuerySetIdentity, encode_query_set_id
from lossless_hermes.eval.recall import RecallReport
from lossless_hermes.eval.judge import QualityReport

EvalTrigger = Literal["manual", "prompt-update", "model-update", "ci", "nightly"]
EvalMode = str  # opaque tag: "fts_only" | "semantic_only" | "hybrid" | custom

@dataclass(frozen=True)
class EvalRunRecord:
    query_set_identity: QuerySetIdentity
    mode: EvalMode
    run_id: str | None = None                  # generated if None
    recall_report: RecallReport | None = None
    quality_report: QualityReport | None = None
    notes: str | None = None
    trigger: EvalTrigger = "manual"
    prompt_bundle_version: int = 1
    noise_floor_sd: float | None = None

@dataclass(frozen=True)
class DriftDetail:
    query_id: str
    prior_score: float | None
    current_score: float | None
    delta: float | None       # current - prior; None if either side missing

@dataclass(frozen=True)
class DriftSummary:
    drifted: int
    improved: int
    regressed: int
    details: list[DriftDetail]    # sorted by |delta| DESC; nulls last
    prior_run_id: str | None
    cumulative_delta: float

class PerQueryScoresEnvelope(TypedDict):
    v: Literal[1]
    mode: str
    notes: str  # optional in TS; carry as "" or absent — match TS exactly
    hasRecall: bool
    hasQuality: bool
    perQuery: dict[str, dict]  # qid → {"recallRR"?: float, "qualityScore"?: float|None}

def record_eval_run(conn: sqlite3.Connection, record: EvalRunRecord) -> str: ...
def compute_drift(conn: sqlite3.Connection, run_id: str) -> DriftSummary: ...
```

`record_eval_run`:
1. Validate FK target exists (`SELECT 1 FROM lcm_eval_query_set WHERE query_set_id = ?`); on miss raise `EvalRunRecordError("missing_query_set", ...)`.
2. Build envelope from optional reports.
3. Generate `run_id` if not supplied: `evalrun_{base36(now_ms)}_{base36(rand24)}`.
4. INSERT into `lcm_eval_run` with all 9 columns.
5. Return the run_id.

`compute_drift`:
1. Load current run; parse envelope. Raise `EvalRunRecordError("missing_run" | "malformed_envelope", ...)` on miss/parse-fail.
2. `select_prior_run(query_set_id, mode, exclude_run_id=current_run_id)` — scan `lcm_eval_run` rows in `ran_at DESC, run_id DESC` order; parse each envelope's `mode` field; first match wins. Skip rows with malformed envelopes (don't raise — they could be old/legacy).
3. If no prior run: return zero summary, persist nothing.
4. Threshold = `2 * noise_floor_sd` if non-null else `0` (any-nonzero counts).
5. For each query in `prior_perQuery ∪ current_perQuery`: pick comparable score (quality first, recall second), compute delta, count drifted/improved/regressed, accumulate cumulative_delta.
6. Sort details by `abs(delta)` DESC; nulls treated as -1 (last).
7. INSERT one row into `lcm_eval_drift` with `cumulative_delta` + `window_runs=2`.
8. Return summary.

## Dependencies

- **Depends on:** #09-01 (query-set identity + encoder), #09-02 (`RecallReport`), #09-03 (`QualityReport`), Epic 01-15 (schema for `lcm_eval_run` + `lcm_eval_drift`).
- **Blocks:** #09-06 (drift detection adds per-stratum surface on top of `DriftSummary`), #09-07 (CI workflow calls `record_eval_run` → `compute_drift`), #09-08 (benchmark records baseline + hybrid runs to compute the +52.5pp delta).

## Acceptance criteria

- [ ] `record_eval_run` raises a typed error (with `kind="missing_query_set"`) when `query_set_identity` isn't registered — never lets SQLite's opaque FK violation surface to callers.
- [ ] `run_id` generation: `evalrun_<base36(time.time_ns()//1_000_000)>_<8 random base36 chars>` (parity with TS `Math.random().toString(36).slice(2,10)`).
- [ ] When `recall_report` is None: `retrieval_recall_score = 0` and `envelope.hasRecall = false`.
- [ ] When `quality_report` is None: `synthesis_quality_score = 0` and `envelope.hasQuality = false`.
- [ ] Envelope `v = 1` is set unconditionally.
- [ ] Envelope `notes` field is present **only when** `record.notes is not None` (match TS exactly — `if (record.notes !== undefined) env.notes = record.notes`).
- [ ] Envelope `perQuery[qid]` carries `recallRR` (from `RecallReport.per_query[i].reciprocal_rank`) and `qualityScore` (from `QualityReport.per_query[i].mean_score`).
- [ ] `judge_models` column is the **sorted, deduped** list of `judge_id`s from `quality_report.per_query[*].per_judge_scores[*].judge_id`; serialized as JSON array; empty list if no quality report.
- [ ] `compute_drift` returns a zero summary (`drifted=0, prior_run_id=None, cumulative_delta=0.0`) when no prior run of the same `(query_set, mode)` exists; persists nothing to `lcm_eval_drift`.
- [ ] `compute_drift` ignores prior rows with malformed `per_query_scores` JSON (continues to next candidate) and never raises on legacy/garbage data.
- [ ] `compute_drift` raises a typed error when the **current** run's envelope is malformed (this is fatal — we just wrote it).
- [ ] Drift threshold: `noise_floor_sd != null` → `2 × sd` threshold; else any non-zero delta is "drifted".
- [ ] `details` sorted by `abs(delta) DESC`, nulls (delta=None) sorted last.
- [ ] `cumulative_delta` is the sum of all non-None per-query deltas (signed).
- [ ] Drift row's `drift_id` matches TS pattern `drift_<base36(ms)>_<8 random base36>`.
- [ ] All TS unit tests in `test/eval-run.test.ts` (~45 cases) have ported pytest equivalents in `tests/eval/test_run.py`.
- [ ] Function signatures match the spec above; `mypy --strict src/lossless_hermes/eval/run.py` passes.
- [ ] `pytest tests/eval/test_run.py` passes locally + on GitHub CI.
- [ ] PR description cites LCM commit `1f07fbd`.

## Tests

Port `test/eval-run.test.ts` line-for-line to `tests/eval/test_run.py`. Mandatory cases:

- Record a run with both reports → both scores non-zero, envelope has both flags true.
- Record recall-only → `synthesis_quality_score == 0`, `envelope.hasQuality is False`.
- Record quality-only → `retrieval_recall_score == 0`, `envelope.hasRecall is False`.
- Record with missing query set → typed error, no row inserted.
- `prompt_bundle_version` defaults to 1 when omitted.
- `trigger` defaults to `"manual"` when omitted.
- `noise_floor_sd` stored verbatim (None or float).
- `judge_models` is sorted-deduped JSON list.
- `compute_drift` with no prior run → zero summary, nothing written to `lcm_eval_drift`.
- `compute_drift` with prior run of DIFFERENT mode → still zero summary (prior must match mode).
- `compute_drift` with prior + current both having quality scores → quality is preferred for delta.
- `compute_drift` with prior + current both having recall only → recall RR is the delta basis.
- `compute_drift` mixed availability: prior has quality, current has only recall → `delta=None` for that query, surfaced in details.
- Drift threshold: `noise_floor_sd=0.05` → delta of 0.08 counts as drifted; delta of 0.05 counts (≥ 2×0.025=0.05); delta of 0.04 does not.
- Improved/regressed counts: 3 positive + 2 negative drifts → improved=3, regressed=2.
- `details` sorted by `abs(delta) DESC`; ties stable.
- Malformed `per_query_scores` on a prior candidate is skipped, not raised.
- Malformed envelope on the current run raises.

## Estimated effort

**7–9 hours.**

## Confidence

**90%** — the four schema gaps are explicit in TS source. Slight risk in the `run_id`/`drift_id` generators matching TS's `Math.random().toString(36)` byte-for-byte — Python's `secrets.token_hex(4)` produces lowercase hex, which is **not the same alphabet as base36**. Use `random.choices(string.digits + string.ascii_lowercase, k=8)` to match TS exactly; document the alphabet choice in the docstring.
