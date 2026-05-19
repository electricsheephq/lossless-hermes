"""``lcm_doctor`` — read-only, model-callable LCM integrity-scan tool.

Per [ADR-035](../../docs/adr/035-lcm-status-doctor-model-tools.md), this
module exposes LCM's **read-only integrity scan** as a model-callable
agent tool, so the model can self-diagnose mid-turn — see whether the
weird recall it just hit is caused by broken / fallback / truncated
summaries in the substrate, rather than by its own prompt.

### What it wraps

ADR-035 names ``commands/doctor.py::run_scan`` as the read-only doctor
arm to wrap. In the current tree ``run_scan`` is still a **stub** (Epic
08-01 shipped the router + ``run_apply``; the scan-render body was never
filled in). The *actual* read-only scan logic — the part ADR-035 means
by "the existing read-only scan, no new diagnostic logic" — lives in
:func:`lossless_hermes.doctor.shared.get_doctor_summary_stats`: a
DB-wide aggregation over marker-bearing summaries, already ported and
tested (issue 08-06). ``run_apply`` itself calls it.

So this handler wraps :func:`get_doctor_summary_stats` directly. That
honours the ADR's load-bearing invariant — *no new diagnostic logic* —
the handler adds **zero** scan logic; it calls the existing, tested
aggregation and renders its result. (When the ``run_scan`` slash body
is eventually filled in for Epic 08, it will render the same
:class:`~lossless_hermes.doctor.contract.DoctorSummaryStats`; both
surfaces inherit the one scan implementation, exactly as ADR-035
requires.)

### Read-only ⇒ no owner gate

A doctor *scan* enumerates findings; it mutates nothing. The destructive
arm — ``/lcm doctor apply`` — re-summarizes broken rows and IS
owner-gated (ADR-013), and it stays slash-only. Per ADR-035, the scan
tool is **not** owner-gated: ``handle_lcm_doctor`` runs unconditionally,
no policy probe. The write path does not ride in on this tool.

### The output cap (ADR-035 mandatory caveat)

A full integrity scan over a large ``lcm.db`` can enumerate many
findings. Dumped verbatim into a tool result that is the largest
possible diagnostic payload — the exact thing ADR-035 §Consequences
warns against. So the tool variant is **double-capped**:

1. **Finding-count cap** — at most
   :data:`~lossless_hermes.tools._diagnostics.DIAGNOSTIC_DOCTOR_FINDING_CAP`
   (~20, the ADR §"Open questions" row-1 figure) per-summary findings
   are enumerated inline; the rest collapse into a ``"+N more"`` tail
   pointing at the uncapped ``/lcm doctor`` slash command.
2. **Char cap** — the rendered digest is then passed through
   :func:`~lossless_hermes.tools._diagnostics.cap_diagnostic_text` as a
   backstop, so even an adversarially long set of summary IDs cannot
   blow the budget.

The aggregate counts (total / old / truncated / fallback, DB-wide and
per-conversation) are always included — they are tiny and the
highest-signal part of the scan; only the per-summary finding *list* is
capped.

### Handler signature

Per the dispatch contract, every handler is called
``handler(args, **kwargs)`` and returns a JSON string. ``args`` is empty
(:data:`LCM_DOCTOR_SCHEMA` declares no parameters — a whole-DB scan
needs no model input). The handler reads the engine off the ``ctx``
kwarg.

See:

* [ADR-035](../../docs/adr/035-lcm-status-doctor-model-tools.md) — the
  decision record.
* ``doctor/shared.py`` — :func:`get_doctor_summary_stats`, the wrapped
  read-only scan.
* ``commands/doctor.py`` — ``run_scan`` (stubbed) / ``run_apply``
  (the gated write path that stays slash-only).
* ``tools/_diagnostics.py`` — the shared output-cap helper.
* Issue [electricsheephq/lossless-hermes#135].
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Final, Optional

from lossless_hermes.doctor.contract import DoctorSummaryStats
from lossless_hermes.doctor.shared import get_doctor_summary_stats
from lossless_hermes.tools import TOOL_SCHEMAS
from lossless_hermes.tools._common import tool_result
from lossless_hermes.tools._diagnostics import (
    DIAGNOSTIC_DOCTOR_FINDING_CAP,
    cap_diagnostic_text,
)
from lossless_hermes.tools._typebox import object_schema, tool_schema

logger = logging.getLogger("lossless_hermes.tools.doctor")

__all__ = (
    "LCM_DOCTOR_DESCRIPTION",
    "LCM_DOCTOR_SCHEMA",
    "handle_lcm_doctor",
)


# ===========================================================================
# Schema — OpenAI function-call format
# ===========================================================================
#
# ADR-016 §Consequences requires tool-schema description prose to be
# verbatim from the TS source. ``lcm_doctor`` has NO TS source — it is a
# NEW tool introduced by ADR-035, not a port of an ``lcm-*-tool.ts``
# factory. So this description is original prose, written (per ADR-035
# §"Open questions" row 3) to make clear this is a *read-only integrity
# scan* the model should reach for when it suspects broken summaries.

LCM_DOCTOR_DESCRIPTION: Final[str] = (
    "Read-only self-diagnosis: scan LCM's stored summaries for integrity "
    "problems. Call this after a recall that returned a thin or broken summary, "
    "to tell apart 'my prompt is the problem' from 'LCM's substrate is "
    "degraded'. Reports counts of broken summaries by kind — fallback "
    "(synthesis fell back to a degraded summary), truncated (summary content "
    "was cut for size), and legacy-marker — both DB-wide and per conversation, "
    "plus a capped list of the affected summary IDs. This is a SCAN ONLY: it "
    "mutates nothing, writes nothing, and needs no owner permission. It does "
    "NOT repair anything — repairing broken summaries is the owner-gated "
    "/lcm doctor apply slash command, not this tool. Output is capped (~20 "
    "findings, ~1.5K tokens); an operator can run the /lcm doctor slash "
    "command for the full uncapped scan. Use lcm_status for context-pressure "
    "and cache state rather than summary integrity."
)
"""Model-facing prose for ``lcm_doctor``. ADR-035 introduces this tool —
it has no TS source, so this is original prose (not a verbatim port).
Written per ADR-035 §"Open questions" row 3 to steer the model to reach
for it as a *read-only integrity scan*, and to make explicit that it
never repairs (the write path stays slash-only)."""


LCM_DOCTOR_SCHEMA: Final[dict[str, Any]] = tool_schema(
    name="lcm_doctor",
    description=LCM_DOCTOR_DESCRIPTION,
    # ADR-035 §Consequences: "Both schemas take no parameters." A
    # whole-DB integrity scan needs no model-supplied input — it
    # operates on the engine's DB. ``object_schema()`` with no kwargs
    # yields ``{"type": "object", "properties": {}, "required": []}``.
    parameters=object_schema(),
)
"""OpenAI-function-call schema for ``lcm_doctor``. Empty-parameter object
per ADR-035 §Consequences — the tool needs no model-supplied input."""


# Register at module import time per the TOOL_SCHEMAS contract documented
# in tools/__init__.py. tools/__init__.py imports this module AFTER
# TOOL_SCHEMAS is defined, so the append target exists.
TOOL_SCHEMAS.append(LCM_DOCTOR_SCHEMA)


# ===========================================================================
# Engine DB resolution
# ===========================================================================


def _resolve_db(ctx: Any) -> Optional[sqlite3.Connection]:
    """Resolve the open :class:`sqlite3.Connection` off the engine.

    Mirrors the resolver in :mod:`lossless_hermes.commands.doctor` — the
    engine's canonical handle is ``_db`` (set by
    :meth:`~lossless_hermes.engine.lifecycle._LifecycleMixin.on_session_start`),
    but a few test fixtures expose it under alternate names. Returns
    :data:`None` on a miss; the handler then renders a structured
    ``db-unavailable`` payload rather than raising.

    Args:
        ctx: The engine (or engine stand-in).

    Returns:
        The open connection, or :data:`None` when the engine has no DB
        wired yet (pre-``on_session_start``) or the attribute is absent.
    """
    if ctx is None:
        return None
    for attr in ("_db", "db_connection", "db", "_conn", "conn"):
        candidate = getattr(ctx, attr, None)
        if isinstance(candidate, sqlite3.Connection):
            return candidate
    return None


# ===========================================================================
# Digest renderer — bounded text view of a DoctorSummaryStats
# ===========================================================================


def _render_doctor_digest(stats: DoctorSummaryStats) -> str:
    """Render a :class:`DoctorSummaryStats` as a bounded plain-text digest.

    This is the tool-variant render — distinct from (and much smaller
    than) any operator-facing ``/lcm doctor`` report. It adds no scan
    logic: ``stats`` is already the full result of the existing
    :func:`get_doctor_summary_stats` scan; this function only *formats*
    it, applying the ADR-035 finding-count cap.

    Structure:

    * A one-line health verdict (``healthy`` when ``total == 0``,
      ``degraded`` otherwise) — ADR-035 §"Open questions" row 2 notes a
      leading verdict the model can branch on cheaply is recommended.
    * DB-wide totals by marker kind.
    * Per-conversation breakdown (one line per affected conversation).
    * The affected summary IDs, capped at
      :data:`DIAGNOSTIC_DOCTOR_FINDING_CAP` with a ``"+N more"`` tail.

    Args:
        stats: The scan result from :func:`get_doctor_summary_stats`.

    Returns:
        A multi-line plain-text digest. The summary-ID list is capped;
        the caller additionally passes the whole string through
        :func:`cap_diagnostic_text` as a char-budget backstop.
    """
    if stats.total == 0:
        return (
            "verdict: healthy\n"
            "No broken summaries found — LCM's stored summaries pass the "
            "integrity scan (no fallback, truncated, or legacy-marker rows)."
        )

    lines: list[str] = [
        "verdict: degraded",
        (
            f"Integrity scan found {stats.total} broken summary(s) "
            f"({stats.fallback} fallback, {stats.truncated} truncated, "
            f"{stats.old} legacy-marker)."
        ),
        "",
        "By conversation:",
    ]
    for conversation_id, counts in stats.by_conversation.items():
        lines.append(
            f"  conversation {conversation_id}: {counts.total} total "
            f"({counts.fallback} fallback, {counts.truncated} truncated, "
            f"{counts.old} legacy)"
        )

    # ADR-035 finding-count cap — enumerate at most DIAGNOSTIC_DOCTOR_
    # FINDING_CAP per-summary findings inline; the rest collapse into a
    # "+N more" tail that points at the uncapped slash command.
    lines.append("")
    lines.append("Affected summaries:")
    shown = stats.candidates[:DIAGNOSTIC_DOCTOR_FINDING_CAP]
    for candidate in shown:
        lines.append(
            f"  {candidate.summary_id} "
            f"(conversation {candidate.conversation_id}, "
            f"{candidate.marker_kind.value})"
        )
    remaining = len(stats.candidates) - len(shown)
    if remaining > 0:
        lines.append(f"  ... +{remaining} more — run `/lcm doctor` for the full list")

    return "\n".join(lines)


# ===========================================================================
# Handler
# ===========================================================================


def handle_lcm_doctor(
    args: dict[str, Any],
    *,
    ctx: Optional[Any] = None,
    **_kwargs: Any,
) -> str:
    """Handle an ``lcm_doctor`` tool call — read-only integrity scan.

    Resolves the engine's DB, runs the existing read-only scan
    (:func:`lossless_hermes.doctor.shared.get_doctor_summary_stats`,
    DB-wide), renders a bounded digest of the result, and caps it to
    the tool-result budget. Adds **no** scan logic — see the module
    docstring's "what it wraps" section and ADR-035's delegation
    invariant.

    ``lcm_doctor`` is **not** owner-gated (ADR-013 gates the
    *destructive* ``/lcm doctor apply``; a scan mutates nothing) and is
    **not** in
    :data:`lossless_hermes.plugin.needs_compact_gate.TOKEN_GATE_TOOLS`
    (a diagnostic must stay callable when context is near-full).

    Args:
        args: The tool-call ``arguments`` dict. ``lcm_doctor`` declares
            no parameters (:data:`LCM_DOCTOR_SCHEMA`), so this is
            expected to be empty; any keys are ignored.
        ctx: The :class:`~lossless_hermes.engine.LCMEngine` (passed by
            the dispatch layer as the ``ctx`` kwarg). When ``None`` the
            handler returns a structured ``engine-unavailable`` payload.
        **_kwargs: Other dispatch kwargs — unused; the scan reads only
            the engine's DB.

    Returns:
        A JSON string (per :func:`tool_result`). The inner payload is:

        * ``{"ok": False, "reason": "engine-unavailable", "note": ...}``
          when ``ctx`` is ``None``.
        * ``{"ok": False, "reason": "db-unavailable", "note": ...}``
          when the engine has no DB open yet (pre-``on_session_start``).
        * ``{"ok": True, "report": <capped digest>, "capped": <bool>,
          "total": <int>, "verdict": "healthy" | "degraded"}`` on
          success. ``report`` is the bounded text digest; ``total`` is
          the DB-wide broken-summary count; ``capped`` is ``True`` when
          the digest text was truncated by the char backstop.
        * ``{"ok": False, "reason": "exception", "note": ...}`` if the
          scan raises unexpectedly.
    """
    del args  # lcm_doctor takes no parameters (empty-param schema).

    if ctx is None:
        return tool_result({
            "ok": False,
            "reason": "engine-unavailable",
            "note": (
                "LCM engine is not available. The plugin may still be "
                "initializing — try again on the next turn."
            ),
        })

    db = _resolve_db(ctx)
    if db is None:
        return tool_result({
            "ok": False,
            "reason": "db-unavailable",
            "note": (
                "LCM database is not open yet — send at least one message "
                "so the engine runs on_session_start, then retry."
            ),
        })

    try:
        # DB-wide scan (conversation_id=None). This is the existing,
        # tested read-only aggregation — the handler adds no scan logic.
        stats = get_doctor_summary_stats(db, conversation_id=None)
    except Exception as exc:  # noqa: BLE001 — handler must return text, not raise
        logger.warning("[lcm] lcm_doctor tool: scan raised", exc_info=True)
        return tool_result({
            "ok": False,
            "reason": "exception",
            "note": (
                f"lcm_doctor scan failed: {exc}. Check the gateway log; "
                "the /lcm doctor slash command may still work."
            ),
        })

    digest = _render_doctor_digest(stats)
    # ADR-035 mandatory caveat — char-budget backstop on top of the
    # finding-count cap already applied inside _render_doctor_digest.
    capped_digest = cap_diagnostic_text(digest, slash_command="doctor")
    verdict = "healthy" if stats.total == 0 else "degraded"
    return tool_result({
        "ok": True,
        "report": capped_digest,
        "capped": capped_digest != digest,
        "total": stats.total,
        "verdict": verdict,
    })
