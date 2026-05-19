"""Tests for ``scripts/run_live_eval.py`` — the live-eval orchestrator.

NO LIVE CALLS. Voyage + Anthropic are never contacted: the retrieval
adapters are canned dict-backed mocks, the DB is in-memory, and the
auth-gate / cost-ceiling logic is driven directly.

Mirrors the five scenarios in the issue spec
(``epics/09-eval/09-07-ci-live-eval.md`` §Tests):

1. Happy path — FTS-only recall low, hybrid recall high, paraphrastic
   stratum reports an improvement, the run exits 0.
2. Regression path — a second hybrid run with degraded hits regresses
   the paraphrastic stratum; the workflow exit code is non-zero.
3. Cost-ceiling breach — measured spend over ``EVAL_COST_CEILING_USD``
   aborts ``_finalize`` with a non-zero exit before the run is treated
   as a clean baseline.
4. First-run path — no prior run in the baseline DB → drift renders
   "(baseline established)" and the exit code is 0.
5. Auth-skip path — ``VOYAGE_API_KEY=""`` → ``main()`` exits 78
   (``EX_CONFIG``), which the workflow maps to a clean skip.

Plus unit coverage for :class:`CostMeter`, the per-stratum drift
fallback, and the markdown renderers.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

import run_live_eval as rle
from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.eval.query_set import (
    QueryRecord,
    QuerySet,
    QuerySetIdentity,
    get_query_set,
    register_query_set,
)
from lossless_hermes.eval.run import DriftDetail, DriftSummary

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------

_IDENTITY = QuerySetIdentity(name="eva-baseline", version=2)


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    """In-memory migrated DB — same setup as tests/eval/test_run.py."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(conn, fts5_available=False, seed_default_prompts=False)
    try:
        yield conn
    finally:
        conn.close()


def _mini_query_set() -> tuple[QueryRecord, ...]:
    """A 4-query stratified set: 2 fts-easy, 2 paraphrastic.

    Small enough to reason about recall by hand; carries both an
    informational stratum (fts-easy) and the load-bearing one
    (paraphrastic) so the pass/fail gate can be exercised.
    """
    return (
        QueryRecord(
            query_id="fe-1",
            query_text="exact term match one",
            stratum="fts-easy",
            expected_summary_ids=("sum-fe-1",),
        ),
        QueryRecord(
            query_id="fe-2",
            query_text="exact term match two",
            stratum="fts-easy",
            expected_summary_ids=("sum-fe-2",),
        ),
        QueryRecord(
            query_id="p-1",
            query_text="how did we decide the thing",
            stratum="paraphrastic",
            expected_summary_ids=("sum-p-1",),
        ),
        QueryRecord(
            query_id="p-2",
            query_text="what was the rationale",
            stratum="paraphrastic",
            expected_summary_ids=("sum-p-2",),
        ),
    )


class _CannedAdapter:
    """Retrieval adapter returning a per-query canned hit list.

    Ports the ``_DictAdapter`` shape from tests/eval/test_run.py — the
    hit list at index 0 is the top-ranked result, so placing the
    expected ID first yields reciprocal_rank 1.0; omitting it yields 0.0.
    """

    def __init__(self, canned: dict[str, list[str]]) -> None:
        self._canned = canned

    async def search(self, query: QueryRecord) -> list[str]:
        return list(self._canned.get(query.query_id, []))


def _registered(db: sqlite3.Connection) -> QuerySet:
    """Register the mini query set and return the QuerySet."""
    register_query_set(db, _IDENTITY, _mini_query_set())
    qs = get_query_set(db, _IDENTITY)
    assert qs is not None
    return qs


# Hit lists where paraphrastic queries MISS (no expected ID anywhere) —
# models the FTS-only baseline that the spike measured at ~5% recall.
_FTS_ONLY_HITS: dict[str, list[str]] = {
    "fe-1": ["sum-fe-1", "noise-a"],
    "fe-2": ["sum-fe-2", "noise-b"],
    "p-1": ["noise-c", "noise-d"],  # miss — RR 0.0
    "p-2": ["noise-e", "noise-f"],  # miss — RR 0.0
}

