"""Eval run recording + drift — LCM v4.1 §11 / D.03.

Ports ``lossless-claw/src/eval/run.ts`` (LCM commit ``1f07fbd`` on
branch ``pr-613``, 376 LOC TS → ~470 LOC Python with docstrings).

Records eval invocations into ``lcm_eval_run`` (one row per
``(query_set_id, mode, run)`` triple) and computes drift vs the
most-recent prior run of the same ``(query_set_id, mode)``.

### Schema gaps (documented; not patched here — TS source ``run.ts:8-34``)

1. ``lcm_eval_run`` has no ``mode`` column. The architecture spec asks
   us to compare runs of the SAME ``(query_set, mode)`` pair, so we
   serialize ``mode`` into the ``per_query_scores`` JSON envelope
   (``{"mode": "...", "perQuery": [...]}``). :func:`_select_prior_run`
   parses this back out to find the right prior run. A schema migration
   that adds ``mode TEXT NOT NULL`` would let us index this directly
   and is recommended for v4.2.

2. ``lcm_eval_drift`` is aggregate-only — ``cumulative_delta`` +
   ``window_runs``. The task spec asks for per-query drift; we surface
   that detail via the function return value (``details``) but only
   PERSIST the aggregate. A future migration could add a
   ``lcm_eval_drift_per_query`` table.

3. ``lcm_eval_run.prompt_bundle_version`` is NOT NULL with no schema
   default. Callers that don't yet wire the prompt-registry version
   into here can pass any positive integer (we default to 1 if
   ``prompt_bundle_version`` is omitted on the :class:`EvalRunRecord`).

4. ``lcm_eval_run.retrieval_recall_score`` and
   ``synthesis_quality_score`` are both NOT NULL. If the caller provides
   only ``recall_report`` (no ``quality_report``), the quality score
   is recorded as 0 (and the ``hasQuality`` flag in the
   ``per_query_scores`` envelope marks which side was actually
   measured).

See:

* ``epics/08-cli-ops/08-13-eval-runner.md`` — this issue.
* ``lossless-claw/src/eval/run.ts:1-376`` — TS source.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass
from typing import Any, Literal, Optional

from lossless_hermes.eval.query_set import (
    QuerySetIdentity,
    encode_query_set_id,
)
from lossless_hermes.eval.recall import RecallReport

__all__ = [
    "DriftDetail",
    "DriftSummary",
    "EvalRunRecord",
    "EvalTrigger",
    "compute_drift",
    "record_eval_run",
]


EvalTrigger = Literal["manual", "prompt-update", "model-update", "ci", "nightly"]
"""Ports TS ``EvalTrigger`` (``run.ts:41-46``). Stored on
``lcm_eval_run.trigger``."""


@dataclass(frozen=True, slots=True)
class EvalRunRecord:
    """Inputs to :func:`record_eval_run`.

    Ports TS ``EvalRunRecord`` interface (``run.ts:48-68``).

    Attributes:
        query_set_identity: Identifies which query set this run targets.
        mode: Opaque mode tag (e.g. ``"fts_only"`` / ``"hybrid"`` /
            ``"semantic_only"``). Stored in the JSON envelope; used
            by :func:`compute_drift` to find the prior run.
        recall_report: Optional recall report. If omitted,
            ``retrieval_recall_score`` is recorded as 0.
        quality_report: Optional quality report. **Deferred** — v4.1
            first cut is recall-only. Reserved for future judge
            integration.
        run_id: Optional caller-provided run_id. If omitted we generate
            one (timestamp + random suffix).
        notes: Free-form caller note; stored in the
            ``per_query_scores`` envelope.
        trigger: Defaults to ``"manual"`` if ``None``.
        prompt_bundle_version: Defaults to 1 if ``None``. See SCHEMA
            GAPS §3.
        noise_floor_sd: Optional noise-floor SD from baseline
            calibration. Used by :func:`compute_drift` to threshold
            "drifted" at 2× SD.
    """

    query_set_identity: QuerySetIdentity
    mode: str
    recall_report: Optional[RecallReport] = None
    # Quality reports are deferred — kept as Any for forward compat.
    quality_report: Optional[Any] = None
    run_id: Optional[str] = None
    notes: Optional[str] = None
    trigger: Optional[EvalTrigger] = None
    prompt_bundle_version: Optional[int] = None
    noise_floor_sd: Optional[float] = None


@dataclass(frozen=True, slots=True)
class DriftDetail:
    """Per-query drift entry.

    Ports TS ``DriftDetail`` interface (``run.ts:69-77``).

    Attributes:
        query_id: The query's id.
        prior_score: Score on the prior run; ``None`` if the query
            wasn't in the prior run.
        current_score: Score on the current run; ``None`` if not in
            the current run.
        delta: ``current_score - prior_score``. ``None`` if either
            side is missing.
    """

    query_id: str
    prior_score: Optional[float]
    current_score: Optional[float]
    delta: Optional[float]


@dataclass(frozen=True, slots=True)
class DriftSummary:
    """Aggregate drift summary.

    Ports TS ``DriftSummary`` interface (``run.ts:79-92``).

    Attributes:
        drifted: Number of per-query scores that changed by ≥ noise
            floor (or by any amount if no floor).
        improved: Of those, count that improved (``delta > 0``).
        regressed: Of those, count that regressed (``delta < 0``).
        details: Per-query detail, sorted by absolute delta DESC.
        prior_run_id: ID of the run we compared against; ``None`` if
            no prior run existed.
        cumulative_delta: Aggregate cumulative delta (sum of per-query
            deltas) — written to ``lcm_eval_drift``.
    """

    drifted: int
    improved: int
    regressed: int
    details: tuple[DriftDetail, ...]
    prior_run_id: Optional[str]
    cumulative_delta: float


# ---------------------------------------------------------------------------
# JSON envelope shape
# ---------------------------------------------------------------------------


# Envelope written to lcm_eval_run.per_query_scores. Versioned (v=1).
# Schema:
#   {
#     "v": 1,
#     "mode": str,
#     "notes"?: str,
#     "hasRecall": bool,
#     "hasQuality": bool,
#     "perQuery": { queryId: { "recallRR"?: number, "qualityScore"?: number | null } }
#   }
# Ports TS ``PerQueryScoresEnvelope`` (``run.ts:94-116``).


def _build_envelope(record: EvalRunRecord) -> dict[str, Any]:
    """Ports TS ``buildEnvelope`` (``run.ts:118-144``)."""
    env: dict[str, Any] = {
        "v": 1,
        "mode": record.mode,
        "hasRecall": record.recall_report is not None,
        "hasQuality": record.quality_report is not None,
        "perQuery": {},
    }
    if record.notes is not None:
        env["notes"] = record.notes

    per_query: dict[str, dict[str, Any]] = env["perQuery"]

    if record.recall_report is not None:
        for r in record.recall_report.per_query:
            slot = per_query.setdefault(r.query_id, {})
            slot["recallRR"] = r.reciprocal_rank

    if record.quality_report is not None:
        # Quality reports are deferred — duck-typed access for forward
        # compat. Mirrors TS ``run.ts:135-141``.
        per_query_items = getattr(record.quality_report, "per_query", None)
        if per_query_items is None:
            per_query_items = getattr(record.quality_report, "perQuery", [])
        for r in per_query_items:
            qid = getattr(r, "query_id", None) or getattr(r, "queryId", None)
            mean_score = getattr(r, "mean_score", None)
            if mean_score is None:
                mean_score = getattr(r, "meanScore", None)
            if qid is None:
                continue
            slot = per_query.setdefault(qid, {})
            slot["qualityScore"] = mean_score

    return env


def _generate_run_id() -> str:
    """Generate a run id.

    Ports TS ``generateRunId`` (``run.ts:146-150``):
    ``evalrun_{ts_base36}_{rand_base36}``.
    """
    import time as _time

    ts = _to_base36(int(_time.time() * 1000))
    rand = _to_base36(secrets.randbits(40))[:8].rjust(8, "0")
    return f"evalrun_{ts}_{rand}"


def _generate_drift_id() -> str:
    """Generate a drift id.

    Ports TS ``generateDriftId`` (``run.ts:152-156``):
    ``drift_{ts_base36}_{rand_base36}``.
    """
    import time as _time

    ts = _to_base36(int(_time.time() * 1000))
    rand = _to_base36(secrets.randbits(40))[:8].rjust(8, "0")
    return f"drift_{ts}_{rand}"


def _to_base36(n: int) -> str:
    """Convert a non-negative int to lowercase base-36.

    Matches JS ``Number.prototype.toString(36)``.
    """
    if n == 0:
        return "0"
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out: list[str] = []
    while n > 0:
        out.append(digits[n % 36])
        n //= 36
    return "".join(reversed(out))


def _judge_models_from_quality_report(report: Optional[Any]) -> list[str]:
    """Ports TS ``judgeModelsFromQualityReport`` (``run.ts:158-165``).

    Quality reports are deferred — duck-typed access for forward
    compat. Returns sorted unique judge IDs or empty list.
    """
    if report is None:
        return []
    per_query_items = getattr(report, "per_query", None)
    if per_query_items is None:
        per_query_items = getattr(report, "perQuery", [])
    seen: set[str] = set()
    for r in per_query_items:
        per_judge = getattr(r, "per_judge_scores", None)
        if per_judge is None:
            per_judge = getattr(r, "perJudgeScores", [])
        for s in per_judge:
            judge_id = getattr(s, "judge_id", None) or getattr(s, "judgeId", None)
            if judge_id:
                seen.add(judge_id)
    return sorted(seen)


def record_eval_run(db: sqlite3.Connection, record: EvalRunRecord) -> str:
    """Insert a single eval run row. Returns the ``run_id``.

    Ports TS ``recordEvalRun`` (``run.ts:170-211``).

    Args:
        db: SQLite connection.
        record: The run record to persist.

    Returns:
        The ``run_id`` (provided or generated).

    Raises:
        ValueError: If the ``query_set_id`` does not exist in
            ``lcm_eval_query_set`` (better error than the SQLite FK
            violation).
    """
    run_id = record.run_id or _generate_run_id()
    query_set_id = encode_query_set_id(record.query_set_identity)

    # Verify FK target exists — better error than the SQLite FK violation.
    fk_row = db.execute(
        "SELECT 1 FROM lcm_eval_query_set WHERE query_set_id = ?",
        (query_set_id,),
    ).fetchone()
    if fk_row is None:
        raise ValueError(f"cannot record eval run for unregistered query set {query_set_id}")

    recall_score = record.recall_report.overall.mean_rr if record.recall_report is not None else 0.0
    quality_score = 0.0
    if record.quality_report is not None:
        overall = getattr(record.quality_report, "overall", None)
        if overall is not None:
            quality_score = float(
                getattr(overall, "mean_score", None) or getattr(overall, "meanScore", 0.0) or 0.0
            )

    envelope = _build_envelope(record)
    judge_models = _judge_models_from_quality_report(record.quality_report)
    trigger: EvalTrigger = record.trigger if record.trigger is not None else "manual"
    prompt_bundle_version = (
        record.prompt_bundle_version if record.prompt_bundle_version is not None else 1
    )

    db.execute(
        """
        INSERT INTO lcm_eval_run (
            run_id, query_set_id, prompt_bundle_version,
            retrieval_recall_score, synthesis_quality_score,
            per_query_scores, judge_models, noise_floor_sd, trigger
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            query_set_id,
            prompt_bundle_version,
            recall_score,
            quality_score,
            json.dumps(envelope, separators=(",", ":")),
            json.dumps(judge_models, separators=(",", ":")),
            record.noise_floor_sd,
            trigger,
        ),
    )

    return run_id


