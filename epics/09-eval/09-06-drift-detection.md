---
name: Port issue
about: Drift-detection thresholds + per-stratum drift surface
title: '[epic-09] eval: drift-detection thresholds + per-stratum surface'
labels: 'port, epic-09-eval'
---

## Source (TypeScript)

- File: `src/eval/run.ts` (within `computeDrift`) — `pr-613` HEAD `1f07fbd`
- Lines: ~80 LOC of the 375-LOC file (the drift-aggregation logic, lines ~283–375 of `run.ts`).
- **No standalone "drift detector" module exists** in TS — the drift logic is inline in `computeDrift`. This issue's Python work splits per-stratum surface into a thin helper module so the operator UI (`/lcm eval`, Epic 08) can render drift by stratum without re-parsing envelopes.
- Reference for the threshold math: `architecture-v4.1.md §11.1` ("2× empirical SD").

## Target (Python)

- File: `src/lossless_hermes/eval/drift.py`
- Estimated LOC: ~150

## What this issue covers

`compute_drift` (issue #09-04) returns a flat `DriftSummary`. The operator UI and the +52.5pp benchmark both need **per-stratum drift** ("paraphrastic regressed -0.04, fts-easy improved +0.02"). This issue adds the per-stratum surface on top of the run-record port.

Concretely:

1. **Detection thresholds** — surface the threshold computation as a pure function so tests and UI can reason about it:
   ```python
   def drift_threshold(noise_floor_sd: float | None) -> float:
       """Per architecture-v4.1 §11.1: 2 × empirical SD when calibrated; else 0 (any non-zero counts)."""
       return 2.0 * noise_floor_sd if noise_floor_sd is not None and noise_floor_sd > 0 else 0.0

   def is_drifted(delta: float, threshold: float) -> bool:
       return abs(delta) >= threshold if threshold > 0 else delta != 0.0
   ```
2. **Per-stratum drift aggregation** — given a `DriftSummary` + the `QuerySet` it was computed against, group `details` by stratum and aggregate (`drifted/improved/regressed/cumulative` per stratum):
   ```python
   @dataclass(frozen=True)
   class StratumDriftAggregate:
       stratum: str
       drifted: int
       improved: int
       regressed: int
       cumulative_delta: float
       n_scored: int      # queries with non-None delta in this stratum

   @dataclass(frozen=True)
   class PerStratumDrift:
       overall: DriftSummary
       by_stratum: dict[str, StratumDriftAggregate]

   def per_stratum_drift(summary: DriftSummary, query_set: QuerySet) -> PerStratumDrift: ...
   ```
3. **Regression flagging** — surface a boolean `regressed: bool` on `PerStratumDrift` that returns True if any stratum has `cumulative_delta < -threshold`. This is what the CI workflow (#09-07) reads to decide whether to fail the build.

## Why a separate module

`run.py` is already 375 LOC and crosses the schema-write seam. Splitting per-stratum into `drift.py` keeps `run.py`'s testable surface small and lets the CI workflow import only the pure functions without pulling in SQL dependencies. The TS source didn't split this only because TS's smaller-module convention is less strict — the Python port adopts the split deliberately.

## Python public API

```python
from dataclasses import dataclass
from collections import defaultdict
from lossless_hermes.eval.run import DriftSummary, DriftDetail
from lossless_hermes.eval.query_set import QuerySet, Stratum

@dataclass(frozen=True)
class StratumDriftAggregate:
    stratum: str
    drifted: int
    improved: int
    regressed: int
    cumulative_delta: float
    n_scored: int

@dataclass(frozen=True)
class PerStratumDrift:
    overall: DriftSummary
    by_stratum: dict[str, StratumDriftAggregate]
    threshold_used: float

    @property
    def any_stratum_regressed(self) -> bool:
        return any(s.cumulative_delta < -self.threshold_used for s in self.by_stratum.values())

def drift_threshold(noise_floor_sd: float | None) -> float: ...
def is_drifted(delta: float, threshold: float) -> bool: ...
def per_stratum_drift(
    summary: DriftSummary,
    query_set: QuerySet,
    noise_floor_sd: float | None = None,
) -> PerStratumDrift: ...
```

`per_stratum_drift` joins `summary.details[*].query_id` against `query_set.queries[*].stratum` and aggregates. Queries in `details` that don't appear in `query_set` (shouldn't happen but possible if the set is mutated mid-eval — defensive) are assigned to a sentinel stratum `"unknown"` and counted there.

## Dependencies

- **Depends on:** #09-01 (`QuerySet`, `Stratum`), #09-04 (`DriftSummary`, `DriftDetail`).
- **Blocks:** #09-07 (CI workflow imports `PerStratumDrift.any_stratum_regressed` to set the workflow exit code), #09-08 (benchmark reports per-stratum delta — paraphrastic is the +52.5pp line item).

## Acceptance criteria

- [ ] `drift_threshold(None) == 0.0`; `drift_threshold(0) == 0.0`; `drift_threshold(0.05) == 0.10`; negative SDs are treated as None (return 0).
- [ ] `is_drifted` with `threshold=0` returns True for any non-zero delta and False for delta=0.
- [ ] `is_drifted` with `threshold > 0` returns True iff `abs(delta) >= threshold`.
- [ ] `per_stratum_drift` produces one aggregate per stratum present in `query_set`; strata with zero scored queries are omitted (parity with `RecallReport.by_stratum`).
- [ ] `improved + regressed <= drifted` per stratum (drifted counts those above threshold; improved/regressed within them by sign).
- [ ] `n_scored` per stratum is the count of `DriftDetail` with non-None delta in that stratum.
- [ ] `cumulative_delta` per stratum is the signed sum of non-None deltas.
- [ ] `any_stratum_regressed` returns True iff at least one stratum has `cumulative_delta < -threshold_used`; False if every stratum is flat or improved.
- [ ] Queries not in the query set are bucketed under stratum `"unknown"` (not silently dropped — defensive).
- [ ] `mypy --strict src/lossless_hermes/eval/drift.py` passes.
- [ ] `pytest tests/eval/test_drift.py` passes locally + on GitHub CI.
- [ ] PR description cites LCM commit `1f07fbd`.

## Tests

`tests/eval/test_drift.py`:

- `drift_threshold` table: None / 0 / negative / 0.05 / 1e-10 → 0 / 0 / 0 / 0.10 / 2e-10.
- `is_drifted` table: threshold=0 with delta ∈ {-0.01, 0, 0.01} → {True, False, True}; threshold=0.10 with delta ∈ {-0.15, -0.05, 0, 0.05, 0.15} → {True, False, False, False, True}.
- Per-stratum with 3 fts-easy (deltas +0.1, +0.05, -0.02) and 2 paraphrastic (deltas +0.6, +0.5) under threshold=0.1: fts-easy → drifted=1, improved=1, regressed=0, cumulative=+0.13; paraphrastic → drifted=2, improved=2, regressed=0, cumulative=+1.10.
- Per-stratum with all-positive deltas → `any_stratum_regressed is False`.
- Per-stratum where paraphrastic regresses by -0.5 (threshold 0.1) → `any_stratum_regressed is True`.
- Detail with query_id not in the query set → bucketed under `"unknown"` stratum; doesn't crash.
- Empty `DriftSummary.details` → `by_stratum == {}`, `any_stratum_regressed is False`.
- All deltas are None → `by_stratum` still produces an entry per stratum, with `n_scored=0, cumulative_delta=0.0`.

## Estimated effort

**3–4 hours.**

## Confidence

**92%** — pure-function aggregation on top of already-typed dataclasses. No external dependencies, no SQL. The only judgment call is whether to include an "unknown" stratum bucket vs raise on unknown queries — choosing the bucket because the CI workflow shouldn't fail-the-build on a defensive corner-case.
