"""Drift-detection thresholds + per-stratum drift surface — LCM v4.1 §11.

Thin helper module layered on top of :func:`lossless_hermes.eval.run.compute_drift`.

``compute_drift`` (issue #09-04, in :mod:`lossless_hermes.eval.run`) returns
a flat :class:`~lossless_hermes.eval.run.DriftSummary` — aggregate
``drifted``/``improved``/``regressed`` counts plus a per-query ``details``
list. The operator UI (``/lcm eval``, Epic 08) and the +52.5pp benchmark
(#09-08) both need **per-stratum** drift instead — e.g. "paraphrastic
regressed -0.04, fts-easy improved +0.02". This module adds that surface.

### Why a separate module

There is **no standalone "drift detector" module in the TS source** — the
drift logic is inline in ``computeDrift`` (``lossless-claw/src/eval/run.ts``,
~lines 283–375). The Python port splits the per-stratum surface into this
helper deliberately:

* :mod:`lossless_hermes.eval.run` is already 375 LOC and crosses the
  schema-write seam (it INSERTs into ``lcm_eval_run`` / ``lcm_eval_drift``).
* The CI workflow (#09-07) needs to import only the pure decision functions
  (:func:`drift_threshold`, :func:`is_drifted`,
  :attr:`PerStratumDrift.any_stratum_regressed`) **without** pulling in the
  ``sqlite3`` dependency chain.

The TS source did not split this only because TS's smaller-module convention
is less strict — the Python port adopts the split on purpose.

### Threshold math (architecture-v4.1 §11.1)

The "drifted" threshold is ``2 × empirical SD`` when a noise floor was
calibrated, else ``0`` (any non-zero delta counts). :func:`drift_threshold`
surfaces that as a pure function so tests and UI can reason about it
independently of the run record.

**Note vs the TS source:** TS ``computeDrift`` computes the threshold inline
as ``noiseFloor !== null ? 2 * noiseFloor : 0`` — it does *not* guard against
a zero or negative SD. :func:`drift_threshold` adds that guard
(``noise_floor_sd is not None and noise_floor_sd > 0``): a zero SD would make
*every* non-zero delta "drifted" anyway (the same as the no-floor branch), and
a negative SD is nonsensical calibration output. Treating both as "no floor"
is strictly safer and matches the acceptance criteria.

See:

* ``epics/09-eval/09-06-drift-detection.md`` — this issue.
* ``lossless-claw/src/eval/run.ts:283-375`` — inline TS drift logic.
* ``architecture-v4.1.md §11.1`` — "2× empirical SD" threshold reference.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from lossless_hermes.eval.query_set import QuerySet
from lossless_hermes.eval.run import DriftSummary

__all__ = [
    "UNKNOWN_STRATUM",
    "PerStratumDrift",
    "StratumDriftAggregate",
    "drift_threshold",
    "is_drifted",
    "per_stratum_drift",
]


UNKNOWN_STRATUM = "unknown"
"""Sentinel stratum for :class:`~lossless_hermes.eval.run.DriftDetail`
entries whose ``query_id`` is not present in the :class:`QuerySet`.