def _select_prior_run(
    db: sqlite3.Connection,
    query_set_id: str,
    mode: str,
    current_run_id: str,
) -> Optional[tuple[str, dict[str, Any]]]:
    """Find the most-recent run with the same ``(query_set_id, mode)``.

    Ports TS ``selectPriorRun`` (``run.ts:218-244``).

    Mode is parsed from the JSON envelope (see SCHEMA GAPS §1).

    Returns:
        ``(prior_run_id, envelope)`` tuple, or ``None`` if no matching
        prior run exists.
    """
    rows = db.execute(
        """
        SELECT run_id, per_query_scores
          FROM lcm_eval_run
          WHERE query_set_id = ? AND run_id != ?
          ORDER BY ran_at DESC, run_id DESC
        """,
        (query_set_id, current_run_id),
    ).fetchall()
    for row in rows:
        try:
            env = json.loads(row[1])
        except (json.JSONDecodeError, TypeError):
            continue
        if env.get("mode") == mode:
            return (row[0], env)
    return None


def _pick_comparable_score(
    prior_slot: Optional[dict[str, Any]],
    current_slot: Optional[dict[str, Any]],
) -> tuple[Optional[float], Optional[float]]:
    """Pick the per-query score we'll diff.

    Ports TS ``pickComparableScore`` (``run.ts:252-270``).

    Preference order:
        1. ``qualityScore`` (if both runs have it)
        2. ``recallRR``     (if both runs have it)
        3. ``None``         (otherwise — the query is excluded from drift)
    """
    pq = prior_slot.get("qualityScore") if prior_slot else None
    cq = current_slot.get("qualityScore") if current_slot else None
    if isinstance(pq, (int, float)) and isinstance(cq, (int, float)):
        return (float(pq), float(cq))

    pr = prior_slot.get("recallRR") if prior_slot else None
    cr = current_slot.get("recallRR") if current_slot else None
    if isinstance(pr, (int, float)) and isinstance(cr, (int, float)):
        return (float(pr), float(cr))

    prior_val = (
        float(pq)
        if isinstance(pq, (int, float))
        else float(pr)
        if isinstance(pr, (int, float))
        else None
    )
    current_val = (
        float(cq)
        if isinstance(cq, (int, float))
        else float(cr)
        if isinstance(cr, (int, float))
        else None
    )
    return (prior_val, current_val)