# Hit lists where paraphrastic queries HIT at rank 1 — models the
# hybrid (semantic + rerank) arm that lifts paraphrastic recall.
_HYBRID_HITS: dict[str, list[str]] = {
    "fe-1": ["sum-fe-1", "noise-a"],
    "fe-2": ["sum-fe-2", "noise-b"],
    "p-1": ["sum-p-1", "noise-c"],  # hit at rank 1 — RR 1.0
    "p-2": ["sum-p-2", "noise-d"],  # hit at rank 1 — RR 1.0
}

# Degraded hybrid hits — paraphrastic queries miss again. Used to model a
# regression on a *second* hybrid run vs the good first run.
_HYBRID_HITS_DEGRADED: dict[str, list[str]] = {
    "fe-1": ["sum-fe-1", "noise-a"],
    "fe-2": ["sum-fe-2", "noise-b"],
    "p-1": ["noise-c", "noise-d"],  # regressed — RR 1.0 -> 0.0
    "p-2": ["noise-e", "noise-f"],  # regressed — RR 1.0 -> 0.0
}


# ---------------------------------------------------------------------------
# 1. Happy path — hybrid lifts paraphrastic recall over FTS-only
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_hybrid_recall_beats_fts_on_paraphrastic(self, db: sqlite3.Connection) -> None:
        """FTS-only paraphrastic recall is 0; hybrid is 1.0 — the lift the
        +52.5pp benchmark is built on, in miniature."""
        query_set = _registered(db)
        queries = query_set.queries

        fts = rle.run_mode(
            db,
            mode="fts_only",
            queries=queries,
            adapter=_CannedAdapter(_FTS_ONLY_HITS),
            query_set=query_set,
        )
        hybrid = rle.run_mode(
            db,
            mode="hybrid",
            queries=queries,
            adapter=_CannedAdapter(_HYBRID_HITS),
            query_set=query_set,
        )

        # FTS-only: paraphrastic queries all miss → recall@5 == 0.
        fts_para = fts.recall_report.by_stratum["paraphrastic"]
        assert fts_para.mean_recall_at_k[5] == pytest.approx(0.0)
        # Hybrid: paraphrastic queries hit at rank 1 → recall@5 == 1.0.
        hybrid_para = hybrid.recall_report.by_stratum["paraphrastic"]
        assert hybrid_para.mean_recall_at_k[5] == pytest.approx(1.0)

    def test_happy_path_finalize_exits_ok(self, db: sqlite3.Connection) -> None:
        """First fts + hybrid run → both baselines, _finalize exits 0."""
        query_set = _registered(db)
        queries = query_set.queries
        fts = rle.run_mode(
            db,
            mode="fts_only",
            queries=queries,
            adapter=_CannedAdapter(_FTS_ONLY_HITS),
            query_set=query_set,
        )
        hybrid = rle.run_mode(
            db,
            mode="hybrid",
            queries=queries,
            adapter=_CannedAdapter(_HYBRID_HITS),
            query_set=query_set,
        )
        cost = rle.CostMeter(voyage_tokens=5_000)  # ~$0.0009 — well under ceiling

        exit_code = rle._finalize(
            fts, hybrid, cost, cost_ceiling_usd=0.50, summary_md_path=None, report_md_path=None
        )
        assert exit_code == rle.EX_OK

    def test_paraphrastic_improvement_shows_in_drift(self, db: sqlite3.Connection) -> None:
        """A hybrid run that improves on a prior hybrid run reports the
        paraphrastic stratum improvement (not a regression)."""
        query_set = _registered(db)
        queries = query_set.queries

        # Prior hybrid run with paraphrastic MISSES.
        rle.run_mode(
            db,
            mode="hybrid",
            queries=queries,
            adapter=_CannedAdapter(_FTS_ONLY_HITS),
            query_set=query_set,
        )
        # Current hybrid run with paraphrastic HITS → improvement.
        hybrid2 = rle.run_mode(
            db,
            mode="hybrid",
            queries=queries,
            adapter=_CannedAdapter(_HYBRID_HITS),
            query_set=query_set,
        )

        assert not hybrid2.is_baseline
        para = hybrid2.per_stratum.by_stratum["paraphrastic"]
        assert para.cumulative_delta > 0  # improved
        assert para.regressed == 0
        assert not hybrid2.per_stratum.any_stratum_regressed