This shouldn't happen in practice — ``compute_drift`` derives its query IDs
from the same query set — but it *is* possible if the set is mutated
mid-eval. :func:`per_stratum_drift` buckets such queries here rather than
raising or silently dropping them: the CI workflow (#09-07) must not
fail-the-build on a defensive corner-case, and a dropped query would make
``cumulative_delta`` disagree with :attr:`DriftSummary.cumulative_delta`.
"""


def drift_threshold(noise_floor_sd: float | None) -> float:
    """Compute the drift-detection threshold from a calibrated noise floor.

    Per architecture-v4.1 §11.1: the threshold is ``2 × empirical SD`` when a
    noise floor was calibrated, else ``0`` (meaning any non-zero delta
    counts as drift — see :func:`is_drifted`).

    A ``None``, zero, or negative ``noise_floor_sd`` all map to ``0.0``: a
    zero SD would make the ``2 × SD`` threshold zero anyway, and a negative
    SD is nonsensical calibration output, so both are treated as "no floor".

    Args:
        noise_floor_sd: Empirical standard deviation from baseline
            calibration, or ``None`` if no baseline was calibrated.

    Returns:
        The drift threshold — ``2 × noise_floor_sd`` if it is a positive
        number, else ``0.0``.
    """
    if noise_floor_sd is not None and noise_floor_sd > 0:
        return 2.0 * noise_floor_sd
    return 0.0


def is_drifted(delta: float, threshold: float) -> bool:
    """Decide whether a per-query ``delta`` counts as drift.

    Ports the inline check in TS ``computeDrift``
    (``run.ts``: ``threshold > 0 ? Math.abs(delta) >= threshold : delta !== 0``).

    Args:
        delta: The signed per-query score change (``current - prior``).
        threshold: The drift threshold from :func:`drift_threshold`. A
            non-positive threshold means "no calibrated floor".

    Returns:
        * If ``threshold > 0``: ``True`` iff ``abs(delta) >= threshold``.
        * If ``threshold <= 0``: ``True`` iff ``delta != 0.0`` (any
          non-zero change is drift when there is no floor to absorb noise).
    """
    if threshold > 0:
        return abs(delta) >= threshold
    return delta != 0.0


@dataclass(frozen=True, slots=True)
class StratumDriftAggregate:
    """Per-stratum drift roll-up.

    Aggregates the :class:`~lossless_hermes.eval.run.DriftDetail` entries
    that belong to a single stratum.

    Attributes:
        stratum: The stratum name (one of ``fts-easy`` / ``fts-medium`` /
            ``paraphrastic``, or :data:`UNKNOWN_STRATUM` for queries not in
            the query set).
        drifted: Count of scored queries whose ``delta`` crossed the
            threshold (per :func:`is_drifted`).
        improved: Of the ``drifted`` queries, how many improved
            (``delta > 0``).
        regressed: Of the ``drifted`` queries, how many regressed
            (``delta < 0``). ``improved + regressed <= drifted`` always
            holds (a ``delta`` exactly ``0`` can never be drifted).
        cumulative_delta: Signed sum of all non-``None`` deltas in this
            stratum — improvements and regressions can cancel.
        n_scored: Count of :class:`~lossless_hermes.eval.run.DriftDetail`
            entries in this stratum with a non-``None`` ``delta`` (i.e.
            present in both the prior and current run).
    """

    stratum: str
    drifted: int
    improved: int
    regressed: int
    cumulative_delta: float
    n_scored: int


@dataclass(frozen=True, slots=True)
class PerStratumDrift:
    """Per-stratum drift surface for one eval run.

    Returned by :func:`per_stratum_drift`. Carries the original flat
    :class:`~lossless_hermes.eval.run.DriftSummary` alongside the
    per-stratum breakdown so callers don't have to re-thread it.

    Attributes:
        overall: The flat :class:`~lossless_hermes.eval.run.DriftSummary`
            this breakdown was computed from (unchanged passthrough).
        by_stratum: One :class:`StratumDriftAggregate` per stratum that had
            at least one scored query. Strata with zero scored queries are
            omitted — parity with
            :attr:`~lossless_hermes.eval.recall.RecallReport.by_stratum`.
        threshold_used: The drift threshold applied (from
            :func:`drift_threshold`). Surfaced so the CI workflow and UI
            can render it and reason about :attr:`any_stratum_regressed`.
    """

    overall: DriftSummary
    by_stratum: dict[str, StratumDriftAggregate]
    threshold_used: float

    @property
    def any_stratum_regressed(self) -> bool:
        """Whether any stratum regressed beyond the threshold.

        This is the boolean the CI workflow (#09-07) reads to decide
        whether to fail the build.

        Returns:
            ``True`` iff at least one stratum has
            ``cumulative_delta < -threshold_used``. With
            ``threshold_used == 0`` (no calibrated floor) this is ``True``
            iff some stratum has a strictly-negative cumulative delta.
            ``False`` when every stratum is flat or improved.
        """
        return any(s.cumulative_delta < -self.threshold_used for s in self.by_stratum.values())


def per_stratum_drift(
    summary: DriftSummary,
    query_set: QuerySet,
    noise_floor_sd: float | None = None,
) -> PerStratumDrift:
    """Group a flat :class:`DriftSummary` into per-stratum aggregates.

    Joins ``summary.details[*].query_id`` against
    ``query_set.queries[*].stratum`` and rolls up
    ``drifted``/``improved``/``regressed``/``cumulative_delta``/``n_scored``
    per stratum.

    A :class:`~lossless_hermes.eval.run.DriftDetail` whose ``query_id`` is
    not in ``query_set`` is bucketed under :data:`UNKNOWN_STRATUM` rather
    than dropped (defensive — see :data:`UNKNOWN_STRATUM`). Details with a
    ``None`` ``delta`` (query present in only one of the two runs) do not
    contribute to ``n_scored``, ``drifted``, or ``cumulative_delta``, but a
    stratum that contains *only* such details still gets an entry (with
    ``n_scored=0``).

    Args:
        summary: The flat drift summary from
            :func:`~lossless_hermes.eval.run.compute_drift`.
        query_set: The query set ``summary`` was computed against —
            supplies the ``query_id`` → ``stratum`` mapping.
        noise_floor_sd: Optional noise-floor SD. Passed through
            :func:`drift_threshold` to derive
            :attr:`PerStratumDrift.threshold_used`.

    Returns:
        A :class:`PerStratumDrift` with one
        :class:`StratumDriftAggregate` per stratum that has at least one
        :class:`~lossless_hermes.eval.run.DriftDetail` (scored or not).
    """
    threshold = drift_threshold(noise_floor_sd)

    stratum_of: dict[str, str] = {q.query_id: q.stratum for q in query_set.queries}

    # Accumulate per stratum. defaultdict keeps the first-seen ordering of
    # strata stable, which is fine — callers index by key, not position.
    drifted: dict[str, int] = defaultdict(int)
    improved: dict[str, int] = defaultdict(int)
    regressed: dict[str, int] = defaultdict(int)
    cumulative: dict[str, float] = defaultdict(float)
    n_scored: dict[str, int] = defaultdict(int)
    # Track every stratum that has *any* detail so a stratum with only
    # None-delta details still produces an (empty) aggregate.
    seen: set[str] = set()

    for detail in summary.details:
        stratum = stratum_of.get(detail.query_id, UNKNOWN_STRATUM)
        seen.add(stratum)
        if detail.delta is None:
            continue
        delta = detail.delta
        n_scored[stratum] += 1
        cumulative[stratum] += delta
        if is_drifted(delta, threshold):
            drifted[stratum] += 1
            if delta > 0:
                improved[stratum] += 1
            elif delta < 0:
                regressed[stratum] += 1

    by_stratum: dict[str, StratumDriftAggregate] = {
        stratum: StratumDriftAggregate(
            stratum=stratum,
            drifted=drifted[stratum],
            improved=improved[stratum],
            regressed=regressed[stratum],
            cumulative_delta=cumulative[stratum],
            n_scored=n_scored[stratum],
        )
        for stratum in seen
    }

    return PerStratumDrift(
        overall=summary,
        by_stratum=by_stratum,
        threshold_used=threshold,
    )
