"""Port of ``lcm_synthesize_around`` — fresh synthesis over a window of leaves.

Ports ``lossless-claw/src/tools/lcm-synthesize-around-tool.ts`` (LCM commit
``1f07fbd`` on branch ``pr-613``, 1477 LOC TS → ~900 LOC Python). The
TypeBox-declared schema lives at TS lines 61-144; the handler body at
lines 637-1474. Both are translated structurally verbatim per ADR-016
(description prose byte-identical from TS source).

What this tool does
-------------------

``lcm_synthesize_around`` is the **synthesis** tool — given a window of
leaf summaries, run them through a single-pass synthesis LLM call and
return the resulting markdown rollup, backed by
:sql:`lcm_synthesis_cache` so subsequent identical calls hit the cache
rather than re-LLM. Three window kinds:

1. **period** — direct date-range or shortcut (``yesterday``,
   ``last-7-days``, etc.). No target required — this is the
   ``lcm_recent``-replacement surface ("what did we work on yesterday?").
2. **time** — ±``windowHours`` around a target summary's ``created_at``.
   Target is REQUIRED (summary_id).
3. **semantic** — top-``windowK`` most-similar leaves to a target
   content/query. Target REQUIRED. Requires Voyage + vec0 (Epic 05).

Period boundaries are computed in the operator's local timezone so
"yesterday" / "this-week" / etc. reflect what a human at the operator's
clock would expect (Wave-10 reviewer P1 fix, handled in
:mod:`._period_parser`).

Wave-N invariants preserved
---------------------------

Per [ADR-029](../../docs/adr/029-wave-fix-provenance.md), the
load-bearing scar tissue this module carries:

* **Wave-7 Auditor #6 P0** — the cache row's ``session_key`` MUST be
  non-empty (the 4-step fallback chain in
  :mod:`lossless_hermes.synthesis.cache_key.resolve_session_key`).
  Without this, the UNIQUE cache index collapses to ``""`` for all
  callers without a session identity, causing CROSS-SESSION CACHE
  POLLUTION.
* **Wave-10 reviewer P1** — the cache UNIQUE index keys on all 7 of
  ``(session_key, range_start, range_end, leaf_fingerprint, grep_filter,
  tier_label, prompt_id)`` — including ``tier_label`` AND ``prompt_id``.
  Without these, ``tier='custom'`` and ``tier='filtered'`` for the same
  ``(range, leaves)`` tuple collided silently returning wrong-tier text,
  and active-prompt updates served stale text from the old ``prompt_id``.
  Handled by :func:`cache_key.insert_cache_row_single_flight`.
* **Wave-12 W2A1 P0 #2** — this tool IS now wrapped by
  :func:`run_with_token_gate`. Previously the 4K-8K-token markdown
  output silently bypassed the per-session token cache, drifting
  downstream gate decisions low. The wrap is invocation-time middleware
  (per Wave-12 F5) — see :meth:`LCMEngine.handle_tool_call`.
* **Wave-12 F8** — the audit row records the *resolved* model (the
  summarizer's primary candidate), NOT dispatch's ``pick_model``
  recommendation. The summarizer-shaped :class:`LlmCall` adapter forwards
  ``actual_model`` so the audit is honest.

Architecture seams
------------------

The handler does NOT depend on :class:`LCMEngine` directly — it consumes
a narrow :class:`SynthesizeAroundContext` Protocol that exposes the
db / conversation_store / timezone / max_source_text_tokens collaborator
surface plus the injected ``build_llm_call`` factory. This lets the test
suite construct a minimal context dict without spinning up the full
engine, and lets the eventual engine wiring substitute the real
summarizer-backed adapter.

References
----------

* TS source: ``lossless-claw/src/tools/lcm-synthesize-around-tool.ts``
  (1477 LOC).
* Porting guide: ``docs/porting-guides/tools.md`` §"lcm_synthesize_around"
  (lines 325-392).
* Issue spec: ``epics/06-tools/06-13-lcm-synthesize-around.md``.
* [ADR-016](../../docs/adr/016-typebox-translation.md) — TypeBox
  hand-translate policy.
* [ADR-029](../../docs/adr/029-wave-fix-provenance.md) — Wave-N
  provenance comments at preserved fix sites.
* TS test fixtures: ``test/lcm-synthesize-around-tool.test.ts`` (757
  LOC) + ``test/v41-period-timezone.test.ts``.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final, Optional, Protocol

from lossless_hermes.estimate_tokens import estimate_tokens
from lossless_hermes.store.conversation import ConversationStore
from lossless_hermes.synthesis.cache_key import (
    CacheKey,
    ExistingCacheRow,
    generate_cache_id,
    insert_cache_row_single_flight,
    leaf_fingerprint as fingerprint_leaves,
    lookup_cache_row,
    resolve_session_key,
)
from lossless_hermes.synthesis.dispatch import (
    LlmCall,
    LlmCallArgs,
    LlmCallResult,
    SynthesisDispatchError,
    SynthesizeRequest,
    dispatch_synthesis,
)
from lossless_hermes.tools import TOOL_SCHEMAS
from lossless_hermes.tools._common import (
    read_string_param,
    tool_result,
)
from lossless_hermes.tools._period_parser import (
    PeriodParseError,
    parse_period_shortcut,
)
from lossless_hermes.tools._typebox import (
    boolean_field,
    number_field,
    object_schema,
    optional,
    string_field,
    tool_schema,
)
from lossless_hermes.tools.conversation_scope import (
    LcmDependencies,
    parse_iso_timestamp_param,
    resolve_lcm_conversation_scope,
)

__all__ = (
    "DEFAULT_WINDOW_HOURS",
    "DEFAULT_WINDOW_K",
    "LCM_SYNTHESIZE_AROUND_DESCRIPTION",
    "LCM_SYNTHESIZE_AROUND_SCHEMA",
    "MAX_SOURCE_TEXT_TOKENS",
    "MAX_WINDOW_HOURS",
    "MAX_WINDOW_K",
    "MIN_WINDOW_HOURS",
    "MIN_WINDOW_K",
    "BuildLlmCall",
    "LeafRow",
    "SynthesizeAroundContext",
    "TargetSummaryRow",
    "build_source_text",
    "handle_lcm_synthesize_around",
    "select_time_window_leaves",
)


# ===========================================================================
# Constants — match TS lines 53-59 verbatim
# ===========================================================================


DEFAULT_WINDOW_HOURS: Final[int] = 24
"""Default ``windowHours`` for time mode (TS line 53)."""

DEFAULT_WINDOW_K: Final[int] = 30
"""Default ``windowK`` for semantic mode (TS line 54)."""

MIN_WINDOW_HOURS: Final[int] = 1
"""Minimum ``windowHours`` (TS line 55)."""

MAX_WINDOW_HOURS: Final[int] = 24 * 7 * 4
"""Maximum ``windowHours`` — 4 weeks (TS line 56)."""

MIN_WINDOW_K: Final[int] = 1
"""Minimum ``windowK`` (TS line 57)."""

MAX_WINDOW_K: Final[int] = 200
"""Maximum ``windowK`` (TS line 58)."""

MAX_SOURCE_TEXT_TOKENS: Final[int] = 50_000
"""Dispatch-side cap on concatenated leaf source text (TS line 59)."""


# ===========================================================================
# Schema — verbatim from TS source (ADR-016 §Consequences)
# ===========================================================================
#
# Description prose is byte-identical to lcm-synthesize-around-tool.ts
# lines 641-653 (the tool-level `description:` block) and the per-field
# `description` strings at lines 62-143. The mechanical TypeBox -> dict
# translation uses the helpers in `_typebox.py`.

LCM_SYNTHESIZE_AROUND_DESCRIPTION: Final[str] = (
    "Synthesize a fresh summary of leaves over a window (replaces old lcm_recent). "
    "Three modes: 'period' (date range or shortcut like 'yesterday' / 'last-7-days' / "
    "'this-month' — target OPTIONAL; this is the direct \"what did we work on yesterday\" "
    "surface), 'time' (leaves within ±windowHours of a target summary's timestamp — "
    "target REQUIRED), or 'semantic' (top windowK most-similar leaves to target "
    "content/query — target REQUIRED). Period boundaries are computed in the operator's "
    "local timezone (configured on the LCM engine; handles half-hour offsets like Asia/Kolkata "
    "and DST transitions). Returns a markdown summary backed by lcm_synthesis_cache so "
    "subsequent identical calls hit the cache. The actual LLM call goes through the "
    "operator's configured summarizer chain (summaryModel/summaryProvider) for inheritance of auth "
    "retries + fallback handling; the audit table records the resolved model that actually ran "
    "(Wave-12 fix — was previously recording the dispatched recommendation). Distinct from "
    "lcm_grep --mode semantic (which returns ranked snippets, not a synthesized rollup)."
)
"""Verbatim from ``lcm-synthesize-around-tool.ts:641-653``. Per ADR-016
§Consequences this is the load-bearing model-facing prose that drives
tool selection. The Wave-12 fix note about "the audit table records the
resolved model that actually ran" is load-bearing — keep verbatim."""


