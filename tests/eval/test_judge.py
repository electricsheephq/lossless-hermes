"""Tests for :mod:`lossless_hermes.eval.judge` — LLM-as-judge ensemble.

Ports ``lossless-claw/test/eval-judge.test.ts`` (commit ``1f07fbd`` on
branch ``pr-613``) line-for-line, plus the spec-mandated concurrency /
sequentiality / non-finite-score / int-vs-float fixtures from
``epics/09-eval/09-03-eval-judge.md``.

Covers:

* Basic ensemble aggregation (single + N judges).
* Empty-``judges`` ``ValueError``.
* Judge-failure tolerance: ``None`` score, raised exception, non-finite
  score — each counted as a failure with the right ``reason``.
* ``mean_score is None`` when every judge fails for a query.
* ``judge_failures`` counts total events across ``(queries × judges)``.
* Query selection: queries missing a candidate are skipped.
* Reference text forwarded only when the query carries one.
* Within-query parallelism + across-query sequentiality (wall-time).
"""

from __future__ import annotations

import asyncio
import math
import time

import pytest

from lossless_hermes.eval.judge import (
    JudgeCallArgs,
    JudgeCallResult,
    JudgeEntry,
    run_quality_eval,
)
from lossless_hermes.eval.query_set import QueryRecord

# ---------------------------------------------------------------------------
# Test judges (port of the TS test helpers, judge.test.ts:11-37)
# ---------------------------------------------------------------------------


class _ConstJudge:
    """Deterministic judge that always returns the same score+reason.

    Port of TS ``constJudge`` (``eval-judge.test.ts:11-18``).
    """

    def __init__(self, score: float | None, reason: str = "test") -> None:
        self._score = score
        self._reason = reason

    async def judge(self, args: JudgeCallArgs) -> JudgeCallResult:
        return JudgeCallResult(score=self._score, reason=self._reason)


class _ThrowingJudge:
    """Judge that raises on every call.

    Port of TS ``throwingJudge`` (``eval-judge.test.ts:20-27``).
    """

    def __init__(self, message: str) -> None:
        self._message = message

    async def judge(self, args: JudgeCallArgs) -> JudgeCallResult:
        raise RuntimeError(self._message)


class _PerQueryJudge:
    """Judge that varies score by query text.

    Port of TS ``perQueryJudge`` (``eval-judge.test.ts:29-37``).
    """

    def __init__(self, scores: dict[str, float | None]) -> None:
        self._scores = scores

    async def judge(self, args: JudgeCallArgs) -> JudgeCallResult:
        score = self._scores.get(args.query)
        return JudgeCallResult(score=score, reason=f"score for {args.query}")


class _SleepyJudge:
    """Judge that sleeps before returning — exercises concurrency.

    Not in the TS source; the spec mandates a wall-time fixture
    (``09-03-eval-judge.md`` ACs "judges fire in parallel" /
    "across queries, sequential").
    """

    def __init__(self, sleep_s: float, score: float) -> None:
        self._sleep_s = sleep_s
        self._score = score

    async def judge(self, args: JudgeCallArgs) -> JudgeCallResult:
        await asyncio.sleep(self._sleep_s)
        return JudgeCallResult(score=self._score, reason="slept")


class _SniffJudge:
    """Judge that records the last ``reference`` it was passed.

    Port of the inline ``sniff`` judge (``eval-judge.test.ts:184-209``).
    """

    def __init__(self) -> None:
        self.seen_ref: str | None = None
        self.ref_was_set = False

    async def judge(self, args: JudgeCallArgs) -> JudgeCallResult:
        self.seen_ref = args.reference
        self.ref_was_set = True
        return JudgeCallResult(score=5, reason="ok")


# ---------------------------------------------------------------------------
# Fixture data (port of judge.test.ts:39-54)
# ---------------------------------------------------------------------------


QUERIES: list[QueryRecord] = [
    QueryRecord(query_id="q1", query_text="what is X", stratum="fts-easy"),
    QueryRecord(query_id="q2", query_text="explain Y", stratum="fts-medium"),
    QueryRecord(
        query_id="q3",
        query_text="describe Z",
        stratum="paraphrastic",
        reference_summary="Z is the third letter of the latin alphabet from the end.",
    ),
]

CANDIDATES: dict[str, str] = {
    "q1": "X is a thing.",
    "q2": "Y is another thing.",
    "q3": "Z is the last letter of the latin alphabet.",
}


