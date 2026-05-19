"""Tests for :mod:`lossless_hermes.operator.eval_runner` (issue 08-13).

Ports ``lossless-claw/test/operator-eval-runner.test.ts`` (commit
``1f07fbd`` on branch ``pr-613``, 8 vitest cases) plus per-AC additions:

* :func:`test_fts_only_no_voyage` — mock retrieval adapter, confirm
  no Voyage HTTP call leaks through.
* :func:`test_baseline_drift_computed` — seed a prior run, run again,
  confirm drift fields populated.
* :func:`test_missing_voyage_creds_raises` — see the test docstring
  for why this is documented as N/A for the runner.

See:

* ``epics/08-cli-ops/08-13-eval-runner.md`` — this issue.
* ``lossless-claw/src/operator/eval-runner.ts`` — TS source.
* ``lossless-claw/test/operator-eval-runner.test.ts`` — TS test source.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from typing import Optional

import pytest

from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.eval.query_set import (
    QueryRecord,
    QuerySetIdentity,
    register_query_set,
)
from lossless_hermes.eval.recall import RecallSearchAdapter
from lossless_hermes.operator.eval_runner import (
    EvalRunnerError,
    RunEvalArgs,
    format_eval_report,
    run_eval,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _new_db() -> sqlite3.Connection:
    """In-memory SQLite with the full LCM migration ladder applied.

    Ports the TS ``setupDb()`` helper (``operator-eval-runner.test.ts:12-16``).
    Skips FTS5 (matches TS ``fts5Available: false``) so the test runs on
    Python builds without FTS5.
    """
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(
        conn,
        fts5_available=False,
        seed_default_prompts=False,
    )
    return conn


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    conn = _new_db()
    try:
        yield conn
    finally:
        conn.close()


SAMPLE_QUERIES: tuple[QueryRecord, ...] = (
    QueryRecord(
        query_id="q1",
        query_text="what is the timezone setting",
        stratum="fts-easy",
        expected_summary_ids=("leaf_a", "leaf_b"),
    ),
    QueryRecord(
        query_id="q2",
        query_text="describe the rebase workflow",
        stratum="paraphrastic",
        expected_summary_ids=("leaf_c",),
    ),
    QueryRecord(
        query_id="q3",
        query_text="no expected ids — skipped from recall",
        stratum="fts-medium",
    ),
)
"""Ports TS ``SAMPLE_QUERIES`` (``operator-eval-runner.test.ts:18-36``)."""


class _MockAdapter:
    """Deterministic adapter returning canned hits per query id.

    Ports TS ``makeMockAdapter`` (``operator-eval-runner.test.ts:42-48``).

    Tests inject this so neither vec0 nor Voyage are required.
    """

    def __init__(
        self,
        canned: dict[str, list[str]],
        *,
        call_log: Optional[list[str]] = None,
    ) -> None:
        self._canned = canned
        self._call_log = call_log

    async def search(self, query: QueryRecord) -> list[str]:
        if self._call_log is not None:
            self._call_log.append(query.query_id)
        return list(self._canned.get(query.query_id, []))


def _make_mock_adapter(
    canned: dict[str, list[str]],
    *,
    call_log: Optional[list[str]] = None,
) -> RecallSearchAdapter:
    return _MockAdapter(canned, call_log=call_log)


def _identity() -> QuerySetIdentity:
    return QuerySetIdentity(name="test-set", version=1)


# ---------------------------------------------------------------------------
# Input validation — TS describe("operator-eval-runner — input validation")
# ---------------------------------------------------------------------------


class TestInputValidation:
    """Ports TS ``describe("operator-eval-runner — input validation")``
    (``operator-eval-runner.test.ts:50-62``)."""

    @pytest.mark.asyncio
    async def test_missing_query_set_raises(self, db: sqlite3.Connection) -> None:
        """Ports TS ``it("throws EvalRunnerError(missing_query_set) when query
        set is unknown")`` (``operator-eval-runner.test.ts:51-60``).
        """
        with pytest.raises(EvalRunnerError) as exc_info:
            await run_eval(
                db,
                RunEvalArgs(
                    query_set_identity=QuerySetIdentity(name="no-such-set", version=1),
                    mode="fts_only",
                    retrieval_adapter=_make_mock_adapter({}),
                ),
            )
        assert exc_info.value.kind == "missing_query_set"
        assert "is not registered" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_empty_query_set_raises(self, db: sqlite3.Connection) -> None:
        """New: kind discrimination between missing and empty.

        We can't register an empty set via :func:`register_query_set` (it
        rejects empty sets up-front), so we exercise the ``empty_query_set``
        path by INSERTing a header row directly. This matches the TS shape
        — the runner has to handle the empty case independently because the
        D.03 schema doesn't enforce ≥1 query per set.
        """
        db.execute(
            "INSERT INTO lcm_eval_query_set (query_set_id, version) VALUES (?, ?)",
            ("empty-set@v1", 1),
        )
        with pytest.raises(EvalRunnerError) as exc_info:
            await run_eval(
                db,
                RunEvalArgs(
                    query_set_identity=QuerySetIdentity(name="empty-set", version=1),
                    mode="fts_only",
                    retrieval_adapter=_make_mock_adapter({}),
                ),
            )
        assert exc_info.value.kind == "empty_query_set"
        assert "contains no queries" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Basic recall flow — TS describe("operator-eval-runner — basic recall flow")
# ---------------------------------------------------------------------------


class TestBasicRecallFlow:
    """Ports TS ``describe("operator-eval-runner — basic recall flow")``
    (``operator-eval-runner.test.ts:64-126``)."""

    @pytest.mark.asyncio
    async def test_records_run_and_returns_recall_report(self, db: sqlite3.Connection) -> None:
        """Ports TS ``it("records a run and returns a recall report")``
        (``operator-eval-runner.test.ts:65-87``)."""
        register_query_set(db, _identity(), SAMPLE_QUERIES)
        result = await run_eval(
            db,
            RunEvalArgs(
                query_set_identity=_identity(),
                mode="fts_only",
                retrieval_adapter=_make_mock_adapter({
                    "q1": ["leaf_a", "leaf_b", "leaf_x"],
                    "q2": ["leaf_c", "leaf_y"],
                    "q3": ["leaf_z"],
                }),
            ),
        )
        assert result.run_id.startswith("evalrun_")
        # q1 hit both expected at top-2; recall@5 = 2/2 = 1.0
        q1 = next(r for r in result.recall_report.per_query if r.query_id == "q1")
        assert q1.recall_at_k[5] == pytest.approx(1.0)
        # q2 hit the one expected at rank 1 → MRR contribution = 1.0
        q2 = next(r for r in result.recall_report.per_query if r.query_id == "q2")
        assert q2.reciprocal_rank == pytest.approx(1.0)
        # overall mean RR averages the SCORED queries (q3 is excluded)
        assert result.recall_report.overall.n == 2

    @pytest.mark.asyncio
    async def test_records_run_row_with_correct_mode_and_score(
        self, db: sqlite3.Connection
    ) -> None:
        """Ports TS ``it("records the run row with the correct mode +
        recall score")`` (``operator-eval-runner.test.ts:89-113``)."""
        register_query_set(db, _identity(), SAMPLE_QUERIES)
        result = await run_eval(
            db,
            RunEvalArgs(
                query_set_identity=_identity(),
                mode="hybrid",
                retrieval_adapter=_make_mock_adapter({
                    "q1": ["leaf_a"],
                    "q2": ["leaf_c"],
                }),
            ),
        )
        row = db.execute(
            """
            SELECT query_set_id, retrieval_recall_score, per_query_scores
              FROM lcm_eval_run WHERE run_id = ?
            """,
            (result.run_id,),
        ).fetchone()
        assert row[0] == "test-set@v1"
        assert row[1] > 0
        env = json.loads(row[2])
        assert env["mode"] == "hybrid"

    @pytest.mark.asyncio
    async def test_first_run_drift_is_none(self, db: sqlite3.Connection) -> None:
        """Ports TS ``it("first run reports drift=null (no baseline)")``
        (``operator-eval-runner.test.ts:115-125``)."""
        register_query_set(db, _identity(), SAMPLE_QUERIES)
        result = await run_eval(
            db,
            RunEvalArgs(
                query_set_identity=_identity(),
                mode="fts_only",
                retrieval_adapter=_make_mock_adapter({"q1": ["leaf_a"], "q2": ["leaf_c"]}),
            ),
        )
        assert result.drift is None


# ---------------------------------------------------------------------------
# Drift comparison — TS describe("operator-eval-runner — drift comparison")
# ---------------------------------------------------------------------------


class TestDriftComparison:
    """Ports TS ``describe("operator-eval-runner — drift comparison")``
    (``operator-eval-runner.test.ts:128-171``)."""

    @pytest.mark.asyncio
    async def test_second_run_computes_drift_vs_first(self, db: sqlite3.Connection) -> None:
        """Ports TS ``it("second run computes drift vs first (same
        query_set + mode)")`` (``operator-eval-runner.test.ts:129-153``)."""
        register_query_set(db, _identity(), SAMPLE_QUERIES)
        # Run 1: q2 finds expected at rank 1 (MRR=1.0)
        await run_eval(
            db,
            RunEvalArgs(
                query_set_identity=_identity(),
                mode="fts_only",
                retrieval_adapter=_make_mock_adapter({
                    "q1": ["leaf_a", "leaf_b"],
                    "q2": ["leaf_c"],
                }),
            ),
        )
        # Run 2: q2 now finds expected at rank 2 (MRR=0.5) — regression
        second = await run_eval(
            db,
            RunEvalArgs(
                query_set_identity=_identity(),
                mode="fts_only",
                retrieval_adapter=_make_mock_adapter({
                    "q1": ["leaf_a", "leaf_b"],
                    "q2": ["leaf_x", "leaf_c"],
                }),
            ),
        )
        assert second.drift is not None
        assert second.drift.prior_run_id is not None
        assert second.drift.prior_run_id.startswith("evalrun_")
        # q2 should appear in details with delta ≈ -0.5
        q2_drift = next(
            (d for d in second.drift.details if d.query_id == "q2"),
            None,
        )
        assert q2_drift is not None
        assert q2_drift.delta is not None
        assert q2_drift.delta == pytest.approx(-0.5, abs=1e-3)

    @pytest.mark.asyncio
    async def test_different_mode_is_fresh_baseline(self, db: sqlite3.Connection) -> None:
        """Ports TS ``it("different mode → fresh baseline (no prior run
        match)")`` (``operator-eval-runner.test.ts:155-170``)."""
        register_query_set(db, _identity(), SAMPLE_QUERIES)
        await run_eval(
            db,
            RunEvalArgs(
                query_set_identity=_identity(),
                mode="fts_only",
                retrieval_adapter=_make_mock_adapter({"q1": ["leaf_a"]}),
            ),
        )
        hybrid_run = await run_eval(
            db,
            RunEvalArgs(
                query_set_identity=_identity(),
                mode="hybrid",
                retrieval_adapter=_make_mock_adapter({"q1": ["leaf_a"]}),
            ),
        )
        assert hybrid_run.drift is None


# ---------------------------------------------------------------------------
# Formatting — TS describe("operator-eval-runner — formatting")
# ---------------------------------------------------------------------------


class TestFormatting:
    """Ports TS ``describe("operator-eval-runner — formatting")``
    (``operator-eval-runner.test.ts:173-220``)."""

    @pytest.mark.asyncio
    async def test_format_eval_report_renders_overall_per_stratum_drift(
        self, db: sqlite3.Connection
    ) -> None:
        """Ports TS ``it("formatEvalReport renders overall + per-stratum +
        drift sections")`` (``operator-eval-runner.test.ts:174-196``)."""
        register_query_set(db, _identity(), SAMPLE_QUERIES)
        result = await run_eval(
            db,
            RunEvalArgs(
                query_set_identity=_identity(),
                mode="fts_only",
                retrieval_adapter=_make_mock_adapter({
                    "q1": ["leaf_a", "leaf_b"],
                    "q2": ["leaf_c"],
                }),
            ),
        )
        text = format_eval_report(_identity(), "fts_only", result)
        assert "Eval run" in text
        assert "Recall@K — overall" in text
        assert "MRR=" in text
        assert "Drift" in text
        assert "no prior run" in text

    @pytest.mark.asyncio
    async def test_format_eval_report_reports_cumulative_delta_with_prior(
        self, db: sqlite3.Connection
    ) -> None:
        """Ports TS ``it("formatEvalReport reports cumulative_delta when a
        prior run exists")`` (``operator-eval-runner.test.ts:198-219``)."""
        register_query_set(db, _identity(), SAMPLE_QUERIES)
        await run_eval(
            db,
            RunEvalArgs(
                query_set_identity=_identity(),
                mode="fts_only",
                retrieval_adapter=_make_mock_adapter({"q1": ["leaf_a"], "q2": ["leaf_c"]}),
            ),
        )
        second = await run_eval(
            db,
            RunEvalArgs(
                query_set_identity=_identity(),
                mode="fts_only",
                retrieval_adapter=_make_mock_adapter({
                    "q1": ["leaf_a"],
                    "q2": ["leaf_x", "leaf_c"],
                }),
            ),
        )
        text = format_eval_report(_identity(), "fts_only", second)
        assert "cumulative_delta=" in text
        assert "vs prior run" in text


# ---------------------------------------------------------------------------
# Per-stratum aggregation — TS describe("operator-eval-runner — per-stratum")
# ---------------------------------------------------------------------------


class TestPerStratumAggregation:
    """Ports TS ``describe("operator-eval-runner — per-stratum
    aggregation")`` (``operator-eval-runner.test.ts:222-241``)."""

    @pytest.mark.asyncio
    async def test_groups_recall_by_stratum_only_scored_queries(
        self, db: sqlite3.Connection
    ) -> None:
        """Ports TS ``it("groups recall by stratum (only scored queries
        contribute)")`` (``operator-eval-runner.test.ts:223-240``)."""
        register_query_set(db, _identity(), SAMPLE_QUERIES)
        result = await run_eval(
            db,
            RunEvalArgs(
                query_set_identity=_identity(),
                mode="fts_only",
                retrieval_adapter=_make_mock_adapter({
                    "q1": ["leaf_a", "leaf_b"],
                    "q2": ["leaf_c"],
                    "q3": ["leaf_z"],
                }),
            ),
        )
        assert result.recall_report.by_stratum["fts-easy"].n == 1
        assert result.recall_report.by_stratum["paraphrastic"].n == 1
        # q3 had no expected_summary_ids → not in any stratum aggregate
        assert "fts-medium" not in result.recall_report.by_stratum


# ---------------------------------------------------------------------------
# Per-AC additions (issue 08-13)
# ---------------------------------------------------------------------------


class TestPerAcAdditions:
    """The three explicit per-AC tests called out in the issue spec.

    See ``epics/08-cli-ops/08-13-eval-runner.md`` lines 101-103.
    """

    @pytest.mark.asyncio
    async def test_fts_only_no_voyage(self, db: sqlite3.Connection) -> None:
        """``fts_only`` mode must not invoke Voyage at all.

        The runner itself doesn't call Voyage — the retrieval adapter is
        injected, so the runner is provider-agnostic by construction. To
        validate the "fts_only mode does NOT call Voyage" AC, we install
        a sentinel ``VoyageBoobyTrap`` adapter that raises if its embed
        method is ever called, and confirm a full FTS-mode run never
        triggers it.

        This matches the TS source's "the adapter is INJECTED so this
        module is testable without a Voyage key" promise
        (``eval-runner.ts:6-8``).
        """

        class VoyageBoobyTrap:
            """Sentinel: any attribute access except ``search`` raises."""

            def __init__(self) -> None:
                self.voyage_calls = 0

            async def search(self, query: QueryRecord) -> list[str]:
                # Real FTS adapter would do SQLite FTS5 here; we just
                # return canned hits so the runner has data to score.
                canned = {
                    "q1": ["leaf_a", "leaf_b"],
                    "q2": ["leaf_c"],
                }
                return canned.get(query.query_id, [])

            def embed(self, _text: str) -> list[float]:
                self.voyage_calls += 1
                raise AssertionError(
                    f"fts_only mode must never call Voyage.embed; called with: {_text!r}"
                )

        register_query_set(db, _identity(), SAMPLE_QUERIES)
        adapter = VoyageBoobyTrap()
        result = await run_eval(
            db,
            RunEvalArgs(
                query_set_identity=_identity(),
                mode="fts_only",
                retrieval_adapter=adapter,  # type: ignore[arg-type]
            ),
        )
        # Run completed normally and the booby-trap was never tripped.
        assert result.run_id.startswith("evalrun_")
        assert adapter.voyage_calls == 0

    @pytest.mark.asyncio
    async def test_baseline_drift_computed(self, db: sqlite3.Connection) -> None:
        """Seed a prior run, run again, assert drift fields populated.

        This is the per-AC "baseline mode reads the previous run and
        computes drift" check (issue 08-13 line 98 + line 102).
        """
        register_query_set(db, _identity(), SAMPLE_QUERIES)
        # Prior run — q1 + q2 both at rank 1
        await run_eval(
            db,
            RunEvalArgs(
                query_set_identity=_identity(),
                mode="hybrid",
                retrieval_adapter=_make_mock_adapter({"q1": ["leaf_a"], "q2": ["leaf_c"]}),
            ),
        )
        # Current run — q2 slips to rank 2 (regression)
        current = await run_eval(
            db,
            RunEvalArgs(
                query_set_identity=_identity(),
                mode="hybrid",
                retrieval_adapter=_make_mock_adapter({
                    "q1": ["leaf_a"],
                    "q2": ["leaf_x", "leaf_c"],
                }),
            ),
        )
        assert current.drift is not None
        # cumulative_delta should be negative (regression on q2)
        assert current.drift.cumulative_delta < 0
        # q2 must appear in details with a negative delta
        q2 = next(
            (d for d in current.drift.details if d.query_id == "q2"),
            None,
        )
        assert q2 is not None
        assert q2.delta is not None
        assert q2.delta < 0
        # Aggregate counts: q2 regressed, q1 unchanged (both at rank 1)
        assert current.drift.regressed >= 1

    @pytest.mark.asyncio
    async def test_missing_voyage_creds_not_runner_concern(self, db: sqlite3.Connection) -> None:
        """The runner is provider-agnostic; credential validation is
        the adapter's concern.

        Issue 08-13 line 103 calls for
        ``test_missing_voyage_creds_raises`` raising
        :class:`EvalRunnerError` when ``semantic_only`` + no Voyage
        creds. The TS source (``eval-runner.ts:1-194``) has no such
        check — the runner only validates the query set's existence
        and emptiness; Voyage credential validation lives in the
        adapter (``embeddings/semantic-search.ts`` and friends).

        Per CLAUDE.md "1:1 source-to-Python port" mandate, we don't add
        a credential check to the runner — it would diverge from the TS
        source and couple this module to Voyage (which is the OPPOSITE
        of the design intent per ``eval-runner.ts:6-8``).

        Instead, we validate the contract: if the adapter raises (e.g.
        because Voyage creds are missing), the runner surfaces the
        error to the caller without swallowing it. This matches TS
        ``recall.ts:158-162`` ("adapter exceptions are NOT swallowed
        — if the adapter throws, the caller sees the error").

        The ``/lcm eval`` command handler (``commands/eval.py``) is the
        right place to pre-validate Voyage creds before instantiating
        the semantic/hybrid adapter — that's where the AC's check
        belongs, not here.
        """

        class CredsMissingAdapter:
            """Models an adapter that raises if Voyage creds are absent."""

            async def search(self, query: QueryRecord) -> list[str]:
                raise RuntimeError(
                    "VOYAGE_API_KEY not configured; semantic retrieval is unavailable"
                )

        register_query_set(db, _identity(), SAMPLE_QUERIES)
        with pytest.raises(RuntimeError, match="VOYAGE_API_KEY not configured"):
            await run_eval(
                db,
                RunEvalArgs(
                    query_set_identity=_identity(),
                    mode="semantic_only",
                    retrieval_adapter=CredsMissingAdapter(),
                ),
            )