def compute_drift(db: sqlite3.Connection, run_id: str) -> DriftSummary:
    """Compare ``run_id`` to the most-recent prior run.

    Ports TS ``computeDrift`` (``run.ts:283-375``).

    Records aggregate drift into ``lcm_eval_drift``.

    "drifted" threshold: if ``noise_floor_sd`` was recorded on the
    current run, the threshold is 2× that SD (per architecture-v4.1
    §11.1 — "2× empirical SD"). Otherwise any non-zero delta counts.

    Args:
        db: SQLite connection.
        run_id: The current run's id.

    Returns:
        :class:`DriftSummary` with per-query detail. If no prior run
        exists, returns a zero summary with ``prior_run_id=None`` and
        writes nothing to ``lcm_eval_drift``.

    Raises:
        ValueError: If ``run_id`` does not exist, or if the run's
            ``per_query_scores`` envelope is malformed.
    """
    current_row = db.execute(
        """
        SELECT query_set_id, per_query_scores, noise_floor_sd
          FROM lcm_eval_run
          WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()
    if current_row is None:
        raise ValueError(f"compute_drift: no eval run found with id {run_id}")

    try:
        current_env = json.loads(current_row[1])
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(
            f"compute_drift: malformed per_query_scores for run {run_id}: {exc}"
        ) from exc

    current_query_set_id = current_row[0]
    current_mode = current_env.get("mode", "")
    noise_floor = current_row[2]

    prior = _select_prior_run(db, current_query_set_id, current_mode, run_id)
    if prior is None:
        return DriftSummary(
            drifted=0,
            improved=0,
            regressed=0,
            details=(),
            prior_run_id=None,
            cumulative_delta=0.0,
        )

    prior_run_id, prior_env = prior
    prior_per_query: dict[str, dict[str, Any]] = prior_env.get("perQuery", {})
    current_per_query: dict[str, dict[str, Any]] = current_env.get("perQuery", {})
    all_query_ids = set(prior_per_query.keys()) | set(current_per_query.keys())

    drift_threshold = 2.0 * float(noise_floor) if noise_floor is not None else 0.0

    details: list[DriftDetail] = []
    drifted = 0
    improved = 0
    regressed = 0
    cumulative = 0.0

    for qid in all_query_ids:
        ps, cs = _pick_comparable_score(
            prior_per_query.get(qid),
            current_per_query.get(qid),
        )
        delta = cs - ps if ps is not None and cs is not None else None
        if delta is not None:
            cumulative += delta
            drifted_p = abs(delta) >= drift_threshold if drift_threshold > 0 else delta != 0
            if drifted_p:
                drifted += 1
                if delta > 0:
                    improved += 1
                elif delta < 0:
                    regressed += 1
        details.append(
            DriftDetail(
                query_id=qid,
                prior_score=ps,
                current_score=cs,
                delta=delta,
            )
        )

    # Sort by abs(delta) DESC. None deltas go to the back (TS uses -1).
    def _sort_key(d: DriftDetail) -> float:
        return -abs(d.delta) if d.delta is not None else 1.0

    details.sort(key=_sort_key)

    # Persist aggregate drift (per-query detail not persisted — see
    # SCHEMA GAPS §2).
    db.execute(
        """
        INSERT INTO lcm_eval_drift
            (drift_id, query_set_id, cumulative_delta, window_runs)
          VALUES (?, ?, ?, ?)
        """,
        (_generate_drift_id(), current_query_set_id, cumulative, 2),
    )

    return DriftSummary(
        drifted=drifted,
        improved=improved,
        regressed=regressed,
        details=tuple(details),
        prior_run_id=prior_run_id,
        cumulative_delta=cumulative,
    )