# ---------------------------------------------------------------------------
# Basic ensemble (port of judge.test.ts:56-86)
# ---------------------------------------------------------------------------


class TestBasicEnsemble:
    async def test_aggregates_across_multiple_judges(self) -> None:
        """Port of ``eval-judge.test.ts:57-72``."""
        judges = [
            JudgeEntry(judge_id="j-a", call=_ConstJudge(4)),
            JudgeEntry(judge_id="j-b", call=_ConstJudge(5)),
            JudgeEntry(judge_id="j-c", call=_ConstJudge(3)),
        ]
        report = await run_quality_eval(QUERIES, CANDIDATES, judges)
        assert len(report.per_query) == 3
        for r in report.per_query:
            assert len(r.per_judge_scores) == 3
            assert r.mean_score == 4.0  # (4+5+3)/3 = 4
        assert report.overall.mean_score == 4.0
        assert report.overall.n == 3
        assert report.overall.judge_failures == 0

    async def test_works_with_a_single_judge(self) -> None:
        """Port of ``eval-judge.test.ts:74-79``."""
        judges = [JudgeEntry(judge_id="solo", call=_ConstJudge(3.5))]
        report = await run_quality_eval(QUERIES, CANDIDATES, judges)
        assert report.overall.mean_score == 3.5
        assert report.overall.n == 3

    async def test_single_judge_single_query_score_4_5(self) -> None:
        """Spec-mandated: single judge, single query, score=4.5 → 4.5.

        ``09-03-eval-judge.md`` Tests section.
        """
        judges = [JudgeEntry(judge_id="solo", call=_ConstJudge(4.5))]
        report = await run_quality_eval([QUERIES[0]], CANDIDATES, judges)
        assert len(report.per_query) == 1
        assert report.per_query[0].mean_score == 4.5
        assert report.overall.mean_score == 4.5
        assert report.overall.n == 1

    async def test_ensemble_of_3_judges_5_3_4_means_4(self) -> None:
        """Spec-mandated: ensemble (5, 3, 4) → mean_score == 4.0."""
        judges = [
            JudgeEntry(judge_id="j-5", call=_ConstJudge(5)),
            JudgeEntry(judge_id="j-3", call=_ConstJudge(3)),
            JudgeEntry(judge_id="j-4", call=_ConstJudge(4)),
        ]
        report = await run_quality_eval([QUERIES[0]], CANDIDATES, judges)
        assert report.per_query[0].mean_score == 4.0

    async def test_requires_at_least_one_judge(self) -> None:
        """Port of ``eval-judge.test.ts:81-85``."""
        with pytest.raises(ValueError, match="at least one judge"):
            await run_quality_eval(QUERIES, CANDIDATES, [])


# ---------------------------------------------------------------------------
# Failure handling (port of judge.test.ts:88-171)
# ---------------------------------------------------------------------------


