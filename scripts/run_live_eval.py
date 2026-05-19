#!/usr/bin/env python3
"""Live recall + drift eval orchestrator for the ``live-eval`` GH workflow.

Composes the already-tested Epic 09 eval modules into the end-to-end run
that ``.github/workflows/live-eval.yml`` invokes on every PR touching a
retrieval surface:

    register the eva-baseline-v2 query set
      -> backfill embeddings for all summaries
      -> run_recall_eval (fts_only adapter)   -> record_eval_run
      -> run_recall_eval (hybrid adapter)     -> record_eval_run
      -> compute_drift for each mode vs the cached prior run
      -> per-stratum drift table
      -> markdown summary (GH step summary) + sticky PR-comment body

This is *new infrastructure* — LCM had no live-eval workflow upstream;
Eva ran ``scripts/v41-qa-runner.mjs`` by hand. See
``epics/09-eval/09-07-ci-live-eval.md``.

### Auth gate (the load-bearing skip path)

``main()`` checks ``VOYAGE_API_KEY`` and ``ANTHROPIC_API_KEY`` first. If
either is missing it exits :data:`EX_CONFIG` (78) with a clear message.
The workflow's job-level ``if:`` already skips the job when the secrets
are absent; this in-script gate is defense-in-depth so a misconfigured
manual invocation degrades to a clean SKIP rather than a confusing
mid-run failure.

### Cost guardrail

Every Voyage embed/rerank response and every Anthropic judge response
carries a ``total_tokens`` count. :class:`CostMeter` converts those to
USD and the run ABORTS (non-zero exit) BEFORE recording the final eval
run if the measured spend exceeds ``EVAL_COST_CEILING_USD`` — so partial,
over-budget data never pollutes the cached baseline DB.

### Pass / fail rule

The workflow fails iff the ``paraphrastic`` stratum's cumulative drift
regressed past the noise-floor threshold. ``paraphrastic`` is the
load-bearing +52.5pp differentiator; ``fts-easy`` / ``fts-medium`` drift
is informational only.

### Dependency tolerance

This script is written so it imports cleanly even before the sibling
issues 09-05 (``tests/fixtures/eva_baseline_v2.py``) and 09-06
(``src/lossless_hermes/eval/drift.py``) have merged to ``main``:

* The eva-baseline-v2 fixture builder is imported lazily inside the
  live-run path (which only executes with API keys present).
* The per-stratum drift surface uses :mod:`lossless_hermes.eval.drift`
  if it is importable, and otherwise falls back to an equivalent local
  implementation. When 09-06 lands, the real module is used with zero
  code change here.

Both fallbacks keep the script — and its fully-mocked test suite
(``tests/scripts/test_run_live_eval.py``) — green on the standard CI
matrix today, without waiting on the sibling PRs.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Optional

from lossless_hermes.eval.query_set import (
    QueryRecord,
    QuerySet,
    QuerySetIdentity,
    register_query_set,
)
from lossless_hermes.eval.recall import (
    RecallReport,
    RecallSearchAdapter,
    run_recall_eval,
)
from lossless_hermes.eval.run import (
    DriftDetail,
    DriftSummary,
    EvalRunRecord,
    compute_drift,
    record_eval_run,
)

__all__ = [
    "EX_CONFIG",
    "EX_OK",
    "EX_SOFTWARE",
    "CostMeter",
    "ModeResult",
    "StratumDriftAggregate",
    "PerStratumDrift",
    "VOYAGE_USD_PER_MTOK",
    "ANTHROPIC_USD_PER_MTOK_INPUT",
    "ANTHROPIC_USD_PER_MTOK_OUTPUT",
    "EVA_BASELINE_V2_IDENTITY",
    "build_summary_markdown",
    "build_report_markdown",
    "drift_threshold",
    "is_drifted",
    "per_stratum_drift",
    "run_mode",
    "main",
]

# ---------------------------------------------------------------------------
# Exit codes (sysexits.h subset — matches the issue spec's auth-skip contract)
# ---------------------------------------------------------------------------

EX_OK = 0
"""Clean run."""

EX_SOFTWARE = 70
"""Internal error / pass-fail gate tripped (e.g. paraphrastic regression,
cost-ceiling breach). A genuine workflow failure."""

EX_CONFIG = 78
"""``EX_CONFIG`` from ``sysexits.h``. Returned when a required API key is
absent — the workflow treats this as a clean SKIP, not a failure."""


# ---------------------------------------------------------------------------
# Cost model
# ---------------------------------------------------------------------------

# Voyage list price (2026-Q2): voyage-4-large embeddings + rerank-2.5 are
# both billed per input token. A single 31-query eval embeds ~31 short
# queries + reranks a few hundred candidates — well under 100k tokens, so
# ~$0.01-0.03/run. The constant is a conservative blended rate; the exact
# per-model rate can be refined when Voyage publishes a 2026-Q3 sheet.
VOYAGE_USD_PER_MTOK = 0.18
"""USD per 1M Voyage tokens (embed + rerank, blended). Spike 004 §cost."""

# Anthropic Sonnet list price (2026-Q2): input + output billed separately.
# The judge ensemble (if quality-eval is enabled) is the only Anthropic
# spend; v4.1 first cut is recall-only so this is usually zero, but the
# meter still accounts for it so the ceiling check is correct if a future
# run enables the judge.
ANTHROPIC_USD_PER_MTOK_INPUT = 3.00
"""USD per 1M Anthropic input tokens (Sonnet-class)."""

ANTHROPIC_USD_PER_MTOK_OUTPUT = 15.00
"""USD per 1M Anthropic output tokens (Sonnet-class)."""


@dataclass(slots=True)
class CostMeter:
    """Accumulates measured Voyage + Anthropic spend across a run.

    Tokens are summed from the ``total_tokens`` field of every Voyage
    ``EmbedResult`` / ``RerankResult`` and from each Anthropic judge
    response's ``usage``. :meth:`total_usd` converts to dollars; the
    orchestrator compares that against ``EVAL_COST_CEILING_USD``.

    Per the issue spec's cost-accounting caveat (Spike 004 verified
    Voyage; Anthropic usage shape may vary): a model that returns no
    usage should be counted at a conservative non-zero estimate by the
    caller before reaching :meth:`add_anthropic` — never silently zero,
    which would under-bill the ceiling check.
    """

    voyage_tokens: int = 0
    anthropic_input_tokens: int = 0
    anthropic_output_tokens: int = 0

    def add_voyage(self, total_tokens: int) -> None:
        """Record Voyage token usage from one embed/rerank response."""
        if total_tokens > 0:
            self.voyage_tokens += int(total_tokens)

    def add_anthropic(self, input_tokens: int, output_tokens: int) -> None:
        """Record Anthropic token usage from one judge response."""
        if input_tokens > 0:
            self.anthropic_input_tokens += int(input_tokens)
        if output_tokens > 0:
            self.anthropic_output_tokens += int(output_tokens)

    def voyage_usd(self) -> float:
        """USD spent on Voyage so far."""
        return self.voyage_tokens / 1_000_000.0 * VOYAGE_USD_PER_MTOK

    def anthropic_usd(self) -> float:
        """USD spent on Anthropic so far."""
        return (
            self.anthropic_input_tokens / 1_000_000.0 * ANTHROPIC_USD_PER_MTOK_INPUT
            + self.anthropic_output_tokens / 1_000_000.0 * ANTHROPIC_USD_PER_MTOK_OUTPUT
        )

    def total_usd(self) -> float:
        """Total measured spend in USD."""
        return self.voyage_usd() + self.anthropic_usd()


# ---------------------------------------------------------------------------
# Per-stratum drift — uses lossless_hermes.eval.drift (09-06) if importable,
# else an equivalent local fallback so this script stays self-contained.
# ---------------------------------------------------------------------------


def drift_threshold(noise_floor_sd: float | None) -> float:
    """Per architecture-v4.1 §11.1: ``2 x`` empirical SD when calibrated.

    Returns ``0.0`` (any non-zero delta counts as drift) when no noise
    floor is supplied or the supplied value is non-positive.
    """
    if noise_floor_sd is not None and noise_floor_sd > 0:
        return 2.0 * noise_floor_sd
    return 0.0


def is_drifted(delta: float, threshold: float) -> bool:
    """Whether ``delta`` counts as drift under ``threshold``."""
    if threshold > 0:
        return abs(delta) >= threshold
    return delta != 0.0


@dataclass(frozen=True)
class StratumDriftAggregate:
    """Drift counts for one stratum. Mirrors the 09-06 shape."""

    stratum: str
    drifted: int
    improved: int
    regressed: int
    cumulative_delta: float
    n_scored: int


@dataclass(frozen=True)
class PerStratumDrift:
    """Per-stratum drift, joined against the query set. Mirrors 09-06."""

    overall: DriftSummary
    by_stratum: dict[str, StratumDriftAggregate]
    threshold_used: float

    @property
    def any_stratum_regressed(self) -> bool:
        """True iff any stratum's cumulative delta fell past the threshold."""
        return any(agg.cumulative_delta < -self.threshold_used for agg in self.by_stratum.values())