# ---------------------------------------------------------------------------
# 2. Regression path — paraphrastic recall drops on a second hybrid run
# ---------------------------------------------------------------------------


class TestRegressionPath:
    def test_paraphrastic_regression_fails_workflow(self, db: sqlite3.Connection) -> None:
        """Second hybrid run with degraded paraphrastic hits → _finalize
        returns EX_SOFTWARE (the workflow fails)."""
        query_set = _registered(db)
        queries = query_set.queries

        # Prior fts + hybrid runs — hybrid paraphrastic HITS.
        fts = rle.run_mode(
            db,
            mode="fts_only",
            queries=queries,
            adapter=_CannedAdapter(_FTS_ONLY_HITS),
            query_set=query_set,
        )
        rle.run_mode(
            db,
            mode="hybrid",
            queries=queries,
            adapter=_CannedAdapter(_HYBRID_HITS),
            query_set=query_set,
        )
        # Current hybrid run — paraphrastic MISSES → regression.
        hybrid_regressed = rle.run_mode(
            db,
            mode="hybrid",
            queries=queries,
            adapter=_CannedAdapter(_HYBRID_HITS_DEGRADED),
            query_set=query_set,
        )

        assert not hybrid_regressed.is_baseline
        para = hybrid_regressed.per_stratum.by_stratum["paraphrastic"]
        assert para.cumulative_delta < 0  # regressed
        assert hybrid_regressed.per_stratum.any_stratum_regressed

        exit_code = rle._finalize(
            fts,
            hybrid_regressed,
            rle.CostMeter(voyage_tokens=5_000),
            cost_ceiling_usd=0.50,
            summary_md_path=None,
            report_md_path=None,
        )
        assert exit_code == rle.EX_SOFTWARE

    def test_fts_easy_regression_alone_does_not_fail(self, db: sqlite3.Connection) -> None:
        """A regression confined to fts-easy is informational — the
        pass/fail gate only trips on paraphrastic."""
        query_set = _registered(db)
        queries = query_set.queries

        # Prior hybrid run: everything hits.
        rle.run_mode(
            db,
            mode="hybrid",
            queries=queries,
            adapter=_CannedAdapter(_HYBRID_HITS),
            query_set=query_set,
        )
        # Current hybrid run: fts-easy regresses, paraphrastic still hits.
        fts_easy_regressed = {
            "fe-1": ["noise-a", "noise-b"],  # fts-easy miss
            "fe-2": ["noise-c", "noise-d"],  # fts-easy miss
            "p-1": ["sum-p-1", "noise-e"],  # paraphrastic still hits
            "p-2": ["sum-p-2", "noise-f"],
        }
        hybrid2 = rle.run_mode(
            db,
            mode="hybrid",
            queries=queries,
            adapter=_CannedAdapter(fts_easy_regressed),
            query_set=query_set,
        )

        para = hybrid2.per_stratum.by_stratum["paraphrastic"]
        assert para.cumulative_delta >= 0  # paraphrastic did NOT regress

        # The fts-only side of _finalize doesn't matter here; reuse hybrid2's
        # prior as a stand-in fts result (its mode tag is irrelevant to the
        # paraphrastic gate, which reads only the hybrid result).
        fts_stub = rle.run_mode(
            db,
            mode="fts_only",
            queries=queries,
            adapter=_CannedAdapter(_FTS_ONLY_HITS),
            query_set=query_set,
        )
        exit_code = rle._finalize(
            fts_stub,
            hybrid2,
            rle.CostMeter(voyage_tokens=5_000),
            cost_ceiling_usd=0.50,
            summary_md_path=None,
            report_md_path=None,
        )
        assert exit_code == rle.EX_OK


# ---------------------------------------------------------------------------
# 3. Cost-ceiling breach
# ---------------------------------------------------------------------------