class TestFailureHandling:
    async def test_null_score_returns_are_failures_not_in_mean(self) -> None:
        """Port of ``eval-judge.test.ts:89-101``."""
        judges = [
            JudgeEntry(judge_id="j-a", call=_ConstJudge(4)),
            JudgeEntry(judge_id="j-b", call=_ConstJudge(None, "no-decision")),
        ]
        report = await run_quality_eval(QUERIES, CANDIDATES, judges)
        for r in report.per_query:
            assert len(r.per_judge_scores) == 2
            assert r.mean_score == 4.0  # 4 from j-a; j-b's None excluded
        assert report.overall.judge_failures == 3  # j-b failed on all 3 queries
        assert report.overall.n == 3  # all 3 queries had ≥1 success

    async def test_null_score_preserves_judge_reason(self) -> None:
        """A ``None``-score judge keeps its own reason verbatim."""
        judges = [
            JudgeEntry(judge_id="j-a", call=_ConstJudge(4)),
            JudgeEntry(judge_id="j-b", call=_ConstJudge(None, "no-decision")),
        ]
        report = await run_quality_eval([QUERIES[0]], CANDIDATES, judges)
        j_b = next(s for s in report.per_query[0].per_judge_scores if s.judge_id == "j-b")
        assert j_b.score is None
        assert j_b.reason == "no-decision"

    async def test_null_score_with_empty_reason_falls_back_to_no_decision(self) -> None:
        """A ``None``-score judge with an empty reason → ``"no_decision"``."""
        judges = [JudgeEntry(judge_id="j", call=_ConstJudge(None, ""))]
        report = await run_quality_eval([QUERIES[0]], CANDIDATES, judges)
        s = report.per_query[0].per_judge_scores[0]
        assert s.score is None
        assert s.reason == "no_decision"

    async def test_throwing_judges_are_failures_with_judge_error_reason(self) -> None:
        """Port of ``eval-judge.test.ts:103-115``."""
        judges = [
            JudgeEntry(judge_id="j-good", call=_ConstJudge(5)),
            JudgeEntry(judge_id="j-broken", call=_ThrowingJudge("network timeout")),
        ]
        report = await run_quality_eval(QUERIES, CANDIDATES, judges)
        q1 = next(r for r in report.per_query if r.query_id == "q1")
        broken = next(s for s in q1.per_judge_scores if s.judge_id == "j-broken")
        assert broken.score is None
        assert broken.reason.startswith("judge_error")
        assert "network timeout" in broken.reason
        assert q1.mean_score == 5.0
        assert report.overall.judge_failures == 3

    async def test_judge_runtime_error_oops_captured(self) -> None:
        """Spec-mandated: ``RuntimeError("oops")`` → ``reason="judge_error: oops"``."""
        judges = [
            JudgeEntry(judge_id="j-good", call=_ConstJudge(4)),
            JudgeEntry(judge_id="j-oops", call=_ThrowingJudge("oops")),
        ]
        report = await run_quality_eval([QUERIES[0]], CANDIDATES, judges)
        oops = next(s for s in report.per_query[0].per_judge_scores if s.judge_id == "j-oops")
        assert oops.score is None
        assert oops.reason == "judge_error: oops"
        # eval continues — the good judge still scored.
        assert report.per_query[0].mean_score == 4.0

    async def test_exception_type_does_not_leak(self) -> None:
        """The exception type is not surfaced — only the message string is."""

        class _CustomError(Exception):
            pass

        class _CustomRaiser:
            async def judge(self, args: JudgeCallArgs) -> JudgeCallResult:
                raise _CustomError("custom boom")

        judges = [JudgeEntry(judge_id="j", call=_CustomRaiser())]
        report = await run_quality_eval([QUERIES[0]], CANDIDATES, judges)
        s = report.per_query[0].per_judge_scores[0]
        assert s.score is None
        assert isinstance(s.reason, str)
        assert s.reason == "judge_error: custom boom"
        assert "_CustomError" not in s.reason

    async def test_non_finite_scores_are_failures_nan(self) -> None:
        """Port of ``eval-judge.test.ts:117-132`` (NaN)."""
        judges = [
            JudgeEntry(judge_id="j-good", call=_ConstJudge(4)),
            JudgeEntry(judge_id="j-weird", call=_ConstJudge(math.nan, "?")),
        ]
        report = await run_quality_eval(QUERIES, CANDIDATES, judges)
        q1 = report.per_query[0]
        weird = next(s for s in q1.per_judge_scores if s.judge_id == "j-weird")
        assert weird.score is None
        assert weird.reason.startswith("invalid_score")
        assert "nan" in weird.reason.lower()

    async def test_non_finite_scores_are_failures_inf(self) -> None:
        """Spec-mandated: ``float("inf")`` → invalidated to ``None``."""
        judges = [
            JudgeEntry(judge_id="j-good", call=_ConstJudge(4)),
            JudgeEntry(judge_id="j-inf", call=_ConstJudge(math.inf, "?")),
        ]
        report = await run_quality_eval([QUERIES[0]], CANDIDATES, judges)
        inf = next(s for s in report.per_query[0].per_judge_scores if s.judge_id == "j-inf")
        assert inf.score is None
        assert inf.reason.startswith("invalid_score")
        # j-good still counts → mean is 4.0, not poisoned by the inf.
        assert report.per_query[0].mean_score == 4.0

    async def test_non_finite_scores_are_failures_negative_inf(self) -> None:
        """``float("-inf")`` → invalidated to ``None``."""
        judges = [JudgeEntry(judge_id="j", call=_ConstJudge(-math.inf, "?"))]
        report = await run_quality_eval([QUERIES[0]], CANDIDATES, judges)
        s = report.per_query[0].per_judge_scores[0]
        assert s.score is None
        assert s.reason.startswith("invalid_score")
        assert report.per_query[0].mean_score is None

    async def test_integer_scores_accepted(self) -> None:
        """The ``isFinite`` check accepts ``int`` scores (rubric may use them).

        ``09-03-eval-judge.md`` Confidence note: ``int`` and ``float``
        both pass; ``nan``/``inf`` reject regardless of type.
        """
        judges = [JudgeEntry(judge_id="j-int", call=_ConstJudge(5))]
        report = await run_quality_eval([QUERIES[0]], CANDIDATES, judges)
        s = report.per_query[0].per_judge_scores[0]
        assert s.score == 5.0
        assert report.per_query[0].mean_score == 5.0

    async def test_zero_score_is_valid_not_a_failure(self) -> None:
        """A finite ``0`` score is a real score, not a failure."""
        judges = [JudgeEntry(judge_id="j-zero", call=_ConstJudge(0))]
        report = await run_quality_eval([QUERIES[0]], CANDIDATES, judges)
        assert report.per_query[0].per_judge_scores[0].score == 0.0
        assert report.per_query[0].mean_score == 0.0
        assert report.overall.n == 1
        assert report.overall.judge_failures == 0

    async def test_null_mean_score_when_all_judges_fail_for_a_query(self) -> None:
        """Port of ``eval-judge.test.ts:134-147``."""
        judges = [
            JudgeEntry(judge_id="j-a", call=_ThrowingJudge("A down")),
            JudgeEntry(judge_id="j-b", call=_ConstJudge(None, "B can't tell")),
        ]
        report = await run_quality_eval(QUERIES, CANDIDATES, judges)
        for r in report.per_query:
            assert r.mean_score is None
        # overall.n = number of queries with ≥1 success → 0
        assert report.overall.n == 0
        assert report.overall.mean_score == 0.0
        assert report.overall.judge_failures == 6  # 2 judges × 3 queries

    async def test_all_judges_fail_single_query_judge_failures_3(self) -> None:
        """Spec-mandated: all 3 judges fail for one query → failures == 3."""
        judges = [
            JudgeEntry(judge_id="j-a", call=_ThrowingJudge("a")),
            JudgeEntry(judge_id="j-b", call=_ThrowingJudge("b")),
            JudgeEntry(judge_id="j-c", call=_ConstJudge(None)),
        ]
        report = await run_quality_eval([QUERIES[0]], CANDIDATES, judges)
        assert report.per_query[0].mean_score is None
        assert report.overall.n == 0
        assert report.overall.judge_failures == 3

    async def test_one_null_judge_two_judges_still_average(self) -> None:
        """Spec-mandated: one ``null`` judge, two valid → mean of the two; failures==1."""
        judges = [
            JudgeEntry(judge_id="j-a", call=_ConstJudge(4)),
            JudgeEntry(judge_id="j-b", call=_ConstJudge(2)),
            JudgeEntry(judge_id="j-null", call=_ConstJudge(None)),
        ]
        report = await run_quality_eval([QUERIES[0]], CANDIDATES, judges)
        # (4+2)/2 = 3.0 — the null judge is excluded.
        assert report.per_query[0].mean_score == 3.0
        assert report.overall.judge_failures == 1

    async def test_overall_mean_over_only_queries_with_one_success(self) -> None:
        """Port of ``eval-judge.test.ts:149-170``.

        q1 + q2 succeed; q3 has all judges fail.
        """
        j1 = _PerQueryJudge({"what is X": 4, "explain Y": 5, "describe Z": None})
        j2 = _PerQueryJudge({"what is X": 2, "explain Y": 3, "describe Z": None})
        judges = [
            JudgeEntry(judge_id="j1", call=j1),
            JudgeEntry(judge_id="j2", call=j2),
        ]
        report = await run_quality_eval(QUERIES, CANDIDATES, judges)
        # q1.mean = (4+2)/2 = 3; q2.mean = (5+3)/2 = 4; q3.mean = None
        assert next(r for r in report.per_query if r.query_id == "q1").mean_score == 3.0
        assert next(r for r in report.per_query if r.query_id == "q2").mean_score == 4.0
        assert next(r for r in report.per_query if r.query_id == "q3").mean_score is None
        # overall = (3+4)/2 = 3.5; n = 2; failures = 2 (both judges on q3)
        assert report.overall.mean_score == 3.5
        assert report.overall.n == 2
        assert report.overall.judge_failures == 2