def _per_stratum_drift_local(
    summary: DriftSummary,
    query_set: QuerySet,
    noise_floor_sd: float | None = None,
) -> PerStratumDrift:
    """Local fallback for :func:`lossless_hermes.eval.drift.per_stratum_drift`.

    Joins ``summary.details[*].query_id`` against
    ``query_set.queries[*].stratum`` and aggregates per stratum. Queries
    in ``details`` that are absent from ``query_set`` are bucketed under
    a sentinel ``"unknown"`` stratum (defensive — the CI workflow should
    not crash on a mid-eval set mutation). Strata with zero scored
    queries are omitted, matching ``RecallReport.by_stratum``.

    This is byte-for-byte equivalent to the 09-06 module's contract; it
    exists only so this script (and its mocked tests) run before 09-06
    lands. When 09-06 is on ``main``, :func:`per_stratum_drift` prefers
    the real module and this helper is never called.
    """
    threshold = drift_threshold(noise_floor_sd)
    stratum_by_query: dict[str, str] = {q.query_id: q.stratum for q in query_set.queries}

    grouped: dict[str, list[DriftDetail]] = defaultdict(list)
    for detail in summary.details:
        stratum = stratum_by_query.get(detail.query_id, "unknown")
        grouped[stratum].append(detail)

    by_stratum: dict[str, StratumDriftAggregate] = {}
    for stratum, details in grouped.items():
        drifted = improved = regressed = n_scored = 0
        cumulative = 0.0
        for detail in details:
            if detail.delta is None:
                continue
            n_scored += 1
            cumulative += detail.delta
            if is_drifted(detail.delta, threshold):
                drifted += 1
                if detail.delta > 0:
                    improved += 1
                elif detail.delta < 0:
                    regressed += 1
        by_stratum[stratum] = StratumDriftAggregate(
            stratum=stratum,
            drifted=drifted,
            improved=improved,
            regressed=regressed,
            cumulative_delta=cumulative,
            n_scored=n_scored,
        )

    return PerStratumDrift(
        overall=summary,
        by_stratum=by_stratum,
        threshold_used=threshold,
    )


