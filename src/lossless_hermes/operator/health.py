"""Operator-facing v4.1 health snapshot — Epic 08-03.

Ports ``lossless-claw/src/operator/health.ts`` (LCM commit ``1f07fbd``).
Aggregates the operational state of the v4.1 subsystems (embeddings,
workers, synthesis, eval, suppression) into a single typed object so
the ``/lcm health`` command can render it without poking at internals.

### Design rules (verbatim port from TS module-level docs)

* **Pure read-only.** NEVER mutates DB state. Safe to call at any
  latency budget (no LLM calls, no network).
* **Tolerant of "subsystem not initialized yet."** Every section
  handles its own missing-table / missing-row case rather than
  throwing, so the snapshot is meaningful on a fresh DB too. Per
  ``docs/porting-guides/doctor-ops.md`` §"Operator modules" line 308:
  "pure read-only, tolerant of missing tables."
* **vec0 not loaded is reported, not thrown.** Backfill counters
  degrade gracefully (we report the active model's pending count from
  the meta sidecar even when the vec0 table itself is missing).
* **Worker statuses are derived from ``lcm_worker_lock`` row presence**
  — no row means ``(idle)``. Expired locks (``datetime('now') > expires_at``)
  are STILL reported, with an ``expired: true`` flag — operators want
  to see crashed workers, not silently filter them out.

### Wave-N provenance comments preserved (per ADR-029)

This module port preserves four Wave-N fixes from the TS source:

* **Wave-4 Auditor #15 P1** (``getEmbeddingsHealth``) — surface
  over-cap leaves so ``pending=0`` doesn't lie about coverage.
* **Wave-4 Auditor #15 P1** (``getSynthesisHealth``) — total + breakdown
  audit rows so operators see if GC is keeping the table bounded.
* **Wave-5 P2** (``getSynthesisHealth``) — split SUM(CASE...) into
  separate queries so each hits a partial index.
* **Wave-11 reviewer P2** (``readActiveProfile``) — filter
  ``archive_after IS NULL`` to match the semantic-side filter, so
  health doesn't report a profile semantic search won't actually use
  during model cutover.

See:

* ``epics/08-cli-ops/08-03-health.md`` — this issue.
* ``docs/porting-guides/doctor-ops.md`` §"Operator modules" line 308
  — pure-read-only / tolerant-of-missing-tables contract.
* ``docs/adr/029-wave-fix-provenance.md`` — Wave-N comment protocol.
* ``lossless-claw/src/operator/health.ts:1-442`` — TS source pinned
  at commit ``1f07fbd``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Optional

from lossless_hermes.concurrency.model import WORKER_JOB_KINDS
from lossless_hermes.concurrency.worker_lock import lock_info
from lossless_hermes.db.connection import vec0_version
from lossless_hermes.embeddings.backfill import count_pending_docs
from lossless_hermes.synthesis.prompt_registry import list_active_prompts
from lossless_hermes.voyage.client import MAX_TOKENS_PER_EMBED_DOC

_log = logging.getLogger("lossless_hermes.operator.health")

__all__ = [
    "ActiveEmbeddingProfile",
    "EmbeddingsHealth",
    "EvalHealth",
    "MostRecentEvalRun",
    "SuppressionHealth",
    "SynthesisHealth",
    "V41HealthSnapshot",
    "WorkerStatus",
    "get_v41_health_snapshot",
]


# ---------------------------------------------------------------------------
# Snapshot dataclasses (TS interface parity)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ActiveEmbeddingProfile:
    """The currently-active embedding profile.

    Ports the TS ``ActiveEmbeddingProfile`` interface
    (``operator/health.ts:38-42``).
    """

    model_name: str
    """The Voyage / embedding-provider model identifier."""

    dim: int
    """Vector dimension. Matches ``lcm_embedding_profile.dim``."""

    registered_at: str
    """ISO-8601 timestamp from the profile row (empty string if missing)."""


@dataclass(frozen=True, slots=True)
class EmbeddingsHealth:
    """Embedding-subsystem health snapshot.

    Ports the TS ``EmbeddingsHealth`` interface
    (``operator/health.ts:44-64``). All counts default to ``0`` if the
    underlying tables are missing / queries fail.
    """

    active_profile: Optional[ActiveEmbeddingProfile]
    """Active model, or :data:`None` if no profile is registered yet."""

    vec0_version: Optional[str]
    """vec0 extension version (e.g. ``"v0.1.6"``) or :data:`None` if not loaded."""

    pending_backfill: int
    """Pending backfill count for the active model (0 if none registered)."""

    embedded_count: int
    """Count of un-archived embedding rows across all models."""

    over_cap_pending: int
    """Leaves with ``token_count > MAX_TOKENS_PER_EMBED_DOC`` that backfill cannot embed.

    Wave-4 Auditor #15 P1: ``count_pending_docs`` filters via
    ``BETWEEN min AND max``, so over-cap leaves are NOT counted as
    pending OR as backfilled. Without surfacing them here,
    ``/lcm health`` could report ``pending=0`` while semantic coverage
    has permanent blind spots. Operator can re-summarize them at a
    lower cap to bring them into range.
    """


@dataclass(frozen=True, slots=True)
class WorkerStatus:
    """Per-job-kind worker status snapshot.

    Ports the TS ``WorkerStatus`` interface (``operator/health.ts:66-79``).
    """

    job_kind: str
    """One of :data:`lossless_hermes.concurrency.model.WORKER_JOB_KINDS`."""

    active: bool
    """``True`` if a row exists for this job in ``lcm_worker_lock``."""

    worker_id: Optional[str]
    """The lock holder's worker id; :data:`None` when ``active=False``."""

    acquired_at: Optional[str]
    """ISO-8601 timestamp; :data:`None` when ``active=False``."""

    expires_at: Optional[str]
    """ISO-8601 timestamp; :data:`None` when ``active=False``."""

    expired: bool
    """``True`` if a row exists but ``expires_at <= now``.

    Indicates the worker died without releasing — operators want to
    see these (suggests a crashed worker another process should
    reclaim soon).
    """