LCM_SYNTHESIZE_AROUND_SCHEMA: Final[dict[str, Any]] = tool_schema(
    name="lcm_synthesize_around",
    description=LCM_SYNTHESIZE_AROUND_DESCRIPTION,
    parameters=object_schema(
        target=optional(
            string_field(
                "Target to anchor the window on. REQUIRED for window_kind='time' and "
                "'semantic'. OPTIONAL (acts as a label) for window_kind='period'. "
                "Pass a `sum_xxx` summary_id (works in 'time' and 'semantic' modes — anchors "
                "on the summary's created_at OR content), OR a free-text query string "
                "(semantic mode only — used as the query embedding directly).",
            ),
        ),
        window_kind=string_field(
            "Window selection. 'time' = ±windowHours around target timestamp (target REQUIRED). "
            "'semantic' = top-windowK most-similar leaves to target content/query (target REQUIRED). "
            "'period' = direct date-range or period-shortcut selection (target OPTIONAL — agent "
            "can ask 'what did we work on yesterday?' without first discovering an anchor leaf).",
            enum=["time", "semantic", "period"],
        ),
        period=optional(
            string_field(
                "Period shortcut for window_kind='period' (case-insensitive). Accepted: "
                "'today' | 'yesterday' | 'this-week' | 'last-week' | 'this-month' | 'last-month' | "
                "'last-7-days' | 'last-30-days' | 'last-Nh' (e.g. 'last-12h' = past 12 hours) | "
                "'last-Nd' (e.g. 'last-3d' = past 3 days). Mutually exclusive with explicit "
                "since/before bounds (use either-or, not both).",
            ),
        ),
        windowHours=optional(
            number_field(
                f"Half-window for time mode (default {DEFAULT_WINDOW_HOURS}, range {MIN_WINDOW_HOURS}-{MAX_WINDOW_HOURS}). Ignored for semantic + period modes.",
                minimum=MIN_WINDOW_HOURS,
                maximum=MAX_WINDOW_HOURS,
            ),
        ),
        windowK=optional(
            number_field(
                f"Top-K size for semantic mode (default {DEFAULT_WINDOW_K}, range {MIN_WINDOW_K}-{MAX_WINDOW_K}). Ignored for time + period modes.",
                minimum=MIN_WINDOW_K,
                maximum=MAX_WINDOW_K,
            ),
        ),
        tier=optional(
            string_field(
                "Synthesis tier (default 'custom'). Both use single-pass dispatch with the "
                "Sonnet-class default model. Use 'filtered' when the leaf set is grep-filtered "
                "(matches the cache CHECK constraint convention).",
                enum=["custom", "filtered"],
            ),
        ),
        conversationId=optional(
            number_field(
                "Physical conversation ID to scope leaf selection to. If omitted, defaults "
                "to the current session family.",
            ),
        ),
        allConversations=optional(
            boolean_field(
                "Set true to include leaves from every conversation. Ignored when "
                "conversationId is provided.",
            ),
        ),
        since=optional(
            string_field(
                "Optional ISO timestamp lower bound. Combined with the chosen window — "
                "e.g., for time mode, the effective window is `MAX(targetCreated - windowHours, since)`.",
            ),
        ),
        before=optional(
            string_field(
                "Optional ISO timestamp upper bound. Combined with the chosen window — "
                "e.g., for time mode, the effective window is `MIN(targetCreated + windowHours, before)`.",
            ),
        ),
    ),
)
"""OpenAI-function-call schema for ``lcm_synthesize_around``. Verbatim
translation of the TypeBox declaration at
``lcm-synthesize-around-tool.ts:61-144`` per ADR-016."""


# Register at module import time per the TOOL_SCHEMAS contract documented
# in tools/__init__.py. The 06-02 dispatch table reads via
# ``get_tool_schemas()`` so this side-effect is what makes the tool
# discoverable to the LCMEngine.
TOOL_SCHEMAS.append(LCM_SYNTHESIZE_AROUND_SCHEMA)


# ===========================================================================
# Types — leaf row, target row, build_llm_call factory shape
# ===========================================================================


class LeafRow(Protocol):
    """Shape of a leaf row returned by :func:`select_time_window_leaves`.

    The handler reads four fields off each leaf — defined as a Protocol
    rather than a dataclass so :class:`sqlite3.Row` (the actual return
    type from the SELECTs) satisfies it structurally. Test stand-ins
    can use a plain dict + accessor.
    """

    summary_id: str
    content: str
    created_at: str
    token_count: int