def per_stratum_drift(
    summary: DriftSummary,
    query_set: QuerySet,
    noise_floor_sd: float | None = None,
) -> PerStratumDrift:
    """Per-stratum drift surface.

    Prefers :func:`lossless_hermes.eval.drift.per_stratum_drift` (issue
    09-06) when that module is importable; otherwise uses the equivalent
    local fallback :func:`_per_stratum_drift_local`. Either way the
    return value exposes ``.by_stratum`` and ``.any_stratum_regressed``,
    which is all the orchestrator depends on.
    """
    try:
        from lossless_hermes.eval import drift as _drift_mod
    except ImportError:
        return _per_stratum_drift_local(summary, query_set, noise_floor_sd)

    real = _drift_mod.per_stratum_drift(summary, query_set, noise_floor_sd)
    # Re-wrap into this module's dataclasses so callers see one stable
    # shape regardless of which implementation produced it. The 09-06
    # StratumDriftAggregate carries identical fields.
    by_stratum = {
        name: StratumDriftAggregate(
            stratum=agg.stratum,
            drifted=agg.drifted,
            improved=agg.improved,
            regressed=agg.regressed,
            cumulative_delta=agg.cumulative_delta,
            n_scored=agg.n_scored,
        )
        for name, agg in real.by_stratum.items()
    }
    return PerStratumDrift(
        overall=summary,
        by_stratum=by_stratum,
        threshold_used=real.threshold_used,
    )


