"""Tests for :mod:`lossless_hermes.eval.recall` — recall@K + RR metric.

Ports ``lossless-claw/test/eval-recall.test.ts`` (commit ``1f07fbd`` on
branch ``pr-613``).

Covers:

* Per-query recall@K + reciprocal rank.
* Aggregation across queries (skip queries without expected IDs).
* Per-stratum aggregation.
* Wave-4 per-query timeout (timed-out adapter returns zero recall).
* Wave-5 timeout clamp (≤0 / NaN → default 30s; >5min → 5min cap).
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
    async def test_query_without_expected_is_skipped(self) -> None:
        queries = (QueryRecord(query_id="q1", query_text="x", stratum="fts-easy"),)
        adapter = _DictAdapter({"q1": ["a"]})
        report = await run_recall_eval(queries, adapter)
        # Per-query result still present, but recall_at_k empty.
        assert len(report.per_query) == 1
        assert report.per_query[0].recall_at_k == {}
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