@dataclass(frozen=True, slots=True)
class SynthesisHealth:
    """Synthesis-subsystem health snapshot.

    Ports the TS ``SynthesisHealth`` interface (``operator/health.ts:81-97``).
    Carries the audit-row breakdown introduced by Wave-4 Auditor #15 P1
    so operators can see whether the GC is keeping the table bounded.
    """

    active_prompt_count: int
    """Number of active prompts in ``lcm_prompt_registry``."""

    distinct_memory_type_count: int
    """Distinct ``memory_type`` values across the active prompts."""

    recent_synthesis_runs_7d: int
    """Synthesis runs in ``lcm_synthesis_audit`` within the last 7 days."""

    total_audit_rows: int
    """Total ``lcm_synthesis_audit`` row count.

    Wave-4 Auditor #15 P1: pre-fix the 7-day window could read 0 while
    the table held millions of stale rows — operators couldn't see the
    GC was broken.
    """

    started_rows_older_than_1h: int
    """Orphaned ``status='started'`` rows older than 1 hour (GC backlog)."""

    completed_or_failed_older_than_30d: int
    """Stale terminal-state rows older than 30 days (GC backlog)."""


@dataclass(frozen=True, slots=True)
class MostRecentEvalRun:
    """Single most-recent ``lcm_eval_run`` row, denormalized for rendering.

    Ports the inline TS object type
    (``operator/health.ts:103-110``).
    """

    run_id: str
    query_set_id: str
    mode: str
    """Decoded from the ``per_query_scores`` envelope (``.mode``);
    ``"unknown"`` if malformed."""
    recall_score: float
    """``retrieval_recall_score`` from the row."""


@dataclass(frozen=True, slots=True)
class EvalHealth:
    """Eval-subsystem health snapshot.

    Ports the TS ``EvalHealth`` interface (``operator/health.ts:99-116``).
    """

    query_set_count: int
    """Total registered query sets in ``lcm_eval_query_set``."""

    most_recent_run: Optional[MostRecentEvalRun]
    """Most-recent run summary, or :data:`None` if none."""

    drift_index: Optional[float]
    """Latest ``cumulative_delta`` from ``lcm_eval_drift``, or :data:`None`
    if no baseline has been recorded yet."""


@dataclass(frozen=True, slots=True)
class SuppressionHealth:
    """Suppression-subsystem health snapshot.

    Ports the TS ``SuppressionHealth`` interface (``operator/health.ts:118-121``).
    """

    suppressed_leaves: int
    """Count of leaves with ``suppressed_at IS NOT NULL``."""