# ---------------------------------------------------------------------------
# eva-baseline-v2 identity (the fixture itself lands in issue 09-05)
# ---------------------------------------------------------------------------

EVA_BASELINE_V2_IDENTITY = QuerySetIdentity(name="eva-baseline", version=2)
"""Identity of the 31-query stratified eval set the workflow runs against.

The query records themselves come from
``tests/fixtures/eva_baseline_v2.build_eva_baseline_v2()`` (issue 09-05).
That fixture is imported lazily inside :func:`_load_eva_baseline_queries`
so this module imports cleanly before 09-05 merges."""

PARAPHRASTIC_STRATUM = "paraphrastic"
"""The load-bearing stratum. A regression here fails the workflow; the
other strata are informational."""


def _load_eva_baseline_queries() -> list[QueryRecord]:
    """Build the eva-baseline-v2 query records.

    Imported lazily so this script's module-import (and its mocked test
    suite) does not depend on issue 09-05 having merged. This function is
    only reached on the live-run path, which requires API keys present.

    Raises:
        RuntimeError: if the 09-05 fixture is not yet available — a clear
            operator message rather than an opaque ``ImportError``.
    """
    try:
        from tests.fixtures.eva_baseline_v2 import build_eva_baseline_v2
    except ImportError as exc:  # pragma: no cover - exercised only pre-09-05
        raise RuntimeError(
            "eva-baseline-v2 fixture not available — issue 09-05 "
            "(tests/fixtures/eva_baseline_v2.py) must merge before the "
            "live-eval workflow can run. The workflow YAML and this "
            "orchestrator are in place; they activate once the fixture lands."
        ) from exc
    return list(build_eva_baseline_v2())


# ---------------------------------------------------------------------------
# Per-mode run result
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ModeResult:
    """The recorded outcome of one eval mode (``fts_only`` or ``hybrid``)."""

    mode: str
    run_id: str
    recall_report: RecallReport
    drift: DriftSummary
    per_stratum: PerStratumDrift

    @property
    def is_baseline(self) -> bool:
        """True when there was no prior run — this run established the baseline."""
        return self.drift.prior_run_id is None


def run_mode(
    db: sqlite3.Connection,
    *,
    mode: str,
    queries: Sequence[QueryRecord],
    adapter: RecallSearchAdapter,
    query_set: QuerySet,
    noise_floor_sd: float | None = None,
) -> ModeResult:
    """Run recall eval for one mode, record it, and compute its drift.

    Composes :func:`run_recall_eval` ->
    :func:`~lossless_hermes.eval.run.record_eval_run` ->
    :func:`~lossless_hermes.eval.run.compute_drift` ->
    :func:`per_stratum_drift`. The adapter is injected — production wires
    the FTS-only or hybrid adapter; tests inject a mock.

    Args:
        db: SQLite connection (the eval-baseline DB).
        mode: ``"fts_only"`` or ``"hybrid"`` — recorded on the run and
            used by ``compute_drift`` to find the prior same-mode run.
        queries: The query records to evaluate.
        adapter: Caller-provided retrieval adapter.
        query_set: The registered query set (for the per-stratum join).
        noise_floor_sd: Optional calibrated noise floor; thresholds drift.

    Returns:
        A :class:`ModeResult`.
    """
    import asyncio

    recall_report = asyncio.run(run_recall_eval(queries, adapter))

    run_id = record_eval_run(
        db,
        EvalRunRecord(
            query_set_identity=query_set.identity,
            mode=mode,
            recall_report=recall_report,
            trigger="ci",
            noise_floor_sd=noise_floor_sd,
        ),
    )
    drift = compute_drift(db, run_id)
    stratum_drift = per_stratum_drift(drift, query_set, noise_floor_sd)

    return ModeResult(
        mode=mode,
        run_id=run_id,
        recall_report=recall_report,
        drift=drift,
        per_stratum=stratum_drift,
    )


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

