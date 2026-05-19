"""Tests for :mod:`lossless_hermes.eval.run` — run recording + drift.

Ports ``lossless-claw/test/eval-run.test.ts`` (commit ``1f07fbd`` on
branch ``pr-613``).

Covers:

* :func:`record_eval_run` writes a row with the right shape (mode in
  envelope, scores, trigger, prompt_bundle_version).
* :func:`record_eval_run` raises a clear error on unregistered query set
  (instead of opaque FK violation).
* :func:`compute_drift` returns zero-summary on first run (no prior).
* :func:`compute_drift` computes per-query delta when a prior exists.
* :func:`compute_drift` filters by mode (different mode → fresh baseline).
* :func:`compute_drift` selects the MOST-RECENT prior run, not the oldest.
* :func:`compute_drift` writes aggregate row to ``lcm_eval_drift``.
* Noise-floor threshold gates "drifted" count.
* Improved/regressed split with mixed-sign deltas.
* ``details`` sorted by ``abs(delta)`` DESC.
* Quality-report path — quality preferred over recall, ``judge_models``
  sorted-deduped, mixed availability yields ``delta=None``.
* Malformed-envelope handling — skip a malformed prior, raise on a
  malformed current.

### Quality-report fixtures are constructed INLINE

``src/lossless_hermes/eval/judge.py`` (issue 09-03) is being ported in
parallel and is NOT on this branch's base. :mod:`lossless_hermes.eval.run`
accesses quality reports purely by duck-typing (``getattr`` on
``per_query`` / ``overall`` / ``per_judge_scores``), so we build minimal
stand-in dataclasses here — :class:`_QPerJudge`, :class:`_QPerQuery`,
:class:`_QAggregate`, :class:`_QualityReport` — that satisfy exactly the
attribute surface ``run.py`` reads. This keeps the ``run.py`` quality
path fully exercised without importing ``judge.py``. When 09-03 lands,
these fixtures can be swapped for the real ``QualityReport`` with no
change to the assertions.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest

from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.eval.query_set import (
    QueryRecord,
    QuerySetIdentity,
    register_query_set,
)
from lossless_hermes.eval.recall import run_recall_eval
from lossless_hermes.eval.run import (
    EvalRunRecord,
    compute_drift,
    record_eval_run,
)


# ---------------------------------------------------------------------------
# Inline quality-report stand-ins (judge.py / 09-03 not on this branch base)
#
# run.py reads quality reports via getattr only — these dataclasses expose
# exactly the attributes it touches:
#   * _build_envelope          → report.per_query[i].query_id / .mean_score
#   * record_eval_run          → report.overall.mean_score
#   * _judge_models_from_...   → report.per_query[i].per_judge_scores[j].judge_id
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _QPerJudge:
    """One judge's score for a query — stands in for ``judge.py``'s
    per-judge score row. ``run.py`` reads ``judge_id`` off this."""

    judge_id: str
    score: float | None


@dataclass(frozen=True)
class _QPerQuery:
    """Per-query quality result. ``run.py`` reads ``query_id``,
    ``mean_score`` and ``per_judge_scores`` off this."""

    query_id: str
    mean_score: float | None
    per_judge_scores: list[_QPerJudge] = field(default_factory=list)


@dataclass(frozen=True)
class _QAggregate:
    """Overall quality aggregate. ``record_eval_run`` reads
    ``mean_score`` off this for ``synthesis_quality_score``."""

    mean_score: float


@dataclass(frozen=True)
class _QualityReport:
    """Minimal quality report — the duck-typed shape ``run.py`` expects
    from ``judge.py``'s ``QualityReport`` (issue 09-03)."""

    per_query: list[_QPerQuery]
    overall: _QAggregate


def _quality_report(
    scores: dict[str, dict[str, float | None]],
) -> _QualityReport:
    """Build a :class:`_QualityReport` from ``{query_id: {judge_id: score}}``.

    ``mean_score`` per query is the mean of its non-null judge scores;
    the overall ``mean_score`` is the mean of the per-query means.
    """
    per_query: list[_QPerQuery] = []
    for qid, judges in scores.items():
        per_judge = [_QPerJudge(judge_id=jid, score=sc) for jid, sc in judges.items()]
        non_null = [sc for sc in judges.values() if sc is not None]
        mean = sum(non_null) / len(non_null) if non_null else None
        per_query.append(_QPerQuery(query_id=qid, mean_score=mean, per_judge_scores=per_judge))
    query_means = [pq.mean_score for pq in per_query if pq.mean_score is not None]
    overall_mean = sum(query_means) / len(query_means) if query_means else 0.0
    return _QualityReport(per_query=per_query, overall=_QAggregate(mean_score=overall_mean))


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(conn, fts5_available=False, seed_default_prompts=False)
    try:
        yield conn
    finally:
        conn.close()


