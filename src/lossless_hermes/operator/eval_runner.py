"""Operator-facing eval runner — LCM v4.1 §11 / Group F.05.

Ports ``lossless-claw/src/operator/eval-runner.ts`` (LCM commit
``1f07fbd`` on branch ``pr-613``, 193 LOC TS → ~230 LOC Python with
docstrings).

Wires the D.03 eval harness (query sets + recall + run recording +
drift) into the ``/lcm eval`` operator command. The retrieval adapter
is **INJECTED** so this module is testable without a Voyage key or
sqlite-vec extension — production code wires the real adapter
(FTS-only or hybrid) at the call site.

### What this commit covers (verbatim from TS source ``eval-runner.ts:10-18``)

* Recall@K eval with an arbitrary mode tag (caller provides the
  adapter, we report the recall).
* Drift comparison vs the prior run of the same ``(query_set, mode)``
  — :func:`~lossless_hermes.eval.run.record_eval_run` +
  :func:`~lossless_hermes.eval.run.compute_drift` from D.03 do the
  work; we just compose them.
* Tolerant of a missing query set: raises with a clear message instead
  of an opaque FK violation.

### What this commit DOES NOT cover (deferred, verbatim from TS ``eval-runner.ts:20-32``)

* **Synthesis-quality (judge) eval.** The d.03 judge module exists,
  but operator quality eval needs the assemble-pyramid output as input
  plus the judge wiring; v4.1 first cut is recall-only.
* **5× noise-floor calibration.** That's an operational concern (run
  the baseline 5× compute per-query SD) handled outside this module
  by an operator workflow.
* ``--register-set --queries-file``. Operators seed the corpus today
  by inserting rows directly via
  :func:`~lossless_hermes.eval.query_set.register_query_set` — a CLI
  flag for JSON loading lands in a follow-up.

### Divergence from issue spec (08-13)

The issue spec describes a richer surface that does retrieval +
embedding directly (with ``voyage_call_count``, ``estimated_cost_usd``,
``p50_latency_ms``, etc.). The actual TS source is intentionally
simpler — the retrieval is injected as a :class:`RecallSearchAdapter`,
and the cost/latency aggregation lives in the adapter (caller-side),
not in the runner. **Per CLAUDE.md "1:1 source-to-Python port"
mandate, this port mirrors the TS source.** The richer cost/latency
surface can be layered on by the ``/lcm eval`` command handler when
it wires the real adapter — that handler is the right place to time
queries (``time.perf_counter()``), count Voyage calls, and validate
credentials, because it knows which adapter (FTS / semantic / hybrid)
it instantiated. See ``epics/08-cli-ops/08-13-eval-runner.md`` §"AC"
for the spec's richer-shape ACs; they belong to the handler-side issue.

### Caller-side gating

**Owner-gating is NOT enforced inside this module** (per ADR-013). The
``/lcm eval`` slash command dispatcher (``commands/eval.py``) and
Hermes's upstream policy gate the surface — this module trusts that
any caller has already passed the policy gate.

See:

* ``epics/08-cli-ops/08-13-eval-runner.md`` — this issue.
* ``docs/porting-guides/doctor-ops.md`` §"Operator modules" line 312
  — operator module roster.
* ``docs/adr/013-owner-gating.md`` — caller-side gating.
* ``lossless-claw/src/operator/eval-runner.ts:1-194`` — TS source
  pinned at commit ``1f07fbd``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal, Optional

from lossless_hermes.eval.query_set import (
    QuerySetIdentity,
    encode_query_set_id,
    get_query_set,
)
from lossless_hermes.eval.recall import (
    RecallEvalOptions,
    RecallReport,
    RecallSearchAdapter,
    RecallStratumAggregate,
    run_recall_eval,
)
from lossless_hermes.eval.run import (
    DriftSummary,
    EvalRunRecord,
    EvalTrigger,
    compute_drift,
    record_eval_run,
)

__all__ = [
    "EvalMode",
    "EvalRunnerError",
    "EvalRunnerErrorKind",
    "RunEvalArgs",
    "RunEvalResult",
    "format_eval_report",
    "run_eval",
]


EvalMode = Literal["fts_only", "semantic_only", "hybrid"]
"""Ports TS ``EvalMode`` (``eval-runner.ts:40``). Stored on the eval
run's ``per_query_scores`` envelope (``.mode``); used by
:func:`~lossless_hermes.eval.run.compute_drift` to find the prior run."""


EvalRunnerErrorKind = Literal["missing_query_set", "empty_query_set"]
"""Ports the kinds TS ``EvalRunnerError`` discriminates between
(``eval-runner.ts:69-70``)."""


class EvalRunnerError(Exception):
    """Raised by :func:`run_eval` on unsafe / invalid input.

    Ports the TS ``EvalRunnerError`` class (``eval-runner.ts:68-76``).
    The ``kind`` attribute disambiguates failure modes so callers (the
    ``/lcm eval`` handler in ``commands/eval.py``) can render
    operator-facing messages:

    * ``"missing_query_set"`` — the query set's
      ``(name, version)`` pair is not registered. Surfaced with a
      diagnostic message pointing operators at
      :func:`~lossless_hermes.eval.query_set.register_query_set`.
    * ``"empty_query_set"`` — the query set is registered but contains
      no queries. Surfaced separately from "missing" so operators can
      distinguish "set not registered" from "set registered but empty".

    Args:
        kind: One of :data:`EvalRunnerErrorKind`.
        message: Human-readable detail.
    """

    def __init__(self, kind: EvalRunnerErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind: EvalRunnerErrorKind = kind


@dataclass(frozen=True, slots=True)
class RunEvalArgs:
    """Inputs to :func:`run_eval`.

    Ports the TS ``RunEvalArgs`` interface (``eval-runner.ts:42-59``).

    Attributes:
        query_set_identity: Identifies which query set to run.
        mode: Retrieval mode tag — recorded on the run, used to find
            the prior run for drift comparison.
        retrieval_adapter: Caller-provided retrieval adapter — must
            call into FTS / hybrid / semantic search per the mode.
        notes: Optional caller note recorded on the run.
        trigger: Defaults to ``"manual"``. Recorded on the run row.
        prompt_bundle_version: Defaults to 1. Recorded on the run row
            (see :mod:`lossless_hermes.eval.run` SCHEMA GAPS §3).
        k_values: K values for recall@K computation. Default
            :data:`~lossless_hermes.eval.recall.DEFAULT_K_VALUES` =
            ``(1, 5, 10, 20, 50)``.
        per_query_timeout_ms: Optional per-query timeout (ms). Default
            30s; clamped to ≥100ms, ≤5min. See Wave-4 Auditor #15 P1.
    """

    query_set_identity: QuerySetIdentity
    mode: EvalMode
    retrieval_adapter: RecallSearchAdapter
    notes: Optional[str] = None
    trigger: Optional[EvalTrigger] = None
    prompt_bundle_version: Optional[int] = None
    k_values: tuple[int, ...] | None = None
    per_query_timeout_ms: float | None = None


@dataclass(frozen=True, slots=True)
class RunEvalResult:
    """Output of :func:`run_eval`.

    Ports the TS ``RunEvalResult`` interface (``eval-runner.ts:61-66``).

    Attributes:
        run_id: The generated ``lcm_eval_run.run_id`` for the recorded
            run.
        recall_report: Full recall report from
            :func:`~lossless_hermes.eval.recall.run_recall_eval`.
        drift: ``None`` if no prior run exists for the same
            ``(query_set, mode)`` — the "fresh baseline" case. A
            populated :class:`~lossless_hermes.eval.run.DriftSummary`
            otherwise.
    """

    run_id: str
    recall_report: RecallReport
    drift: Optional[DriftSummary]


async def run_eval(
    db: sqlite3.Connection,
    args: RunEvalArgs,
) -> RunEvalResult:
    """Run a recall eval against the registered query set + injected adapter.

    Ports TS ``runEval`` (``eval-runner.ts:82-129``). Records the run
    + computes drift.

    Args:
        db: SQLite connection.
        args: Run inputs.

    Returns:
        :class:`RunEvalResult`.

    Raises:
        EvalRunnerError: with ``kind="missing_query_set"`` if the query
            set's ``(name, version)`` pair is not registered; with
            ``kind="empty_query_set"`` if it's registered but empty.
    """
    query_set = get_query_set(db, args.query_set_identity)
    if query_set is None:
        # Final review Finding #4 fix: previous error pointed at a flag
        # (/lcm reconcile-session-keys --register-set) that doesn't exist.
        # Operators seed the eval corpus today via the register_query_set()
        # service (not via /lcm). The CLI seed flag is deferred to a
        # cycle-2 follow-up.
        raise EvalRunnerError(
            "missing_query_set",
            f"[eval] query set {encode_query_set_id(args.query_set_identity)} "
            f"is not registered. Seed via the register_query_set() service "
            f"(Python REPL: register_query_set(db, identity, queries)) "
            f"or via SQL INSERT into lcm_eval_query_set + lcm_eval_query. "
            f"The /lcm CLI seed flag is deferred to a cycle-2 follow-up.",
        )
    if len(query_set.queries) == 0:
        raise EvalRunnerError(
            "empty_query_set",
            f"[eval] query set {encode_query_set_id(args.query_set_identity)} contains no queries",
        )

    recall_report = await run_recall_eval(
        query_set.queries,
        args.retrieval_adapter,
        opts=_build_recall_opts(args),
    )

    run_id = record_eval_run(
        db,
        EvalRunRecord(
            query_set_identity=args.query_set_identity,
            mode=args.mode,
            recall_report=recall_report,
            notes=args.notes,
            trigger=args.trigger if args.trigger is not None else "manual",
            prompt_bundle_version=args.prompt_bundle_version,
        ),
    )

    # compute_drift returns a DriftSummary even when no prior run exists
    # (prior_run_id is None then). We surface that distinction at the
    # operator level by returning None for the "fresh baseline" case
    # rather than a zeroed summary.
    drift_summary = compute_drift(db, run_id)
    drift = None if drift_summary.prior_run_id is None else drift_summary

    return RunEvalResult(
        run_id=run_id,
        recall_report=recall_report,
        drift=drift,
    )


def _build_recall_opts(args: RunEvalArgs) -> Optional[RecallEvalOptions]:
    """Translate the runner args into a ``RecallEvalOptions``.

    Returns ``None`` if neither knob is set (so the recall module uses
    its own defaults).
    """
    if args.k_values is None and args.per_query_timeout_ms is None:
        return None
    return RecallEvalOptions(
        k_values=args.k_values,
        per_query_timeout_ms=args.per_query_timeout_ms,
    )


def format_eval_report(
    query_set_identity: QuerySetIdentity,
    mode: EvalMode,
    result: RunEvalResult,
) -> str:
    """Format a recall + drift result as an operator-facing markdown summary.

    Ports TS ``formatEvalReport`` (``eval-runner.ts:135-179``).

    Pure formatter — no DB / I/O.

    Args:
        query_set_identity: The identity to display in the header.
        mode: The mode tag to display in the header.
        result: The :class:`RunEvalResult` to format.

    Returns:
        The markdown-formatted report.
    """
    recall_report = result.recall_report
    drift = result.drift
    run_id = result.run_id

    lines: list[str] = []

    lines.append(f"**Eval run** `{run_id}`")
    lines.append(f"query set: `{encode_query_set_id(query_set_identity)}`")
    lines.append(f"mode: `{mode}`")
    lines.append("")

    # ── Recall@K per-stratum table ────────────────────────────────────
    lines.append("**Recall@K — overall**")
    lines.append(_format_recall_line(recall_report.overall))
    lines.append("")

    strata = sorted(recall_report.by_stratum.keys())
    if len(strata) > 0:
        lines.append("**Recall@K — per stratum**")
        for s in strata:
            lines.append(f"  {s}: {_format_recall_line(recall_report.by_stratum[s])}")
        lines.append("")

    # ── Drift ─────────────────────────────────────────────────────────
    lines.append("**Drift**")
    if drift is None:
        lines.append("  no prior run for this (query_set, mode) — recorded as new baseline")
    else:
        sign = "+" if drift.cumulative_delta >= 0 else ""
        lines.append(
            f"  vs prior run `{drift.prior_run_id}`: "
            f"cumulative_delta={sign}{drift.cumulative_delta:.4f}"
        )
        lines.append(
            f"  drifted={drift.drifted} (improved={drift.improved}, regressed={drift.regressed})"
        )

    return "\n".join(lines)


def _format_recall_line(agg: RecallStratumAggregate) -> str:
    """Ports TS ``formatRecallLine`` (``eval-runner.ts:181-193``)."""
    ks = sorted(agg.mean_recall_at_k.keys())
    recall_str = " ".join(f"R@{k}={agg.mean_recall_at_k.get(k, 0.0):.3f}" for k in ks)
    return f"n={agg.n} {recall_str} MRR={agg.mean_rr:.3f}"