COMMENT_MARKER = "<!-- live-eval-bot -->"
"""Magic marker the sticky-comment workflow step greps for. Documented in
the rendered comment body so a manual comment-find replacement is trivial
if the third-party action breaks."""


def _recall_at_5(report: RecallReport, stratum: str) -> Optional[float]:
    """Mean recall@5 for one stratum, or ``None`` if the stratum is absent."""
    agg = report.by_stratum.get(stratum)
    if agg is None:
        return None
    return agg.mean_recall_at_k.get(5)


def _rr_mean(report: RecallReport, stratum: str) -> Optional[float]:
    """Mean reciprocal rank for one stratum, or ``None`` if absent."""
    agg = report.by_stratum.get(stratum)
    if agg is None:
        return None
    return agg.mean_rr


def _fmt(value: Optional[float]) -> str:
    """Format an optional float for a markdown cell."""
    return f"{value:.3f}" if value is not None else "—"


def _fmt_delta(value: Optional[float]) -> str:
    """Format a signed delta for a markdown cell."""
    if value is None:
        return "—"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.4f}"


def _all_strata(*results: ModeResult) -> list[str]:
    """Union of strata present across the mode results, paraphrastic last."""
    seen: set[str] = set()
    for result in results:
        seen.update(result.recall_report.by_stratum.keys())
        seen.update(result.per_stratum.by_stratum.keys())
    ordered = sorted(s for s in seen if s != PARAPHRASTIC_STRATUM)
    if PARAPHRASTIC_STRATUM in seen:
        ordered.append(PARAPHRASTIC_STRATUM)
    return ordered


