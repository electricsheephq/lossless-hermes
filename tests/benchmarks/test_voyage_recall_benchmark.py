"""Tests for ``scripts/benchmark_voyage_recall.py`` — the 09-08 benchmark.

NO LIVE CALLS. Voyage is never contacted: the hybrid arm is driven
either by a canned in-process adapter injected into :func:`run_benchmark`,
or — for the live-path symbol-drift guard — by monkeypatching
``run_hybrid_search`` + ``tick_embedding_backfill`` + ``VoyageClient``.
The DB is in-memory; the corpus is the deterministic ``v41-test-corpus``
Python port.

Covers the 09-08 spec §Tests:

* Mock the Voyage seam to return hits that favor the paraphrastic
  ground truth; run the orchestration; assert the markdown report is
  generated with every required section and the per-stratum delta
  column is filled.
* Assert :func:`compute_lift` correctly subtracts FTS-only from hybrid
  on the same stratum keys.
* The PENDING (no-key) path: the report is still generated, carries the
  FTS baseline, and is clearly marked PENDING.
* The live-arm wiring (``_run_hybrid_arm``) against mocked
  ``run_hybrid_search`` / ``tick_embedding_backfill`` — so a symbol-drift
  in those APIs is caught here rather than only on a live run.

The **live** measurement against real Voyage is NOT part of ``pytest
-q`` — it runs only via the ``scripts/benchmark_voyage_recall.py``
invocation with ``VOYAGE_API_KEY`` set.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

import benchmark_voyage_recall as bench
from lossless_hermes.eval.query_set import (
    QueryRecord,
    QuerySet,
    get_query_set,
    register_query_set,
)
from lossless_hermes.eval.recall import RecallReport, run_recall_eval
from tests.fixtures.eva_baseline_v2 import (
    EVA_BASELINE_V2_IDENTITY,
    build_eva_baseline_v2,
)
from tests.fixtures.test_corpus import build_test_corpus

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_db() -> Iterator[sqlite3.Connection]:
    """In-memory DB with the v41-test-corpus seeded + eva-baseline-v2 registered.

    The same setup the benchmark CLI builds — corpus first (which runs
    the migration ladder), then the query set.
    """
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    build_test_corpus(conn)
    register_query_set(conn, EVA_BASELINE_V2_IDENTITY, build_eva_baseline_v2())
    try:
        yield conn
    finally:
        conn.close()


def _query_set(db: sqlite3.Connection) -> QuerySet:
    qs = get_query_set(db, EVA_BASELINE_V2_IDENTITY)
    assert qs is not None
    return qs


class _PerfectHybridAdapter:
    """A canned hybrid adapter that returns every query's ground truth.

    Models the ideal hybrid arm: paraphrastic queries — which the FTS
    baseline misses entirely — now hit at rank 1. The lift this produces
    is the maximum possible (FTS paraphrastic R@5 0% → hybrid 100%).
    """

    async def search(self, query: QueryRecord) -> list[str]:
        # Expected IDs first (rank 1..), then a noise tail.
        return list(query.expected_summary_ids) + ["noise-x", "noise-y"]


class _FtsParityHybridAdapter:
    """A hybrid adapter that returns exactly what FTS-only would.

    Models a hybrid arm that adds nothing — every stratum's lift is 0.
    Used to prove :func:`compute_lift` reports a genuine 0, not a
    spurious positive.
    """

    def __init__(self, db: sqlite3.Connection) -> None:
        import run_live_eval as rle

        self._fts = rle._build_fts_search(db)

    async def search(self, query: QueryRecord) -> list[str]:
        hits = await self._fts(query.query_text, limit=max(bench.BENCHMARK_K_VALUES))
        return [h.summary_id for h in hits]


# ---------------------------------------------------------------------------
# 1. Full orchestration — injected hybrid adapter, FTS arm real
# ---------------------------------------------------------------------------


class TestFullOrchestration:
    def test_run_benchmark_with_mocked_hybrid_produces_complete_result(
        self, seeded_db: sqlite3.Connection
    ) -> None:
        """run_benchmark drives FTS + hybrid + record + drift + lift."""
        qs = _query_set(seeded_db)
        result = bench.run_benchmark(
            seeded_db,
            qs.queries,
            qs,
            hybrid_adapter=_PerfectHybridAdapter(),
            corpus_leaf_count=54,
        )

        assert result.hybrid_ran is True
        # Both arms recorded distinct run ids in lcm_eval_run.
        assert result.fts_run_id
        assert result.hybrid_run_id
        assert result.fts_run_id != result.hybrid_run_id
        rows = seeded_db.execute("SELECT COUNT(*) FROM lcm_eval_run").fetchone()[0]
        assert rows == 2
        # AC: compute_drift completes without error on both runs.
        assert result.fts_drift_ok is True
        assert result.hybrid_drift_ok is True
        # The per-stratum lift dict is populated.
        assert result.per_stratum_lift is not None
        assert "paraphrastic" in result.per_stratum_lift

    def test_perfect_hybrid_lifts_paraphrastic_from_zero(
        self, seeded_db: sqlite3.Connection
    ) -> None:
        """FTS paraphrastic R@5 is 0; the perfect hybrid arm lifts it to 1.0.

        This is the +52.5pp benchmark's mechanic, in miniature: the lift
        is the full hybrid recall because FTS-only recall is 0.
        """
        qs = _query_set(seeded_db)
        result = bench.run_benchmark(
            seeded_db,
            qs.queries,
            qs,
            hybrid_adapter=_PerfectHybridAdapter(),
            corpus_leaf_count=54,
        )
        fts_para = result.fts_report.by_stratum["paraphrastic"].mean_recall_at_k[5]
        hyb_para = result.hybrid_report.by_stratum["paraphrastic"].mean_recall_at_k[5]
        lift_para = result.per_stratum_lift["paraphrastic"][5]

        assert fts_para == pytest.approx(0.0)
        assert hyb_para == pytest.approx(1.0)
        assert lift_para == pytest.approx(1.0)  # +100pp — the max possible lift

    def test_recorded_runs_carry_correct_modes(self, seeded_db: sqlite3.Connection) -> None:
        """The two lcm_eval_run rows are tagged fts_only / hybrid."""
        qs = _query_set(seeded_db)
        bench.run_benchmark(
            seeded_db,
            qs.queries,
            qs,
            hybrid_adapter=_PerfectHybridAdapter(),
            corpus_leaf_count=54,
        )
        envelopes = [
            r[0] for r in seeded_db.execute("SELECT per_query_scores FROM lcm_eval_run").fetchall()
        ]
        modes = set()
        for env in envelopes:
            import json

            modes.add(json.loads(env)["mode"])
        assert modes == {"fts_only", "hybrid"}


# ---------------------------------------------------------------------------
# 2. No-key (PENDING) path
# ---------------------------------------------------------------------------


class TestPendingPath:
    def test_run_benchmark_without_key_skips_hybrid(self, seeded_db: sqlite3.Connection) -> None:
        """No key + no injected adapter → hybrid arm is skipped."""
        qs = _query_set(seeded_db)
        result = bench.run_benchmark(
            seeded_db,
            qs.queries,
            qs,
            voyage_api_key=None,
            corpus_leaf_count=54,
        )
        assert result.hybrid_ran is False
        assert result.hybrid_report is None
        assert result.per_stratum_lift is None
        assert result.hybrid_run_id is None
        # The FTS arm still ran + recorded + drift-checked.
        assert result.fts_run_id
        assert result.fts_drift_ok is True
        # Only the fts_only run is in lcm_eval_run.
        assert seeded_db.execute("SELECT COUNT(*) FROM lcm_eval_run").fetchone()[0] == 1

    def test_pending_report_is_marked_pending(self, seeded_db: sqlite3.Connection) -> None:
        """The no-key report carries an explicit PENDING section."""
        qs = _query_set(seeded_db)
        result = bench.run_benchmark(
            seeded_db, qs.queries, qs, voyage_api_key=None, corpus_leaf_count=54
        )
        md = bench.build_report_markdown(result)
        assert "Live hybrid run PENDING" in md
        assert "HYBRID ARM PENDING" in md
        # The +52.5pp number, when shown, is explicitly the TS target.
        assert "TS-baseline target" in md
        # The Py Hybrid / Py lift cells read 'pending', not a number.
        assert "_pending_" in md

    def test_pending_report_still_has_measured_fts_baseline(
        self, seeded_db: sqlite3.Connection
    ) -> None:
        """The PENDING report carries the real FTS numbers — not blank."""
        qs = _query_set(seeded_db)
        result = bench.run_benchmark(
            seeded_db, qs.queries, qs, voyage_api_key=None, corpus_leaf_count=54
        )
        md = bench.build_report_markdown(result)
        # The FTS-only recall@K table is present + has the paraphrastic row.
        assert "fts_only — recall@K by stratum" in md
        assert "Measured FTS-only baseline" in md


# ---------------------------------------------------------------------------
# 3. compute_lift correctness
# ---------------------------------------------------------------------------


class TestComputeLift:
    @pytest.mark.asyncio
    async def test_lift_is_hybrid_minus_fts_on_same_strata(self) -> None:
        """compute_lift subtracts FTS recall from hybrid recall per stratum."""
        queries = (
            QueryRecord(
                query_id="fe-1",
                query_text="alpha",
                stratum="fts-easy",
                expected_summary_ids=("s1",),
            ),
            QueryRecord(
                query_id="p-1",
                query_text="beta",
                stratum="paraphrastic",
                expected_summary_ids=("s2",),
            ),
        )

        class _FtsAdapter:
            async def search(self, q: QueryRecord) -> list[str]:
                # fts-easy hits; paraphrastic misses (the classic shape).
                return ["s1"] if q.query_id == "fe-1" else ["noise"]

        class _HybridAdapter:
            async def search(self, q: QueryRecord) -> list[str]:
                # both hit.
                return ["s1"] if q.query_id == "fe-1" else ["s2"]

        fts = await run_recall_eval(queries, _FtsAdapter())
        hybrid = await run_recall_eval(queries, _HybridAdapter())
        lift = bench.compute_lift(fts, hybrid)

        # fts-easy: both 1.0 → lift 0.
        assert lift["fts-easy"][5] == pytest.approx(0.0)
        # paraphrastic: fts 0.0, hybrid 1.0 → lift +1.0.
        assert lift["paraphrastic"][5] == pytest.approx(1.0)
        # Lift keys are exactly the strata present in the hybrid report.
        assert set(lift.keys()) == set(hybrid.by_stratum.keys())

    @pytest.mark.asyncio
    async def test_lift_treats_missing_fts_stratum_as_zero(self) -> None:
        """A stratum present in hybrid but absent from fts → lift == hybrid."""
        # Hybrid has a paraphrastic query; the fts report won't have that
        # stratum if its only paraphrastic query is unscored. Construct an
        # fts report with NO paraphrastic stratum by giving the fts
        # paraphrastic query no expected IDs (unscored → not aggregated).
        fts_queries = (
            QueryRecord(
                query_id="p-1",
                query_text="x",
                stratum="paraphrastic",
                expected_summary_ids=(),  # unscored → no paraphrastic aggregate
            ),
        )
        hybrid_queries = (
            QueryRecord(
                query_id="p-1",
                query_text="x",
                stratum="paraphrastic",
                expected_summary_ids=("s1",),
            ),
        )

        class _Adapter:
            async def search(self, q: QueryRecord) -> list[str]:
                return ["s1"]

        fts = await run_recall_eval(fts_queries, _Adapter())
        hybrid = await run_recall_eval(hybrid_queries, _Adapter())
        assert "paraphrastic" not in fts.by_stratum  # precondition

        lift = bench.compute_lift(fts, hybrid)
        # Missing fts stratum is treated as recall 0 → lift == hybrid recall.
        assert lift["paraphrastic"][5] == pytest.approx(1.0)

    def test_fts_parity_hybrid_yields_zero_lift(self, seeded_db: sqlite3.Connection) -> None:
        """A hybrid arm that returns FTS results verbatim → 0 lift everywhere.

        Proves compute_lift reports a genuine 0 — not a spurious positive
        — when the two arms agree.
        """
        qs = _query_set(seeded_db)
        result = bench.run_benchmark(
            seeded_db,
            qs.queries,
            qs,
            hybrid_adapter=_FtsParityHybridAdapter(seeded_db),
            corpus_leaf_count=54,
        )
        for stratum, by_k in result.per_stratum_lift.items():
            for k, delta in by_k.items():
                assert delta == pytest.approx(0.0), (
                    f"{stratum} R@{k} lift should be 0 when arms agree, got {delta}"
                )


# ---------------------------------------------------------------------------
# 4. Report — required sections
# ---------------------------------------------------------------------------

#: Every section the 09-08 acceptance criteria require in the report.
_REQUIRED_REPORT_SECTIONS = (
    "Result — per-stratum recall lift",
    "Methodology",
    "Cost breakdown",
    "Reproduction recipe",
    "Run metadata",
    "Deviation from the +52.5pp baseline",
)


class TestReportSections:
    def test_full_report_has_every_required_section(self, seeded_db: sqlite3.Connection) -> None:
        """The hybrid-ran report carries every AC-mandated section."""
        qs = _query_set(seeded_db)
        result = bench.run_benchmark(
            seeded_db,
            qs.queries,
            qs,
            hybrid_adapter=_PerfectHybridAdapter(),
            corpus_leaf_count=54,
        )
        md = bench.build_report_markdown(result)
        for section in _REQUIRED_REPORT_SECTIONS:
            assert section in md, f"report missing required section: {section}"

    def test_pending_report_has_every_required_section(self, seeded_db: sqlite3.Connection) -> None:
        """The PENDING report ALSO carries every required section."""
        qs = _query_set(seeded_db)
        result = bench.run_benchmark(
            seeded_db, qs.queries, qs, voyage_api_key=None, corpus_leaf_count=54
        )
        md = bench.build_report_markdown(result)
        for section in _REQUIRED_REPORT_SECTIONS:
            assert section in md, f"report missing required section: {section}"

    def test_report_records_run_metadata(self, seeded_db: sqlite3.Connection) -> None:
        """Run metadata: date, Voyage models, Python version, TS commit."""
        qs = _query_set(seeded_db)
        result = bench.run_benchmark(
            seeded_db,
            qs.queries,
            qs,
            hybrid_adapter=_PerfectHybridAdapter(),
            corpus_leaf_count=54,
        )
        md = bench.build_report_markdown(result)
        assert bench.VOYAGE_EMBED_MODEL in md
        assert bench.VOYAGE_RERANK_MODEL in md
        assert bench.TS_SPIKE_COMMIT in md
        # Python version line.
        import platform

        assert platform.python_version() in md

    def test_report_per_stratum_delta_column_is_filled(self, seeded_db: sqlite3.Connection) -> None:
        """When the hybrid arm ran, the Py-lift column carries pp numbers."""
        qs = _query_set(seeded_db)
        result = bench.run_benchmark(
            seeded_db,
            qs.queries,
            qs,
            hybrid_adapter=_PerfectHybridAdapter(),
            corpus_leaf_count=54,
        )
        md = bench.build_report_markdown(result)
        # The result table's Py-lift cells are signed pp values, not
        # 'pending'. The perfect hybrid arm yields +100.0pp on paraphrastic.
        assert "+100.0pp" in md
        # The acceptance verdict reflects an actual measurement.
        assert "PASS" in md or "CONDITIONAL" in md or "FAIL" in md

    def test_report_acceptance_band_is_47_5_to_57_5(self, seeded_db: sqlite3.Connection) -> None:
        """The report states the [+47.5pp, +57.5pp] acceptance band."""
        qs = _query_set(seeded_db)
        result = bench.run_benchmark(
            seeded_db, qs.queries, qs, voyage_api_key=None, corpus_leaf_count=54
        )
        md = bench.build_report_markdown(result)
        assert "+47.5pp" in md
        assert "+57.5pp" in md


# ---------------------------------------------------------------------------
# 5. CLI entry point — main() writes the report file
# ---------------------------------------------------------------------------


class TestCliMain:
    def test_main_writes_report_no_key(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        """`main` with no key writes a PENDING report + exits EX_OK."""
        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
        out = tmp_path / "report.md"
        exit_code = bench.main(["--out", str(out)])
        assert exit_code == bench.EX_OK
        assert out.exists()
        text = out.read_text(encoding="utf-8")
        assert "Live hybrid run PENDING" in text
        # The measured FTS baseline made it into the file.
        assert "Measured FTS-only baseline" in text

    def test_main_blank_key_is_treated_as_absent(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A blank VOYAGE_API_KEY → hybrid skipped (clean PENDING run)."""
        monkeypatch.setenv("VOYAGE_API_KEY", "   ")
        out = tmp_path / "report.md"
        exit_code = bench.main(["--out", str(out)])
        assert exit_code == bench.EX_OK
        assert "HYBRID ARM PENDING" in out.read_text(encoding="utf-8")

    def test_main_persists_corpus_to_db_path(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`--db <path>` persists the seeded corpus + recorded runs."""
        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
        db_path = tmp_path / "bench.db"
        out = tmp_path / "report.md"
        exit_code = bench.main(["--db", str(db_path), "--out", str(out)])
        assert exit_code == bench.EX_OK
        assert db_path.exists()
        # The persisted DB carries the corpus + the fts_only eval run.
        conn = sqlite3.connect(str(db_path))
        try:
            assert conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0] == 56
            assert conn.execute("SELECT COUNT(*) FROM lcm_eval_run").fetchone()[0] == 1
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 6. Live-arm wiring — symbol-drift guard (Voyage seam fully mocked)
# ---------------------------------------------------------------------------


class _FakeVoyage:
    """A VoyageClient stand-in — no network. ``aclose`` is a no-op.

    Mirrors ``tests/scripts/test_run_live_eval._FakeVoyage``. The hybrid
    tests below stub ``run_hybrid_search`` + ``tick_embedding_backfill``
    directly, so this fake just has to be a non-None closeable object.
    """

    async def aclose(self) -> None:
        return None


class TestLiveArmWiring:
    """`_run_hybrid_arm` against mocked ``run_hybrid_search`` + backfill.

    A drift in the real APIs ``_run_hybrid_arm`` wires — ``VoyageClient``,
    ``tick_embedding_backfill``, ``run_hybrid_search``,
    ``HybridSearchResult.voyage_tokens_consumed`` — breaks these tests,
    so the live path is symbol-checked without spending API budget.
    """

    def test_run_hybrid_arm_drives_backfill_and_hybrid_search(
        self, seeded_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_run_hybrid_arm: backfill → hybrid recall → token accounting."""
        import run_live_eval as rle
        from lossless_hermes.embeddings.backfill import BackfillResult
        from lossless_hermes.embeddings.hybrid_search import (
            HybridHit,
            HybridSearchResult,
        )

        qs = _query_set(seeded_db)

        # Mock the VoyageClient constructor — both backfill + eval arms
        # build one. The fake is a closeable no-network object.
        monkeypatch.setattr(bench, "VoyageClient", lambda **_kw: _FakeVoyage())

        # Mock tick_embedding_backfill: one tick, no per-tick limit, so
        # the backfill loop runs exactly once. Reports 1000 Voyage tokens.
        backfill_calls: list[object] = []

        async def _fake_tick(db: object, **_kw: object) -> BackfillResult:
            backfill_calls.append(db)
            return BackfillResult(
                embedded_count=54,
                per_tick_limit_reached=False,
                voyage_tokens_consumed=1000,
            )

        monkeypatch.setattr(bench, "tick_embedding_backfill", _fake_tick)

        # Mock run_hybrid_search (the name run_live_eval's adapter calls):
        # every query gets its ground truth back at rank 1, plus 50 tokens.
        async def _fake_hybrid(conn: object, **kwargs: object) -> HybridSearchResult:
            query_text = kwargs["query"]
            # Resolve the query record from its text → its expected IDs.
            expected: tuple[str, ...] = ()
            for q in qs.queries:
                if q.query_text == query_text:
                    expected = q.expected_summary_ids or ()
                    break
            hits = [
                HybridHit(
                    summary_id=sid,
                    conversation_id=1,
                    session_key="agent:main:main",
                    kind="leaf",
                    content="",
                    token_count=1,
                    created_at="2026-05-05",
                    score=0.9,
                    from_fts=True,
                    from_semantic=True,
                    semantic_distance=0.1,
                    cosine_similarity=0.99,
                    fts_rank=0,
                )
                for sid in expected
            ]
            return HybridSearchResult(
                hits=hits,
                candidate_count=len(hits),
                voyage_tokens_consumed=50,
            )

        monkeypatch.setattr(rle, "run_hybrid_search", _fake_hybrid)

        report, tokens = bench._run_hybrid_arm(seeded_db, qs.queries, voyage_api_key="fake-key")

        # Backfill ran (one tick).
        assert len(backfill_calls) == 1
        # Token accounting: 1000 (backfill) + 50 * 31 queries (rerank).
        assert tokens == 1000 + 50 * len(qs.queries)
        # The hybrid arm lifts paraphrastic recall to 1.0 (mock returns
        # ground truth at rank 1 for every query).
        assert isinstance(report, RecallReport)
        assert report.by_stratum["paraphrastic"].mean_recall_at_k[5] == pytest.approx(1.0)

    def test_run_benchmark_live_path_records_voyage_tokens(
        self, seeded_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """run_benchmark with a key (and mocked Voyage) records token spend."""
        import run_live_eval as rle
        from lossless_hermes.embeddings.backfill import BackfillResult
        from lossless_hermes.embeddings.hybrid_search import HybridSearchResult

        qs = _query_set(seeded_db)
        monkeypatch.setattr(bench, "VoyageClient", lambda **_kw: _FakeVoyage())

        async def _fake_tick(db: object, **_kw: object) -> BackfillResult:
            return BackfillResult(
                embedded_count=54,
                per_tick_limit_reached=False,
                voyage_tokens_consumed=2000,
            )

        monkeypatch.setattr(bench, "tick_embedding_backfill", _fake_tick)

        async def _fake_hybrid(conn: object, **_kw: object) -> HybridSearchResult:
            return HybridSearchResult(hits=[], candidate_count=0, voyage_tokens_consumed=10)

        monkeypatch.setattr(rle, "run_hybrid_search", _fake_hybrid)

        result = bench.run_benchmark(
            seeded_db,
            qs.queries,
            qs,
            voyage_api_key="fake-key",
            corpus_leaf_count=54,
        )
        assert result.hybrid_ran is True
        # Voyage tokens reached the result (backfill 2000 + 10/query rerank).
        assert result.voyage_tokens == 2000 + 10 * len(qs.queries)
        # The cost breakdown in the report reflects a non-zero spend.
        md = bench.build_report_markdown(result)
        assert "Voyage (embed + rerank)" in md