# ---------------------------------------------------------------------------
# Query selection (port of judge.test.ts:173-210)
# ---------------------------------------------------------------------------


class TestQuerySelection:
    async def test_skips_queries_with_no_candidate(self) -> None:
        """Port of ``eval-judge.test.ts:174-181``."""
        partial = {"q1": "X candidate"}
        judges = [JudgeEntry(judge_id="j", call=_ConstJudge(5))]
        report = await run_quality_eval(QUERIES, partial, judges)
        assert len(report.per_query) == 1
        assert report.per_query[0].query_id == "q1"
        assert report.overall.n == 1

    async def test_empty_candidates_returns_empty_per_query(self) -> None:
        """Spec-mandated: empty ``candidates_by_query`` → empty + ``n == 0``."""
        judges = [JudgeEntry(judge_id="j", call=_ConstJudge(5))]
        report = await run_quality_eval(QUERIES, {}, judges)
        assert report.per_query == []
        assert report.overall.n == 0
        assert report.overall.mean_score == 0.0
        assert report.overall.judge_failures == 0

    async def test_query_missing_from_candidates_is_skipped(self) -> None:
        """Spec-mandated: a query in ``queries`` but not in candidates is skipped.

        No per-query result, no contribution to ``n``.
        """
        # q2 is absent from the candidates map.
        candidates = {"q1": "cand 1", "q3": "cand 3"}
        judges = [JudgeEntry(judge_id="j", call=_ConstJudge(5))]
        report = await run_quality_eval(QUERIES, candidates, judges)
        seen_ids = {r.query_id for r in report.per_query}
        assert seen_ids == {"q1", "q3"}
        assert report.overall.n == 2

    async def test_forwards_reference_text_when_present(self) -> None:
        """Port of ``eval-judge.test.ts:183-196``."""
        sniff = _SniffJudge()
        judges = [JudgeEntry(judge_id="sniff", call=sniff)]
        # QUERIES[2] (q3) carries a reference_summary.
        await run_quality_eval([QUERIES[2]], CANDIDATES, judges)
        assert sniff.seen_ref == "Z is the third letter of the latin alphabet from the end."

    async def test_does_not_forward_reference_when_query_lacks_one(self) -> None:
        """Port of ``eval-judge.test.ts:198-209``."""
        sniff = _SniffJudge()
        judges = [JudgeEntry(judge_id="sniff", call=sniff)]
        # QUERIES[0] (q1) has no reference_summary.
        await run_quality_eval([QUERIES[0]], CANDIDATES, judges)
        assert sniff.ref_was_set is True  # the judge WAS called
        assert sniff.seen_ref is None  # but reference was None

    async def test_candidate_is_echoed_into_result(self) -> None:
        """The judged candidate text is surfaced in the per-query result."""
        judges = [JudgeEntry(judge_id="j", call=_ConstJudge(5))]
        report = await run_quality_eval([QUERIES[0]], CANDIDATES, judges)
        assert report.per_query[0].candidate == "X is a thing."