@dataclass(frozen=True, slots=True)
class V41HealthSnapshot:
    """Aggregate v4.1 health snapshot.

    Ports the TS ``V41HealthSnapshot`` interface (``operator/health.ts:123-129``).
    """

    embeddings: EmbeddingsHealth
    workers: tuple[WorkerStatus, ...]
    synthesis: SynthesisHealth
    eval: EvalHealth
    suppression: SuppressionHealth


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def get_v41_health_snapshot(db: sqlite3.Connection) -> V41HealthSnapshot:
    """Read the v4.1 health snapshot.

    Pure read-only; safe to call at any latency. Each section helper
    tolerates its own missing pieces (per module docstring contract).
    Ports ``operator/health.ts:136-144`` ``getV41HealthSnapshot``.

    Args:
        db: Open :class:`sqlite3.Connection`. The schema is allowed to
            be partial (fresh DB) — every probe defends against the
            ``sqlite3.OperationalError`` raised by ``SELECT FROM
            <missing-table>``.

    Returns:
        :class:`V41HealthSnapshot` — see the dataclass for field-level
        contracts. Every field has a defined value; missing tables map
        to zero counts / :data:`None` references rather than raising.
    """
    return V41HealthSnapshot(
        embeddings=_get_embeddings_health(db),
        workers=tuple(_get_worker_statuses(db)),
        synthesis=_get_synthesis_health(db),
        eval=_get_eval_health(db),
        suppression=_get_suppression_health(db),
    )


# ---------------------------------------------------------------------------
# Section helpers — each tolerates its own missing pieces
# ---------------------------------------------------------------------------


def _get_embeddings_health(db: sqlite3.Connection) -> EmbeddingsHealth:
    """Render the embeddings section. Ports ``operator/health.ts:148-218``."""
    active_profile = _read_active_profile(db)
    vec0 = vec0_version(db)

    pending_backfill = 0
    if active_profile is not None:
        # count_pending_docs only inspects lcm_embedding_meta + summaries —
        # it does NOT need vec0 to be loaded, so the count is meaningful
        # even when sqlite-vec is missing (operator wants to know the
        # backlog).
        try:
            pending_backfill = count_pending_docs(
                db,
                model_name=active_profile.model_name,
                embedded_kind="summary",
            )
        except sqlite3.Error:
            pending_backfill = 0

    embedded_count = 0
    try:
        row = db.execute(
            "SELECT COUNT(*) AS n FROM lcm_embedding_meta WHERE archived = 0"
        ).fetchone()
        embedded_count = int(row[0]) if row and row[0] is not None else 0
    except sqlite3.Error:
        embedded_count = 0

    # LCM Wave-4 Auditor #15 P1 (2026-05-14): count over-cap leaves explicitly.
    # count_pending_docs filters BETWEEN min AND max — so leaves with
    # token_count > MAX_TOKENS_PER_EMBED_DOC are NOT counted as pending OR
    # as backfilled. Surface them so /lcm health doesn't lie about coverage.
    # The hardcoded 30000 in the original TS was the pre-Wave-1 value;
    # Wave-1 dropped MAX_TOKENS_PER_EMBED_DOC to 27000 to absorb Voyage
    # tokenizer inflation. Read the constant rather than hardcoding.
    over_cap_pending = 0
    if active_profile is not None:
        try:
            row = db.execute(
                """
                SELECT COUNT(*) AS n FROM summaries s
                  WHERE s.kind = 'leaf'
                    AND s.suppressed_at IS NULL
                    AND s.token_count > ?
                    AND NOT EXISTS (
                      SELECT 1 FROM lcm_embedding_meta m
                        WHERE m.embedded_id = s.summary_id
                          AND m.embedded_kind = 'summary'
                          AND m.embedding_model = ?
                          AND m.archived = 0
                    )
                """,
                (MAX_TOKENS_PER_EMBED_DOC, active_profile.model_name),
            ).fetchone()
            over_cap_pending = int(row[0]) if row and row[0] is not None else 0
        except sqlite3.Error:
            over_cap_pending = 0

    return EmbeddingsHealth(
        active_profile=active_profile,
        vec0_version=vec0,
        pending_backfill=pending_backfill,
        embedded_count=embedded_count,
        over_cap_pending=over_cap_pending,
    )


