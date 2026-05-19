"""Tests for :mod:`lossless_hermes.eval.drift` — drift thresholds + per-stratum surface.

Covers issue ``09-06`` (drift-detection thresholds + per-stratum drift
surface). There is no standalone TS counterpart — the drift logic is inline
in ``lossless-claw/src/eval/run.ts`` ``computeDrift`` (commit ``1f07fbd`` on
branch ``pr-613``); this module + tests are a deliberate Python-side split.

Covers:

* :func:`drift_threshold` — ``2 × SD`` math; ``None`` / zero / negative SD
  all collapse to ``0.0``.
* :func:`is_drifted` — threshold-zero (any non-zero delta) vs
  threshold-positive (``abs(delta) >= threshold``) branches.
* :func:`per_stratum_drift` — grouping by stratum, ``drifted`` /
  ``improved`` / ``regressed`` / ``cumulative_delta`` / ``n_scored`` roll-up.
* :attr:`PerStratumDrift.any_stratum_regressed` — the CI fail-the-build flag.
* Defensive ``unknown`` stratum bucket for query IDs not in the query set.
* Empty / all-``None``-delta edge cases.
"""

from __future__ import annotations

import pytest

from lossless_hermes.eval.drift import (
    UNKNOWN_STRATUM,
    PerStratumDrift,
    StratumDriftAggregate,
    drift_threshold,
    is_drifted,
    per_stratum_drift,
)
from lossless_hermes.eval.query_set import (
    QueryRecord,
    QuerySet,
    QuerySetIdentity,
    Stratum,
)
from lossless_hermes.eval.run import DriftDetail, DriftSummary


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _detail(query_id: str, delta: float | None) -> DriftDetail:
    """A :class:`DriftDetail` with a given delta.

    ``prior_score`` / ``current_score`` are set so that
    ``current - prior == delta`` when ``delta`` is not ``None`` — only
    ``query_id`` and ``delta`` matter to :func:`per_stratum_drift`, but
    keeping the scores consistent avoids confusing future readers.
    """
    if delta is None:
        return DriftDetail(query_id=query_id, prior_score=0.5, current_score=None, delta=None)
    return DriftDetail(query_id=query_id, prior_score=0.0, current_score=delta, delta=delta)


def _summary(*details: DriftDetail) -> DriftSummary:
    """A :class:`DriftSummary` wrapping ``details``.

    The aggregate counts on the summary are not read by
    :func:`per_stratum_drift` (it recomputes from ``details``), so they are
    set to placeholder values; ``cumulative_delta`` is summed honestly.
    """
    cumulative = sum(d.delta for d in details if d.delta is not None)
    return DriftSummary(
        drifted=0,
        improved=0,
        regressed=0,
        details=tuple(details),
        prior_run_id="evalrun_prior",
        cumulative_delta=cumulative,
    )


def _query_set(*entries: tuple[str, Stratum]) -> QuerySet:
    """A :class:`QuerySet` mapping each ``(query_id, stratum)`` to a query."""
    return QuerySet(
        identity=QuerySetIdentity(name="drift-test", version=1),
        queries=tuple(
            QueryRecord(
                query_id=qid,
                query_text=f"text for {qid}",
                stratum=stratum,
                expected_summary_ids=("x",),
            )
            for qid, stratum in entries
        ),
    )


# ---------------------------------------------------------------------------
# drift_threshold
# ---------------------------------------------------------------------------


class TestDriftThreshold:
    @pytest.mark.parametrize(
        ("noise_floor_sd", "expected"),
        [
            (None, 0.0),
            (0.0, 0.0),
            (-0.05, 0.0),  # negative SD treated as None.
            (0.05, 0.10),
            (1e-10, 2e-10),
        ],
    )
    def test_table(self, noise_floor_sd: float | None, expected: float) -> None:
        assert drift_threshold(noise_floor_sd) == pytest.approx(expected)

    def test_none_is_zero(self) -> None:
        assert drift_threshold(None) == 0.0

    def test_zero_is_zero(self) -> None:
        assert drift_threshold(0.0) == 0.0

    def test_negative_treated_as_none(self) -> None:
        # A negative SD is nonsensical calibration output — treated as
        # "no floor" (return 0), same as None.
        assert drift_threshold(-1.0) == 0.0

    def test_positive_is_doubled(self) -> None:
        assert drift_threshold(0.05) == pytest.approx(0.10)


# ---------------------------------------------------------------------------
# is_drifted
# ---------------------------------------------------------------------------