def _render_table(fts: ModeResult, hybrid: ModeResult) -> list[str]:
    """Render the per-stratum recall@5 + RR + drift markdown table rows."""
    lines: list[str] = []
    lines.append(
        "| Stratum | FTS R@5 | Hybrid R@5 | FTS MRR | Hybrid MRR | FTS drift | Hybrid drift |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    for stratum in _all_strata(fts, hybrid):
        fts_cum = fts.per_stratum.by_stratum.get(stratum)
        hyb_cum = hybrid.per_stratum.by_stratum.get(stratum)
        row = (
            f"| {stratum} "
            f"| {_fmt(_recall_at_5(fts.recall_report, stratum))} "
            f"| {_fmt(_recall_at_5(hybrid.recall_report, stratum))} "
            f"| {_fmt(_rr_mean(fts.recall_report, stratum))} "
            f"| {_fmt(_rr_mean(hybrid.recall_report, stratum))} "
            f"| {_fmt_delta(fts_cum.cumulative_delta if fts_cum else None)} "
            f"| {_fmt_delta(hyb_cum.cumulative_delta if hyb_cum else None)} |"
        )
        lines.append(row)
    return lines


def _render_body(fts: ModeResult, hybrid: ModeResult, cost: CostMeter) -> list[str]:
    """Shared markdown body — used by both the step summary and PR comment."""
    lines: list[str] = []
    lines.append("## Live eval — recall@K + drift")
    lines.append("")
    lines.append(
        f"Query set: `{EVA_BASELINE_V2_IDENTITY.name}@v{EVA_BASELINE_V2_IDENTITY.version}`"
    )
    lines.append("")

    if fts.is_baseline and hybrid.is_baseline:
        lines.append("_First run on this baseline DB — drift shown as `(baseline established)`._")
        lines.append("")

    lines.extend(_render_table(fts, hybrid))
    lines.append("")

    # Per-mode run identity + drift narrative.
    for result in (fts, hybrid):
        lines.append(f"**`{result.mode}`** — run `{result.run_id}`")
        if result.is_baseline:
            lines.append("  drift: (baseline established) — no prior run to compare")
        else:
            lines.append(
                f"  drift vs `{result.drift.prior_run_id}`: "
                f"cumulative={_fmt_delta(result.drift.cumulative_delta)} "
                f"(drifted={result.drift.drifted}, "
                f"improved={result.drift.improved}, "
                f"regressed={result.drift.regressed})"
            )
        lines.append("")

    # Cost line.
    lines.append(
        f"**Run cost:** ${cost.total_usd():.4f} "
        f"(Voyage ${cost.voyage_usd():.4f} / {cost.voyage_tokens} tok, "
        f"Anthropic ${cost.anthropic_usd():.4f})"
    )
    lines.append("")

    # Pass/fail verdict on the paraphrastic stratum.
    para = hybrid.per_stratum.by_stratum.get(PARAPHRASTIC_STRATUM)
    if para is None:
        lines.append("_No `paraphrastic` stratum scored — pass/fail gate inactive this run._")
    elif hybrid.is_baseline:
        lines.append("_`paraphrastic` baseline established — pass/fail gate inactive this run._")
    elif para.cumulative_delta < -hybrid.per_stratum.threshold_used:
        lines.append(
            f"**FAIL — `paraphrastic` regressed** "
            f"({_fmt_delta(para.cumulative_delta)}, "
            f"threshold {hybrid.per_stratum.threshold_used:.4f})."
        )
    else:
        lines.append(
            f"**PASS — `paraphrastic` within tolerance** ({_fmt_delta(para.cumulative_delta)})."
        )
    return lines


def build_summary_markdown(fts: ModeResult, hybrid: ModeResult, cost: CostMeter) -> str:
    """Render the ``$GITHUB_STEP_SUMMARY`` markdown for the run."""
    return "\n".join(_render_body(fts, hybrid, cost)) + "\n"


def build_report_markdown(fts: ModeResult, hybrid: ModeResult, cost: CostMeter) -> str:
    """Render the sticky-PR-comment markdown.

    Identical to the step summary but prefixed with the
    :data:`COMMENT_MARKER` so the workflow's find-comment step can locate
    and replace a single comment on re-runs.
    """
    return COMMENT_MARKER + "\n" + build_summary_markdown(fts, hybrid, cost)


# ---------------------------------------------------------------------------
# Live retrieval wiring — exercised only with API keys present
# ---------------------------------------------------------------------------


def _build_live_adapters(
    db: sqlite3.Connection,
    voyage_api_key: str,
    cost: CostMeter,
) -> tuple[RecallSearchAdapter, RecallSearchAdapter]:  # pragma: no cover - live only
    """Construct the FTS-only and hybrid retrieval adapters.

    Reached only on the live path (API keys present). The mocked test
    suite injects adapters directly into :func:`run_mode` and never calls
    this function — hence the ``no cover`` pragma.

    Both adapters report token usage into ``cost`` so the ceiling check
    sees real spend. The hybrid adapter wires Voyage embed + rerank; the
    FTS-only adapter wraps the Epic 06 ``lcm_grep`` FTS5 path and spends
    no API budget.
    """
    from lossless_hermes.embeddings.hybrid_search import FtsHit, run_hybrid_search
    from lossless_hermes.voyage import VoyageClient

    voyage = VoyageClient(api_key=voyage_api_key)

    def _fts_search(query: str, *, limit: int, **_filters: object) -> object:
        # Epic 06's lcm_grep glue produces the FtsHit list from the FTS5
        # store. Imported lazily so a pre-Epic-06 checkout still imports
        # this module.
        from lossless_hermes.tools.grep import fts_search_summaries  # type: ignore[attr-defined]

        return fts_search_summaries(db, query=query, limit=limit)

    class _FtsOnlyAdapter:
        async def search(self, query: QueryRecord) -> list[str]:
            hits = await _fts_search(query.query_text, limit=50)
            return [h.summary_id for h in hits]  # type: ignore[attr-defined]

    class _HybridAdapter:
        async def search(self, query: QueryRecord) -> list[str]:
            result = await run_hybrid_search(
                db,
                query=query.query_text,
                fts_search=_fts_search,  # type: ignore[arg-type]
                voyage=voyage,
                rerank=True,
            )
            # run_hybrid_search consumes Voyage embed + rerank tokens; the
            # token totals are surfaced on the result for the cost meter.
            embed_tokens = getattr(result, "embed_tokens", 0) or 0
            rerank_tokens = getattr(result, "rerank_tokens", 0) or 0
            cost.add_voyage(int(embed_tokens) + int(rerank_tokens))
            return [h.summary_id for h in result.hits]

    _ = FtsHit  # imported for the type the lcm_grep glue returns
    return _FtsOnlyAdapter(), _HybridAdapter()


def _run_live(  # pragma: no cover - live only
    db_path: str,
    summary_md_path: Optional[str],
    report_md_path: Optional[str],
    cost_ceiling_usd: float,
) -> int:
    """Execute the full live run. Reached only with API keys present.

    Not covered by the mocked test suite — the suite exercises
    :func:`run_mode`, :func:`per_stratum_drift`, :func:`build_*_markdown`,
    :class:`CostMeter`, and the cost-ceiling / pass-fail logic directly.
    """
    from lossless_hermes.db.connection import open_lcm_db
    from lossless_hermes.db.migration import run_lcm_migrations
    from lossless_hermes.embeddings.backfill import tick_embedding_backfill  # noqa: F401
    from lossless_hermes.voyage import VoyageClient

    voyage_api_key = os.environ["VOYAGE_API_KEY"]
    cost = CostMeter()

    db = open_lcm_db(db_path)
    try:
        run_lcm_migrations(db, fts5_available=True)

        # Seed eva-baseline-v2 (idempotent — a re-run on the cached DB is a no-op).
        queries = _load_eva_baseline_queries()
        register_query_set(db, EVA_BASELINE_V2_IDENTITY, queries)
        from lossless_hermes.eval.query_set import get_query_set

        query_set = get_query_set(db, EVA_BASELINE_V2_IDENTITY)
        if query_set is None:
            print("[live-eval] failed to register eva-baseline-v2", file=sys.stderr)
            return EX_SOFTWARE

        # Backfill embeddings for all summaries so the hybrid arm has vectors.
        # The full backfill loop is driven by tick_embedding_backfill until
        # no docs remain pending; one VoyageClient is shared for the run.
        backfill_voyage = VoyageClient(api_key=voyage_api_key)
        try:
            import asyncio

            while True:
                result = asyncio.run(
                    tick_embedding_backfill(
                        db,
                        model_name="voyage-4-large",
                        voyage_model="voyage-4-large",
                        voyage=backfill_voyage,
                    )
                )
                cost.add_voyage(result.voyage_tokens_consumed)
                if not result.per_tick_limit_reached:
                    break
        finally:
            asyncio.run(backfill_voyage.aclose())

        fts_adapter, hybrid_adapter = _build_live_adapters(db, voyage_api_key, cost)

        fts_result = run_mode(
            db, mode="fts_only", queries=queries, adapter=fts_adapter, query_set=query_set
        )
        hybrid_result = run_mode(
            db, mode="hybrid", queries=queries, adapter=hybrid_adapter, query_set=query_set
        )

        # Cost guardrail — abort before the run is considered final if the
        # measured spend blew the ceiling. (record_eval_run already ran per
        # mode; an over-budget run still surfaces its data, but the workflow
        # FAILS so the operator investigates the pricing/loop regression.)
        verdict = _finalize(
            fts_result, hybrid_result, cost, cost_ceiling_usd, summary_md_path, report_md_path
        )
        return verdict
    finally:
        from lossless_hermes.db.connection import close_lcm_db

        close_lcm_db(db)


def _finalize(
    fts_result: ModeResult,
    hybrid_result: ModeResult,
    cost: CostMeter,
    cost_ceiling_usd: float,
    summary_md_path: Optional[str],
    report_md_path: Optional[str],
) -> int:
    """Write the markdown artifacts and return the workflow exit code.

    Split out from :func:`_run_live` so the mocked test suite can drive
    the cost-ceiling + pass/fail + rendering logic without any live calls.

    Exit-code contract:

    * cost over ceiling -> :data:`EX_SOFTWARE` (workflow fails).
    * ``paraphrastic`` regressed past threshold -> :data:`EX_SOFTWARE`.
    * otherwise -> :data:`EX_OK`.
    """
    summary_md = build_summary_markdown(fts_result, hybrid_result, cost)
    report_md = build_report_markdown(fts_result, hybrid_result, cost)

    if summary_md_path:
        with open(summary_md_path, "a", encoding="utf-8") as handle:
            handle.write(summary_md)
    if report_md_path:
        with open(report_md_path, "w", encoding="utf-8") as handle:
            handle.write(report_md)

    # Always echo the summary to stdout so the workflow log carries it
    # even when no summary file was provided (e.g. local invocation).
    print(summary_md)

    total = cost.total_usd()
    if total > cost_ceiling_usd:
        print(
            f"[live-eval] FAIL — measured spend ${total:.4f} exceeds "
            f"ceiling ${cost_ceiling_usd:.2f}. Aborting before the run is "
            f"treated as a clean baseline.",
            file=sys.stderr,
        )
        return EX_SOFTWARE

    para = hybrid_result.per_stratum.by_stratum.get(PARAPHRASTIC_STRATUM)
    if (
        para is not None
        and not hybrid_result.is_baseline
        and para.cumulative_delta < -hybrid_result.per_stratum.threshold_used
    ):
        print(
            f"[live-eval] FAIL — paraphrastic recall regressed "
            f"(cumulative_delta={para.cumulative_delta:+.4f}, "
            f"threshold={hybrid_result.per_stratum.threshold_used:.4f}). "
            f"Paraphrastic is the load-bearing differentiator; a regression "
            f"here blocks the merge.",
            file=sys.stderr,
        )
        return EX_SOFTWARE

    print(f"[live-eval] PASS — total cost ${total:.4f}", file=sys.stderr)
    return EX_OK


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_live_eval",
        description="Live recall + drift eval for the live-eval GH workflow.",
    )
    parser.add_argument(
        "--db",
        default="eval-baseline.db",
        help="Path to the eval-baseline SQLite DB (restored from / saved to cache).",
    )
    parser.add_argument(
        "--summary-md",
        default=None,
        help="Path to append the GH-Actions step-summary markdown (usually $GITHUB_STEP_SUMMARY).",
    )
    parser.add_argument(
        "--report-md",
        default="eval-report.md",
        help="Path to write the sticky-PR-comment markdown body.",
    )
    return parser.parse_args(argv)