class TargetSummaryRow(Protocol):
    """Shape of a target-summary row returned by :func:`_lookup_target_summary`.

    Five fields: includes ``conversation_id`` and ``session_key`` so the
    session-key fallback chain (Wave-7 P0) can consult the target's
    own session_key.
    """

    summary_id: str
    content: str
    created_at: str
    conversation_id: int
    session_key: str


class BuildLlmCall(Protocol):
    """Factory that wires the synthesis dispatcher's :class:`LlmCall` callable.

    Mirrors the TS ``buildLlmCallFromSummarizer`` helper at TS lines
    608-618. The factory returns:

    1. An :class:`LlmCall` callable that the dispatcher invokes with
       ``(model, prompt, pass_kind, max_output_tokens)``.
    2. A ``model_name`` string — the resolved primary candidate's model
       identifier (Wave-12 F8 audit-honesty fix: this is what gets
       recorded on the audit row, NOT dispatch's ``pick_model``
       recommendation).

    Production wires this to the configured summarizer chain
    (:class:`LcmSummarizer`); tests inject a deterministic async mock.

    The factory shape is sync (returns ``(callable, str)``) but the
    callable itself is async — matches the TS Promise-based pattern.
    """

    def __call__(self) -> tuple[LlmCall, str]: ...


class SynthesizeAroundContext(Protocol):
    """The handler's collaborator surface.

    Mirrors the slice of :class:`~lossless_hermes.engine.LCMEngine` that
    ``lcm_synthesize_around`` actually needs. Using a structural Protocol
    keeps the handler decoupled from the engine class shape and lets
    tests construct a tiny stand-in dataclass.

    Required attributes:

    * ``conn``: :class:`sqlite3.Connection` for the raw queries
      (target lookup, leaf selection, cache UPDATE).
    * ``conversation_store``: :class:`ConversationStore` for the
      conversation-scope resolver.
    * ``timezone``: IANA timezone name — passed to the period parser
      so day-boundary periods (``yesterday`` etc.) honour the operator's
      local clock.
    * ``build_llm_call``: A :class:`BuildLlmCall` factory the handler
      invokes inside the dispatch path. Returns ``(LlmCall, model_name)``
      where ``model_name`` is the resolved primary candidate (Wave-12
      F8 audit-honesty).

    The semantic-mode branch additionally requires a Voyage-backed
    embeddings adapter; v0.1 ships without semantic mode (Wave A) and
    surfaces a "vec0 unavailable" error when ``window_kind='semantic'``.
    Wave B (Epic 05) wires the semantic adapter.
    """

    conn: sqlite3.Connection
    conversation_store: ConversationStore
    timezone: str
    build_llm_call: BuildLlmCall


# ===========================================================================
# Handler entry point
# ===========================================================================