class TestCostCeiling:
    def test_spend_over_ceiling_aborts(self, db: sqlite3.Connection) -> None:
        """Measured spend over EVAL_COST_CEILING_USD → _finalize returns
        EX_SOFTWARE before the run is treated as a clean baseline."""
        query_set = _registered(db)
        queries = query_set.queries
        fts = rle.run_mode(
            db,
            mode="fts_only",
            queries=queries,
            adapter=_CannedAdapter(_FTS_ONLY_HITS),
            query_set=query_set,
        )
        hybrid = rle.run_mode(
            db,
            mode="hybrid",
            queries=queries,
            adapter=_CannedAdapter(_HYBRID_HITS),
            query_set=query_set,
        )

        # 10M Voyage tokens @ $0.18/Mtok = $1.80 — well over the $0.50 ceiling.
        over_budget = rle.CostMeter(voyage_tokens=10_000_000)
        assert over_budget.total_usd() > 0.50

        exit_code = rle._finalize(
            fts,
            hybrid,
            over_budget,
            cost_ceiling_usd=0.50,
            summary_md_path=None,
            report_md_path=None,
        )
        assert exit_code == rle.EX_SOFTWARE

    def test_cost_ceiling_checked_before_paraphrastic_gate(self, db: sqlite3.Connection) -> None:
        """Even on an otherwise-clean run, an over-budget cost aborts. The
        cost check runs first so partial over-budget data is flagged."""
        query_set = _registered(db)
        queries = query_set.queries
        # Both runs are first-of-mode baselines → paraphrastic gate inactive.
        fts = rle.run_mode(
            db,
            mode="fts_only",
            queries=queries,
            adapter=_CannedAdapter(_FTS_ONLY_HITS),
            query_set=query_set,
        )
        hybrid = rle.run_mode(
            db,
            mode="hybrid",
            queries=queries,
            adapter=_CannedAdapter(_HYBRID_HITS),
            query_set=query_set,
        )
        assert hybrid.is_baseline  # gate would otherwise pass

        exit_code = rle._finalize(
            fts,
            hybrid,
            rle.CostMeter(voyage_tokens=10_000_000),
            cost_ceiling_usd=0.50,
            summary_md_path=None,
            report_md_path=None,
        )
        assert exit_code == rle.EX_SOFTWARE


# ---------------------------------------------------------------------------
# 4. First-run path — no prior run → "(baseline established)"
# ---------------------------------------------------------------------------


class TestFirstRun:
    def test_first_run_is_baseline(self, db: sqlite3.Connection) -> None:
        """A first run of each mode has no prior → is_baseline True."""
        query_set = _registered(db)
        queries = query_set.queries
        fts = rle.run_mode(
            db,
            mode="fts_only",
            queries=queries,
            adapter=_CannedAdapter(_FTS_ONLY_HITS),
            query_set=query_set,
        )
        hybrid = rle.run_mode(
            db,
            mode="hybrid",
            queries=queries,
            adapter=_CannedAdapter(_HYBRID_HITS),
            query_set=query_set,
        )
        assert fts.is_baseline
        assert hybrid.is_baseline
        assert fts.drift.prior_run_id is None
        assert hybrid.drift.prior_run_id is None

    def test_first_run_summary_says_baseline_established(self, db: sqlite3.Connection) -> None:
        """The rendered summary surfaces '(baseline established)' on a
        first run rather than a bogus drift number."""
        query_set = _registered(db)
        queries = query_set.queries
        fts = rle.run_mode(
            db,
            mode="fts_only",
            queries=queries,
            adapter=_CannedAdapter(_FTS_ONLY_HITS),
            query_set=query_set,
        )
        hybrid = rle.run_mode(
            db,
            mode="hybrid",
            queries=queries,
            adapter=_CannedAdapter(_HYBRID_HITS),
            query_set=query_set,
        )
        summary = rle.build_summary_markdown(fts, hybrid, rle.CostMeter())
        assert "baseline established" in summary

    def test_first_run_finalize_exits_ok(self, db: sqlite3.Connection) -> None:
        """A first run with in-budget cost exits 0 — informational only."""
        query_set = _registered(db)
        queries = query_set.queries
        fts = rle.run_mode(
            db,
            mode="fts_only",
            queries=queries,
            adapter=_CannedAdapter(_FTS_ONLY_HITS),
            query_set=query_set,
        )
        hybrid = rle.run_mode(
            db,
            mode="hybrid",
            queries=queries,
            adapter=_CannedAdapter(_HYBRID_HITS),
            query_set=query_set,
        )
        exit_code = rle._finalize(
            fts,
            hybrid,
            rle.CostMeter(),
            cost_ceiling_usd=0.50,
            summary_md_path=None,
            report_md_path=None,
        )
        assert exit_code == rle.EX_OK