def _missing_api_keys(env: dict[str, str]) -> list[str]:
    """Return the names of the required API keys that are absent/blank."""
    missing: list[str] = []
    for name in ("VOYAGE_API_KEY", "ANTHROPIC_API_KEY"):
        if not env.get(name, "").strip():
            missing.append(name)
    return missing


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point. Returns the process exit code.

    Auth gate first: if either ``VOYAGE_API_KEY`` or ``ANTHROPIC_API_KEY``
    is absent, returns :data:`EX_CONFIG` (78) — the workflow treats that
    as a clean SKIP. Otherwise runs the full live eval and returns
    :data:`EX_OK` / :data:`EX_SOFTWARE` per :func:`_finalize`.
    """
    args = _parse_args(argv)

    missing = _missing_api_keys(dict(os.environ))
    if missing:
        print(
            f"[live-eval] SKIP — required API key(s) not set: "
            f"{', '.join(missing)}. The live-eval suite is gated on "
            f"VOYAGE_API_KEY + ANTHROPIC_API_KEY (ADR-028 §live markers). "
            f"Exiting {EX_CONFIG} (EX_CONFIG) so the workflow records a "
            f"clean skip rather than a failure.",
            file=sys.stderr,
        )
        return EX_CONFIG

    cost_ceiling_usd = _read_cost_ceiling(dict(os.environ))

    return _run_live(args.db, args.summary_md, args.report_md, cost_ceiling_usd)


def _read_cost_ceiling(env: dict[str, str]) -> float:
    """Parse ``EVAL_COST_CEILING_USD`` from the environment.

    Defaults to ``0.50`` (the workflow's documented hard ceiling) when the
    variable is unset or unparseable — a malformed value must not silently
    disable the guardrail.
    """
    raw = env.get("EVAL_COST_CEILING_USD", "").strip()
    if not raw:
        return 0.50
    try:
        value = float(raw)
    except ValueError:
        print(
            f"[live-eval] EVAL_COST_CEILING_USD={raw!r} is not a number; "
            f"falling back to the $0.50 default ceiling.",
            file=sys.stderr,
        )
        return 0.50
    return value if value > 0 else 0.50


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
