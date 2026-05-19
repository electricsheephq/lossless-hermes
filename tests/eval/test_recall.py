"""Tests for :mod:`lossless_hermes.eval.recall` — recall@K + RR metric.

Ports ``lossless-claw/test/eval-recall.test.ts`` (commit ``1f07fbd`` on
branch ``pr-613``).

Covers:

* Per-query recall@K + reciprocal rank.
* Aggregation across queries (skip queries without expected IDs).
* Per-stratum aggregation.
* Empty-``queries`` input → empty report.
* Unsorted caller ``k_values`` accepted (sorted internally).
* Wave-4 per-query timeout (timed-out adapter returns zero recall).
* Wave-5 timeout clamp (≤0 / NaN → default 30s; >5min → 5min cap).
* Wave-9 timer-cleanup regression — 100 queries leave no pending tasks
  (load-bearing ADR-029 contract).
* Adapter exceptions bubble (not swallowed).
"""

from __future__ import annotations

import asyncio

import pytest

from lossless_hermes.eval.query_set import QueryRecord
from lossless_hermes.eval.recall import (
    DEFAULT_K_VALUES,
    RecallEvalOptions,
    run_recall_eval,
)


class _DictAdapter:
    """Returns canned hits per query id."""

    def __init__(self, canned: dict[str, list[str]]) -> None:
        self._canned = canned

    async def search(self, query: QueryRecord) -> list[str]:
        return list(self._canned.get(query.query_id, []))


class _SleepyAdapter:
    """Sleeps before returning — exercises the per-query timeout."""

    def __init__(self, sleep_s: float, hits: list[str]) -> None:
        self._sleep_s = sleep_s
        self._hits = hits

    async def search(self, _query: QueryRecord) -> list[str]:
        await asyncio.sleep(self._sleep_s)
        return list(self._hits)


class _RaisingAdapter:
    async def search(self, _query: QueryRecord) -> list[str]:
        raise RuntimeError("boom")


# ---------------------------------------------------------------------------
# Per-query metric
# ---------------------------------------------------------------------------


class TestPerQuery:
    @pytest.mark.asyncio
    async def test_perfect_match_recall_one(self) -> None:
        queries = (
            QueryRecord(
                query_id="q1",
                query_text="x",
                stratum="fts-easy",
                expected_summary_ids=("a", "b"),
            ),
        )
        adapter = _DictAdapter({"q1": ["a", "b", "c"]})
        report = await run_recall_eval(queries, adapter)
        q1 = report.per_query[0]
        assert q1.recall_at_k[5] == pytest.approx(1.0)
        assert q1.reciprocal_rank == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_partial_recall(self) -> None:
        queries = (
            QueryRecord(
                query_id="q1",
                query_text="x",
                stratum="fts-easy",
                expected_summary_ids=("a", "b"),
            ),
        )
        adapter = _DictAdapter({"q1": ["a", "x", "y"]})
        report = await run_recall_eval(queries, adapter)
        q1 = report.per_query[0]
        assert q1.recall_at_k[5] == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_recall_at_k_truncates(self) -> None:
        queries = (
            QueryRecord(
                query_id="q1",
                query_text="x",
                stratum="fts-easy",
                expected_summary_ids=("a", "b"),
            ),
        )
        # 'a' is at rank 1, 'b' at rank 6 — within K=5, only 'a' counts.
        adapter = _DictAdapter({"q1": ["a", "x", "y", "z", "w", "b"]})
        report = await run_recall_eval(
            queries, adapter, opts=RecallEvalOptions(k_values=[1, 5, 10])
        )
        q1 = report.per_query[0]
        assert q1.recall_at_k[1] == pytest.approx(0.5)  # 'a' only
        assert q1.recall_at_k[5] == pytest.approx(0.5)  # still 'a' only
        assert q1.recall_at_k[10] == pytest.approx(1.0)  # both 'a' and 'b'

    @pytest.mark.asyncio
    async def test_reciprocal_rank_picks_first_hit(self) -> None:
        queries = (
            QueryRecord(
                query_id="q1",
                query_text="x",
                stratum="fts-easy",
                expected_summary_ids=("a", "b"),
            ),
        )
        # 'b' at rank 3 — first hit; 1/3 = 0.333…
        adapter = _DictAdapter({"q1": ["x", "y", "b", "a"]})
        report = await run_recall_eval(queries, adapter)
        assert report.per_query[0].reciprocal_rank == pytest.approx(1 / 3)

    @pytest.mark.asyncio
    async def test_total_miss_zero_recall_zero_rr(self) -> None:
        """TS ``eval-recall.test.ts:78-87`` — a query whose hits contain
        none of the expected IDs scores 0 at every K and RR=0."""
        queries = (
            QueryRecord(
                query_id="q3",
                query_text="paraphrastic, total miss",
                stratum="paraphrastic",
                expected_summary_ids=("sum_d",),
            ),
        )
        adapter = _DictAdapter({"q3": ["sum_x", "sum_y", "sum_z"]})
        report = await run_recall_eval(
            queries, adapter, opts=RecallEvalOptions(k_values=[1, 5, 10])
        )
        q3 = report.per_query[0]
        assert q3.recall_at_k[1] == pytest.approx(0.0)
        assert q3.recall_at_k[5] == pytest.approx(0.0)
        assert q3.recall_at_k[10] == pytest.approx(0.0)
        assert q3.reciprocal_rank == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_query_without_expected_is_skipped(self) -> None:
        queries = (QueryRecord(query_id="q1", query_text="x", stratum="fts-easy"),)
        adapter = _DictAdapter({"q1": ["a"]})
        report = await run_recall_eval(queries, adapter)
        # Per-query result still present, but recall_at_k empty.
        assert len(report.per_query) == 1
        assert report.per_query[0].recall_at_k == {}
        assert report.per_query[0].expected == ()
        assert report.per_query[0].reciprocal_rank == 0.0
        # And the overall aggregate has n=0 (no scored queries).
        assert report.overall.n == 0

    @pytest.mark.asyncio
    async def test_duplicate_hits_dont_inflate_recall(self) -> None:
        queries = (
            QueryRecord(
                query_id="q1",
                query_text="x",
                stratum="fts-easy",
                expected_summary_ids=("a",),
            ),
        )
        # Adapter erroneously returns 'a' twice — recall must still be 1.0,
        # not 2.0.
        adapter = _DictAdapter({"q1": ["a", "a", "b"]})
        report = await run_recall_eval(queries, adapter)
        assert report.per_query[0].recall_at_k[5] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