# ---------------------------------------------------------------------------
# 5. Auth-skip path — missing API key → main() exits EX_CONFIG (78)
# ---------------------------------------------------------------------------


class TestAuthSkip:
    def test_main_exits_ex_config_when_voyage_key_blank(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """VOYAGE_API_KEY="" → main() returns 78 (EX_CONFIG). The workflow
        maps that to a clean skip, not a failure."""
        monkeypatch.setenv("VOYAGE_API_KEY", "")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "present")
        assert rle.main(["--db", "unused.db"]) == rle.EX_CONFIG

    def test_main_exits_ex_config_when_anthropic_key_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ANTHROPIC_API_KEY unset → main() returns 78 (EX_CONFIG)."""
        monkeypatch.setenv("VOYAGE_API_KEY", "present")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert rle.main(["--db", "unused.db"]) == rle.EX_CONFIG

    def test_main_exits_ex_config_when_voyage_key_whitespace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A whitespace-only key counts as absent — exits 78."""
        monkeypatch.setenv("VOYAGE_API_KEY", "   ")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "present")
        assert rle.main(["--db", "unused.db"]) == rle.EX_CONFIG

    def test_missing_api_keys_reports_both(self) -> None:
        """_missing_api_keys lists every absent key, not just the first."""
        assert rle._missing_api_keys({}) == ["VOYAGE_API_KEY", "ANTHROPIC_API_KEY"]
        assert rle._missing_api_keys({"VOYAGE_API_KEY": "x"}) == ["ANTHROPIC_API_KEY"]
        assert rle._missing_api_keys({"VOYAGE_API_KEY": "x", "ANTHROPIC_API_KEY": "y"}) == []


# ---------------------------------------------------------------------------
# CostMeter unit coverage
# ---------------------------------------------------------------------------


class TestCostMeter:
    def test_voyage_usd_conversion(self) -> None:
        """1M Voyage tokens → VOYAGE_USD_PER_MTOK dollars."""
        meter = rle.CostMeter(voyage_tokens=1_000_000)
        assert meter.voyage_usd() == pytest.approx(rle.VOYAGE_USD_PER_MTOK)

    def test_anthropic_usd_splits_input_output(self) -> None:
        """Anthropic input + output tokens are billed at separate rates."""
        meter = rle.CostMeter(anthropic_input_tokens=1_000_000, anthropic_output_tokens=1_000_000)
        expected = rle.ANTHROPIC_USD_PER_MTOK_INPUT + rle.ANTHROPIC_USD_PER_MTOK_OUTPUT
        assert meter.anthropic_usd() == pytest.approx(expected)

    def test_total_usd_sums_both_providers(self) -> None:
        meter = rle.CostMeter(voyage_tokens=1_000_000, anthropic_input_tokens=1_000_000)
        assert meter.total_usd() == pytest.approx(
            rle.VOYAGE_USD_PER_MTOK + rle.ANTHROPIC_USD_PER_MTOK_INPUT
        )

    def test_add_voyage_accumulates(self) -> None:
        meter = rle.CostMeter()
        meter.add_voyage(100)
        meter.add_voyage(250)
        assert meter.voyage_tokens == 350

    def test_add_voyage_ignores_nonpositive(self) -> None:
        """A zero/negative token count (e.g. an empty-batch response) is a
        no-op — never decrements the meter."""
        meter = rle.CostMeter(voyage_tokens=500)
        meter.add_voyage(0)
        meter.add_voyage(-10)
        assert meter.voyage_tokens == 500

    def test_single_eval_is_cheap(self) -> None:
        """Sanity: a realistic 31-query eval (~50k Voyage tokens) costs
        well under a cent — the $0.50 ceiling is pure defense-in-depth."""
        meter = rle.CostMeter(voyage_tokens=50_000)
        assert meter.total_usd() < 0.05


# ---------------------------------------------------------------------------
# Cost-ceiling env parsing
# ---------------------------------------------------------------------------