class TestIsDrifted:
    @pytest.mark.parametrize(
        ("delta", "expected"),
        [
            (-0.01, True),
            (0.0, False),
            (0.01, True),
        ],
    )
    def test_threshold_zero_any_nonzero(self, delta: float, expected: bool) -> None:
        # threshold=0 → any non-zero delta is drift; delta=0 is not.
        assert is_drifted(delta, 0.0) is expected

    @pytest.mark.parametrize(
        ("delta", "expected"),
        [
            (-0.15, True),
            (-0.05, False),
            (0.0, False),
            (0.05, False),
            (0.15, True),
        ],
    )
    def test_threshold_positive(self, delta: float, expected: bool) -> None:
        # threshold=0.10 → drift iff abs(delta) >= 0.10.
        assert is_drifted(delta, 0.10) is expected

    def test_threshold_boundary_is_inclusive(self) -> None:
        # The check is `>=`, so a delta exactly on the threshold drifts.
        assert is_drifted(0.10, 0.10) is True
        assert is_drifted(-0.10, 0.10) is True

    def test_negative_threshold_behaves_like_zero(self) -> None:
        # A non-positive threshold means "no floor" — any non-zero delta.
        assert is_drifted(0.01, -0.10) is True
        assert is_drifted(0.0, -0.10) is False


# ---------------------------------------------------------------------------
# per_stratum_drift — grouping + aggregation
# ---------------------------------------------------------------------------


class TestPerStratumDrift:
    def test_basic_two_strata(self) -> None:
        # 3 fts-easy (deltas +0.1, +0.05, -0.02) and 2 paraphrastic
        # (deltas +0.6, +0.5) under threshold 0.1 (noise_floor_sd=0.05).
        summary = _summary(
            _detail("e1", 0.10),
            _detail("e2", 0.05),
            _detail("e3", -0.02),
            _detail("p1", 0.60),
            _detail("p2", 0.50),
        )
        query_set = _query_set(
            ("e1", "fts-easy"),
            ("e2", "fts-easy"),
            ("e3", "fts-easy"),
            ("p1", "paraphrastic"),
            ("p2", "paraphrastic"),
        )

        result = per_stratum_drift(summary, query_set, noise_floor_sd=0.05)

        assert result.threshold_used == pytest.approx(0.10)
        assert set(result.by_stratum) == {"fts-easy", "paraphrastic"}

        easy = result.by_stratum["fts-easy"]
        assert easy.stratum == "fts-easy"
        # +0.10 crosses the 0.10 threshold (inclusive); +0.05 and -0.02 do not.
        assert easy.drifted == 1
        assert easy.improved == 1
        assert easy.regressed == 0
        assert easy.cumulative_delta == pytest.approx(0.13)
        assert easy.n_scored == 3

        para = result.by_stratum["paraphrastic"]
        assert para.stratum == "paraphrastic"
        assert para.drifted == 2
        assert para.improved == 2
        assert para.regressed == 0
        assert para.cumulative_delta == pytest.approx(1.10)
        assert para.n_scored == 2

    def test_overall_passthrough(self) -> None:
        # `overall` is the original DriftSummary, unchanged.
        summary = _summary(_detail("q1", 0.1))
        query_set = _query_set(("q1", "fts-easy"))
        result = per_stratum_drift(summary, query_set)
        assert result.overall is summary

    def test_n_scored_excludes_none_deltas(self) -> None:
        # n_scored counts only details with a non-None delta.
        summary = _summary(
            _detail("q1", 0.10),
            _detail("q2", None),
            _detail("q3", -0.20),
        )
        query_set = _query_set(
            ("q1", "fts-easy"),
            ("q2", "fts-easy"),
            ("q3", "fts-easy"),
        )
        result = per_stratum_drift(summary, query_set, noise_floor_sd=0.05)
        easy = result.by_stratum["fts-easy"]
        assert easy.n_scored == 2  # q2 (None delta) excluded.
        # cumulative is the signed sum of the two non-None deltas.
        assert easy.cumulative_delta == pytest.approx(-0.10)

    def test_cumulative_delta_is_signed_sum(self) -> None:
        # Improvements and regressions cancel in the cumulative.
        summary = _summary(
            _detail("q1", 0.30),
            _detail("q2", -0.30),
            _detail("q3", 0.05),
        )
        query_set = _query_set(
            ("q1", "fts-medium"),
            ("q2", "fts-medium"),
            ("q3", "fts-medium"),
        )
        result = per_stratum_drift(summary, query_set)
        assert result.by_stratum["fts-medium"].cumulative_delta == pytest.approx(0.05)

    def test_improved_plus_regressed_le_drifted(self) -> None:
        # Invariant: improved + regressed <= drifted, per stratum.
        # delta exactly 0 can be "drifted" only when threshold>0 is False,
        # but 0 != 0 is False, so a 0 delta is never drifted regardless.
        summary = _summary(
            _detail("q1", 0.50),  # drifted + improved
            _detail("q2", -0.40),  # drifted + regressed
            _detail("q3", 0.00),  # not drifted (delta == 0)
            _detail("q4", 0.01),  # not drifted (below threshold 0.10)
        )
        query_set = _query_set(
            ("q1", "fts-easy"),
            ("q2", "fts-easy"),
            ("q3", "fts-easy"),
            ("q4", "fts-easy"),
        )
        result = per_stratum_drift(summary, query_set, noise_floor_sd=0.05)
        easy = result.by_stratum["fts-easy"]
        assert easy.drifted == 2
        assert easy.improved == 1
        assert easy.regressed == 1
        assert easy.improved + easy.regressed <= easy.drifted

    def test_regression_within_stratum_counted(self) -> None:
        # A stratum with a clear regression: counts and sign.
        summary = _summary(
            _detail("p1", -0.30),
            _detail("p2", -0.20),
        )
        query_set = _query_set(
            ("p1", "paraphrastic"),
            ("p2", "paraphrastic"),
        )
        result = per_stratum_drift(summary, query_set, noise_floor_sd=0.05)
        para = result.by_stratum["paraphrastic"]
        assert para.drifted == 2
        assert para.improved == 0
        assert para.regressed == 2
        assert para.cumulative_delta == pytest.approx(-0.50)

    def test_threshold_zero_no_noise_floor(self) -> None:
        # With no noise floor, threshold is 0 — any non-zero delta drifts.
        summary = _summary(
            _detail("q1", 0.001),
            _detail("q2", -0.001),
            _detail("q3", 0.0),
        )
        query_set = _query_set(
            ("q1", "fts-easy"),
            ("q2", "fts-easy"),
            ("q3", "fts-easy"),
        )
        result = per_stratum_drift(summary, query_set)  # noise_floor_sd=None
        assert result.threshold_used == 0.0
        easy = result.by_stratum["fts-easy"]
        assert easy.drifted == 2  # q1 and q2; q3 (delta 0) not drifted.
        assert easy.improved == 1
        assert easy.regressed == 1