def handle_lcm_synthesize_around(  # noqa: PLR0912, PLR0915 — mirrors TS structure
    args: dict[str, Any],
    *,
    ctx: SynthesizeAroundContext,
    deps: LcmDependencies,
    session_key: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Handle an ``lcm_synthesize_around`` tool call.

    **Wave-12 F5 invariant:** this is the INNER handler. The
    ``run_with_token_gate`` middleware MUST wrap this call at the
    dispatch layer (issue 06-02 — ``LCMEngine.handle_tool_call``); see
    the module docstring's "Wave-N invariants" section. The wrap MUST
    happen at invocation time, NOT at registration time.

    Args:
        args: The tool-call ``arguments`` dict from the LLM provider.
            Read defensively — see :mod:`lossless_hermes.tools._common`.
        ctx: A :class:`SynthesizeAroundContext` exposing the SQL +
            conversation-store + timezone + LLM-call-factory surface.
        deps: :class:`LcmDependencies` slice.
        session_key: Optional cross-conversation session-family key.
        session_id: Optional runtime session id.

    Returns:
        A JSON string per the :func:`tool_result` contract. The success
        payload is structured ``{"text": "<markdown>", "details": ...}``
        with telemetry; error payloads are ``{"error": "...", ...}``.
    """
    # ----- 1. Validate window_kind FIRST (period mode allows missing target) ---
    window_kind_raw = read_string_param(args, "window_kind")
    if window_kind_raw not in ("time", "semantic", "period"):
        return tool_result({"error": "`window_kind` must be 'time', 'semantic', or 'period'."})
    window_kind: str = window_kind_raw

    # ----- 2. Validate target — REQUIRED for time + semantic ----------------
    target_raw = read_string_param(args, "target")
    target = target_raw if target_raw is not None else ""
    if target == "" and window_kind != "period":
        return tool_result(
            {
                "error": (
                    "`target` is required for window_kind='time' or 'semantic' "
                    "(sum_xxx summary_id OR free-text query). For period mode, "
                    "target is optional."
                ),
            },
        )

    # ----- 3. Numeric window args -------------------------------------------
    window_hours = _clamp_number(
        args.get("windowHours"),
        default=DEFAULT_WINDOW_HOURS,
        minimum=MIN_WINDOW_HOURS,
        maximum=MAX_WINDOW_HOURS,
        truncate=False,
    )
    window_k = _clamp_number(
        args.get("windowK"),
        default=DEFAULT_WINDOW_K,
        minimum=MIN_WINDOW_K,
        maximum=MAX_WINDOW_K,
        truncate=True,
    )

    # ----- 4. Tier selection ------------------------------------------------
    tier_raw = read_string_param(args, "tier")
    tier = tier_raw if tier_raw in ("custom", "filtered") else "custom"
    # lcm_synthesis_cache CHECK constrains tier_label to allowed values.
    cache_tier_label = tier

    # ----- 5. Optional time bounds ------------------------------------------
    try:
        since_bound = parse_iso_timestamp_param(args, "since")
        before_bound = parse_iso_timestamp_param(args, "before")
    except ValueError as exc:
        return tool_result({"error": str(exc)})
    if since_bound is not None and before_bound is not None:
        if _ensure_utc(since_bound) >= _ensure_utc(before_bound):
            return tool_result({"error": "`since` must be earlier than `before`."})

    # ----- 6. Resolve conversation scope ------------------------------------
    scope = resolve_lcm_conversation_scope(
        lcm=_LcmScopeAdapter(_conversation_store=ctx.conversation_store),
        params=args,
        session_id=session_id,
        session_key=session_key,
        deps=deps,
    )
    if not scope.all_conversations and scope.conversation_id is None:
        return tool_result(
            {
                "error": (
                    "No LCM conversation found for this session. "
                    "Provide conversationId or set allConversations=true."
                ),
            },
        )
    conversation_ids: Optional[list[int]] = None
    if not scope.all_conversations:
        if scope.conversation_ids:
            conversation_ids = list(scope.conversation_ids)
        elif scope.conversation_id is not None:
            conversation_ids = [scope.conversation_id]

    db = ctx.conn

    # ----- 7. Resolve target — only summary_id targets allowed for time mode --
    target_is_summary_id = target.startswith("sum_")
    target_summary: Optional[dict[str, Any]] = None
    if target_is_summary_id:
        target_summary = _lookup_target_summary(db, target, conversation_ids)
        if target_summary is None:
            return tool_result(
                {
                    "error": f"Target summary not found in scope: {target}",
                    "hint": (
                        "Verify the summary_id and (if scoped) the conversationId/allConversations."
                    ),
                },
            )
    elif window_kind == "time":
        return tool_result(
            {
                "error": (
                    "time window requires a summary_id target (sum_xxx). "
                    "Free-text queries are only supported in semantic mode."
                ),
            },
        )

    # ----- Wave-7 P0: session_key fallback chain BEFORE leaf selection ------
    # The cache row's session_key MUST be non-empty (the UNIQUE index on
    # the cache table collapses empty strings cross-caller, causing
    # cross-session cache pollution). Resolve via the central helper.
    # Original: lossless-claw/src/tools/lcm-synthesize-around-tool.ts:775-814.
    session_key_for_cache = resolve_session_key(
        db,
        target_summary_session_key=(
            target_summary.get("session_key") if target_summary is not None else None
        ),
        input_session_key=session_key,
        conversation_ids=conversation_ids or (),
    )

    # ----- 8. Build leaf set per window mode --------------------------------
    leaf_rows: list[dict[str, Any]]
    range_start_iso: str
    range_end_iso: str
    semantic_meta: Optional[dict[str, Any]] = None

    if window_kind == "period":
        period_raw = read_string_param(args, "period")
        period_since: Optional[datetime] = None
        period_before: Optional[datetime] = None
        period_label = "custom-range"
        if period_raw is not None and period_raw.strip():
            try:
                parsed = parse_period_shortcut(
                    period_raw,
                    timezone_name=ctx.timezone,
                )
            except PeriodParseError as exc:
                return tool_result({"error": str(exc)})
            period_since = parsed.since
            period_before = parsed.before
            period_label = parsed.label

        # Combine period bounds + explicit since/before. Tightest wins.
        range_start = _tightest_lower(since_bound, period_since)
        range_end = _tightest_upper(before_bound, period_before)
        if range_start is None or range_end is None:
            return tool_result(
                {
                    "error": (
                        "window_kind='period' requires either `period` (shortcut) "
                        "or both `since` and `before` (explicit range)."
                    ),
                    "hint": (
                        "Examples: {window_kind:'period', period:'yesterday'} | "
                        "{window_kind:'period', period:'last-7-days'} | "
                        "{window_kind:'period', since:'2026-05-01T00:00:00Z', "
                        "before:'2026-05-02T00:00:00Z'}"
                    ),
                },
            )
        if _ensure_utc(range_start) >= _ensure_utc(range_end):
            return tool_result(
                {
                    "error": (
                        "Effective period window is empty after combining "
                        "period + since/before bounds."
                    ),
                },
            )
        range_start_iso = _iso_z(range_start)
        range_end_iso = _iso_z(range_end)

        leaf_rows = select_time_window_leaves(
            db,
            range_start=range_start_iso,
            range_end=range_end_iso,
            conversation_ids=conversation_ids,
            exclude_summary_id=(
                target_summary["summary_id"] if target_summary is not None else None
            ),
        )
        if not leaf_rows:
            return tool_result(
                {
                    "error": (
                        f"No leaves found in period {period_label} "
                        f"({range_start_iso} → {range_end_iso})."
                    ),
                    "hint": (
                        "Widen the period (e.g. 'last-7-days' instead of "
                        "'yesterday') or set allConversations=true if leaves "
                        "live elsewhere."
                    ),
                    "window": {
                        "kind": "period",
                        "label": period_label,
                        "since": range_start_iso,
                        "before": range_end_iso,
                    },
                },
            )

    elif window_kind == "time":
        assert target_summary is not None  # validated above
        anchor_raw = target_summary["created_at"]
        anchor = _parse_sqlite_utc_timestamp(anchor_raw)
        if anchor is None:
            return tool_result(
                {
                    "error": (f"Target summary has invalid created_at: {anchor_raw}"),
                },
            )
        # ±windowHours window, clamped to since/before bounds.
        half_ms = window_hours * 60 * 60
        range_start_dt = anchor - _timedelta_seconds(half_ms)
        range_end_dt = anchor + _timedelta_seconds(half_ms)
        if since_bound is not None and _ensure_utc(since_bound) > _ensure_utc(range_start_dt):
            range_start_dt = since_bound
        if before_bound is not None and _ensure_utc(before_bound) < _ensure_utc(range_end_dt):
            range_end_dt = before_bound
        if _ensure_utc(range_start_dt) >= _ensure_utc(range_end_dt):
            return tool_result({
                "error": "Effective window is empty after applying since/before bounds."
            })
        range_start_iso = _iso_z(range_start_dt)
        range_end_iso = _iso_z(range_end_dt)
        leaf_rows = select_time_window_leaves(
            db,
            range_start=range_start_iso,
            range_end=range_end_iso,
            conversation_ids=conversation_ids,
            exclude_summary_id=target_summary["summary_id"],
        )

    else:
        # semantic mode — Wave A defers Voyage + vec0; surface a clear
        # graceful error. Wave B wires the real semantic adapter (epic 05).
        return tool_result(
            {
                "error": (
                    "Semantic search is unavailable (sqlite-vec / vec0 not "
                    "loaded or no active embedding model). Use window_kind='time' "
                    "with a summary_id target instead."
                ),
                "detail": (
                    "semantic mode requires Epic 05 (Voyage + vec0); not shipped "
                    "in v0.1 Wave A. Use window_kind='time' or 'period' for now."
                ),
            },
        )

    if not leaf_rows:
        return tool_result(
            {
                "error": "Window selected zero leaves.",
                "hint": (
                    "Widen windowHours, or set allConversations=true if leaves live elsewhere."
                    if window_kind == "time"
                    else "Widen the period (e.g. 'last-30-days' instead of 'yesterday'), "
                    "or set allConversations=true."
                ),
                "window": (
                    {
                        "kind": "time",
                        "hours": window_hours,
                        "since": range_start_iso,
                        "before": range_end_iso,
                    }
                    if window_kind == "time"
                    else {"kind": "period", "since": range_start_iso, "before": range_end_iso}
                ),
            },
        )

    # ----- Build source text + leaf fingerprint ------------------------------
    built = build_source_text(leaf_rows)
    source_text = built["text"]
    truncated_at: Optional[int] = built["truncated_at"]
    source_token_count = estimate_tokens(source_text)
    if truncated_at is not None:
        leaf_ids = [r["summary_id"] for r in leaf_rows[:truncated_at]]
    else:
        leaf_ids = [r["summary_id"] for r in leaf_rows]
    leaf_fp = fingerprint_leaves(leaf_ids)

    # ----- 9. Look up the active prompt FIRST (FK requires it) --------------
    # Look up the active prompt_id BEFORE the cache write so we can
    # satisfy the FK to lcm_prompt_registry. If no prompt is registered
    # we surface a clear error before any LLM call.
    prompt_id_row = db.execute(
        "SELECT prompt_id FROM lcm_prompt_registry"
        " WHERE memory_type = 'episodic-condensed' AND tier_label = ? "
        "   AND pass_kind = 'single' AND active = 1"
        " ORDER BY version DESC LIMIT 1",
        (tier,),
    ).fetchone()
    if prompt_id_row is None:
        return tool_result(
            {
                "error": (
                    f"missing_prompt: no active prompt for (memory_type=episodic-condensed, "
                    f"tier={tier}, pass_kind=single)."
                ),
                "hint": (
                    f"Register a prompt via `register_prompt(db, "
                    f"RegisterPromptOptions(memory_type='episodic-condensed', "
                    f"tier_label='{tier}', pass_kind='single', template='...'))` "
                    f"before calling this tool."
                ),
            },
        )
    initial_prompt_id = (
        prompt_id_row["prompt_id"] if isinstance(prompt_id_row, sqlite3.Row) else prompt_id_row[0]
    )

    # ----- 10. Build the LLM-call adapter via the injected factory ----------
    # Wave-12 reviewer F8 audit-honesty: pass the resolved primary
    # candidate's model name so the audit row records the actually-run
    # model, not dispatch's pick_model recommendation.
    # Original: lossless-claw/src/tools/lcm-synthesize-around-tool.ts:608-618.
    try:
        llm_call, summarizer_model = ctx.build_llm_call()
    except Exception as exc:  # noqa: BLE001 — vendor-specific exceptions vary
        return tool_result(
            {
                "error": (
                    "No summarization model resolved — set summary_model / "
                    "summary_provider on the LCM config or LCM_SUMMARY_MODEL env."
                ),
                "detail": str(exc),
            },
        )

    # ----- 11. Pre-write cache row in 'building' state (single-flight) ------
    # LCM Wave-10 (2026-03-22): cache UNIQUE index includes tier_label + prompt_id
    # to prevent cross-tier cache collisions.
    # Original: lossless-claw/src/synthesis/dispatch.ts (and src/db/migration.ts).
    cache_id = _generate_cache_id_with_prefix()
    pass_session_id = _generate_pass_session_id()
    key = CacheKey(
        session_key=session_key_for_cache,
        range_start=range_start_iso,
        range_end=range_end_iso,
        leaf_fingerprint=leaf_fp,
        grep_filter=None,
        tier_label=cache_tier_label,
        prompt_id=initial_prompt_id,
    )
    actual_range_covered = json.dumps(
        {
            "mode": window_kind,
            "anchorSummaryId": (
                target_summary["summary_id"] if target_summary is not None else None
            ),
            **(
                {"hours": window_hours}
                if window_kind == "time"
                else {
                    "period": (
                        (read_string_param(args, "period") or "")
                        if window_kind == "period"
                        else None
                    ),
                    "rangeStart": range_start_iso,
                    "rangeEnd": range_end_iso,
                }
            ),
            "since": _iso_z(since_bound) if since_bound is not None else None,
            "before": _iso_z(before_bound) if before_bound is not None else None,
        },
        ensure_ascii=False,
    )

    try:
        insert_result = insert_cache_row_single_flight(
            db,
            cache_id=cache_id,
            key=key,
            model_used=summarizer_model,
            source_leaf_ids_json=json.dumps(leaf_ids, ensure_ascii=False),
            source_token_count=source_token_count,
            actual_range_covered=actual_range_covered,
            leaf_count_synthesized=len(leaf_ids),
        )
    except sqlite3.DatabaseError as exc:
        return tool_result({"error": f"Failed to insert synthesis cache row: {exc}"})

    if not insert_result.won_latch:
        # Latch lost — another concurrent caller is synthesizing the
        # same (session_key, range, leaf_fingerprint, tier, prompt) tuple.
        # SELECT-back to either return cached result (ready) or surface
        # a "building elsewhere" / "recent_failure" hint without re-LLM-ing.
        existing = lookup_cache_row(db, key)
        return _emit_loser_path_response(existing, insert_result.cache_id)

    # ----- 12. Dispatch synthesis -------------------------------------------
    # Synchronous bridge: run the async dispatch on a fresh event loop.
    # Per ADR-017 (sync-by-design), the tool handler is sync; the async
    # dispatch + LLM adapter run via :func:`asyncio.run` here.
    import asyncio

    cache_id_won = insert_result.cache_id

    # ``tier`` is constrained to {"custom", "filtered"} above; cast for
    # the dispatch's broader ``TierLabel`` union (which also includes
    # daily/weekly/monthly/yearly).
    from typing import cast as _cast  # noqa: PLC0415

    from lossless_hermes.synthesis.dispatch import TierLabel  # noqa: PLC0415

    try:
        dispatch_result = asyncio.run(
            dispatch_synthesis(
                db,
                llm_call,
                SynthesizeRequest(
                    tier=_cast(TierLabel, tier),
                    memory_type="episodic-condensed",
                    source_text=source_text,
                    pass_session_id=pass_session_id,
                    target_cache_id=cache_id_won,
                ),
            )
        )
    except SynthesisDispatchError as exc:
        # Update cache row to failed.
        _mark_cache_failed(db, cache_id_won, str(exc))
        return tool_result(
            {
                "error": f"{exc.kind}: {exc}",
                "cache_id": cache_id_won,
                "hint": (
                    f"Register an active prompt for (memory_type='episodic-condensed', "
                    f"tier_label='{tier}', pass_kind='single') before calling this tool."
                    if exc.kind == "missing_prompt"
                    else None
                ),
            },
        )
    except Exception as exc:  # noqa: BLE001 — vendor failures are heterogeneous
        _mark_cache_failed(db, cache_id_won, str(exc))
        return tool_result({"error": f"Synthesis dispatch failed: {exc}", "cache_id": cache_id_won})

    output_text = dispatch_result.output
    output_tokens = estimate_tokens(output_text)

    # ----- 13. Update cache row → ready ------------------------------------
    try:
        db.execute(
            "UPDATE lcm_synthesis_cache"
            " SET status = 'ready', content = ?, output_token_count = ?,"
            "     prompt_id = ?, building_started_at = NULL"
            " WHERE cache_id = ?",
            (output_text, output_tokens, dispatch_result.primary_prompt_id, cache_id_won),
        )
        db.commit()
    except sqlite3.DatabaseError:
        # The synthesis succeeded; cache update failure is logged silently
        # (shouldn't block the response).
        pass

    # ----- 14. Best-effort cache_leaf_refs INSERTs --------------------------
    try:
        for leaf_id in leaf_ids:
            db.execute(
                "INSERT OR IGNORE INTO lcm_cache_leaf_refs "
                "(cache_id, leaf_summary_id) VALUES (?, ?)",
                (cache_id_won, leaf_id),
            )
        db.commit()
    except sqlite3.DatabaseError:
        # Best-effort — the synthesis already succeeded.
        pass

    # ----- 15. Build the markdown response ---------------------------------
    text = _render_markdown(
        window_kind=window_kind,
        window_hours=window_hours,
        window_k=int(window_k),
        target_summary=target_summary,
        period_arg=read_string_param(args, "period"),
        timezone_name=ctx.timezone,
        range_start_iso=range_start_iso,
        range_end_iso=range_end_iso,
        leaf_count=len(leaf_ids),
        total_leaves=len(leaf_rows),
        truncated_at=truncated_at,
        tier=tier,
        cache_id=cache_id_won,
        dispatch_result_cost=dispatch_result.total_cost_cents,
        dispatch_result_latency=dispatch_result.total_latency_ms,
        hallucination_flagged=dispatch_result.hallucination_flagged,
        output_text=output_text,
        semantic_meta=semantic_meta,
    )

    voyage_tokens = semantic_meta.get("voyage_tokens_consumed", 0) if semantic_meta else 0
    payload = {
        "text": text,
        "cache_id": cache_id_won,
        "details": {
            "cache_id": cache_id_won,
            "mode": window_kind,
            "tier": tier,
            "range_start": range_start_iso,
            "range_end": range_end_iso,
            "leaf_count": len(leaf_ids),
            "source_token_count": source_token_count,
            "output_token_count": output_tokens,
            "truncated": truncated_at is not None,
            "model_used": summarizer_model,
            "embedding_model": (semantic_meta.get("model_name") if semantic_meta else None),
            "voyage_tokens_consumed": voyage_tokens,
            "voyageTokensConsumed": voyage_tokens,
            "synthesis": {
                "primary_prompt_id": dispatch_result.primary_prompt_id,
                "audit_ids": list(dispatch_result.audit_ids),
                "total_latency_ms": dispatch_result.total_latency_ms,
                "total_cost_cents": dispatch_result.total_cost_cents,
                "hallucination_flagged": dispatch_result.hallucination_flagged,
            },
            "target": {
                "kind": "summary_id" if target_is_summary_id else "query",
                "value": target,
                "summary_anchor_at": (
                    target_summary["created_at"] if target_summary is not None else None
                ),
            },
        },
    }
    return tool_result(payload)


# ===========================================================================
# Public helpers — exported for tests + reuse
# ===========================================================================


def select_time_window_leaves(
    conn: sqlite3.Connection,
    *,
    range_start: str,
    range_end: str,
    conversation_ids: Optional[list[int]] = None,
    exclude_summary_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Pure SQL: pick leaves within ``[range_start, range_end)``.

    Mirrors TS ``selectTimeWindowLeaves`` (TS lines 481-530). Filters
    on:

    * ``kind = 'leaf'``
    * ``suppressed_at IS NULL``
    * ``julianday(COALESCE(latest_at, created_at)) BETWEEN ? AND ?``
      (Wave-2 Auditor #7 fix A1: matches the summary FTS + semantic-
      search timestamp convention)
    * Optional ``conversation_id IN (...)`` and
      ``summary_id != exclude_summary_id``

    Returns rows in ``created_at ASC`` order. The four returned fields
    match :class:`LeafRow` Protocol.

    Args:
        conn: Open :class:`sqlite3.Connection`.
        range_start: ISO-8601 UTC lower bound (inclusive).
        range_end: ISO-8601 UTC upper bound (exclusive).
        conversation_ids: Optional explicit conversation scope.
            ``None`` means cross-conversation.
        exclude_summary_id: Optional summary_id to exclude (the time-mode
            anchor leaf).

    Returns:
        List of dicts with keys ``summary_id``, ``content``,
        ``created_at``, ``token_count``.

    Source pin: TS ``selectTimeWindowLeaves`` at
    ``lossless-claw/src/tools/lcm-synthesize-around-tool.ts:481-530``.
    """
    filters: list[str] = [
        "julianday(COALESCE(latest_at, created_at)) >= julianday(?)",
        "julianday(COALESCE(latest_at, created_at)) < julianday(?)",
        "suppressed_at IS NULL",
        "kind = 'leaf'",
    ]
    binds: list[Any] = [range_start, range_end]
    if conversation_ids:
        placeholders = ",".join("?" for _ in conversation_ids)
        filters.append(f"conversation_id IN ({placeholders})")
        binds.extend(conversation_ids)
    if exclude_summary_id is not None:
        filters.append("summary_id != ?")
        binds.append(exclude_summary_id)
    sql = (
        "SELECT summary_id, content, created_at, token_count"
        " FROM summaries"
        f" WHERE {' AND '.join(filters)}"
        " ORDER BY created_at ASC"
    )
    cur = conn.execute(sql, binds)
    columns = [d[0] for d in cur.description]
    return [dict(zip(columns, row, strict=False)) for row in cur.fetchall()]


def build_source_text(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Concatenate leaves with separators, hard-capping at MAX_SOURCE_TEXT_TOKENS.

    Mirrors TS ``buildSourceText`` (TS lines 532-548). Each leaf is
    formatted as ``### Leaf <id> (<ts>)\\n\\n<content>`` and joined with
    ``\\n\\n---\\n\\n`` separators. If cumulative token count exceeds
    :data:`MAX_SOURCE_TEXT_TOKENS`, returns the partial text plus the
    index at which truncation occurred.

    Token count per row uses ``token_count`` from the row when > 0,
    otherwise falls through to :func:`estimate_tokens` on the rendered
    block.

    Args:
        rows: List of leaf row dicts (must contain ``summary_id``,
            ``content``, ``created_at``, ``token_count``).

    Returns:
        Dict with keys ``text`` (the joined output) and ``truncated_at``
        (the row index at which truncation occurred, or ``None`` if all
        rows fit).

    Source pin: TS ``buildSourceText`` at
    ``lossless-claw/src/tools/lcm-synthesize-around-tool.ts:532-548``.
    """
    parts: list[str] = []
    total_tokens = 0
    truncated_at: Optional[int] = None
    for i, row in enumerate(rows):
        block = f"### Leaf {row['summary_id']} ({row['created_at']})\n\n{row['content']}"
        tc = row.get("token_count") or 0
        block_tokens = tc if tc > 0 else estimate_tokens(block)
        total_tokens += block_tokens
        if total_tokens > MAX_SOURCE_TEXT_TOKENS:
            truncated_at = i
            break
        parts.append(block)
    return {"text": "\n\n---\n\n".join(parts), "truncated_at": truncated_at}


# ===========================================================================
# Private helpers
# ===========================================================================


@dataclass
class _LcmScopeAdapter:
    """Adapter for :class:`~..conversation_scope._LcmLike` Protocol.

    Holds a :class:`ConversationStore` so the conversation-scope resolver
    can read off ``_conversation_store``. The field type is
    :class:`Optional` to byte-match the Protocol's declaration —
    structural typing requires the substituted type to be assignable to
    the protocol's declared type. In practice the adapter is always
    constructed with a non-None store at the call site.
    """

    _conversation_store: Optional[ConversationStore]


def _lookup_target_summary(
    conn: sqlite3.Connection,
    summary_id: str,
    conversation_ids: Optional[list[int]],
) -> Optional[dict[str, Any]]:
    """Look up the target summary by ID, scoped to ``conversation_ids``.

    Mirrors TS ``lookupTargetSummary`` (TS lines 459-479). Returns the
    5-field row (``summary_id``, ``content``, ``created_at``,
    ``conversation_id``, ``session_key``) or ``None``.
    """
    filters: list[str] = ["summary_id = ?", "suppressed_at IS NULL"]
    binds: list[Any] = [summary_id]
    if conversation_ids:
        placeholders = ",".join("?" for _ in conversation_ids)
        filters.append(f"conversation_id IN ({placeholders})")
        binds.extend(conversation_ids)
    sql = (
        "SELECT summary_id, content, created_at, conversation_id, session_key"
        " FROM summaries"
        f" WHERE {' AND '.join(filters)}"
        " LIMIT 1"
    )
    cur = conn.execute(sql, binds)
    row = cur.fetchone()
    if row is None:
        return None
    columns = [d[0] for d in cur.description]
    return dict(zip(columns, row, strict=False))


def _emit_loser_path_response(existing: Optional[ExistingCacheRow], cache_id: str) -> str:
    """Loser-path response — return cached / building_elsewhere / recent_failure.

    Mirrors TS lines 1235-1335. The 3 outcomes:

    * ``status='ready'``: return ``{"status": "cached", "text": ..., ...}``.
    * ``status='failed'``: return ``{"status": "recent_failure", ...}`` with
      ``retry_after_ms`` so the caller can sleep precisely.
    * ``status='building'``: return ``{"status": "building_elsewhere", ...}``.
    """
    if existing is None:
        return tool_result(
            {
                "status": "building_elsewhere",
                "cache_id": cache_id or "(unknown)",
                "building_started_at": None,
                "retry_after_ms": None,
                "hint": (
                    "Another caller is synthesizing the same window. Wait a "
                    "few seconds before retrying — the janitor will reap "
                    "stalled work after 10 minutes max."
                ),
                "single_flight_outcome": "lost_latch",
            },
        )

    if existing.status == "ready" and existing.content is not None:
        return tool_result(
            {
                "cache_id": existing.cache_id,
                "status": "cached",
                "text": existing.content,
                "output_token_count": existing.output_token_count,
                "single_flight_outcome": "winner_already_ready",
            },
        )

    if existing.status == "failed":
        started_at_ms = _parse_sqlite_utc_ms(existing.building_started_at)
        if started_at_ms is not None:
            now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
            retry_after_ms = max(0, started_at_ms + 10 * 60 * 1000 - now_ms)
        else:
            retry_after_ms = None
        failure_reason = existing.failure_reason or ""
        return tool_result(
            {
                "status": "recent_failure",
                "cache_id": existing.cache_id,
                "building_started_at": existing.building_started_at,
                "failure_reason": failure_reason,
                "retry_after_ms": retry_after_ms,
                "hint": (
                    f"Last attempt failed: {failure_reason[:200]}. Retries are "
                    f"exponentially backed off (10 min × 2^attempt, capped 6h). "
                    f"Wait retry_after_ms then re-call, or pass slightly "
                    f"different criteria."
                    if failure_reason
                    else (
                        "A recent attempt failed. Retries are exponentially "
                        "backed off; wait retry_after_ms or pass slightly "
                        "different criteria."
                    )
                ),
                "single_flight_outcome": "lost_latch",
            },
        )

    # status == 'building'
    started_at_ms = _parse_sqlite_utc_ms(existing.building_started_at)
    if started_at_ms is not None:
        now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
        retry_after_ms = max(0, started_at_ms + 10 * 60 * 1000 - now_ms)
    else:
        retry_after_ms = None
    return tool_result(
        {
            "status": "building_elsewhere",
            "cache_id": existing.cache_id,
            "building_started_at": existing.building_started_at,
            "retry_after_ms": retry_after_ms,
            "hint": (
                "Another caller is synthesizing the same window. Wait "
                "retry_after_ms (or a few seconds) before retrying — the "
                "janitor will reap stalled work after 10 minutes max."
            ),
            "single_flight_outcome": "lost_latch",
        },
    )


def _mark_cache_failed(
    conn: sqlite3.Connection,
    cache_id: str,
    error_message: str,
) -> None:
    """Update cache row to ``status='failed'`` with failure reason.

    Truncates the error message to 800 chars (TS line 1356 parity).
    Best-effort — silent failure if the UPDATE itself errors.
    """
    try:
        conn.execute(
            "UPDATE lcm_synthesis_cache"
            " SET status = 'failed', failure_reason = ?"
            " WHERE cache_id = ?",
            (error_message[:800], cache_id),
        )
        conn.commit()
    except sqlite3.DatabaseError:
        pass


# ===========================================================================
# Markdown rendering
# ===========================================================================


def _render_markdown(  # noqa: PLR0913 — passing through context is intentional
    *,
    window_kind: str,
    window_hours: float,
    window_k: int,
    target_summary: Optional[dict[str, Any]],
    period_arg: Optional[str],
    timezone_name: str,
    range_start_iso: str,
    range_end_iso: str,
    leaf_count: int,
    total_leaves: int,
    truncated_at: Optional[int],
    tier: str,
    cache_id: str,
    dispatch_result_cost: int,
    dispatch_result_latency: float,
    hallucination_flagged: Optional[bool],
    output_text: str,
    semantic_meta: Optional[dict[str, Any]],
) -> str:
    """Render the markdown response. Mirrors TS lines 1409-1434."""
    lines: list[str] = []
    lines.append("## LCM Synthesize-Around")
    lines.append(f"**Mode:** {window_kind}")
    if window_kind == "time":
        assert target_summary is not None
        lines.append(
            f"**Window:** ±{int(window_hours)}h around "
            f"{_format_display_time(target_summary['created_at'], timezone_name)}"
        )
    elif window_kind == "period":
        label = (period_arg or "").strip() if period_arg else "custom-range"
        if not label:
            label = "custom-range"
        lines.append(f"**Window:** period='{label}' (direct date-range — no anchor required)")
    else:
        lines.append(f"**Window:** top-{int(window_k)} semantic neighbours")
        if semantic_meta and semantic_meta.get("model_name"):
            lines.append(f"**Embedding model:** {semantic_meta['model_name']}")
    lines.append(
        f"**Effective range:** {_format_display_time(range_start_iso, timezone_name)} "
        f"→ {_format_display_time(range_end_iso, timezone_name)}"
    )
    truncated_suffix = f" (truncated from {total_leaves})" if truncated_at is not None else ""
    lines.append(f"**Leaves synthesized:** {leaf_count}{truncated_suffix}")
    lines.append(f"**Tier:** {tier}")
    lines.append(f"**Cache id:** `{cache_id}`")
    lines.append(
        f"**Cost:** {dispatch_result_cost} cents | **Latency:** {int(dispatch_result_latency)}ms"
    )
    if hallucination_flagged is True:
        lines.append("**Verify-fidelity:** flagged possible hallucination — see audit")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(output_text)
    return "\n".join(lines)


def _format_display_time(value: Any, timezone_name: str) -> str:
    """Format a timestamp for display. Lazy import to dodge import cycles."""
    if value is None:
        return "-"
    dt: Optional[datetime] = None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        s = value.strip()
        if not s:
            return "-"
        try:
            # Handle both `2026-05-01 12:00:00` and ISO-8601 strings.
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return "-"
    if dt is None:
        return "-"
    # Lazy import to dodge top-level cycle.
    from lossless_hermes.compaction import _format_timestamp  # noqa: PLC0415

    try:
        return _format_timestamp(dt, timezone_name)
    except (TypeError, ValueError):
        return "-"


# ===========================================================================
# Misc helpers — clamps, ID generators, type coercions
# ===========================================================================


def _clamp_number(
    value: Any,
    *,
    default: float,
    minimum: float,
    maximum: float,
    truncate: bool = False,
) -> float:
    """Clamp ``value`` to ``[minimum, maximum]`` with fallback to ``default``.

    Mirrors the ``Math.max(min, Math.min(max, val))`` pattern at TS lines
    695-702. Rejects bools (an ``int`` subclass) and non-finite floats.
    If ``truncate`` is True, applies ``int()`` (TS ``Math.trunc``) before
    clamping — used for ``windowK``.
    """
    if isinstance(value, bool):
        return float(default)
    if not isinstance(value, (int, float)):
        return float(default)
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return float(default)
    v = float(value)
    if truncate:
        v = float(int(v))
    if v < minimum:
        return float(minimum)
    if v > maximum:
        return float(maximum)
    return v


def _ensure_utc(dt: datetime) -> datetime:
    """Make sure a datetime has UTC tzinfo — naive datetimes treated as UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso_z(dt: datetime) -> str:
    """Format a datetime as ISO-8601 with Z suffix (TS toISOString parity)."""
    utc = _ensure_utc(dt)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def _timedelta_seconds(seconds: float):  # noqa: ANN202 — small helper
    """Trivial wrapper for :func:`datetime.timedelta` with seconds."""
    from datetime import timedelta as _td  # noqa: PLC0415

    return _td(seconds=seconds)


def _tightest_lower(a: Optional[datetime], b: Optional[datetime]) -> Optional[datetime]:
    """Return the max of two optional UTC datetimes (tighter lower bound)."""
    if a is None:
        return b
    if b is None:
        return a
    return a if _ensure_utc(a) >= _ensure_utc(b) else b


def _tightest_upper(a: Optional[datetime], b: Optional[datetime]) -> Optional[datetime]:
    """Return the min of two optional UTC datetimes (tighter upper bound)."""
    if a is None:
        return b
    if b is None:
        return a
    return a if _ensure_utc(a) <= _ensure_utc(b) else b


def _parse_sqlite_utc_timestamp(value: str) -> Optional[datetime]:
    """Parse a SQLite-style or ISO-8601 timestamp as UTC.

    Mirrors TS ``parseSqliteUtcTimestamp`` (TS lines 572-580). SQLite
    stores timestamps via ``datetime('now')`` as UTC strings of the
    form ``'YYYY-MM-DD HH:MM:SS'`` (no T, no Z). When fed to Python's
    :meth:`datetime.fromisoformat`, the space-separator form works on
    Python 3.11+ but the result is naive. We coerce to UTC here.

    ISO-8601 strings with an explicit T/Z/offset are parsed directly.
    """
    trimmed = value.strip()
    if not trimmed:
        return None
    try:
        if "T" in trimmed or "Z" in trimmed.upper() or trimmed.endswith(("+00:00", "-00:00")):
            # ISO 8601 with explicit T/Z/offset.
            dt = datetime.fromisoformat(trimmed.replace("Z", "+00:00"))
        else:
            # SQLite default form — parse + force UTC.
            dt = datetime.fromisoformat(trimmed)
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return _ensure_utc(dt)


def _parse_sqlite_utc_ms(value: Optional[str]) -> Optional[int]:
    """Parse a SQLite-style timestamp to UTC ms-since-epoch, or None."""
    if value is None:
        return None
    dt = _parse_sqlite_utc_timestamp(value)
    if dt is None:
        return None
    return int(dt.timestamp() * 1000)


def _generate_cache_id_with_prefix() -> str:
    """Generate a cache_id with the TS-style ``cache_around_<ts36>_<6hex>`` shape.

    Mirrors TS line 1043 ``cache_around_${Date.now().toString(36)}_${shortRandomSuffix()}``.
    Keeps the human-readable prefix for log lines / debug — the
    :func:`generate_cache_id` from cache_key generates a plain 24-hex
    PK, which we wrap here to preserve TS-test fixture compatibility
    (the TS suite asserts ``cache_id`` matches ``^cache_around_``).
    """
    ts36 = _to_base36(int(datetime.now(tz=timezone.utc).timestamp() * 1000))
    suffix = secrets.token_hex(3)
    return f"cache_around_{ts36}_{suffix}"


def _generate_pass_session_id() -> str:
    """Generate a ``pas_around_<ts36>_<6hex>`` pass_session_id (TS line 1044)."""
    ts36 = _to_base36(int(datetime.now(tz=timezone.utc).timestamp() * 1000))
    suffix = secrets.token_hex(3)
    return f"pas_around_{ts36}_{suffix}"


def _to_base36(n: int) -> str:
    """Encode a non-negative integer as base-36 (matches JS ``Number.toString(36)``)."""
    if n == 0:
        return "0"
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    parts: list[str] = []
    while n > 0:
        n, r = divmod(n, 36)
        parts.append(digits[r])
    return "".join(reversed(parts))