class TestCostCeilingParsing:
    def test_default_when_unset(self) -> None:
        assert rle._read_cost_ceiling({}) == pytest.approx(0.50)

    def test_parses_valid_value(self) -> None:
        assert rle._read_cost_ceiling({"EVAL_COST_CEILING_USD": "1.25"}) == pytest.approx(1.25)

    def test_malformed_value_falls_back_to_default(self) -> None:
        """A non-numeric value must NOT silently disable the guardrail."""
        assert rle._read_cost_ceiling({"EVAL_COST_CEILING_USD": "lots"}) == pytest.approx(0.50)

    def test_nonpositive_value_falls_back_to_default(self) -> None:
        """Zero / negative ceiling would disable the check — reject it."""
        assert rle._read_cost_ceiling({"EVAL_COST_CEILING_USD": "0"}) == pytest.approx(0.50)
        assert rle._read_cost_ceiling({"EVAL_COST_CEILING_USD": "-1"}) == pytest.approx(0.50)


# ---------------------------------------------------------------------------
# drift_threshold / is_drifted — the 09-06 pure functions (local fallback)
# ---------------------------------------------------------------------------


class TestDriftThreshold:
    def test_threshold_none_is_zero(self) -> None:
        assert rle.drift_threshold(None) == 0.0

    def test_threshold_zero_is_zero(self) -> None:
        assert rle.drift_threshold(0.0) == 0.0

    def test_threshold_negative_is_zero(self) -> None:
        """A negative SD is nonsensical — treated as no floor."""
        assert rle.drift_threshold(-0.05) == 0.0

    def test_threshold_doubles_sd(self) -> None:
        assert rle.drift_threshold(0.05) == pytest.approx(0.10)

    def test_is_drifted_threshold_zero_any_nonzero(self) -> None:
        assert rle.is_drifted(0.01, 0.0) is True
        assert rle.is_drifted(-0.01, 0.0) is True
        assert rle.is_drifted(0.0, 0.0) is False

    def test_is_drifted_threshold_positive(self) -> None:
        assert rle.is_drifted(0.15, 0.10) is True
        assert rle.is_drifted(-0.15, 0.10) is True
        assert rle.is_drifted(0.05, 0.10) is False
        assert rle.is_drifted(0.0, 0.10) is False


# ---------------------------------------------------------------------------
# per_stratum_drift local fallback — joins drift details against the set
# ---------------------------------------------------------------------------


def _drift_summary(details: list[DriftDetail], cumulative: float) -> DriftSummary:
    """Build a DriftSummary with a non-None prior so it isn't a baseline."""
    return DriftSummary(
        drifted=sum(1 for d in details if d.delta not in (None, 0.0)),
        improved=sum(1 for d in details if d.delta is not None and d.delta > 0),
        regressed=sum(1 for d in details if d.delta is not None and d.delta < 0),
        details=tuple(details),
        prior_run_id="evalrun_prior",
        cumulative_delta=cumulative,
    )