# ---------------------------------------------------------------------------
# per_stratum_drift — any_stratum_regressed
# ---------------------------------------------------------------------------


class TestAnyStratumRegressed:
    def test_all_positive_is_false(self) -> None:
        # Every stratum improves → no regression.
        summary = _summary(
            _detail("e1", 0.10),
            _detail("e2", 0.05),
            _detail("p1", 0.60),
            _detail("p2", 0.50),
        )
        query_set = _query_set(
            ("e1", "fts-easy"),
            ("e2", "fts-easy"),
            ("p1", "paraphrastic"),
            ("p2", "paraphrastic"),
        )
        result = per_stratum_drift(summary, query_set, noise_floor_sd=0.05)
        assert result.any_stratum_regressed is False

    def test_paraphrastic_regression_is_true(self) -> None:
        # paraphrastic regresses by -0.5 with threshold 0.10
        # (noise_floor_sd=0.05) → cumulative -0.5 < -0.10 → regressed.
        summary = _summary(
            _detail("e1", 0.10),
            _detail("p1", -0.50),
        )
        query_set = _query_set(
            ("e1", "fts-easy"),
            ("p1", "paraphrastic"),
        )
        result = per_stratum_drift(summary, query_set, noise_floor_sd=0.05)
        assert result.any_stratum_regressed is True

    def test_flat_strata_is_false(self) -> None:
        # Every stratum is exactly flat → not regressed.
        summary = _summary(
            _detail("e1", 0.0),
            _detail("p1", 0.0),
        )
        query_set = _query_set(
            ("e1", "fts-easy"),
            ("p1", "paraphrastic"),
        )
        result = per_stratum_drift(summary, query_set, noise_floor_sd=0.05)
        assert result.any_stratum_regressed is False

    def test_small_regression_below_threshold_is_false(self) -> None:
        # A cumulative regression smaller than the threshold magnitude
        # does NOT trip any_stratum_regressed.
        summary = _summary(_detail("p1", -0.05))  # cumulative -0.05.
        query_set = _query_set(("p1", "paraphrastic"))
        # threshold = 2 * 0.05 = 0.10; -0.05 < -0.10 is False.
        result = per_stratum_drift(summary, query_set, noise_floor_sd=0.05)
        assert result.any_stratum_regressed is False

    def test_regression_with_zero_threshold(self) -> None:
        # With no noise floor (threshold 0), any strictly-negative
        # cumulative trips the flag.
        summary = _summary(_detail("p1", -0.01))
        query_set = _query_set(("p1", "paraphrastic"))
        result = per_stratum_drift(summary, query_set)  # threshold 0.
        assert result.any_stratum_regressed is True

    def test_property_directly_on_dataclass(self) -> None:
        # any_stratum_regressed reads by_stratum + threshold_used directly.
        pos = PerStratumDrift(
            overall=_summary(),
            by_stratum={
                "fts-easy": StratumDriftAggregate(
                    stratum="fts-easy",
                    drifted=1,
                    improved=0,
                    regressed=1,
                    cumulative_delta=-0.30,
                    n_scored=1,
                ),
            },
            threshold_used=0.10,
        )
        assert pos.any_stratum_regressed is True

        neg = PerStratumDrift(
            overall=_summary(),
            by_stratum={
                "fts-easy": StratumDriftAggregate(
                    stratum="fts-easy",
                    drifted=0,
                    improved=0,
                    regressed=0,
                    cumulative_delta=0.05,
                    n_scored=1,
                ),
            },
            threshold_used=0.10,
        )
        assert neg.any_stratum_regressed is False