def _read_active_profile(db: sqlite3.Connection) -> Optional[ActiveEmbeddingProfile]:
    """Return the active (non-archived) embedding profile.

    Ports ``operator/health.ts:220-243`` ``readActiveProfile``.
    """
    try:
        # LCM Wave-11 reviewer P2 (2026-05-14): previously selected on
        # `active = 1` alone, ignoring `archive_after IS NOT NULL`.
        # Semantic retrieval correctly skips archived profiles
        # (embeddings/semantic_search.py), so health was reporting a
        # profile semantic search would not actually use during model
        # cutover. Match the semantic-side filter exactly.
        row = db.execute(
            """
            SELECT model_name, dim, registered_at FROM lcm_embedding_profile
              WHERE active = 1 AND archive_after IS NULL
              ORDER BY registered_at DESC LIMIT 1
            """,
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    model_name = row[0]
    dim = row[1]
    registered_at = row[2]
    if not model_name or dim is None:
        return None
    return ActiveEmbeddingProfile(
        model_name=str(model_name),
        dim=int(dim),
        registered_at=str(registered_at) if registered_at is not None else "",
    )


def _get_worker_statuses(db: sqlite3.Connection) -> list[WorkerStatus]:
    """Render the workers section. Ports ``operator/health.ts:245-277``.

    Compute ``now`` inside SQL for an apples-to-apples comparison with
    ``expires_at`` strings (matches the TS implementation; both sides
    are SQL ``datetime('now')`` output).
    """
    now = ""
    try:
        now_row = db.execute("SELECT datetime('now') AS now").fetchone()
        now = str(now_row[0]) if now_row and now_row[0] is not None else ""
    except sqlite3.Error:
        now = ""

    result: list[WorkerStatus] = []
    for job_kind in WORKER_JOB_KINDS:
        try:
            info = lock_info(db, job_kind)
        except sqlite3.Error:
            # Table missing (lcm_worker_lock not created yet) → report idle.
            info = None
        if info is None:
            result.append(
                WorkerStatus(
                    job_kind=job_kind,
                    active=False,
                    worker_id=None,
                    acquired_at=None,
                    expires_at=None,
                    expired=False,
                )
            )
            continue
        # Lexicographic compare on ISO-8601 strings is correct here
        # (matches the SQLite acquire_lock / heartbeat_lock comparisons).
        expired = bool(now) and info.expires_at <= now
        result.append(
            WorkerStatus(
                job_kind=job_kind,
                active=True,
                worker_id=info.worker_id,
                acquired_at=info.acquired_at,
                expires_at=info.expires_at,
                expired=expired,
            )
        )
    return result


def _get_synthesis_health(db: sqlite3.Connection) -> SynthesisHealth:
    """Render the synthesis section. Ports ``operator/health.ts:279-354``."""
    try:
        active_prompts = list_active_prompts(db)
    except sqlite3.Error:
        active_prompts = []

    distinct_memory_types: set[str] = set()
    for p in active_prompts:
        distinct_memory_types.add(p.memory_type)

    # LCM Wave-5 P2 (2026-05-14): split into separate queries so each
    # hits a partial index (lcm_synthesis_audit_started_gc_idx for
    # stale-started, lcm_synthesis_audit_completed_gc_idx for stale-done;
    # the 7-day + total queries scan via primary key but are O(n)
    # bounded). Previously a single SELECT with multiple SUM(CASE...)
    # couldn't use any partial index → O(n) full table scan →
    # /lcm health latency degraded precisely under the "millions of
    # stale rows" condition this is meant to surface.
    recent_runs = 0
    total_audit_rows = 0
    started_rows_older_than_1h = 0
    completed_or_failed_older_than_30d = 0

    try:
        total_row = db.execute("SELECT COUNT(*) AS n FROM lcm_synthesis_audit").fetchone()
        total_audit_rows = int(total_row[0]) if total_row and total_row[0] is not None else 0
    except sqlite3.Error:
        # Table may not exist yet.
        pass

    try:
        recent_row = db.execute(
            """
            SELECT COUNT(*) AS n FROM lcm_synthesis_audit
              WHERE ran_at >= datetime('now', '-7 days')
            """
        ).fetchone()
        recent_runs = int(recent_row[0]) if recent_row and recent_row[0] is not None else 0
    except sqlite3.Error:
        pass

    try:
        # Hits lcm_synthesis_audit_started_gc_idx.
        stale_started_row = db.execute(
            """
            SELECT COUNT(*) AS n FROM lcm_synthesis_audit
              WHERE status = 'started'
                AND ran_at < datetime('now', '-1 hour')
            """
        ).fetchone()
        started_rows_older_than_1h = (
            int(stale_started_row[0])
            if stale_started_row and stale_started_row[0] is not None
            else 0
        )
    except sqlite3.Error:
        pass

    try:
        # Hits lcm_synthesis_audit_completed_gc_idx.
        stale_done_row = db.execute(
            """
            SELECT COUNT(*) AS n FROM lcm_synthesis_audit
              WHERE status IN ('completed', 'failed')
                AND ran_at < datetime('now', '-30 days')
            """
        ).fetchone()
        completed_or_failed_older_than_30d = (
            int(stale_done_row[0]) if stale_done_row and stale_done_row[0] is not None else 0
        )
    except sqlite3.Error:
        pass

    return SynthesisHealth(
        active_prompt_count=len(active_prompts),
        distinct_memory_type_count=len(distinct_memory_types),
        recent_synthesis_runs_7d=recent_runs,
        total_audit_rows=total_audit_rows,
        started_rows_older_than_1h=started_rows_older_than_1h,
        completed_or_failed_older_than_30d=completed_or_failed_older_than_30d,
    )


def _get_eval_health(db: sqlite3.Connection) -> EvalHealth:
    """Render the eval section. Ports ``operator/health.ts:356-411``."""
    query_set_count = 0
    try:
        row = db.execute("SELECT COUNT(*) AS n FROM lcm_eval_query_set").fetchone()
        query_set_count = int(row[0]) if row and row[0] is not None else 0
    except sqlite3.Error:
        query_set_count = 0

    most_recent_run: Optional[MostRecentEvalRun] = None
    try:
        row = db.execute(
            """
            SELECT run_id, query_set_id, retrieval_recall_score, per_query_scores
              FROM lcm_eval_run
              ORDER BY ran_at DESC, run_id DESC
              LIMIT 1
            """
        ).fetchone()
        if row is not None and row[0] is not None and row[1] is not None:
            most_recent_run = MostRecentEvalRun(
                run_id=str(row[0]),
                query_set_id=str(row[1]),
                mode=_extract_mode(row[3]),
                recall_score=float(row[2]) if row[2] is not None else 0.0,
            )
    except sqlite3.Error:
        most_recent_run = None

    drift_index: Optional[float] = None
    try:
        row = db.execute(
            """
            SELECT cumulative_delta FROM lcm_eval_drift
              ORDER BY computed_at DESC, drift_id DESC
              LIMIT 1
            """
        ).fetchone()
        if row is not None and row[0] is not None:
            drift_index = float(row[0])
    except sqlite3.Error:
        drift_index = None

    return EvalHealth(
        query_set_count=query_set_count,
        most_recent_run=most_recent_run,
        drift_index=drift_index,
    )


def _extract_mode(envelope_json: object) -> str:
    """Decode ``.mode`` from a ``per_query_scores`` envelope.

    Ports ``operator/health.ts:413-421`` ``extractMode``. Returns
    ``"unknown"`` if the input is missing, not a string, parses to
    non-object JSON, or has no ``mode`` field / a non-string ``mode``.
    """
    if not envelope_json or not isinstance(envelope_json, str):
        return "unknown"
    try:
        parsed = json.loads(envelope_json)
    except (json.JSONDecodeError, ValueError):
        return "unknown"
    if not isinstance(parsed, dict):
        return "unknown"
    mode = parsed.get("mode")
    if isinstance(mode, str) and mode:
        return mode
    return "unknown"


def _get_suppression_health(db: sqlite3.Connection) -> SuppressionHealth:
    """Render the suppression section. Ports ``operator/health.ts:423-442``.

    Note: the TS source's ``pendingPurgeRebuilds`` counter was REMOVED
    in the first-principles pass (TS comment at line 437-439). The
    hard-delete drainer + queue schema was preserved in deferred-features
    draft PR (#616); when that ships, restore the counter here.
    """
    suppressed_leaves = 0
    try:
        row = db.execute(
            """
            SELECT COUNT(*) AS n FROM summaries
              WHERE suppressed_at IS NOT NULL AND kind = 'leaf'
            """
        ).fetchone()
        suppressed_leaves = int(row[0]) if row and row[0] is not None else 0
    except sqlite3.Error:
        suppressed_leaves = 0

    return SuppressionHealth(suppressed_leaves=suppressed_leaves)
