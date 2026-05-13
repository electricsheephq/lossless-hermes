"""Tests for :mod:`lossless_hermes.operator.health` (issue 08-03).

Ports ``lossless-claw/test/operator-health.test.ts`` (commit
``1f07fbd`` on branch ``pr-613``) plus the per-AC additions from the
issue spec:

* ``test_missing_table_unavailable`` — drops ``lcm_eval_run`` and
  confirms probe reports zero counts / :data:`None` without raising.
* ``test_workers_held_by_other_host`` — seeds a held lock row from a
  hypothetical second host and confirms it's surfaced.

See:

* ``epics/08-cli-ops/08-03-health.md`` — this issue.
* ``lossless-claw/src/operator/health.ts:1-442`` — TS source.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator

import pytest

from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.operator.health import (
    ActiveEmbeddingProfile,
    EmbeddingsHealth,
    EvalHealth,
    MostRecentEvalRun,
    SuppressionHealth,
    SynthesisHealth,
    V41HealthSnapshot,
    WorkerStatus,
    _extract_mode,
    get_v41_health_snapshot,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _new_db(*, seed_prompts: bool = False) -> sqlite3.Connection:
    """In-memory SQLite with the full LCM migration ladder applied.

    ``seed_prompts=False`` keeps the synthesis prompt registry empty
    so tests can assert exact counts without coupling to the seed
    inventory.
    """
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(
        conn,
        fts5_available=False,
        seed_default_prompts=seed_prompts,
    )
    return conn


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    """Migrated in-memory DB with seeding disabled."""
    conn = _new_db()
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Helper seeders — tight, just-enough data to drive each probe
# ---------------------------------------------------------------------------


def _seed_active_profile(
    db: sqlite3.Connection,
    *,
    model_name: str = "voyage-3",
    dim: int = 1024,
    archive_after: str | None = None,
) -> None:
    """Insert a row into ``lcm_embedding_profile``."""
    db.execute(
        """
        INSERT INTO lcm_embedding_profile (model_name, dim, active, archive_after)
        VALUES (?, ?, 1, ?)
        """,
        (model_name, dim, archive_after),
    )


def _seed_summary(
    db: sqlite3.Connection,
    *,
    summary_id: str,
    kind: str = "leaf",
    conversation_id: int = 1,
    token_count: int = 100,
    suppressed_at: str | None = None,
) -> None:
    """Insert one summary. ``conversation_id=1`` requires a seeded conv."""
    db.execute(
        """
        INSERT INTO summaries
            (summary_id, conversation_id, kind, content, token_count,
             source_message_token_count, descendant_token_count, suppressed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (summary_id, conversation_id, kind, f"body {summary_id}", token_count, 0, 0, suppressed_at),
    )


def _seed_conversation(db: sqlite3.Connection, *, conv_id: int = 1) -> None:
    """Insert one conversation row so summaries' FK is satisfied."""
    db.execute(
        "INSERT INTO conversations (conversation_id, session_id, session_key, active) "
        "VALUES (?, ?, NULL, 1)",
        (conv_id, f"sess-{conv_id}"),
    )


def _seed_worker_lock(
    db: sqlite3.Connection,
    *,
    job_kind: str,
    worker_id: str = "host:eva.local-pid:1234-start:0-nonce:abc",
    expires_at_offset_s: int = 90,
    metadata: str | None = None,
) -> None:
    """Insert an active lock row. ``expires_at_offset_s`` is relative to now."""
    db.execute(
        f"""
        INSERT INTO lcm_worker_lock
            (job_kind, worker_id, acquired_at, expires_at, last_heartbeat_at, job_metadata)
        VALUES (?, ?, datetime('now'), datetime('now', '{expires_at_offset_s} seconds'),
                datetime('now'), ?)
        """,
        (job_kind, worker_id, metadata),
    )


def _seed_prompt(
    db: sqlite3.Connection,
    *,
    prompt_id: str,
    memory_type: str = "episodic-leaf",
    pass_kind: str = "single",
    tier_label: str | None = None,
) -> None:
    """Insert one active prompt row directly (bypasses register_prompt's PK gen)."""
    db.execute(
        """
        INSERT INTO lcm_prompt_registry
            (prompt_id, memory_type, tier_label, pass_kind, version, template,
             model_recommendation, active, bundle_version, notes)
        VALUES (?, ?, ?, ?, 1, 'tpl', NULL, 1, 1, NULL)
        """,
        (prompt_id, memory_type, tier_label, pass_kind),
    )