class TestPerStratumDriftFallback:
    def test_groups_by_stratum(self) -> None:
        """Details are joined to strata via the query set."""
        query_set = QuerySet(
            identity=_IDENTITY,
            queries=(
                QueryRecord("fe-1", "q", "fts-easy", expected_summary_ids=("a",)),
                QueryRecord("p-1", "q", "paraphrastic", expected_summary_ids=("b",)),
            ),
        )
        summary = _drift_summary(
            [
                DriftDetail("fe-1", prior_score=0.5, current_score=0.6, delta=0.1),
                DriftDetail("p-1", prior_score=1.0, current_score=0.4, delta=-0.6),
            ],
            cumulative=-0.5,
        )
        psd = rle._per_stratum_drift_local(summary, query_set)
        assert set(psd.by_stratum) == {"fts-easy", "paraphrastic"}
        assert psd.by_stratum["fts-easy"].cumulative_delta == pytest.approx(0.1)
        assert psd.by_stratum["paraphrastic"].cumulative_delta == pytest.approx(-0.6)

    def test_paraphrastic_regression_flag(self) -> None:
        """any_stratum_regressed True when paraphrastic falls past threshold."""
        query_set = QuerySet(
            identity=_IDENTITY,
            queries=(QueryRecord("p-1", "q", "paraphrastic", expected_summary_ids=("b",)),),
        )
        summary = _drift_summary(
            [DriftDetail("p-1", prior_score=1.0, current_score=0.4, delta=-0.6)],
            cumulative=-0.6,
        )
        psd = rle._per_stratum_drift_local(summary, query_set)
        assert psd.any_stratum_regressed is True

    def test_all_positive_deltas_no_regression(self) -> None:
        query_set = QuerySet(
            identity=_IDENTITY,
            queries=(
                QueryRecord("fe-1", "q", "fts-easy", expected_summary_ids=("a",)),
                QueryRecord("p-1", "q", "paraphrastic", expected_summary_ids=("b",)),
            ),
        )
        summary = _drift_summary(
            [
                DriftDetail("fe-1", prior_score=0.5, current_score=0.6, delta=0.1),
                DriftDetail("p-1", prior_score=0.5, current_score=1.0, delta=0.5),
            ],
            cumulative=0.6,
        )
        psd = rle._per_stratum_drift_local(summary, query_set)
        assert psd.any_stratum_regressed is False

    def test_unknown_query_bucketed_not_dropped(self) -> None:
        """A drift detail whose query_id is absent from the set lands in an
        'unknown' bucket — defensive, never crashes."""
        query_set = QuerySet(identity=_IDENTITY, queries=())
        summary = _drift_summary(
            [DriftDetail("ghost", prior_score=0.5, current_score=0.4, delta=-0.1)],
            cumulative=-0.1,
        )
        psd = rle._per_stratum_drift_local(summary, query_set)
        assert "unknown" in psd.by_stratum
        assert psd.by_stratum["unknown"].n_scored == 1

    def test_none_deltas_not_counted(self) -> None:
        """A query present in only one run (delta None) doesn't count
        toward n_scored / cumulative."""
        query_set = QuerySet(
            identity=_IDENTITY,
            queries=(QueryRecord("fe-1", "q", "fts-easy", expected_summary_ids=("a",)),),
        )
        summary = _drift_summary(
            [DriftDetail("fe-1", prior_score=None, current_score=0.4, delta=None)],
            cumulative=0.0,
        )
        psd = rle._per_stratum_drift_local(summary, query_set)
        assert psd.by_stratum["fts-easy"].n_scored == 0
        assert psd.by_stratum["fts-easy"].cumulative_delta == pytest.approx(0.0)

    def test_improved_plus_regressed_within_drifted(self) -> None:
        """improved + regressed <= drifted per stratum."""
        query_set = QuerySet(
            identity=_IDENTITY,
            queries=(
                QueryRecord("p-1", "q", "paraphrastic", expected_summary_ids=("a",)),
                QueryRecord("p-2", "q", "paraphrastic", expected_summary_ids=("b",)),
                QueryRecord("p-3", "q", "paraphrastic", expected_summary_ids=("c",)),
            ),
        )
        summary = _drift_summary(
            [
                DriftDetail("p-1", prior_score=0.5, current_score=0.7, delta=0.2),
                DriftDetail("p-2", prior_score=0.7, current_score=0.5, delta=-0.2),
                DriftDetail("p-3", prior_score=0.5, current_score=0.5, delta=0.0),
            ],
            cumulative=0.0,
        )
        psd = rle._per_stratum_drift_local(summary, query_set)
        agg = psd.by_stratum["paraphrastic"]
        assert agg.improved + agg.regressed <= agg.drifted


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