# ---------------------------------------------------------------------------
# per_stratum_drift — defensive / edge cases
# ---------------------------------------------------------------------------


class TestPerStratumDriftEdgeCases:
    def test_unknown_query_bucketed(self) -> None:
        # A detail whose query_id is not in the query set lands in the
        # `unknown` stratum — not dropped, doesn't crash.
        summary = _summary(
            _detail("known", 0.10),
            _detail("ghost", 0.50),
        )
        query_set = _query_set(("known", "fts-easy"))  # `ghost` absent.

        result = per_stratum_drift(summary, query_set, noise_floor_sd=0.05)

        assert UNKNOWN_STRATUM in result.by_stratum
        unknown = result.by_stratum[UNKNOWN_STRATUM]
        assert unknown.stratum == UNKNOWN_STRATUM
        assert unknown.n_scored == 1
        assert unknown.drifted == 1
        assert unknown.improved == 1
        assert unknown.cumulative_delta == pytest.approx(0.50)
        # The known query is still bucketed correctly.
        assert result.by_stratum["fts-easy"].n_scored == 1

    def test_unknown_sentinel_value(self) -> None:
        # The sentinel is the literal string "unknown".
        assert UNKNOWN_STRATUM == "unknown"

    def test_empty_details(self) -> None:
        # No details at all → empty by_stratum, no regression.
        summary = _summary()
        query_set = _query_set(("q1", "fts-easy"), ("q2", "paraphrastic"))
        result = per_stratum_drift(summary, query_set, noise_floor_sd=0.05)
        assert result.by_stratum == {}
        assert result.any_stratum_regressed is False

    def test_all_deltas_none(self) -> None:
        # Every detail has a None delta → each stratum present in the
        # details still gets an entry, with n_scored=0, cumulative=0.0.
        summary = _summary(
            _detail("q1", None),
            _detail("q2", None),
            _detail("q3", None),
        )
        query_set = _query_set(
            ("q1", "fts-easy"),
            ("q2", "fts-easy"),
            ("q3", "paraphrastic"),
        )
        result = per_stratum_drift(summary, query_set, noise_floor_sd=0.05)

        assert set(result.by_stratum) == {"fts-easy", "paraphrastic"}
        for agg in result.by_stratum.values():
            assert agg.n_scored == 0
            assert agg.cumulative_delta == 0.0
            assert agg.drifted == 0
            assert agg.improved == 0
            assert agg.regressed == 0
        assert result.any_stratum_regressed is False

    def test_stratum_with_no_details_omitted(self) -> None:
        # A stratum in the query set with zero matching details is omitted
        # — parity with RecallReport.by_stratum.
        summary = _summary(_detail("q1", 0.10))
        query_set = _query_set(
            ("q1", "fts-easy"),
            ("q2", "fts-medium"),  # no detail references q2.
            ("q3", "paraphrastic"),  # no detail references q3.
        )
        result = per_stratum_drift(summary, query_set, noise_floor_sd=0.05)
        assert set(result.by_stratum) == {"fts-easy"}

    def test_mixed_none_and_scored_in_same_stratum(self) -> None:
        # A stratum with one None-delta and one scored detail: n_scored
        # counts only the scored one, but the stratum entry exists.
        summary = _summary(
            _detail("q1", None),
            _detail("q2", 0.30),
        )
        query_set = _query_set(
            ("q1", "fts-easy"),
            ("q2", "fts-easy"),
        )
        result = per_stratum_drift(summary, query_set, noise_floor_sd=0.05)
        easy = result.by_stratum["fts-easy"]
        assert easy.n_scored == 1
        assert easy.drifted == 1
        assert easy.cumulative_delta == pytest.approx(0.30)