def _seed_synthesis_audit(
    db: sqlite3.Connection,
    *,
    audit_id: str,
    prompt_id: str,
    summary_id: str,
    status: str = "completed",
    ran_at_offset: str | None = None,
) -> None:
    """Insert one ``lcm_synthesis_audit`` row.

    ``ran_at_offset`` is an SQLite ``datetime()`` modifier like
    ``"-2 days"``; :data:`None` means ``datetime('now')``.
    """
    if ran_at_offset is None:
        ran_at_expr = "datetime('now')"
    else:
        ran_at_expr = f"datetime('now', '{ran_at_offset}')"
    db.execute(
        f"""
        INSERT INTO lcm_synthesis_audit
            (audit_id, pass_session_id, target_summary_id, target_cache_id,
             prompt_id, pass_kind, pass_input_truncated, pass_output, status,
             model_used, latency_ms, cost_usd_cents, ran_at)
        VALUES (?, 'sess-1', ?, NULL, ?, 'single', 'input', 'output', ?,
                'model-x', 10, 0, {ran_at_expr})
        """,
        (audit_id, summary_id, prompt_id, status),
    )


def _seed_eval_query_set(db: sqlite3.Connection, *, query_set_id: str = "qs-1") -> None:
    db.execute(
        """
        INSERT INTO lcm_eval_query_set (query_set_id, version, description)
        VALUES (?, 1, 'test')
        """,
        (query_set_id,),
    )


def _seed_eval_run(
    db: sqlite3.Connection,
    *,
    run_id: str,
    query_set_id: str = "qs-1",
    recall: float = 0.84,
    mode: str = "hybrid",
    ran_at_offset: str | None = None,
) -> None:
    if ran_at_offset is None:
        ran_at_expr = "datetime('now')"
    else:
        ran_at_expr = f"datetime('now', '{ran_at_offset}')"
    per_query_scores = json.dumps({"mode": mode})
    db.execute(
        f"""
        INSERT INTO lcm_eval_run
            (run_id, query_set_id, prompt_bundle_version, ran_at,
             retrieval_recall_score, synthesis_quality_score,
             per_query_scores, judge_models, trigger)
        VALUES (?, ?, 1, {ran_at_expr}, ?, 0.9, ?, '[]', 'manual')
        """,
        (run_id, query_set_id, recall, per_query_scores),
    )


def _seed_eval_drift(
    db: sqlite3.Connection,
    *,
    drift_id: str = "drift-1",
    query_set_id: str = "qs-1",
    delta: float = -0.02,
) -> None:
    db.execute(
        """
        INSERT INTO lcm_eval_drift
            (drift_id, query_set_id, cumulative_delta, window_runs)
        VALUES (?, ?, ?, 3)
        """,
        (drift_id, query_set_id, delta),
    )


# ---------------------------------------------------------------------------
# Tolerance contract — fresh DB returns full snapshot with sentinel values
# ---------------------------------------------------------------------------


class TestFreshDbDoesNotRaise:
    """Per ``operator/health.ts`` module docstring: the snapshot is
    meaningful on a fresh DB — every section handles its own missing-row
    case rather than throwing.
    """

    def test_snapshot_on_empty_migrated_db_returns_zero_state(self, db: sqlite3.Connection) -> None:
        snapshot = get_v41_health_snapshot(db)
        # Embeddings — no profile registered, no vec0, zero pending.
        assert snapshot.embeddings.active_profile is None
        assert snapshot.embeddings.vec0_version is None
        assert snapshot.embeddings.pending_backfill == 0
        assert snapshot.embeddings.embedded_count == 0
        assert snapshot.embeddings.over_cap_pending == 0
        # Workers — one row per WORKER_JOB_KINDS, all idle.
        assert len(snapshot.workers) == 6  # match WORKER_JOB_KINDS tuple length
        assert all(not w.active for w in snapshot.workers)
        # Synthesis — empty prompt registry, zero audit rows.
        assert snapshot.synthesis.active_prompt_count == 0
        assert snapshot.synthesis.distinct_memory_type_count == 0
        assert snapshot.synthesis.recent_synthesis_runs_7d == 0
        assert snapshot.synthesis.total_audit_rows == 0
        # Eval — no query sets, no runs, no drift.
        assert snapshot.eval.query_set_count == 0
        assert snapshot.eval.most_recent_run is None
        assert snapshot.eval.drift_index is None
        # Suppression — zero leaves.
        assert snapshot.suppression.suppressed_leaves == 0


