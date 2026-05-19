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
* :func:`compute_drift` writes aggregate row to ``lcm_eval_drift``.
* Noise-floor threshold gates "drifted" count.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator

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