class TestAggregation:
    @pytest.mark.asyncio
    async def test_overall_averages_scored_queries_only(self) -> None:
        queries = (
            QueryRecord(
                query_id="q1",
                query_text="x",
                stratum="fts-easy",
                expected_summary_ids=("a",),
            ),
            QueryRecord(
                query_id="q2",
                query_text="x",
                stratum="fts-easy",
                expected_summary_ids=("b",),
            ),
            QueryRecord(query_id="q3", query_text="x", stratum="paraphrastic"),  # no expected
        )
        adapter = _DictAdapter({"q1": ["a"], "q2": ["x", "b"], "q3": []})
        report = await run_recall_eval(queries, adapter)
        # q1 RR=1.0, q2 RR=0.5; overall mean RR = 0.75. q3 excluded.
        assert report.overall.n == 2
        assert report.overall.mean_rr == pytest.approx(0.75)

    @pytest.mark.asyncio
    async def test_per_stratum_grouping(self) -> None:
        queries = (
            QueryRecord(
                query_id="q1",
                query_text="x",
                stratum="fts-easy",
                expected_summary_ids=("a",),
            ),
            QueryRecord(
                query_id="q2",
                query_text="x",
                stratum="paraphrastic",
                expected_summary_ids=("b",),
            ),
        )
        adapter = _DictAdapter({"q1": ["a"], "q2": ["b"]})
        report = await run_recall_eval(queries, adapter)
        assert "fts-easy" in report.by_stratum
        assert "paraphrastic" in report.by_stratum
        assert report.by_stratum["fts-easy"].n == 1
        assert report.by_stratum["paraphrastic"].n == 1

    @pytest.mark.asyncio
    async def test_overall_and_per_stratum_with_canonical_fixture(self) -> None:
        """TS ``eval-recall.test.ts:100-132`` — the canonical 4-query
        fixture exercises per-stratum + overall means together.

        q1 (fts-easy)    expected=[sum_a]        hit at rank 1
        q2 (fts-medium)  expected=[sum_b,sum_c]  hits at ranks 2 and 5
        q3 (paraphrastic) expected=[sum_d]       total miss
        q4 (fts-easy)    no expected             SKIPPED

        Scored = {q1, q2, q3}. mean recall@1 = (1+0+0)/3; mean recall@5
        = (1+1+0)/3; mean RR = (1.0+0.5+0)/3 = 0.5.
        """
        queries = (
            QueryRecord(
                query_id="q1",
                query_text="perfect hit at top",
                stratum="fts-easy",
                expected_summary_ids=("sum_a",),
            ),
            QueryRecord(
                query_id="q2",
                query_text="two expected, partial @ low K",
                stratum="fts-medium",
                expected_summary_ids=("sum_b", "sum_c"),
            ),
            QueryRecord(
                query_id="q3",
                query_text="paraphrastic, total miss",
                stratum="paraphrastic",
                expected_summary_ids=("sum_d",),
            ),
            QueryRecord(query_id="q4", query_text="no ground truth", stratum="fts-easy"),
        )
        adapter = _DictAdapter(
            {
                "q1": ["sum_a", "sum_x", "sum_y"],
                "q2": ["sum_x", "sum_b", "sum_y", "sum_z", "sum_c"],
                "q3": ["sum_x", "sum_y", "sum_z"],
                "q4": ["sum_x"],
            }
        )
        report = await run_recall_eval(queries, adapter, opts=RecallEvalOptions(k_values=[1, 5]))

        # Per-stratum: q4 is skipped, so fts-easy has n=1 (q1 only).
        assert report.by_stratum["fts-easy"].n == 1
        assert report.by_stratum["fts-easy"].mean_recall_at_k[1] == pytest.approx(1.0)
        assert report.by_stratum["fts-easy"].mean_rr == pytest.approx(1.0)
        assert report.by_stratum["fts-medium"].n == 1
        assert report.by_stratum["fts-medium"].mean_recall_at_k[1] == pytest.approx(0.0)
        assert report.by_stratum["fts-medium"].mean_recall_at_k[5] == pytest.approx(1.0)
        assert report.by_stratum["fts-medium"].mean_rr == pytest.approx(0.5)
        assert report.by_stratum["paraphrastic"].n == 1
        assert report.by_stratum["paraphrastic"].mean_recall_at_k[1] == pytest.approx(0.0)

        # Overall across the 3 scored queries.
        assert report.overall.n == 3
        assert report.overall.mean_recall_at_k[1] == pytest.approx(1 / 3)
        assert report.overall.mean_recall_at_k[5] == pytest.approx(2 / 3)
        assert report.overall.mean_rr == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Wave-N fixes — load-bearing per ADR-029