# ---------------------------------------------------------------------------
# Embeddings probe
# ---------------------------------------------------------------------------


class TestEmbeddingsProbe:
    def test_active_profile_surfaced(self, db: sqlite3.Connection) -> None:
        _seed_active_profile(db, model_name="voyage-3-large", dim=1024)
        snapshot = get_v41_health_snapshot(db)
        assert snapshot.embeddings.active_profile is not None
        assert snapshot.embeddings.active_profile.model_name == "voyage-3-large"
        assert snapshot.embeddings.active_profile.dim == 1024

    def test_archived_profile_filtered_out(self, db: sqlite3.Connection) -> None:
        """LCM Wave-11 reviewer P2: ``archive_after IS NOT NULL`` rows are skipped.

        Semantic retrieval skips archived profiles; health was reporting
        a profile semantic search wouldn't use during model cutover.
        """
        _seed_active_profile(
            db,
            model_name="voyage-2-old",
            archive_after="2025-01-01T00:00:00Z",
        )
        snapshot = get_v41_health_snapshot(db)
        assert snapshot.embeddings.active_profile is None

    def test_most_recent_profile_wins(self, db: sqlite3.Connection) -> None:
        """Two non-archived profiles → ``ORDER BY registered_at DESC LIMIT 1``."""
        db.execute(
            """
            INSERT INTO lcm_embedding_profile (model_name, dim, active, registered_at)
            VALUES ('older', 512, 1, '2024-01-01T00:00:00Z')
            """
        )
        db.execute(
            """
            INSERT INTO lcm_embedding_profile (model_name, dim, active, registered_at)
            VALUES ('newer', 1024, 1, '2025-01-01T00:00:00Z')
            """
        )
        snapshot = get_v41_health_snapshot(db)
        assert snapshot.embeddings.active_profile is not None
        assert snapshot.embeddings.active_profile.model_name == "newer"

    def test_over_cap_pending_counts_oversized_leaves(self, db: sqlite3.Connection) -> None:
        """LCM Wave-4 Auditor #15 P1: over-cap leaves not in pending OR embedded buckets.

        Without this counter, ``pending=0`` could lie about coverage —
        operator wouldn't see the permanent blind spot for huge leaves.
        """
        from lossless_hermes.voyage.client import MAX_TOKENS_PER_EMBED_DOC

        _seed_active_profile(db, model_name="voyage-3", dim=1024)
        _seed_conversation(db)
        # One leaf BELOW the cap (counts as normal pending, NOT over-cap).
        _seed_summary(db, summary_id="s-small", token_count=100)
        # One leaf ABOVE the cap (the over-cap bucket).
        _seed_summary(
            db,
            summary_id="s-huge",
            token_count=MAX_TOKENS_PER_EMBED_DOC + 1_000,
        )
        snapshot = get_v41_health_snapshot(db)
        assert snapshot.embeddings.over_cap_pending == 1

    def test_over_cap_excludes_suppressed_leaves(self, db: sqlite3.Connection) -> None:
        """Suppressed leaves should NOT count as over-cap pending.

        Suppression is a deliberate operator action; over-cap pending is
        a coverage-warning surface only — suppressed leaves are already
        out of scope for backfill.
        """
        from lossless_hermes.voyage.client import MAX_TOKENS_PER_EMBED_DOC

        _seed_active_profile(db, model_name="voyage-3", dim=1024)
        _seed_conversation(db)
        _seed_summary(
            db,
            summary_id="s-huge-suppressed",
            token_count=MAX_TOKENS_PER_EMBED_DOC + 1_000,
            suppressed_at="2025-05-13T12:00:00Z",
        )
        snapshot = get_v41_health_snapshot(db)
        assert snapshot.embeddings.over_cap_pending == 0


# ---------------------------------------------------------------------------
# Workers probe
# ---------------------------------------------------------------------------