class _DictAdapter:
    def __init__(self, canned: dict[str, list[str]]) -> None:
        self._canned = canned

    async def search(self, query: QueryRecord) -> list[str]:
        return list(self._canned.get(query.query_id, []))


def _seed_basic_set(
    db: sqlite3.Connection,
    identity: QuerySetIdentity,
) -> tuple[QueryRecord, ...]:
    queries = (
        QueryRecord(
            query_id="q1",
            query_text="hello",
            stratum="fts-easy",
            expected_summary_ids=("a",),
        ),
        QueryRecord(
            query_id="q2",
            query_text="world",
            stratum="fts-easy",
            expected_summary_ids=("b",),
        ),
    )
    register_query_set(db, identity, queries)
    return queries


# ---------------------------------------------------------------------------
# record_eval_run
# ---------------------------------------------------------------------------


class TestRecordEvalRun:
    def test_writes_row_with_generated_id(self, db: sqlite3.Connection) -> None:
        identity = QuerySetIdentity(name="set", version=1)
        _seed_basic_set(db, identity)
        run_id = record_eval_run(
            db,
            EvalRunRecord(
                query_set_identity=identity,
                mode="fts_only",
            ),
        )
        assert run_id.startswith("evalrun_")
        row = db.execute(
            "SELECT query_set_id, trigger, prompt_bundle_version FROM lcm_eval_run WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        assert row[0] == "set@v1"
        assert row[1] == "manual"
        assert row[2] == 1

    def test_uses_provided_run_id(self, db: sqlite3.Connection) -> None:
        identity = QuerySetIdentity(name="set", version=1)
        _seed_basic_set(db, identity)
        run_id = record_eval_run(
            db,
            EvalRunRecord(
                query_set_identity=identity,
                mode="fts_only",
                run_id="custom-run-id",
            ),
        )
        assert run_id == "custom-run-id"

    def test_serializes_mode_into_envelope(self, db: sqlite3.Connection) -> None:
        identity = QuerySetIdentity(name="set", version=1)
        _seed_basic_set(db, identity)
        run_id = record_eval_run(
            db,
            EvalRunRecord(
                query_set_identity=identity,
                mode="hybrid",
                notes="optional caller note",
            ),
        )
        env_json = db.execute(
            "SELECT per_query_scores FROM lcm_eval_run WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0]
        env = json.loads(env_json)
        assert env["mode"] == "hybrid"
        assert env["v"] == 1
        assert env["notes"] == "optional caller note"
        assert env["hasRecall"] is False
        assert env["hasQuality"] is False

    def test_recall_score_pulled_from_report(self, db: sqlite3.Connection) -> None:
        identity = QuerySetIdentity(name="set", version=1)
        queries = _seed_basic_set(db, identity)
        # Build a real RecallReport via run_recall_eval — both queries
        # hit at rank 1 → mean RR = 1.0.
        report = asyncio.run(
            run_recall_eval(
                queries,
                _DictAdapter({"q1": ["a"], "q2": ["b"]}),
            )
        )
        run_id = record_eval_run(
            db,
            EvalRunRecord(
                query_set_identity=identity,
                mode="fts_only",
                recall_report=report,
            ),
        )
        row = db.execute(
            "SELECT retrieval_recall_score, per_query_scores FROM lcm_eval_run WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        assert row[0] == pytest.approx(1.0)
        env = json.loads(row[1])
        assert env["hasRecall"] is True
        assert env["perQuery"]["q1"]["recallRR"] == pytest.approx(1.0)

    def test_raises_on_unregistered_query_set(self, db: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="unregistered query set"):
            record_eval_run(
                db,
                EvalRunRecord(
                    query_set_identity=QuerySetIdentity(name="missing", version=1),
                    mode="fts_only",
                ),
            )

    def test_trigger_defaults_to_manual(self, db: sqlite3.Connection) -> None:
        identity = QuerySetIdentity(name="set", version=1)
        _seed_basic_set(db, identity)
        run_id = record_eval_run(
            db,
            EvalRunRecord(query_set_identity=identity, mode="fts_only"),
        )
        trigger = db.execute(
            "SELECT trigger FROM lcm_eval_run WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
        assert trigger == "manual"

    def test_trigger_uses_provided_value(self, db: sqlite3.Connection) -> None:
        identity = QuerySetIdentity(name="set", version=1)
        _seed_basic_set(db, identity)
        run_id = record_eval_run(
            db,
            EvalRunRecord(
                query_set_identity=identity,
                mode="fts_only",
                trigger="nightly",
            ),
        )
        trigger = db.execute(
            "SELECT trigger FROM lcm_eval_run WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
        assert trigger == "nightly"

    def test_noise_floor_sd_stored_verbatim(self, db: sqlite3.Connection) -> None:
        """09-04 acceptance: ``noise_floor_sd`` round-trips — ``None``
        stays NULL, a float stays that float."""
        identity = QuerySetIdentity(name="set", version=1)
        _seed_basic_set(db, identity)
        # None → NULL.
        run_none = record_eval_run(db, EvalRunRecord(query_set_identity=identity, mode="fts_only"))
        assert (
            db.execute(
                "SELECT noise_floor_sd FROM lcm_eval_run WHERE run_id = ?", (run_none,)
            ).fetchone()[0]
            is None
        )
        # Float → stored verbatim.
        run_float = record_eval_run(
            db,
            EvalRunRecord(query_set_identity=identity, mode="hybrid", noise_floor_sd=0.15),
        )
        assert db.execute(
            "SELECT noise_floor_sd FROM lcm_eval_run WHERE run_id = ?", (run_float,)
        ).fetchone()[0] == pytest.approx(0.15)


# ---------------------------------------------------------------------------
# record_eval_run — quality-report path
#
# Quality reports are built inline (see module docstring) — judge.py
# (09-03) is not on this branch base. run.py reads quality reports purely
# by duck-typing, so these fixtures exercise the real quality code path.
# ---------------------------------------------------------------------------


class TestRecordEvalRunQuality:
    def test_quality_only_run_records_quality_score(self, db: sqlite3.Connection) -> None:
        """TS ``eval-run.test.ts:102-144`` — a quality-only run records
        ``synthesis_quality_score`` from the report and 0 for recall.

        Two judges score q1/q2 at 5 and 4 → per-query mean 4.5 → overall
        4.5. ``hasQuality`` is true, ``hasRecall`` false, and each
        query's ``qualityScore`` lands in the envelope. q3 has no
        candidate so it never appears in ``perQuery``.
        """
        identity = QuerySetIdentity(name="set", version=1)
        _seed_basic_set(db, identity)
        quality = _quality_report({
            "q1": {"claude": 5.0, "gpt": 4.0},
            "q2": {"claude": 5.0, "gpt": 4.0},
        })
        run_id = record_eval_run(
            db,
            EvalRunRecord(
                query_set_identity=identity,
                mode="hybrid",
                quality_report=quality,
                notes="after prompt v3 rollout",
                trigger="prompt-update",
                prompt_bundle_version=7,
                noise_floor_sd=0.15,
            ),
        )
        row = db.execute(
            """
            SELECT retrieval_recall_score, synthesis_quality_score,
                   per_query_scores, trigger, prompt_bundle_version, noise_floor_sd
              FROM lcm_eval_run WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        assert row[0] == pytest.approx(0.0)  # recall side absent → 0
        assert row[1] == pytest.approx(4.5)  # quality side present
        assert row[3] == "prompt-update"
        assert row[4] == 7
        assert row[5] == pytest.approx(0.15)
        env = json.loads(row[2])
        assert env["hasRecall"] is False
        assert env["hasQuality"] is True
        assert env["notes"] == "after prompt v3 rollout"
        assert env["perQuery"]["q1"]["qualityScore"] == pytest.approx(4.5)
        assert env["perQuery"]["q2"]["qualityScore"] == pytest.approx(4.5)

    def test_judge_models_sorted_and_deduped(self, db: sqlite3.Connection) -> None:
        """TS ``eval-run.test.ts:137`` / 09-04 acceptance — ``judge_models``
        is the SORTED, DEDUPED list of judge IDs across all per-query
        per-judge scores, serialized as a JSON array.

        Judges appear out of order and repeat across queries
        (``gpt``/``claude`` on q1, ``claude``/``anthropic`` on q2) — the
        stored column must be the unique set, sorted ASC.
        """
        identity = QuerySetIdentity(name="set", version=1)
        _seed_basic_set(db, identity)
        quality = _quality_report({
            "q1": {"gpt": 4.0, "claude": 5.0},
            "q2": {"claude": 4.0, "anthropic": 3.0},
        })
        run_id = record_eval_run(
            db,
            EvalRunRecord(query_set_identity=identity, mode="hybrid", quality_report=quality),
        )
        judge_models = db.execute(
            "SELECT judge_models FROM lcm_eval_run WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
        assert json.loads(judge_models) == ["anthropic", "claude", "gpt"]

    def test_judge_models_empty_without_quality_report(self, db: sqlite3.Connection) -> None:
        """TS ``eval-run.test.ts:89`` — no quality report → ``judge_models``
        is the empty JSON array, not NULL."""
        identity = QuerySetIdentity(name="set", version=1)
        _seed_basic_set(db, identity)
        run_id = record_eval_run(db, EvalRunRecord(query_set_identity=identity, mode="fts_only"))
        judge_models = db.execute(
            "SELECT judge_models FROM lcm_eval_run WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
        assert judge_models == "[]"
        assert json.loads(judge_models) == []

    def test_both_reports_set_both_flags_and_scores(self, db: sqlite3.Connection) -> None:
        """09-04 acceptance: a run with BOTH reports records both scores
        non-zero and both envelope flags true."""
        identity = QuerySetIdentity(name="set", version=1)
        queries = _seed_basic_set(db, identity)
        recall = asyncio.run(run_recall_eval(queries, _DictAdapter({"q1": ["a"], "q2": ["b"]})))
        quality = _quality_report({"q1": {"j": 5.0}, "q2": {"j": 5.0}})
        run_id = record_eval_run(
            db,
            EvalRunRecord(
                query_set_identity=identity,
                mode="hybrid",
                recall_report=recall,
                quality_report=quality,
            ),
        )
        row = db.execute(
            "SELECT retrieval_recall_score, synthesis_quality_score, per_query_scores "
            "FROM lcm_eval_run WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        assert row[0] == pytest.approx(1.0)  # mean RR
        assert row[1] == pytest.approx(5.0)  # mean quality
        env = json.loads(row[2])
        assert env["hasRecall"] is True
        assert env["hasQuality"] is True


# ---------------------------------------------------------------------------
# compute_drift
# ---------------------------------------------------------------------------


class TestComputeDrift:
    def test_first_run_returns_zero_summary(self, db: sqlite3.Connection) -> None:
        identity = QuerySetIdentity(name="set", version=1)
        _seed_basic_set(db, identity)
        run_id = record_eval_run(
            db,
            EvalRunRecord(query_set_identity=identity, mode="fts_only"),
        )
        drift = compute_drift(db, run_id)
        assert drift.prior_run_id is None
        assert drift.drifted == 0
        assert drift.cumulative_delta == 0.0
        # No row written to lcm_eval_drift on first run.
        n = db.execute("SELECT COUNT(*) FROM lcm_eval_drift").fetchone()[0]
        assert n == 0

    def test_second_run_computes_delta(self, db: sqlite3.Connection) -> None:
        identity = QuerySetIdentity(name="set", version=1)
        queries = _seed_basic_set(db, identity)
        # Run 1: q1+q2 both at rank 1 (RR=1.0 each)
        report1 = asyncio.run(run_recall_eval(queries, _DictAdapter({"q1": ["a"], "q2": ["b"]})))
        run1 = record_eval_run(
            db,
            EvalRunRecord(
                query_set_identity=identity,
                mode="fts_only",
                recall_report=report1,
            ),
        )
        compute_drift(db, run1)  # first run, returns zero

        # Run 2: q2 slips to rank 2 (RR=0.5) — regression
        report2 = asyncio.run(
            run_recall_eval(
                queries,
                _DictAdapter({"q1": ["a"], "q2": ["x", "b"]}),
            )
        )
        run2 = record_eval_run(
            db,
            EvalRunRecord(
                query_set_identity=identity,
                mode="fts_only",
                recall_report=report2,
            ),
        )
        drift = compute_drift(db, run2)
        assert drift.prior_run_id == run1
        q2 = next(d for d in drift.details if d.query_id == "q2")
        assert q2.delta == pytest.approx(-0.5)
        assert drift.regressed == 1
        assert drift.improved == 0
        assert drift.cumulative_delta == pytest.approx(-0.5)

    def test_different_mode_is_fresh_baseline(self, db: sqlite3.Connection) -> None:
        identity = QuerySetIdentity(name="set", version=1)
        _seed_basic_set(db, identity)
        record_eval_run(
            db,
            EvalRunRecord(query_set_identity=identity, mode="fts_only"),
        )
        hybrid_run = record_eval_run(
            db,
            EvalRunRecord(query_set_identity=identity, mode="hybrid"),
        )
        drift = compute_drift(db, hybrid_run)
        assert drift.prior_run_id is None

    def test_drift_writes_aggregate_row(self, db: sqlite3.Connection) -> None:
        identity = QuerySetIdentity(name="set", version=1)
        queries = _seed_basic_set(db, identity)
        report1 = asyncio.run(run_recall_eval(queries, _DictAdapter({"q1": ["a"], "q2": ["b"]})))
        record_eval_run(
            db,
            EvalRunRecord(
                query_set_identity=identity,
                mode="fts_only",
                recall_report=report1,
            ),
        )
        report2 = asyncio.run(
            run_recall_eval(queries, _DictAdapter({"q1": ["a"], "q2": ["x", "b"]}))
        )
        run2 = record_eval_run(
            db,
            EvalRunRecord(
                query_set_identity=identity,
                mode="fts_only",
                recall_report=report2,
            ),
        )
        compute_drift(db, run2)
        row = db.execute(
            "SELECT query_set_id, cumulative_delta, window_runs FROM lcm_eval_drift"
        ).fetchone()
        assert row is not None
        assert row[0] == "set@v1"
        assert row[1] == pytest.approx(-0.5)
        assert row[2] == 2

    def test_noise_floor_threshold(self, db: sqlite3.Connection) -> None:
        """Noise floor 0.4 → 2× = 0.8 → only deltas ≥0.8 count as drifted."""
        identity = QuerySetIdentity(name="set", version=1)
        queries = _seed_basic_set(db, identity)
        report1 = asyncio.run(run_recall_eval(queries, _DictAdapter({"q1": ["a"], "q2": ["b"]})))
        record_eval_run(
            db,
            EvalRunRecord(
                query_set_identity=identity,
                mode="fts_only",
                recall_report=report1,
            ),
        )
        # Run 2: q2 slips by 0.5 only (rank 2) — below 2*0.4=0.8 threshold
        report2 = asyncio.run(
            run_recall_eval(queries, _DictAdapter({"q1": ["a"], "q2": ["x", "b"]}))
        )
        run2 = record_eval_run(
            db,
            EvalRunRecord(
                query_set_identity=identity,
                mode="fts_only",
                recall_report=report2,
                noise_floor_sd=0.4,
            ),
        )
        drift = compute_drift(db, run2)
        # 0.5 < 0.8 → not "drifted" by 2× SD threshold
        assert drift.drifted == 0
        # But cumulative_delta still tracks the raw change
        assert drift.cumulative_delta == pytest.approx(-0.5)

    def test_compute_drift_raises_on_unknown_run(self, db: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="no eval run found"):
            compute_drift(db, "no-such-run")

    def test_selects_most_recent_prior_not_oldest(self, db: sqlite3.Connection) -> None:
        """TS ``eval-run.test.ts:347-392`` — drift compares against the
        MOST-RECENT prior run of the same ``(query_set, mode)``.

        Three priors are recorded for q1 with RR 1.0, 0.5, 1.0. The
        current run has RR 0.25. ``_select_prior_run`` orders by
        ``ran_at DESC, run_id DESC``; ``ran_at`` (``datetime('now')``)
        has 1-second resolution so the rows likely share a timestamp —
        the ``run_id DESC`` tiebreak then decides. We name the runs so
        that ``"z-current" > "recent-prior" > "old-2" > "old-1"``: the
        current run is excluded, and ``recent-prior`` wins. The delta
        must be ``0.25 - 1.0 == -0.75`` (vs ``recent-prior``'s RR=1.0),
        NOT vs ``old-1`` or ``old-2``.
        """
        identity = QuerySetIdentity(name="set", version=1)
        queries = _seed_basic_set(db, identity)

        r1 = asyncio.run(run_recall_eval(queries, _DictAdapter({"q1": ["a"], "q2": ["b"]})))
        r2 = asyncio.run(
            run_recall_eval(queries, _DictAdapter({"q1": ["x", "a"], "q2": ["b"]}))
        )  # q1 RR=0.5
        r3 = asyncio.run(run_recall_eval(queries, _DictAdapter({"q1": ["a"], "q2": ["b"]})))
        record_eval_run(
            db,
            EvalRunRecord(
                run_id="old-1",
                query_set_identity=identity,
                mode="fts_only",
                recall_report=r1,
            ),
        )
        record_eval_run(
            db,
            EvalRunRecord(
                run_id="old-2",
                query_set_identity=identity,
                mode="fts_only",
                recall_report=r2,
            ),
        )
        recent_prior_id = record_eval_run(
            db,
            EvalRunRecord(
                run_id="recent-prior",
                query_set_identity=identity,
                mode="fts_only",
                recall_report=r3,
            ),
        )

        curr = asyncio.run(
            run_recall_eval(queries, _DictAdapter({"q1": ["x", "y", "z", "a"], "q2": ["b"]}))
        )  # q1 RR=0.25
        current_id = record_eval_run(
            db,
            EvalRunRecord(
                run_id="z-current",  # 'z' > 'r' → DESC tiebreak picks recent-prior
                query_set_identity=identity,
                mode="fts_only",
                recall_report=curr,
            ),
        )
        drift = compute_drift(db, current_id)
        assert drift.prior_run_id == recent_prior_id
        q1 = next(d for d in drift.details if d.query_id == "q1")
        assert q1.prior_score == pytest.approx(1.0)  # from r3, not r1/r2
        assert q1.current_score == pytest.approx(0.25)
        assert q1.delta == pytest.approx(-0.75)

    def test_improved_and_regressed_split(self, db: sqlite3.Connection) -> None:
        """TS ``eval-run.test.ts:195-247`` — mixed-sign deltas split
        cleanly into improved (delta>0) and regressed (delta<0).

        q1 regresses 1.0 → 0.5; q2 improves 0.5 → 1.0. Without a noise
        floor any non-zero delta counts, so drifted=2, improved=1,
        regressed=1, cumulative = -0.5 + 0.5 = 0.
        """
        identity = QuerySetIdentity(name="set", version=1)
        queries = _seed_basic_set(db, identity)

        prior = asyncio.run(
            run_recall_eval(queries, _DictAdapter({"q1": ["a"], "q2": ["x", "b"]}))
        )  # q1 RR=1.0, q2 RR=0.5
        record_eval_run(
            db,
            EvalRunRecord(query_set_identity=identity, mode="fts_only", recall_report=prior),
        )
        current = asyncio.run(
            run_recall_eval(queries, _DictAdapter({"q1": ["x", "a"], "q2": ["b"]}))
        )  # q1 RR=0.5, q2 RR=1.0
        current_id = record_eval_run(
            db,
            EvalRunRecord(query_set_identity=identity, mode="fts_only", recall_report=current),
        )
        drift = compute_drift(db, current_id)
        assert drift.drifted == 2
        assert drift.improved == 1
        assert drift.regressed == 1
        q1 = next(d for d in drift.details if d.query_id == "q1")
        q2 = next(d for d in drift.details if d.query_id == "q2")
        assert q1.delta == pytest.approx(-0.5)
        assert q2.delta == pytest.approx(0.5)
        assert drift.cumulative_delta == pytest.approx(0.0)

    def test_details_sorted_by_abs_delta_desc(self, db: sqlite3.Connection) -> None:
        """09-04 acceptance: ``details`` is sorted by ``abs(delta)`` DESC.

        q1 swings by 0.75 (1.0 → 0.25), q2 by 0.5 (1.0 → 0.5). The
        larger absolute swing (q1) must sort first regardless of sign.
        """
        identity = QuerySetIdentity(name="set", version=1)
        queries = _seed_basic_set(db, identity)

        prior = asyncio.run(
            run_recall_eval(queries, _DictAdapter({"q1": ["a"], "q2": ["b"]}))
        )  # both RR=1.0
        record_eval_run(
            db,
            EvalRunRecord(query_set_identity=identity, mode="fts_only", recall_report=prior),
        )
        current = asyncio.run(
            run_recall_eval(queries, _DictAdapter({"q1": ["x", "y", "z", "a"], "q2": ["x", "b"]}))
        )  # q1 RR=0.25, q2 RR=0.5
        current_id = record_eval_run(
            db,
            EvalRunRecord(query_set_identity=identity, mode="fts_only", recall_report=current),
        )
        drift = compute_drift(db, current_id)
        abs_deltas = [abs(d.delta) for d in drift.details if d.delta is not None]
        assert abs_deltas == sorted(abs_deltas, reverse=True)
        # q1's 0.75 swing sorts ahead of q2's 0.5 swing.
        assert drift.details[0].query_id == "q1"
        assert drift.details[0].delta == pytest.approx(-0.75)

    def test_mixed_availability_yields_none_delta(self, db: sqlite3.Connection) -> None:
        """09-04 acceptance: a query present on only ONE side of the
        comparison gets ``delta=None`` and is excluded from the counts.

        ``compute_drift`` unions the prior and current ``perQuery`` key
        sets. A query that appears in the prior envelope but not the
        current one (or vice versa) has no scalar on the missing side,
        so ``_pick_comparable_score`` returns ``None`` for it and
        ``delta`` is ``None`` (``run.py`` only subtracts when BOTH
        ``prior_score`` and ``current_score`` are non-``None``).

        Note on the cross-metric case: when a query is present on both
        sides but one side carries only quality and the other only
        recall, ``_pick_comparable_score``'s third (fallback) branch
        still surfaces each side's lone scalar — so that pair DOES yield
        a numeric (cross-metric) delta. The genuine ``delta=None`` path
        is the one-sided-presence case exercised here.

        Set-up: a quality report covers ``q1`` only; a recall report
        covers ``q2`` only. The two share no query id, so EVERY detail
        row is one-sided → ``delta=None`` everywhere, ``drifted=0``,
        ``cumulative_delta=0``.
        """
        identity = QuerySetIdentity(name="set", version=1)

        # Register a 2-query set so both run reports are FK-valid, but
        # have each report cover a DIFFERENT single query.
        register_query_set(
            db,
            identity,
            (
                QueryRecord(
                    query_id="q1",
                    query_text="hello",
                    stratum="fts-easy",
                    expected_summary_ids=("a",),
                ),
                QueryRecord(
                    query_id="q2",
                    query_text="world",
                    stratum="fts-easy",
                    expected_summary_ids=("b",),
                ),
            ),
        )

        # Prior run: quality envelope mentions ONLY q1.
        prior_quality = _quality_report({"q1": {"j": 4.0}})
        record_eval_run(
            db,
            EvalRunRecord(
                query_set_identity=identity,
                mode="hybrid",
                quality_report=prior_quality,
            ),
        )
        # Current run: recall envelope mentions ONLY q2.
        current_recall = asyncio.run(
            run_recall_eval(
                (
                    QueryRecord(
                        query_id="q2",
                        query_text="world",
                        stratum="fts-easy",
                        expected_summary_ids=("b",),
                    ),
                ),
                _DictAdapter({"q2": ["b"]}),
            )
        )
        current_id = record_eval_run(
            db,
            EvalRunRecord(
                query_set_identity=identity,
                mode="hybrid",
                recall_report=current_recall,
            ),
        )
        drift = compute_drift(db, current_id)
        # A prior run exists, so it is selected.
        assert drift.prior_run_id is not None
        # q1 (prior-only) and q2 (current-only) — both one-sided.
        detail_qids = {d.query_id for d in drift.details}
        assert detail_qids == {"q1", "q2"}
        for d in drift.details:
            assert d.delta is None
        assert drift.drifted == 0
        assert drift.improved == 0
        assert drift.regressed == 0
        assert drift.cumulative_delta == pytest.approx(0.0)
        # None-delta details sort last (sort key treats them as +1).
        assert drift.details[-1].delta is None

    def test_skips_prior_with_malformed_envelope(self, db: sqlite3.Connection) -> None:
        """09-04 acceptance: ``compute_drift`` skips a prior candidate
        whose ``per_query_scores`` JSON is malformed and never raises on
        legacy/garbage data — it falls through to the next candidate.

        We record a valid prior, then corrupt its envelope. The current
        run then finds NO usable prior (the only candidate is garbage),
        so drift is a zero summary rather than an exception.
        """
        identity = QuerySetIdentity(name="set", version=1)
        queries = _seed_basic_set(db, identity)

        prior = asyncio.run(run_recall_eval(queries, _DictAdapter({"q1": ["a"], "q2": ["b"]})))
        prior_id = record_eval_run(
            db,
            EvalRunRecord(query_set_identity=identity, mode="fts_only", recall_report=prior),
        )
        # Corrupt the prior's envelope to non-JSON garbage.
        db.execute(
            "UPDATE lcm_eval_run SET per_query_scores = ? WHERE run_id = ?",
            ("{not json at all", prior_id),
        )
        current = asyncio.run(
            run_recall_eval(queries, _DictAdapter({"q1": ["x", "a"], "q2": ["b"]}))
        )
        current_id = record_eval_run(
            db,
            EvalRunRecord(query_set_identity=identity, mode="fts_only", recall_report=current),
        )
        # The malformed prior is skipped → treated as "no prior run".
        drift = compute_drift(db, current_id)  # must NOT raise
        assert drift.prior_run_id is None
        assert drift.drifted == 0
        assert drift.cumulative_delta == 0.0

    def test_raises_on_malformed_current_envelope(self, db: sqlite3.Connection) -> None:
        """09-04 acceptance: a malformed envelope on the CURRENT run is
        fatal — ``compute_drift`` raises.

        Unlike a legacy prior (which we skip), the current run's
        envelope was just written by us; malformed JSON there is a bug,
        not tolerable drift-history noise.
        """
        identity = QuerySetIdentity(name="set", version=1)
        _seed_basic_set(db, identity)
        current_id = record_eval_run(
            db,
            EvalRunRecord(query_set_identity=identity, mode="fts_only"),
        )
        db.execute(
            "UPDATE lcm_eval_run SET per_query_scores = ? WHERE run_id = ?",
            ("}{ broken", current_id),
        )
        with pytest.raises(ValueError, match="malformed per_query_scores"):
            compute_drift(db, current_id)

    def test_quality_score_preferred_over_recall_for_drift(self, db: sqlite3.Connection) -> None:
        """TS ``eval-run.test.ts:308-345`` — when both runs carry a
        quality score AND a recall score for a query, drift diffs the
        QUALITY scores (``_pick_comparable_score`` preference order:
        quality, then recall).

        Recall is held identical across both runs, so a recall-based
        delta would be 0. Quality moves 3 → 5 per query. The drift delta
        must be ``+2`` per query — proof that quality, not recall, is the
        comparison basis.
        """
        identity = QuerySetIdentity(name="set", version=1)
        queries = _seed_basic_set(db, identity)
        # Identical recall on both runs.
        recall = asyncio.run(run_recall_eval(queries, _DictAdapter({"q1": ["a"], "q2": ["b"]})))

        prior_quality = _quality_report({"q1": {"j": 3.0}, "q2": {"j": 3.0}})
        record_eval_run(
            db,
            EvalRunRecord(
                query_set_identity=identity,
                mode="hybrid",
                recall_report=recall,
                quality_report=prior_quality,
            ),
        )
        current_quality = _quality_report({"q1": {"j": 5.0}, "q2": {"j": 5.0}})
        current_id = record_eval_run(
            db,
            EvalRunRecord(
                query_set_identity=identity,
                mode="hybrid",
                recall_report=recall,
                quality_report=current_quality,
            ),
        )
        drift = compute_drift(db, current_id)
        q1 = next(d for d in drift.details if d.query_id == "q1")
        assert q1.delta == pytest.approx(2.0)  # 5 - 3 quality, NOT 0 recall
        assert drift.improved == 2
        assert drift.regressed == 0

    def test_recall_rr_is_basis_when_only_recall_on_both(self, db: sqlite3.Connection) -> None:
        """09-04 acceptance: when both runs have ONLY recall scores,
        recall RR is the delta basis (the second branch of
        ``_pick_comparable_score``)."""
        identity = QuerySetIdentity(name="set", version=1)
        queries = _seed_basic_set(db, identity)
        prior = asyncio.run(
            run_recall_eval(queries, _DictAdapter({"q1": ["a"], "q2": ["b"]}))
        )  # q1 RR=1.0
        record_eval_run(
            db,
            EvalRunRecord(query_set_identity=identity, mode="fts_only", recall_report=prior),
        )
        current = asyncio.run(
            run_recall_eval(queries, _DictAdapter({"q1": ["x", "a"], "q2": ["b"]}))
        )  # q1 RR=0.5
        current_id = record_eval_run(
            db,
            EvalRunRecord(query_set_identity=identity, mode="fts_only", recall_report=current),
        )
        drift = compute_drift(db, current_id)
        q1 = next(d for d in drift.details if d.query_id == "q1")
        assert q1.delta == pytest.approx(-0.5)  # recall RR delta