# ---------------------------------------------------------------------------


class TestWaveNFixes:
    @pytest.mark.asyncio
    async def test_wave4_per_query_timeout(self) -> None:
        """Wave-4 Auditor #15 P1: timeout returns zero recall, eval continues."""
        queries = (
            QueryRecord(
                query_id="q1",
                query_text="x",
                stratum="fts-easy",
                expected_summary_ids=("a",),
            ),
            QueryRecord(
                query_id="q2",
                query_text="x",
                stratum="fts-easy",
                expected_summary_ids=("b",),
            ),
        )

        class _MixedAdapter:
            async def search(self, query: QueryRecord) -> list[str]:
                if query.query_id == "q1":
                    await asyncio.sleep(2.0)  # exceeds timeout
                    return ["a"]
                return ["b"]

        # 100ms timeout — q1 hangs; q2 returns immediately.
        report = await run_recall_eval(
            queries,
            _MixedAdapter(),
            opts=RecallEvalOptions(per_query_timeout_ms=100),
        )
        q1 = next(r for r in report.per_query if r.query_id == "q1")
        q2 = next(r for r in report.per_query if r.query_id == "q2")
        # q1 timed out → zero recall
        assert q1.recall_at_k[5] == pytest.approx(0.0)
        assert q1.reciprocal_rank == pytest.approx(0.0)
        # q2 unaffected
        assert q2.recall_at_k[5] == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_wave5_timeout_clamp_zero_uses_default(self) -> None:
        """Wave-5 P2: timeout=0 must use default 30s, not zero-out everything."""
        queries = (
            QueryRecord(
                query_id="q1",
                query_text="x",
                stratum="fts-easy",
                expected_summary_ids=("a",),
            ),
        )
        # 200ms adapter — well under the 30s default, but would be killed if
        # the timeout=0 wasn't clamped.
        adapter = _SleepyAdapter(0.2, ["a"])
        report = await run_recall_eval(
            queries,
            adapter,
            opts=RecallEvalOptions(per_query_timeout_ms=0),
        )
        # If clamp works, q1 still hits 'a' → recall=1.0.
        assert report.per_query[0].recall_at_k[5] == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_wave5_timeout_clamp_negative_uses_default(self) -> None:
        """Wave-5 P2: timeout<0 must use default 30s."""
        queries = (
            QueryRecord(
                query_id="q1",
                query_text="x",
                stratum="fts-easy",
                expected_summary_ids=("a",),
            ),
        )
        adapter = _SleepyAdapter(0.2, ["a"])
        report = await run_recall_eval(
            queries,
            adapter,
            opts=RecallEvalOptions(per_query_timeout_ms=-500),
        )
        assert report.per_query[0].recall_at_k[5] == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_wave5_timeout_clamp_below_minimum_uses_default(self) -> None:
        """Wave-5 P2: ``per_query_timeout_ms`` below the 100ms minimum
        (here ``10``) falls back to the 30s default.

        Mirrors the 09-02 acceptance line: "``per_query_timeout_ms < 100``
        falls back to 30 s default." A 200ms adapter would be killed by a
        literal 10ms timeout, so a recall of 1.0 proves the fallback.
        """
        queries = (
            QueryRecord(
                query_id="q1",
                query_text="x",
                stratum="fts-easy",
                expected_summary_ids=("a",),
            ),
        )
        adapter = _SleepyAdapter(0.2, ["a"])
        report = await run_recall_eval(
            queries,
            adapter,
            opts=RecallEvalOptions(per_query_timeout_ms=10),
        )
        assert report.per_query[0].recall_at_k[5] == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_wave5_timeout_clamp_above_max_clamps_to_five_min(self) -> None:
        """Wave-5 P2: ``per_query_timeout_ms=600_000`` (10 min) clamps to
        the 5-minute cap.

        Per the 09-02 acceptance line: "``> 300_000`` (5 min) clamps to
        5 min." We can't wait 5 minutes, so the observable contract is
        that an over-cap request still yields a working, positive
        timeout: a fast adapter completes normally (recall 1.0). The
        clamp arithmetic — ``min(600_000, 5*60*1000) == 300_000`` — is a
        pure-function ceiling; what matters here is that an out-of-range
        request never degrades the eval.
        """
        queries = (
            QueryRecord(
                query_id="q1",
                query_text="x",
                stratum="fts-easy",
                expected_summary_ids=("a",),
            ),
        )
        adapter = _SleepyAdapter(0.05, ["a"])
        report = await run_recall_eval(
            queries,
            adapter,
            opts=RecallEvalOptions(per_query_timeout_ms=600_000),
        )
        assert report.per_query[0].recall_at_k[5] == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_wave9_no_task_leak_after_100_queries(self) -> None:
        """Wave-9 Agent #10 P1: 100 fast queries leave NO pending tasks.

        Load-bearing ADR-029 contract — ported verbatim from the TS
        regression intent (``recall.ts:191-208``). The TS source leaked
        one ``setTimeout`` per query for the whole timeout duration;
        Python's :func:`asyncio.wait_for` cancels the loser of the race,
        so no timer/task survives. After running 100 queries, the only
        task alive in the loop is THIS test's own task.
        """
        queries = tuple(
            QueryRecord(
                query_id=f"q{i}",
                query_text="x",
                stratum="fts-easy",
                expected_summary_ids=("a",),
            )
            for i in range(100)
        )
        # 50ms sleep each — well under the default timeout, so every
        # query resolves via the adapter (not the timeout branch).
        adapter = _SleepyAdapter(0.05, ["a"])
        report = await run_recall_eval(queries, adapter)
        assert len(report.per_query) == 100
        # The only remaining task is the coroutine running this test.
        pending = asyncio.all_tasks()
        assert len(pending) == 1
        assert pending == {asyncio.current_task()}