class TestWorkersProbe:
    def test_idle_when_no_lock(self, db: sqlite3.Connection) -> None:
        snapshot = get_v41_health_snapshot(db)
        kinds = {w.job_kind: w for w in snapshot.workers}
        assert kinds["embedding-backfill"].active is False
        assert kinds["embedding-backfill"].worker_id is None

    def test_active_lock_surfaced(self, db: sqlite3.Connection) -> None:
        _seed_worker_lock(
            db,
            job_kind="embedding-backfill",
            worker_id="worker-eva.local-pid-1234",
            expires_at_offset_s=60,
        )
        snapshot = get_v41_health_snapshot(db)
        kinds = {w.job_kind: w for w in snapshot.workers}
        embedding = kinds["embedding-backfill"]
        assert embedding.active is True
        assert embedding.worker_id == "worker-eva.local-pid-1234"
        assert embedding.acquired_at is not None
        assert embedding.expires_at is not None
        assert embedding.expired is False

    def test_workers_held_by_other_host(self, db: sqlite3.Connection) -> None:
        """Per AC line 68: lock held by a hypothetical second host surfaces.

        Health is host-agnostic — any active row in ``lcm_worker_lock``
        is reported regardless of which process / host id holds it.
        """
        _seed_worker_lock(
            db,
            job_kind="extraction",
            worker_id="worker-other-host.example.com-pid-9999-start-0-nonce-abc",
            metadata="from-second-host",
        )
        snapshot = get_v41_health_snapshot(db)
        kinds = {w.job_kind: w for w in snapshot.workers}
        extraction = kinds["extraction"]
        assert extraction.active is True
        assert "other-host" in (extraction.worker_id or "")
        # Surfaced even though we're "this host" — no host filter applied.

    def test_expired_lock_flagged(self, db: sqlite3.Connection) -> None:
        """``expires_at <= now`` → ``expired=True`` (crashed-worker signal)."""
        # Seed an expired lock by setting expires_at in the past.
        db.execute(
            """
            INSERT INTO lcm_worker_lock
                (job_kind, worker_id, acquired_at, expires_at, last_heartbeat_at)
            VALUES (?, ?, datetime('now', '-180 seconds'),
                    datetime('now', '-90 seconds'),
                    datetime('now', '-180 seconds'))
            """,
            ("condensation", "crashed-worker-1"),
        )
        snapshot = get_v41_health_snapshot(db)
        kinds = {w.job_kind: w for w in snapshot.workers}
        cond = kinds["condensation"]
        assert cond.active is True
        assert cond.expired is True

    def test_one_row_per_known_kind(self, db: sqlite3.Connection) -> None:
        """Snapshot includes every kind in :data:`WORKER_JOB_KINDS`."""
        from lossless_hermes.concurrency.model import WORKER_JOB_KINDS

        snapshot = get_v41_health_snapshot(db)
        kinds = {w.job_kind for w in snapshot.workers}
        assert kinds == set(WORKER_JOB_KINDS)


# ---------------------------------------------------------------------------
# Synthesis probe
# ---------------------------------------------------------------------------


class TestSynthesisProbe:
    def test_counts_active_prompts(self, db: sqlite3.Connection) -> None:
        _seed_prompt(db, prompt_id="p-1", memory_type="episodic-leaf")
        _seed_prompt(db, prompt_id="p-2", memory_type="episodic-condensed")
        _seed_prompt(db, prompt_id="p-3", memory_type="episodic-leaf", pass_kind="verify_fidelity")
        snapshot = get_v41_health_snapshot(db)
        assert snapshot.synthesis.active_prompt_count == 3
        # Two distinct memory_types across the three prompts.
        assert snapshot.synthesis.distinct_memory_type_count == 2

    def test_recent_synthesis_runs_7d_window(self, db: sqlite3.Connection) -> None:
        _seed_prompt(db, prompt_id="p-1")
        _seed_conversation(db)
        _seed_summary(db, summary_id="s-1")
        _seed_synthesis_audit(db, audit_id="a-recent", prompt_id="p-1", summary_id="s-1")
        _seed_synthesis_audit(
            db,
            audit_id="a-old",
            prompt_id="p-1",
            summary_id="s-1",
            ran_at_offset="-10 days",
        )
        snapshot = get_v41_health_snapshot(db)
        # Only one row is within the 7-day window.
        assert snapshot.synthesis.recent_synthesis_runs_7d == 1
        # But total count includes both.
        assert snapshot.synthesis.total_audit_rows == 2

    def test_stale_started_rows_counted(self, db: sqlite3.Connection) -> None:
        """LCM Wave-4 Auditor #15 P1: orphaned ``status='started'`` > 1h surfaced."""
        _seed_prompt(db, prompt_id="p-1")
        _seed_conversation(db)
        _seed_summary(db, summary_id="s-1")
        _seed_synthesis_audit(
            db,
            audit_id="a-orphan",
            prompt_id="p-1",
            summary_id="s-1",
            status="started",
            ran_at_offset="-2 hours",
        )
        snapshot = get_v41_health_snapshot(db)
        assert snapshot.synthesis.started_rows_older_than_1h == 1

    def test_stale_completed_failed_30d_counted(self, db: sqlite3.Connection) -> None:
        _seed_prompt(db, prompt_id="p-1")
        _seed_conversation(db)
        _seed_summary(db, summary_id="s-1")
        _seed_synthesis_audit(
            db,
            audit_id="a-old-completed",
            prompt_id="p-1",
            summary_id="s-1",
            status="completed",
            ran_at_offset="-40 days",
        )
        _seed_synthesis_audit(
            db,
            audit_id="a-old-failed",
            prompt_id="p-1",
            summary_id="s-1",
            status="failed",
            ran_at_offset="-35 days",
        )
        snapshot = get_v41_health_snapshot(db)
        assert snapshot.synthesis.completed_or_failed_older_than_30d == 2