# ---------------------------------------------------------------------------
# Concurrency contract (spec-mandated, 09-03-eval-judge.md ACs)
# ---------------------------------------------------------------------------


class TestConcurrency:
    async def test_judges_within_a_query_fire_in_parallel(self) -> None:
        """Spec AC: 3 judges each sleeping 100 ms → wall time < 300 ms.

        If the judges ran sequentially this would take ~300 ms; parallel
        dispatch via ``asyncio.gather`` overlaps them to ~100 ms.
        """
        judges = [
            JudgeEntry(judge_id="j-1", call=_SleepyJudge(0.1, 4)),
            JudgeEntry(judge_id="j-2", call=_SleepyJudge(0.1, 4)),
            JudgeEntry(judge_id="j-3", call=_SleepyJudge(0.1, 4)),
        ]
        start = time.perf_counter()
        report = await run_quality_eval([QUERIES[0]], CANDIDATES, judges)
        elapsed = time.perf_counter() - start
        assert report.per_query[0].mean_score == 4.0
        assert elapsed < 0.3, f"judges did not overlap: {elapsed:.3f}s"

    async def test_queries_run_sequentially(self) -> None:
        """Spec AC: 3 queries each with 1 judge sleeping 100 ms → ≥ 300 ms.

        Across queries the harness is sequential — total wall time is the
        sum, not the max.
        """
        judges = [JudgeEntry(judge_id="j", call=_SleepyJudge(0.1, 5))]
        start = time.perf_counter()
        report = await run_quality_eval(QUERIES, CANDIDATES, judges)
        elapsed = time.perf_counter() - start
        assert len(report.per_query) == 3
        assert elapsed >= 0.3, f"queries were not serialized: {elapsed:.3f}s"