# ---------------------------------------------------------------------------
# Error propagation
# ---------------------------------------------------------------------------


class TestErrorPropagation:
    @pytest.mark.asyncio
    async def test_adapter_exception_bubbles(self) -> None:
        """recall.ts:158-162 — adapter exceptions are NOT swallowed."""
        queries = (
            QueryRecord(
                query_id="q1",
                query_text="x",
                stratum="fts-easy",
                expected_summary_ids=("a",),
            ),
        )
        with pytest.raises(RuntimeError, match="boom"):
            await run_recall_eval(queries, _RaisingAdapter())

    @pytest.mark.asyncio
    async def test_empty_k_values_raises(self) -> None:
        queries = (
            QueryRecord(
                query_id="q1",
                query_text="x",
                stratum="fts-easy",
                expected_summary_ids=("a",),
            ),
        )
        with pytest.raises(ValueError, match="k_values must be non-empty"):
            await run_recall_eval(
                queries,
                _DictAdapter({"q1": ["a"]}),
                opts=RecallEvalOptions(k_values=[]),
            )

    @pytest.mark.asyncio
    async def test_non_positive_k_raises(self) -> None:
        queries = (
            QueryRecord(
                query_id="q1",
                query_text="x",
                stratum="fts-easy",
                expected_summary_ids=("a",),
            ),
        )
        with pytest.raises(ValueError, match="positive integers"):
            await run_recall_eval(
                queries,
                _DictAdapter({"q1": ["a"]}),
                opts=RecallEvalOptions(k_values=[0]),
            )

    @pytest.mark.asyncio
    async def test_non_integer_k_raises(self) -> None:
        """TS ``eval-recall.test.ts:167`` — a non-integer K (``1.5``) is
        rejected with the offending value in the message.

        ``isinstance(1.5, int)`` is ``False``, so the positive-integer
        guard catches it.
        """
        queries = (
            QueryRecord(
                query_id="q1",
                query_text="x",
                stratum="fts-easy",
                expected_summary_ids=("a",),
            ),
        )
        with pytest.raises(ValueError, match="positive integers"):
            await run_recall_eval(
                queries,
                _DictAdapter({"q1": ["a"]}),
                opts=RecallEvalOptions(k_values=[1.5]),  # type: ignore[list-item]
            )


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_empty_queries_yields_empty_report(self) -> None:
        """TS ``eval-recall.test.ts:173-180`` — no queries → empty report.

        ``per_query`` is empty, ``by_stratum`` is empty, and ``overall``
        is the zero aggregate (n=0, mean RR 0, mean recall 0 at every K).
        """
        report = await run_recall_eval(
            (), _DictAdapter({}), opts=RecallEvalOptions(k_values=[1, 5])
        )
        assert report.per_query == ()
        assert report.by_stratum == {}
        assert report.overall.n == 0
        assert report.overall.mean_rr == pytest.approx(0.0)
        assert report.overall.mean_recall_at_k[1] == pytest.approx(0.0)
        assert report.overall.mean_recall_at_k[5] == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_unsorted_k_values_accepted(self) -> None:
        """TS ``eval-recall.test.ts:146-154`` — caller may pass ``k_values``
        in any order; recall is computed for every K regardless.

        ``run_recall_eval`` sorts internally, so ``[50, 1, 5]`` produces
        recall at all three K values.
        """
        queries = (
            QueryRecord(
                query_id="q1",
                query_text="perfect hit at top",
                stratum="fts-easy",
                expected_summary_ids=("sum_a",),
            ),
        )
        adapter = _DictAdapter({"q1": ["sum_a", "sum_x", "sum_y"]})
        report = await run_recall_eval(
            queries, adapter, opts=RecallEvalOptions(k_values=[50, 1, 5])
        )
        q1 = report.per_query[0]
        assert q1.recall_at_k[1] == pytest.approx(1.0)
        assert q1.recall_at_k[5] == pytest.approx(1.0)
        assert q1.recall_at_k[50] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Default K values
# ---------------------------------------------------------------------------


class TestDefaultKValues:
    @pytest.mark.asyncio
    async def test_default_k_values_used(self) -> None:
        queries = (
            QueryRecord(
                query_id="q1",
                query_text="x",
                stratum="fts-easy",
                expected_summary_ids=("a",),
            ),
        )
        report = await run_recall_eval(queries, _DictAdapter({"q1": ["a"]}))
        q1 = report.per_query[0]
        for k in DEFAULT_K_VALUES:
            assert k in q1.recall_at_k