# ---------------------------------------------------------------------------
# Eval probe
# ---------------------------------------------------------------------------


class TestEvalProbe:
    def test_query_set_count(self, db: sqlite3.Connection) -> None:
        _seed_eval_query_set(db, query_set_id="qs-1")
        _seed_eval_query_set(db, query_set_id="qs-2")
        snapshot = get_v41_health_snapshot(db)
        assert snapshot.eval.query_set_count == 2

    def test_most_recent_run_decoded(self, db: sqlite3.Connection) -> None:
        _seed_eval_query_set(db)
        _seed_eval_run(db, run_id="run-1", recall=0.81, mode="fts_only")
        _seed_eval_run(
            db,
            run_id="run-2",
            recall=0.84,
            mode="hybrid",
            ran_at_offset="+1 second",
        )
        snapshot = get_v41_health_snapshot(db)
        assert snapshot.eval.most_recent_run is not None
        # ORDER BY ran_at DESC — run-2 should win.
        assert snapshot.eval.most_recent_run.run_id == "run-2"
        assert snapshot.eval.most_recent_run.mode == "hybrid"
        assert snapshot.eval.most_recent_run.recall_score == pytest.approx(0.84)

    def test_mode_unknown_for_malformed_envelope(self, db: sqlite3.Connection) -> None:
        """Malformed ``per_query_scores`` JSON → ``mode='unknown'``, no crash."""
        _seed_eval_query_set(db)
        # Insert with raw invalid JSON.
        db.execute(
            """
            INSERT INTO lcm_eval_run
                (run_id, query_set_id, prompt_bundle_version, ran_at,
                 retrieval_recall_score, synthesis_quality_score,
                 per_query_scores, judge_models, trigger)
            VALUES ('run-broken', 'qs-1', 1, datetime('now'), 0.5, 0.5,
                    'not-valid-json', '[]', 'manual')
            """
        )
        snapshot = get_v41_health_snapshot(db)
        assert snapshot.eval.most_recent_run is not None
        assert snapshot.eval.most_recent_run.mode == "unknown"

    def test_drift_index_surfaced(self, db: sqlite3.Connection) -> None:
        _seed_eval_query_set(db)
        _seed_eval_drift(db, delta=-0.025)
        snapshot = get_v41_health_snapshot(db)
        assert snapshot.eval.drift_index == pytest.approx(-0.025)

    def test_drift_index_none_when_no_baseline(self, db: sqlite3.Connection) -> None:
        snapshot = get_v41_health_snapshot(db)
        assert snapshot.eval.drift_index is None


# ---------------------------------------------------------------------------
# Suppression probe — AC line 65: count by suppressed_at
# ---------------------------------------------------------------------------


class TestSuppressionProbe:
    def test_counts_suppressed_leaves(self, db: sqlite3.Connection) -> None:
        _seed_conversation(db)
        _seed_summary(db, summary_id="s-active", kind="leaf")
        _seed_summary(
            db,
            summary_id="s-suppressed-1",
            kind="leaf",
            suppressed_at="2025-05-13T10:00:00Z",
        )
        _seed_summary(
            db,
            summary_id="s-suppressed-2",
            kind="leaf",
            suppressed_at="2025-05-13T11:00:00Z",
        )
        snapshot = get_v41_health_snapshot(db)
        assert snapshot.suppression.suppressed_leaves == 2

    def test_only_leaves_counted_not_condensed(self, db: sqlite3.Connection) -> None:
        _seed_conversation(db)
        _seed_summary(
            db,
            summary_id="s-cond-suppressed",
            kind="condensed",
            suppressed_at="2025-05-13T10:00:00Z",
        )
        snapshot = get_v41_health_snapshot(db)
        # Suppression probe filters ``kind = 'leaf'``.
        assert snapshot.suppression.suppressed_leaves == 0