class TestMarkdownRendering:
    def test_report_carries_sticky_marker(self, db: sqlite3.Connection) -> None:
        """The PR-comment body MUST carry the magic marker so the workflow
        find-comment step can locate and replace it."""
        query_set = _registered(db)
        queries = query_set.queries
        fts = rle.run_mode(
            db,
            mode="fts_only",
            queries=queries,
            adapter=_CannedAdapter(_FTS_ONLY_HITS),
            query_set=query_set,
        )
        hybrid = rle.run_mode(
            db,
            mode="hybrid",
            queries=queries,
            adapter=_CannedAdapter(_HYBRID_HITS),
            query_set=query_set,
        )
        report = rle.build_report_markdown(fts, hybrid, rle.CostMeter())
        assert report.startswith(rle.COMMENT_MARKER)

    def test_summary_has_per_stratum_table_and_cost(self, db: sqlite3.Connection) -> None:
        """The summary table includes per-stratum recall@5, MRR, drift, and
        the total run cost in USD (AC: workflow summary table contents)."""
        query_set = _registered(db)
        queries = query_set.queries
        fts = rle.run_mode(
            db,
            mode="fts_only",
            queries=queries,
            adapter=_CannedAdapter(_FTS_ONLY_HITS),
            query_set=query_set,
        )
        hybrid = rle.run_mode(
            db,
            mode="hybrid",
            queries=queries,
            adapter=_CannedAdapter(_HYBRID_HITS),
            query_set=query_set,
        )
        summary = rle.build_summary_markdown(fts, hybrid, rle.CostMeter(voyage_tokens=50_000))
        # Per-stratum table header.
        assert "FTS R@5" in summary and "Hybrid R@5" in summary
        assert "FTS MRR" in summary and "Hybrid MRR" in summary
        assert "drift" in summary
        # Both strata present as rows.
        assert "fts-easy" in summary and "paraphrastic" in summary
        # Cost line.
        assert "Run cost:" in summary and "$" in summary

    def test_summary_writes_to_file(self, db: sqlite3.Connection, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """_finalize appends the summary to --summary-md and writes the
        report to --report-md."""
        query_set = _registered(db)
        queries = query_set.queries
        fts = rle.run_mode(
            db,
            mode="fts_only",
            queries=queries,
            adapter=_CannedAdapter(_FTS_ONLY_HITS),
            query_set=query_set,
        )
        hybrid = rle.run_mode(
            db,
            mode="hybrid",
            queries=queries,
            adapter=_CannedAdapter(_HYBRID_HITS),
            query_set=query_set,
        )
        summary_path = tmp_path / "summary.md"
        report_path = tmp_path / "report.md"
        # Pre-seed the summary file — _finalize APPENDS (GH step summary semantics).
        summary_path.write_text("preexisting\n", encoding="utf-8")

        rle._finalize(
            fts,
            hybrid,
            rle.CostMeter(),
            cost_ceiling_usd=0.50,
            summary_md_path=str(summary_path),
            report_md_path=str(report_path),
        )
        summary_text = summary_path.read_text(encoding="utf-8")
        report_text = report_path.read_text(encoding="utf-8")
        assert summary_text.startswith("preexisting\n")  # appended, not clobbered
        assert "Live eval" in summary_text
        assert report_text.startswith(rle.COMMENT_MARKER)

    def test_paraphrastic_pass_verdict_in_summary(self, db: sqlite3.Connection) -> None:
        """A non-regressing second hybrid run renders a PASS verdict."""
        query_set = _registered(db)
        queries = query_set.queries
        rle.run_mode(
            db,
            mode="hybrid",
            queries=queries,
            adapter=_CannedAdapter(_HYBRID_HITS),
            query_set=query_set,
        )
        hybrid2 = rle.run_mode(
            db,
            mode="hybrid",
            queries=queries,
            adapter=_CannedAdapter(_HYBRID_HITS),
            query_set=query_set,
        )
        fts = rle.run_mode(
            db,
            mode="fts_only",
            queries=queries,
            adapter=_CannedAdapter(_FTS_ONLY_HITS),
            query_set=query_set,
        )
        summary = rle.build_summary_markdown(fts, hybrid2, rle.CostMeter())
        assert "PASS" in summary

    def test_paraphrastic_fail_verdict_in_summary(self, db: sqlite3.Connection) -> None:
        """A regressing second hybrid run renders a FAIL verdict."""
        query_set = _registered(db)
        queries = query_set.queries
        rle.run_mode(
            db,
            mode="hybrid",
            queries=queries,
            adapter=_CannedAdapter(_HYBRID_HITS),
            query_set=query_set,
        )
        hybrid_regressed = rle.run_mode(
            db,
            mode="hybrid",
            queries=queries,
            adapter=_CannedAdapter(_HYBRID_HITS_DEGRADED),
            query_set=query_set,
        )
        fts = rle.run_mode(
            db,
            mode="fts_only",
            queries=queries,
            adapter=_CannedAdapter(_FTS_ONLY_HITS),
            query_set=query_set,
        )
        summary = rle.build_summary_markdown(fts, hybrid_regressed, rle.CostMeter())
        assert "FAIL" in summary
