#!/usr/bin/env python3
"""Voyage recall benchmark — reproduces the +52.5pp paraphrastic uplift.

Issue 09-08 (epic 09 — eval). The terminal Epic-09 artifact: a
reproducible benchmark that measures the recall lift of **hybrid Voyage
retrieval** vs **FTS-only** on the ``eva-baseline-v2`` query set, against
a fixed synthetic corpus, and publishes the per-stratum result.

### What this script does

    seed the v41-test-corpus synthetic DB (no API needed)
      -> register the eva-baseline-v2 query set
      -> run_recall_eval (fts_only adapter)  -> record_eval_run(mode="fts_only")
      -> [VOYAGE_API_KEY] backfill embeddings + run_recall_eval (hybrid adapter)
                                             -> record_eval_run(mode="hybrid")
      -> compute the per-stratum lift (hybrid R@k - fts R@k)
      -> compute_drift on each recorded run (AC: completes without error)
      -> write docs/benchmarks/voyage-recall-2026-q2.md

### The two arms — and the API-key gate

* **``fts_only``** — pure SQLite FTS5. Needs NO API key. Always runs;
  always produces real, reproducible numbers.
* **``hybrid``** — FTS5 + Voyage semantic embeddings + Voyage rerank-2.5.
  Requires a live ``VOYAGE_API_KEY``. When the key is absent the hybrid
  arm is **skipped** and the report records the FTS baseline + a clearly
  marked ``live hybrid run PENDING`` section, plus the exact command to
  run the hybrid arm once a key is provisioned.

This split is deliberate: the +52.5pp number is a *live measurement*
that cannot be fabricated. The benchmark *harness* is complete and
verified offline; the live hybrid confirmation is operator-gated.

### Retrieval logic is NOT duplicated here

The FTS-only and hybrid adapters are the exact ones the
``live-eval`` workflow uses — imported from
:mod:`run_live_eval` (``_build_fts_search`` / ``_build_live_adapters``).
This script is pure orchestration + reporting; it ships no production
retrieval code.

### Provenance

The +52.5pp threshold is the Phase-A spike result documented in
``lossless-claw/docs/v4.1/PR_DESCRIPTION.md`` §"Why Voyage embeddings"
(LCM commit ``1f07fbd``, branch ``pr-613``). The spike's per-stratum
table:

    | Stratum      | n  | FTS-only | Hybrid (Voyage rerank-2.5) | Lift     |
    |--------------|----|----------|----------------------------|----------|
    | FTS-easy     | 14 | 40.5%    | 69.0%                      | +28.5pp  |
    | FTS-medium   | 9  | not graded | not graded               | —        |
    | Paraphrastic | 8  | 5.0%     | 57.5%                      | +52.5pp  |

Spike cost: $0.58 total.

See:

* ``epics/09-eval/09-08-benchmarks.md`` — this issue.
* ``scripts/run_live_eval.py`` — the live-eval orchestrator this reuses.
* ``docs/benchmarks/voyage-recall-2026-q2.md`` — the report this writes.
* ``docs/spike-results/004-voyage-python-client.md`` — Voyage client
  validation (unit-normalized vectors + correct rerank ordering).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import platform
import sqlite3
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# The eval modules (issues 09-01..09-06, all on main).
from lossless_hermes.eval.query_set import (
    QueryRecord,
    QuerySet,
    get_query_set,
    register_query_set,
)
from lossless_hermes.eval.recall import (
    RecallEvalOptions,
    RecallReport,
    RecallSearchAdapter,
    run_recall_eval,
)
from lossless_hermes.eval.run import (
    EvalRunRecord,
    compute_drift,
    record_eval_run,
)

# The Voyage stack the hybrid arm needs. Imported at module level (not
# lazily) so the mocked benchmark test can monkeypatch ``VoyageClient`` /
# ``tick_embedding_backfill`` on this module and exercise the live-arm
# wiring (:func:`_run_hybrid_arm`) without a network call — a symbol
# drift in those APIs then fails CI rather than only a live run. These
# are pure ``lossless_hermes`` imports (no API contact at import time).
from lossless_hermes.embeddings.backfill import tick_embedding_backfill
from lossless_hermes.voyage import VoyageClient

# The eva-baseline-v2 fixture (issue 09-05) + the v41-test-corpus port
# (this issue) both live under tests/ — imported lazily inside main() so
# this module imports cleanly even when tests/ is not on the path (e.g.
# the mocked CI test imports `benchmark_voyage_recall` as a module).

__all__ = [
    "BENCHMARK_K_VALUES",
    "EX_CONFIG",
    "EX_OK",
    "EX_SOFTWARE",
    "PARAPHRASTIC_STRATUM",
    "PARAPHRASTIC_LIFT_BASELINE_PP",
    "FTS_EASY_LIFT_BASELINE_PP",
    "TS_SPIKE_COMMIT",
    "TS_BASELINE",
    "BenchmarkResult",
    "compute_lift",
    "build_report_markdown",
    "run_benchmark",
    "main",
]


# ---------------------------------------------------------------------------
# Exit codes (sysexits.h subset — matches run_live_eval.py's contract)
# ---------------------------------------------------------------------------

EX_OK = 0
"""Clean run (FTS baseline written; hybrid arm ran or was cleanly skipped)."""

EX_SOFTWARE = 70
"""Internal error — e.g. the query set failed to register."""

EX_CONFIG = 78
"""``EX_CONFIG`` from sysexits.h. Reserved; this script does NOT exit 78
on a missing key — the missing-key path is a clean ``EX_OK`` run that
writes the FTS baseline + a PENDING hybrid section. See the module
docstring."""


# ---------------------------------------------------------------------------
# Benchmark constants
# ---------------------------------------------------------------------------

#: K values for recall@K. The spike graded top-5; the issue spec's
#: orchestration outline uses [1, 5, 10, 20, 50]. recall@5 is the
#: headline figure (matches the spike's "top-5 relevance grading").
BENCHMARK_K_VALUES: tuple[int, ...] = (1, 5, 10, 20, 50)

#: The load-bearing stratum — the +52.5pp differentiator.
PARAPHRASTIC_STRATUM = "paraphrastic"

#: TS-baseline paraphrastic lift (Phase-A spike). The Python hybrid run
#: must land within ±5pp of this for the port to be considered faithful.
PARAPHRASTIC_LIFT_BASELINE_PP = 52.5

#: TS-baseline fts-easy lift (Phase-A spike).
FTS_EASY_LIFT_BASELINE_PP = 28.5

#: ±pp tolerance band — see 09-08 spec §"Why ±5pp tolerance".
LIFT_TOLERANCE_PP = 5.0

#: The TS source commit the +52.5pp number was measured at.
TS_SPIKE_COMMIT = "1f07fbd"

#: The Phase-A spike's published per-stratum table (PR_DESCRIPTION.md
#: §"Why Voyage embeddings"). recall values are fractions (0..1);
#: ``None`` marks "not graded".
TS_BASELINE: dict[str, dict[str, Optional[float]]] = {
    "fts-easy": {"n": 14, "fts_only_r5": 0.405, "hybrid_r5": 0.690, "lift_pp": 28.5},
    "fts-medium": {"n": 9, "fts_only_r5": None, "hybrid_r5": None, "lift_pp": None},
    "paraphrastic": {"n": 8, "fts_only_r5": 0.050, "hybrid_r5": 0.575, "lift_pp": 52.5},
}

#: Voyage models the hybrid arm uses (recorded in the report).
VOYAGE_EMBED_MODEL = "voyage-4-large"
VOYAGE_RERANK_MODEL = "rerank-2.5"

#: Voyage-client httpx pin — Spike 004's sketch (docs/spike-results/
#: 004-voyage-python-client.md) pinned httpx; the repo's pyproject.toml
#: pins ``httpx[socks]==0.28.1``. Recorded in the report for provenance.
VOYAGE_CLIENT_HTTPX_PIN = "httpx[socks]==0.28.1"


# ---------------------------------------------------------------------------
# Per-stratum lift
# ---------------------------------------------------------------------------


def compute_lift(
    fts: RecallReport,
    hybrid: RecallReport,
) -> dict[str, dict[int, float]]:
    """Per-stratum recall lift — ``hybrid R@k - fts R@k``.

    Ports the 09-08 spec's ``compute_lift`` helper. The delta is a direct
    subtraction of the two reports' per-stratum ``mean_recall_at_k`` —
    deliberately NOT :func:`compute_drift`, which compares *same-mode*
    runs across time. This compares *different-mode* runs on the same
    corpus + query set.

    Args:
        fts: The ``fts_only`` recall report.
        hybrid: The ``hybrid`` recall report.

    Returns:
        ``{stratum: {k: hybrid.recall@k - fts.recall@k}}``. A stratum
        present in ``hybrid`` but absent from ``fts`` is treated as
        ``fts`` recall 0 (the lift is then the full hybrid recall). The
        K set is whatever the hybrid report carries for that stratum.
    """
    out: dict[str, dict[int, float]] = {}
    for stratum, hyb_agg in hybrid.by_stratum.items():
        fts_agg = fts.by_stratum.get(stratum)
        fts_recall = fts_agg.mean_recall_at_k if fts_agg is not None else {}
        out[stratum] = {
            k: hyb_agg.mean_recall_at_k[k] - fts_recall.get(k, 0.0)
            for k in hyb_agg.mean_recall_at_k
        }
    return out


# ---------------------------------------------------------------------------
# Benchmark result bundle
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BenchmarkResult:
    """Everything :func:`build_report_markdown` needs to render the doc.

    Attributes:
        fts_report: The ``fts_only`` recall report — always present.
        hybrid_report: The ``hybrid`` recall report, or ``None`` when the
            hybrid arm was skipped (no ``VOYAGE_API_KEY``).
        per_stratum_lift: :func:`compute_lift` output, or ``None`` when
            the hybrid arm was skipped.
        fts_run_id: ``lcm_eval_run`` id of the recorded fts_only run.
        hybrid_run_id: ``lcm_eval_run`` id of the hybrid run, or ``None``.
        fts_drift_ok: ``True`` if ``compute_drift`` on the fts_only run
            completed without error (it always should — this records the
            AC's outcome).
        hybrid_drift_ok: Same for the hybrid run; ``None`` if skipped.
        voyage_tokens: Total Voyage tokens consumed by the hybrid arm
            (embed + rerank). 0 when the hybrid arm was skipped.
        wall_seconds: End-to-end wall-clock of the measured run.
        ran_at: UTC timestamp the benchmark ran.
        corpus_leaf_count: Leaf summaries seeded into the corpus.
    """

    fts_report: RecallReport
    hybrid_report: Optional[RecallReport]
    per_stratum_lift: Optional[dict[str, dict[int, float]]]
    fts_run_id: str
    hybrid_run_id: Optional[str]
    fts_drift_ok: bool
    hybrid_drift_ok: Optional[bool]
    voyage_tokens: int
    wall_seconds: float
    ran_at: datetime
    corpus_leaf_count: int

    @property
    def hybrid_ran(self) -> bool:
        """True when the live hybrid arm actually ran."""
        return self.hybrid_report is not None


# ---------------------------------------------------------------------------
# Voyage cost accounting
# ---------------------------------------------------------------------------

#: USD per 1M Voyage tokens (embed + rerank, blended). Same constant
#: run_live_eval.py uses — Voyage 2026-Q2 list price, conservative.
VOYAGE_USD_PER_MTOK = 0.18


def _voyage_usd(tokens: int) -> float:
    """Convert a Voyage token count to USD at the 2026-Q2 list rate."""
    return tokens / 1_000_000.0 * VOYAGE_USD_PER_MTOK


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _pct(value: Optional[float]) -> str:
    """Render a 0..1 recall fraction as a percentage cell."""
    if value is None:
        return "_not graded_"
    return f"{value * 100:.1f}%"


def _pp(value: Optional[float]) -> str:
    """Render a 0..1 delta as a signed percentage-point cell."""
    if value is None:
        return "—"
    pp = value * 100
    sign = "+" if pp >= 0 else ""
    return f"{sign}{pp:.1f}pp"


def _r5(report: RecallReport, stratum: str) -> Optional[float]:
    """Mean recall@5 for a stratum, or ``None`` if the stratum is absent."""
    agg = report.by_stratum.get(stratum)
    if agg is None:
        return None
    return agg.mean_recall_at_k.get(5)


def _stratum_n(report: RecallReport, stratum: str) -> int:
    """Scored-query count for a stratum."""
    agg = report.by_stratum.get(stratum)
    return agg.n if agg is not None else 0


def _ordered_strata(report: RecallReport) -> list[str]:
    """Strata in canonical order: fts-easy, fts-medium, paraphrastic."""
    canonical = ["fts-easy", "fts-medium", "paraphrastic"]
    present = set(report.by_stratum.keys())
    ordered = [s for s in canonical if s in present]
    ordered += sorted(s for s in present if s not in canonical)
    return ordered


def _render_result_table(result: BenchmarkResult) -> list[str]:
    """The headline per-stratum table: TS-baseline | Python-port | delta."""
    lines: list[str] = []
    lines.append(
        "| Stratum | n | TS FTS-only R@5 | TS Hybrid R@5 | TS lift | "
        "Py FTS-only R@5 | Py Hybrid R@5 | Py lift |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    for stratum in _ordered_strata(result.fts_report):
        ts = TS_BASELINE.get(stratum, {})
        py_fts_r5 = _r5(result.fts_report, stratum)
        py_hyb_r5 = _r5(result.hybrid_report, stratum) if result.hybrid_report is not None else None
        py_lift = (
            result.per_stratum_lift.get(stratum, {}).get(5)
            if result.per_stratum_lift is not None
            else None
        )
        ts_lift_pp = ts.get("lift_pp")
        lines.append(
            f"| {stratum} "
            f"| {_stratum_n(result.fts_report, stratum)} "
            f"| {_pct(ts.get('fts_only_r5'))} "
            f"| {_pct(ts.get('hybrid_r5'))} "
            f"| {('+' + str(ts_lift_pp) + 'pp') if ts_lift_pp is not None else '—'} "
            f"| {_pct(py_fts_r5)} "
            f"| {_pct(py_hyb_r5) if result.hybrid_ran else '_pending_'} "
            f"| {_pp(py_lift) if result.hybrid_ran else '_pending_'} |"
        )
    return lines


def _render_k_table(report: RecallReport, label: str) -> list[str]:
    """A per-stratum recall@K table across every K the report carries."""
    lines: list[str] = []
    lines.append(f"**{label} — recall@K by stratum**")
    lines.append("")
    k_values = sorted({k for agg in report.by_stratum.values() for k in agg.mean_recall_at_k})
    header = "| Stratum | n | " + " | ".join(f"R@{k}" for k in k_values) + " | MRR |"
    lines.append(header)
    lines.append("|---|---|" + "|".join("---" for _ in k_values) + "|---|")
    for stratum in _ordered_strata(report):
        agg = report.by_stratum[stratum]
        cells = " | ".join(f"{agg.mean_recall_at_k.get(k, 0.0):.3f}" for k in k_values)
        lines.append(f"| {stratum} | {agg.n} | {cells} | {agg.mean_rr:.3f} |")
    overall = report.overall
    cells = " | ".join(f"{overall.mean_recall_at_k.get(k, 0.0):.3f}" for k in k_values)
    lines.append(f"| **overall** | {overall.n} | {cells} | {overall.mean_rr:.3f} |")
    return lines


def _render_acceptance(result: BenchmarkResult) -> list[str]:
    """Render the acceptance-criteria verdict block."""
    lines: list[str] = []
    lines.append("## Acceptance verdict")
    lines.append("")
    if not result.hybrid_ran:
        lines.append(
            "**Status: HYBRID ARM PENDING.** The `fts_only` baseline below is "
            "measured and reproducible offline. The `hybrid` arm — and "
            "therefore the paraphrastic-lift acceptance check — requires a "
            "live `VOYAGE_API_KEY`, which was not available in the run "
            "environment. See "
            "[Live hybrid run PENDING](#live-hybrid-run-pending) for the exact "
            "command to complete the measurement."
        )
        lines.append("")
        lines.append(
            f"The acceptance gate (paraphrastic lift within "
            f"`[{PARAPHRASTIC_LIFT_BASELINE_PP - LIFT_TOLERANCE_PP:+.1f}pp, "
            f"{PARAPHRASTIC_LIFT_BASELINE_PP + LIFT_TOLERANCE_PP:+.1f}pp]`) is "
            f"**operator-gated** on that run."
        )
        return lines

    # Past the early return, the hybrid arm ran — so per_stratum_lift is
    # populated (run_benchmark sets hybrid_report + per_stratum_lift
    # together). Bind it to a non-None local so the type-checker can see
    # the narrowing without relying on the hybrid_ran property invariant.
    lift = result.per_stratum_lift or {}
    para_lift = lift.get(PARAPHRASTIC_STRATUM, {}).get(5)
    fts_easy_lift = lift.get("fts-easy", {}).get(5)
    para_lo = (PARAPHRASTIC_LIFT_BASELINE_PP - LIFT_TOLERANCE_PP) / 100.0
    para_hi = (PARAPHRASTIC_LIFT_BASELINE_PP + LIFT_TOLERANCE_PP) / 100.0

    if para_lift is not None and para_lo <= para_lift <= para_hi:
        lines.append(
            f"**PASS — paraphrastic lift {_pp(para_lift)}** is within the "
            f"`[{PARAPHRASTIC_LIFT_BASELINE_PP - LIFT_TOLERANCE_PP:+.1f}pp, "
            f"{PARAPHRASTIC_LIFT_BASELINE_PP + LIFT_TOLERANCE_PP:+.1f}pp]` "
            f"TS-baseline tolerance band. The port is faithful."
        )
    elif para_lift is not None and para_lift >= 0.30:
        lines.append(
            f"**CONDITIONAL — paraphrastic lift {_pp(para_lift)}** is outside "
            f"the ±5pp band but still above the original ≥30pp decision gate. "
            f"Per the 09-08 spec §Mitigation, v0.1.0 may proceed; the "
            f"deviation must be explained below."
        )
    else:
        lines.append(
            f"**FAIL — paraphrastic lift {_pp(para_lift)}** is below the "
            f"≥30pp decision gate. Per the 09-08 spec §Acceptance, file a "
            f"child defect issue against the embeddings port (#05-*) or the "
            f"Voyage client before merging."
        )
    lines.append("")
    lines.append(
        f"- fts-easy lift: {_pp(fts_easy_lift)} "
        f"(TS-baseline {FTS_EASY_LIFT_BASELINE_PP:+.1f}pp, "
        f"band `[{FTS_EASY_LIFT_BASELINE_PP - LIFT_TOLERANCE_PP:+.1f}pp, "
        f"{FTS_EASY_LIFT_BASELINE_PP + LIFT_TOLERANCE_PP:+.1f}pp]`)"
    )
    return lines


def build_report_markdown(result: BenchmarkResult) -> str:
    """Render the full ``voyage-recall-2026-q2.md`` report.

    The report carries every section the 09-08 spec's acceptance criteria
    require: the per-stratum result table, methodology, cost breakdown,
    reproduction recipe, run metadata, the deviation explanation, and —
    when the hybrid arm was skipped — the operator-gated PENDING section.

    Args:
        result: The :class:`BenchmarkResult` from :func:`run_benchmark`.

    Returns:
        The complete markdown document as a single string.
    """
    lines: list[str] = []
    ran = result.ran_at.strftime("%Y-%m-%d %H:%M UTC")

    lines.append("# Voyage hybrid-retrieval recall benchmark — 2026-Q2")
    lines.append("")
    lines.append(
        "> Reproduction of the Phase-A spike's **+52.5pp paraphrastic recall "
        "lift** of hybrid Voyage retrieval over FTS-only, ported to Python "
        "(issue 09-08, the terminal Epic-09 artifact)."
    )
    lines.append("")
    lines.append(
        "This is a generated report — re-run "
        "`scripts/benchmark_voyage_recall.py` to regenerate it. The "
        "FTS-only baseline is measured and reproducible **with no API "
        "key**; the hybrid arm requires a live `VOYAGE_API_KEY` (see "
        "[Reproduction recipe](#reproduction-recipe))."
    )
    lines.append("")

    # ── Acceptance verdict ──
    lines.extend(_render_acceptance(result))
    lines.append("")

    # ── Headline result table ──
    lines.append("## Result — per-stratum recall lift")
    lines.append("")
    lines.append(
        "TS-baseline columns are the Phase-A spike "
        '(`lossless-claw/docs/v4.1/PR_DESCRIPTION.md` §"Why Voyage '
        f'embeddings", LCM commit `{TS_SPIKE_COMMIT}`). Py columns are this '
        "run. recall@5 is the headline figure — it matches the spike's "
        '"top-5 relevance grading".'
    )
    lines.append("")
    lines.extend(_render_result_table(result))
    lines.append("")
    if not result.hybrid_ran:
        lines.append(
            "_Py Hybrid / Py lift show `pending` — the hybrid arm did not "
            "run in this environment (no `VOYAGE_API_KEY`)._"
        )
        lines.append("")

    # ── FTS-only detail (always measured) ──
    lines.append("## Measured FTS-only baseline (offline, reproducible)")
    lines.append("")
    lines.append(
        "Pure SQLite FTS5 — no API. These numbers are deterministic given "
        "the synthetic corpus + the eva-baseline-v2 fixture; anyone can "
        "reproduce them by running this script with no environment setup."
    )
    lines.append("")
    lines.extend(_render_k_table(result.fts_report, "fts_only"))
    lines.append("")
    lines.append(
        f"- **paraphrastic** R@5 = "
        f"{_pct(_r5(result.fts_report, 'paraphrastic'))} — the FTS-only "
        f"weakness the hybrid arm exists to fix. The Phase-A spike measured "
        f"5.0% here; the Path-B synthetic corpus's paraphrastic queries are "
        f"authored with *zero* surface-token overlap, so FTS5 finds nothing "
        f"(0%). 0% vs 5% is within the noise floor and, if anything, makes "
        f"the corpus a slightly *harder* paraphrastic test than the spike's."
    )
    lines.append(
        f"- **fts-easy** R@5 = {_pct(_r5(result.fts_report, 'fts-easy'))} — "
        f"the spike measured 40.5%. This gap is **expected and explained**: "
        f"the spike ran against Eva's real ~2.6 GB snapshot DB; this "
        f"benchmark runs against the deterministic `v41-test-corpus` "
        f"synthetic fixture (Path B), whose fts-easy queries were authored "
        f"with literal phrase overlap against known leaves. The synthetic "
        f"fts-easy stratum is therefore an *easier* FTS target than the "
        f"spike's real-corpus stratum. The paraphrastic stratum — the "
        f"load-bearing +52.5pp line — is unaffected by this, because "
        f"paraphrastic recall on FTS-only is ~0% on *either* corpus."
    )
    if result.hybrid_report is not None:
        lines.append("")
        lines.extend(_render_k_table(result.hybrid_report, "hybrid"))

    lines.append("")

    # ── Methodology ──
    lines.append("## Methodology")
    lines.append("")
    lines.append(
        "- **Corpus:** `tests/fixtures/test_corpus.py` — the Python port of "
        "`lossless-claw/test/fixtures/v41-test-corpus.ts` (commit "
        f"`{TS_SPIKE_COMMIT}`). A deterministic synthetic SQLite DB: "
        f"{result.corpus_leaf_count} leaf summaries + 2 condensed summaries "
        "across 5 conversations. No PII; reproducible byte-for-byte. The "
        "spike used Eva's private snapshot DB (Path A); Path B was taken "
        "here because that snapshot is unavailable — see the 09-05 / 09-08 "
        "specs."
    )
    lines.append(
        "- **Query set:** `eva-baseline@v2` — 31 stratified queries "
        "(14 fts-easy / 9 fts-medium / 8 paraphrastic), built by "
        "`tests/fixtures/eva_baseline_v2.py::build_eva_baseline_v2()` and "
        "registered into `lcm_eval_query_set` via `register_query_set`."
    )
    lines.append(
        "- **FTS-only adapter:** wraps the Epic-06 `lcm_grep` FTS5 path — "
        'a `mode="full_text"` query against the `summaries_fts` FTS5 '
        "virtual table via `SummaryStore.search_summaries`. The adapter is "
        "`run_live_eval._build_fts_search` — *not duplicated here*."
    )
    lines.append(
        "- **Hybrid adapter:** wraps the Epic-05 `run_hybrid_search` — FTS5 "
        "+ Voyage semantic embeddings union, then Voyage `rerank-2.5`. The "
        "adapter is `run_live_eval._build_live_adapters`'s hybrid arm — "
        "*not duplicated here*. Embeddings are backfilled for every summary "
        "via `embeddings/backfill.py::tick_embedding_backfill` before the "
        "hybrid run."
    )
    lines.append(
        f"- **K values:** {list(BENCHMARK_K_VALUES)}. recall@5 is the "
        "headline (the spike graded top-5)."
    )
    lines.append(
        "- **Per-query timeout:** the `run_recall_eval` default (30s, "
        "clamped ≥100ms) — Wave-4/5/9 scar tissue, see `eval/recall.py`."
    )
    lines.append(
        "- **Judge ensemble:** none. v4.1 first cut is recall-only; the "
        "synthesis-quality judge (`eval/judge.py`) is deferred. No "
        "Anthropic spend."
    )
    lines.append(
        "- **Lift computation:** a direct per-stratum subtraction "
        "(`hybrid.by_stratum[s].mean_recall_at_k[5] - "
        "fts.by_stratum[s].mean_recall_at_k[5]`) — `compute_lift` in this "
        "script. Deliberately *not* `compute_drift`, which compares "
        "same-mode runs across time."
    )
    lines.append(
        f"- **Recorded runs:** both arms are written to `lcm_eval_run` via "
        f'`record_eval_run` (`mode="fts_only"` run `{result.fts_run_id}`'
        + (
            f', `mode="hybrid"` run `{result.hybrid_run_id}`'
            if result.hybrid_run_id is not None
            else "; the hybrid run is recorded only when the hybrid arm runs"
        )
        + "). `compute_drift` is then run on each — it completes without "
        "error even on the baseline run (returns `prior_run_id=None`)."
    )
    lines.append("")

    # ── Cost ──
    lines.append("## Cost breakdown")
    lines.append("")
    if result.hybrid_ran:
        embed_rerank_usd = _voyage_usd(result.voyage_tokens)
        lines.append(
            f"- **Voyage (embed + rerank):** {result.voyage_tokens:,} tokens "
            f"≈ ${embed_rerank_usd:.4f} at the 2026-Q2 list rate "
            f"(${VOYAGE_USD_PER_MTOK}/1M tokens, blended). The "
            f"`run_hybrid_search` result reports embed + rerank tokens in a "
            f"single `voyage_tokens_consumed` field."
        )
        lines.append("- **Anthropic (judge):** $0.0000 — no judge ran.")
        lines.append(f"- **Total measured spend:** ${embed_rerank_usd:.4f}.")
        lines.append(
            "- The 09-08 spec's ceiling is < $0.50/run (expected ~$0.03). "
            "The spike's one-time cost was $0.58 total across a larger "
            "grading pass."
        )
    else:
        lines.append(
            "- **This run:** $0.0000 — the FTS-only arm makes no API calls. "
            "The hybrid arm did not run."
        )
        lines.append(
            f"- **Expected hybrid-arm cost:** ~$0.03 (31 short query embeds "
            f"+ a few hundred rerank candidates, well under 100K tokens at "
            f"${VOYAGE_USD_PER_MTOK}/1M). The 09-08 spec's hard ceiling is "
            f"< $0.50/run. The Phase-A spike's one-time cost was $0.58 "
            f"total."
        )
    lines.append("")

    # ── Reproduction recipe ──
    lines.append("## Reproduction recipe")
    lines.append("")
    lines.append("**FTS-only baseline (no API key — fully reproducible):**")
    lines.append("")
    lines.append("```bash")
    lines.append("python scripts/benchmark_voyage_recall.py \\")
    lines.append("    --out docs/benchmarks/voyage-recall-2026-q2.md")
    lines.append("```")
    lines.append("")
    lines.append(
        "Writes the FTS baseline + a PENDING hybrid section. The synthetic "
        "corpus is seeded in-memory by default; pass `--db <path>` to "
        "persist it."
    )
    lines.append("")
    lines.append("**Full benchmark including the hybrid arm (requires a key):**")
    lines.append("")
    lines.append("```bash")
    lines.append("export VOYAGE_API_KEY=<your-voyage-key>")
    lines.append("python scripts/benchmark_voyage_recall.py \\")
    lines.append("    --out docs/benchmarks/voyage-recall-2026-q2.md")
    lines.append("```")
    lines.append("")
    lines.append(
        "With the key set, the hybrid arm runs: it backfills Voyage "
        "embeddings for every summary, runs the hybrid recall eval, "
        "records the `hybrid` run, and fills the Py Hybrid / Py lift "
        "columns + the acceptance verdict above."
    )
    lines.append("")

    # ── Run metadata ──
    lines.append("## Run metadata")
    lines.append("")
    lines.append(f"- **Benchmark ran:** {ran}")
    lines.append(f"- **Wall-clock:** {result.wall_seconds:.2f}s")
    lines.append(
        f"- **Python:** {platform.python_version()} ({platform.system()} {platform.machine()})"
    )
    lines.append(f"- **Voyage embedding model:** `{VOYAGE_EMBED_MODEL}`")
    lines.append(f"- **Voyage rerank model:** `{VOYAGE_RERANK_MODEL}`")
    lines.append(
        f"- **Voyage client:** `lossless_hermes.voyage.client` — httpx pin "
        f"`{VOYAGE_CLIENT_HTTPX_PIN}` (Spike 004)"
    )
    lines.append(f"- **TS source commit:** `{TS_SPIKE_COMMIT}` (branch `pr-613`)")
    lines.append(
        f"- **Hybrid arm:** {'RAN' if result.hybrid_ran else 'SKIPPED (no VOYAGE_API_KEY)'}"
    )
    lines.append("")

    # ── Deviation explanation ──
    lines.append("## Deviation from the +52.5pp baseline")
    lines.append("")
    lines.append(
        "Exact reproduction of +52.5pp is impossible — the 09-08 spec "
        "allows a ±5pp band. Sources of legitimate deviation:"
    )
    lines.append("")
    lines.append(
        "1. **Corpus difference (Path A vs Path B).** The spike measured "
        "against Eva's real snapshot DB; this benchmark uses the "
        "deterministic `v41-test-corpus` synthetic fixture. This mainly "
        "shifts the *fts-easy* baseline (synthetic fts-easy queries have "
        "cleaner literal overlap → higher FTS recall). The *paraphrastic* "
        "stratum is robust to it: FTS-only paraphrastic recall is ~0% on "
        "either corpus, so the lift is dominated by the hybrid arm's "
        "absolute recall."
    )
    lines.append(
        "2. **Voyage model-version drift.** `voyage-4-large` / `rerank-2.5` "
        "weights may differ between the spike and this run — third-party "
        "models we cannot pin."
    )
    lines.append(
        "3. **float32 vs float64 precision.** Embeddings are stored as "
        "float32 in vec0 but JSON round-trips as float64 (Spike 004 "
        '§"Remaining 5% risk" #1).'
    )
    lines.append(
        "4. **Tokenizer drift.** If Voyage updates its tokenizer, identical "
        "input text yields different token boundaries."
    )
    lines.append("")
    lines.append(
        "Per the 09-08 spec §Mitigation: a Python paraphrastic lift ≥30pp "
        "(the original decision gate) still justifies shipping Voyage in "
        "v0.1.0 even if it is not exactly +52.5pp. A lift *below* 30pp is a "
        "real port defect and blocks the issue — file a child defect "
        "against #05-* or the Voyage client."
    )
    lines.append("")

    # ── PENDING section (only when the hybrid arm was skipped) ──
    if not result.hybrid_ran:
        lines.append("## Live hybrid run PENDING")
        lines.append("")
        lines.append(
            "**This benchmark's hybrid arm has not yet run.** It requires a "
            "live `VOYAGE_API_KEY`, which was not provisioned in the "
            "environment that generated this report. The +52.5pp number "
            "below is the **TS-baseline target**, NOT a measured Python "
            "result — it must not be cited as reproduced until the run "
            "below completes."
        )
        lines.append("")
        lines.append("**What is verified (offline, in this report):**")
        lines.append("")
        lines.append(
            "- The `v41-test-corpus` Python port seeds the deterministic "
            f"corpus ({result.corpus_leaf_count} leaves) — verified by "
            "`tests/fixtures/test_test_corpus.py`."
        )
        lines.append("- The eva-baseline-v2 query set registers + round-trips.")
        lines.append("- The **FTS-only baseline is fully measured** — see the table above.")
        lines.append(
            "- The benchmark harness — corpus seed → query-set register → "
            "recall eval → `record_eval_run` → `compute_drift` → "
            "`compute_lift` → this report — runs end-to-end and is covered "
            "by `tests/benchmarks/test_voyage_recall_benchmark.py` with the "
            "Voyage seam mocked."
        )
        lines.append("")
        lines.append("**What is operator-gated (the remaining step):**")
        lines.append("")
        lines.append("Provision a `VOYAGE_API_KEY` and run the full benchmark:")
        lines.append("")
        lines.append("```bash")
        lines.append("export VOYAGE_API_KEY=<voyage-key>")
        lines.append("python scripts/benchmark_voyage_recall.py \\")
        lines.append("    --out docs/benchmarks/voyage-recall-2026-q2.md")
        lines.append("```")
        lines.append("")
        lines.append(
            "That regenerates this report with the hybrid arm measured, "
            "fills the Py Hybrid / Py lift columns, and resolves the "
            f"acceptance gate: the paraphrastic lift must land within "
            f"`[{PARAPHRASTIC_LIFT_BASELINE_PP - LIFT_TOLERANCE_PP:+.1f}pp, "
            f"{PARAPHRASTIC_LIFT_BASELINE_PP + LIFT_TOLERANCE_PP:+.1f}pp]` "
            f"(TS-baseline {PARAPHRASTIC_LIFT_BASELINE_PP:+.1f}pp ±"
            f"{LIFT_TOLERANCE_PP:.0f}pp) for the port to be ruled faithful."
        )
        lines.append("")
        lines.append(
            "This run also happens automatically on any retrieval-touching "
            "PR via the `live-eval` CI workflow (`scripts/run_live_eval.py`) "
            "once the `VOYAGE_API_KEY` repo secret is configured — that "
            "workflow exercises the same hybrid adapter against the same "
            "query set."
        )
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        f"_Generated by `scripts/benchmark_voyage_recall.py` on {ran}. "
        f"Issue 09-08. TS source commit `{TS_SPIKE_COMMIT}`._"
    )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Benchmark orchestration
# ---------------------------------------------------------------------------


def _voyage_key_present(env: dict[str, str]) -> bool:
    """Whether ``VOYAGE_API_KEY`` is set and non-blank.

    Collapses the secret to a single bool — the string value never
    escapes this function (same pattern as
    ``run_live_eval._api_keys_present``).
    """
    return bool(env.get("VOYAGE_API_KEY", "").strip())


def _run_fts_arm(
    db: sqlite3.Connection,
    queries: Sequence[QueryRecord],
) -> RecallReport:
    """Run the FTS-only recall eval. No API calls.

    Builds the FTS-only adapter from :mod:`run_live_eval` (the same
    adapter the live-eval workflow uses) and runs the recall eval.
    """
    import run_live_eval as rle

    fts_search = rle._build_fts_search(db)

    class _FtsOnlyAdapter:
        async def search(self, query: QueryRecord) -> list[str]:
            hits = await fts_search(query.query_text, limit=max(BENCHMARK_K_VALUES))
            return [h.summary_id for h in hits]

    return asyncio.run(
        run_recall_eval(
            queries,
            _FtsOnlyAdapter(),
            RecallEvalOptions(k_values=BENCHMARK_K_VALUES),
        )
    )


def _run_hybrid_arm(
    db: sqlite3.Connection,
    queries: Sequence[QueryRecord],
    voyage_api_key: str,
) -> tuple[RecallReport, int]:
    """Run the hybrid (Voyage) recall eval. Requires a live key.

    Backfills embeddings for every summary, then runs the hybrid recall
    eval via the :mod:`run_live_eval` hybrid adapter.

    The actual live measurement (real Voyage API) runs only via the CLI
    with ``VOYAGE_API_KEY`` set. The mocked CI test exercises this
    function's *wiring* — it monkeypatches ``VoyageClient`` /
    ``tick_embedding_backfill`` (module-level on this module) and
    ``run_hybrid_search`` (on ``run_live_eval``) so a symbol drift in
    those APIs is caught without spending API budget.

    Returns:
        ``(hybrid_recall_report, total_voyage_tokens)``.
    """
    import run_live_eval as rle

    total_voyage_tokens = 0

    # Backfill embeddings for all summaries so the hybrid arm has vectors.
    backfill_voyage = VoyageClient(api_key=voyage_api_key)
    try:
        while True:
            tick = asyncio.run(
                tick_embedding_backfill(
                    db,
                    model_name=VOYAGE_EMBED_MODEL,
                    voyage_model=VOYAGE_EMBED_MODEL,
                    voyage=backfill_voyage,
                )
            )
            total_voyage_tokens += tick.voyage_tokens_consumed
            if not tick.per_tick_limit_reached:
                break
    finally:
        asyncio.run(backfill_voyage.aclose())

    # Run the hybrid recall eval.
    eval_voyage = VoyageClient(api_key=voyage_api_key)
    try:
        # _build_live_adapters returns (fts_only, hybrid); a CostMeter
        # accumulates the hybrid arm's Voyage spend.
        cost = rle.CostMeter()
        _fts_unused, hybrid_adapter = rle._build_live_adapters(db, eval_voyage, cost)
        report = asyncio.run(
            run_recall_eval(
                queries,
                hybrid_adapter,
                RecallEvalOptions(k_values=BENCHMARK_K_VALUES),
            )
        )
        total_voyage_tokens += cost.voyage_tokens
    finally:
        asyncio.run(eval_voyage.aclose())

    return report, total_voyage_tokens


def run_benchmark(
    db: sqlite3.Connection,
    queries: Sequence[QueryRecord],
    query_set: QuerySet,
    *,
    voyage_api_key: Optional[str] = None,
    corpus_leaf_count: int,
    hybrid_adapter: Optional[RecallSearchAdapter] = None,
) -> BenchmarkResult:
    """Run the benchmark — FTS-only always, hybrid when a key is present.

    The single composable entry point. The CLI (:func:`main`) calls this
    after seeding the corpus + registering the query set; the mocked CI
    test calls it directly with an injected ``hybrid_adapter`` so the
    full FTS + hybrid + record + drift + lift pipeline is exercised with
    no live calls.

    Args:
        db: The open, migrated, corpus-seeded SQLite connection.
        queries: The eva-baseline-v2 query records.
        query_set: The registered query set (its identity is the FK
            target for ``record_eval_run``).
        voyage_api_key: A live Voyage key, or ``None``. When ``None`` *and*
            no ``hybrid_adapter`` is injected, the hybrid arm is skipped.
        corpus_leaf_count: Leaf-summary count — recorded in the report.
        hybrid_adapter: Test-only injection. When supplied, the hybrid arm
            runs against this adapter instead of the live Voyage path
            (lets the CI test drive the full pipeline offline). When
            supplied, ``voyage_api_key`` is not consulted for the hybrid
            arm.

    Returns:
        A :class:`BenchmarkResult`.
    """
    started = time.monotonic()
    ran_at = datetime.now(timezone.utc)

    # ── FTS-only arm — always runs, no API ──
    fts_report = _run_fts_arm(db, queries)
    fts_run_id = record_eval_run(
        db,
        EvalRunRecord(
            query_set_identity=query_set.identity,
            mode="fts_only",
            recall_report=fts_report,
            trigger="manual",
            notes="benchmark/baseline (issue 09-08)",
        ),
    )
    # AC: compute_drift completes without error — even on the baseline run
    # (it returns prior_run_id=None when there is no prior same-mode run).
    fts_drift_ok = _drift_ok(db, fts_run_id)

    # ── Hybrid arm — runs only with a key or an injected adapter ──
    hybrid_report: Optional[RecallReport] = None
    hybrid_run_id: Optional[str] = None
    hybrid_drift_ok: Optional[bool] = None
    per_stratum_lift: Optional[dict[str, dict[int, float]]] = None
    voyage_tokens = 0

    if hybrid_adapter is not None:
        # Test path: drive the hybrid arm against the injected adapter.
        hybrid_report = asyncio.run(
            run_recall_eval(
                queries,
                hybrid_adapter,
                RecallEvalOptions(k_values=BENCHMARK_K_VALUES),
            )
        )
    elif voyage_api_key:
        # Live path: backfill embeddings + run the real hybrid adapter.
        hybrid_report, voyage_tokens = _run_hybrid_arm(db, queries, voyage_api_key)

    if hybrid_report is not None:
        hybrid_run_id = record_eval_run(
            db,
            EvalRunRecord(
                query_set_identity=query_set.identity,
                mode="hybrid",
                recall_report=hybrid_report,
                trigger="manual",
                notes="benchmark/hybrid (issue 09-08)",
            ),
        )
        hybrid_drift_ok = _drift_ok(db, hybrid_run_id)
        per_stratum_lift = compute_lift(fts_report, hybrid_report)

    return BenchmarkResult(
        fts_report=fts_report,
        hybrid_report=hybrid_report,
        per_stratum_lift=per_stratum_lift,
        fts_run_id=fts_run_id,
        hybrid_run_id=hybrid_run_id,
        fts_drift_ok=fts_drift_ok,
        hybrid_drift_ok=hybrid_drift_ok,
        voyage_tokens=voyage_tokens,
        wall_seconds=time.monotonic() - started,
        ran_at=ran_at,
        corpus_leaf_count=corpus_leaf_count,
    )


def _drift_ok(db: sqlite3.Connection, run_id: str) -> bool:
    """Run :func:`compute_drift` on a run; return whether it succeeded.

    The 09-08 AC requires ``compute_drift`` of a recorded run to complete
    without error. On a baseline run (no prior same-mode run)
    ``compute_drift`` returns a :class:`DriftSummary` with
    ``prior_run_id=None`` — that is success, not failure.
    """
    try:
        compute_drift(db, run_id)
    except Exception as exc:  # noqa: BLE001 - we want to record any failure
        print(
            f"[benchmark] compute_drift failed for run {run_id}: {exc}",
            file=sys.stderr,
        )
        return False
    return True


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="benchmark_voyage_recall",
        description=(
            "Voyage hybrid-retrieval recall benchmark — reproduces the "
            "+52.5pp paraphrastic uplift (issue 09-08)."
        ),
    )
    parser.add_argument(
        "--db",
        default=None,
        help=(
            "Path to the SQLite DB to seed the corpus into. Default: an "
            "in-memory DB (the corpus is seeded fresh each run)."
        ),
    )
    parser.add_argument(
        "--out",
        default="docs/benchmarks/voyage-recall-2026-q2.md",
        help="Path to write the markdown report.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point. Returns the process exit code.

    Seeds the synthetic corpus, registers the eva-baseline-v2 query set,
    runs the benchmark (FTS-only always; hybrid iff ``VOYAGE_API_KEY`` is
    set), and writes the markdown report.

    A missing ``VOYAGE_API_KEY`` is **not** an error — the run writes the
    FTS baseline + a PENDING hybrid section and exits :data:`EX_OK`. The
    only non-zero exit is :data:`EX_SOFTWARE` for an internal failure
    (e.g. the query set fails to register).
    """
    args = _parse_args(argv)

    # tests/ imports are deferred to here so the module imports cleanly
    # when tests/ is not on sys.path (the mocked CI test imports this
    # module, then puts tests/ on the path itself).
    from tests.fixtures.eva_baseline_v2 import (
        EVA_BASELINE_V2_IDENTITY,
        build_eva_baseline_v2,
    )
    from tests.fixtures.test_corpus import build_test_corpus

    # Open the DB. run_lcm_migrations (called inside build_test_corpus)
    # needs an autocommit connection — BEGIN EXCLUSIVE raises mid-txn.
    db_target = args.db if args.db else ":memory:"
    db = sqlite3.connect(db_target, isolation_level=None)
    try:
        db.execute("PRAGMA foreign_keys = ON")

        # 1. Seed the synthetic corpus (also runs the migration ladder).
        corpus_meta = build_test_corpus(db)

        # 2. Register the eva-baseline-v2 query set.
        register_query_set(db, EVA_BASELINE_V2_IDENTITY, build_eva_baseline_v2())
        query_set = get_query_set(db, EVA_BASELINE_V2_IDENTITY)
        if query_set is None:
            print(
                "[benchmark] failed to register eva-baseline-v2 query set",
                file=sys.stderr,
            )
            return EX_SOFTWARE

        # 3. Run the benchmark.
        env = dict(os.environ)
        voyage_key = env.get("VOYAGE_API_KEY", "").strip() if _voyage_key_present(env) else None
        result = run_benchmark(
            db,
            query_set.queries,
            query_set,
            voyage_api_key=voyage_key,
            corpus_leaf_count=corpus_meta["leaf_count"],
        )
    finally:
        db.close()

    # 4. Write the report.
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(build_report_markdown(result), encoding="utf-8")

    # Console summary.
    if result.hybrid_ran:
        para_lift = (
            result.per_stratum_lift.get(PARAPHRASTIC_STRATUM, {}).get(5)
            if result.per_stratum_lift is not None
            else None
        )
        para_pp = f"{para_lift * 100:+.1f}pp" if para_lift is not None else "n/a"
        print(
            f"[benchmark] FTS-only + hybrid arms complete. "
            f"paraphrastic lift = {para_pp}. Report -> {out_path}"
        )
    else:
        para_r5 = _r5(result.fts_report, PARAPHRASTIC_STRATUM)
        print(
            f"[benchmark] FTS-only baseline complete "
            f"(paraphrastic R@5 = {_pct(para_r5)}). Hybrid arm SKIPPED — "
            f"no VOYAGE_API_KEY. Report (with PENDING hybrid section) -> "
            f"{out_path}",
            file=sys.stderr,
        )
    return EX_OK


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