# ---------------------------------------------------------------------------
# AC: missing tables → unavailable (probe returns sentinels, no raise)
# ---------------------------------------------------------------------------


class TestMissingTableUnavailable:
    """Per AC: ``Every probe handles missing tables via has_table guard,
    never raises``. We simulate "table dropped after migration" by
    issuing DROP statements; the probe should still return a snapshot.
    """

    def test_missing_lcm_eval_run_does_not_raise(self, db: sqlite3.Connection) -> None:
        """Drops ``lcm_eval_run`` and confirms eval probe degrades gracefully."""
        db.execute("DROP TABLE lcm_eval_run")
        # Must not raise:
        snapshot = get_v41_health_snapshot(db)
        # Eval probe degrades: most_recent_run becomes None (the table
        # query raises sqlite3.OperationalError; the except clause maps
        # it to None).
        assert snapshot.eval.most_recent_run is None
        # Other probes still return real values.
        assert isinstance(snapshot.embeddings, EmbeddingsHealth)
        assert isinstance(snapshot.workers, tuple)

    def test_missing_lcm_synthesis_audit_does_not_raise(self, db: sqlite3.Connection) -> None:
        db.execute("DROP TABLE lcm_synthesis_audit")
        snapshot = get_v41_health_snapshot(db)
        assert snapshot.synthesis.total_audit_rows == 0
        assert snapshot.synthesis.recent_synthesis_runs_7d == 0
        assert snapshot.synthesis.started_rows_older_than_1h == 0
        assert snapshot.synthesis.completed_or_failed_older_than_30d == 0

    def test_missing_lcm_embedding_profile_does_not_raise(self, db: sqlite3.Connection) -> None:
        # Must drop the meta table FIRST (foreign-key dependency).
        db.execute("DROP TABLE lcm_embedding_meta")
        db.execute("DROP TABLE lcm_embedding_profile")
        snapshot = get_v41_health_snapshot(db)
        assert snapshot.embeddings.active_profile is None
        assert snapshot.embeddings.pending_backfill == 0


# ---------------------------------------------------------------------------
# AC: extract_mode utility
# ---------------------------------------------------------------------------


class TestExtractMode:
    def test_valid_envelope(self) -> None:
        assert _extract_mode('{"mode": "hybrid"}') == "hybrid"

    def test_missing_mode_field(self) -> None:
        assert _extract_mode('{"other": 1}') == "unknown"

    def test_empty_mode_value(self) -> None:
        assert _extract_mode('{"mode": ""}') == "unknown"

    def test_non_string_mode_value(self) -> None:
        assert _extract_mode('{"mode": 1}') == "unknown"

    def test_malformed_json(self) -> None:
        assert _extract_mode("not-json") == "unknown"

    def test_empty_string(self) -> None:
        assert _extract_mode("") == "unknown"

    def test_non_object_json(self) -> None:
        # Top-level array — has no ``.mode`` field.
        assert _extract_mode("[1, 2, 3]") == "unknown"

    def test_none_input(self) -> None:
        """Passing :data:`None` (NULL from SQL) → ``"unknown"``."""
        assert _extract_mode(None) == "unknown"


# ---------------------------------------------------------------------------
# AC: snapshot is a frozen dataclass — readers can rely on attribute access
# ---------------------------------------------------------------------------


class TestSnapshotShape:
    def test_snapshot_is_typed_record(self, db: sqlite3.Connection) -> None:
        snapshot = get_v41_health_snapshot(db)
        assert isinstance(snapshot, V41HealthSnapshot)
        assert isinstance(snapshot.embeddings, EmbeddingsHealth)
        assert isinstance(snapshot.synthesis, SynthesisHealth)
        assert isinstance(snapshot.eval, EvalHealth)
        assert isinstance(snapshot.suppression, SuppressionHealth)
        for w in snapshot.workers:
            assert isinstance(w, WorkerStatus)

    def test_active_profile_is_typed_record(self, db: sqlite3.Connection) -> None:
        _seed_active_profile(db)
        snapshot = get_v41_health_snapshot(db)
        assert isinstance(snapshot.embeddings.active_profile, ActiveEmbeddingProfile)

    def test_most_recent_run_is_typed_record(self, db: sqlite3.Connection) -> None:
        _seed_eval_query_set(db)
        _seed_eval_run(db, run_id="run-1")
        snapshot = get_v41_health_snapshot(db)
        assert isinstance(snapshot.eval.most_recent_run, MostRecentEvalRun)
