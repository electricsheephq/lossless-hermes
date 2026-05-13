"""Pre-call ``needsCompact`` gate for LCM tools.

Ports ``lossless-claw/src/plugin/needs-compact-gate.ts`` (LCM commit
``1f07fbd`` on branch ``pr-613``, ~250 LOC) to Python. The gate is the
Wave-14 negotiated middle-ground: before a big tool runs, estimate its
result size; if ``(current + estimated) / budget > REFUSAL_THRESHOLD``,
refuse with a structured ``{ok: false, needsCompact: true, ...}`` payload
so the agent can call ``lcm_compact`` then retry. Without this layer the
agent would have to proactively monitor context and compact preemptively
— too much cognitive load.

What this is
============

This is **middleware**, not a decorator. Per **Wave-12 F5** ([ADR-029] row),
:class:`LCMEngine.handle_tool_call` consults ``TOKEN_GATE_TOOLS`` membership
and wraps the inner dispatch with :func:`run_with_token_gate` at invocation
time, not at registration time. Decorator-time computation freezes the gate
state to whatever was true at plugin-init, defeating the purpose.

Threshold derivation
====================

:data:`REFUSAL_THRESHOLD` = 0.92 (calibrated from real DB sampling, Wave-14
Agent A). With a 200K context * 0.05 cushion (the 0.95 alternative) =
10K headroom — but every tool's hard cap IS up to
:data:`result_budget.MAX_RESULT_TOKENS` (default 10K, operator-tunable). A
single capped call leaves zero margin. 0.92 -> 16K headroom = one full-cap
call + agent's own response (at the default cap).

Tools that use this
===================

* ``lcm_grep`` (all modes — including ``mode='semantic'`` post Wave-12 SA)
* ``lcm_describe`` (most important — biggest blow-up risk)
* ``lcm_synthesize_around`` (Wave-12 W2A1: previously skipped; the
  "self-protecting via 50K source cap" reasoning covered SOURCE input
  bounds, not OUTPUT — and the markdown response is 4K-8K tokens of
  LLM-generated rollup that flowed past the cache silently. Fixed.)
* ``lcm_expand_query`` (sub-agent path; uniform behavior; deferred to v2
  per ADR-012 so not registered in v0.1.0 but the estimator exists)
* ``lcm_get_entity`` / ``lcm_search_entities`` (uniform; rarely trips)

Skipped:

* ``lcm_compact`` (status response, ~100 tokens; on success it CLEARS
  the cache via :func:`note_successful_compact` so the next call
  re-bootstraps from the post-compact ground truth)
* ``lcm_expand`` (sub-agent only; has its own grant ledger)

Acceptance criterion (issue 06-03)
==================================

::

    # LCM Wave-12 F5: runWithTokenGate is middleware-not-decorator so the
    # gate state is computed at invocation time, not at registration time.
    # Original: lossless-claw/src/plugin/needs-compact-gate.ts.

This comment lives at the wrap site in :class:`LCMEngine.handle_tool_call`
(``engine/__init__.py``) — see ADR-029 §"Known Wave-N fixes" Wave-12 row.

References
----------

* TS source: ``lossless-claw/src/plugin/needs-compact-gate.ts``.
* Porting guide: ``docs/porting-guides/tools.md`` lines 599–617 plus the
  per-tool estimator subsections (lines 124–128, 192–197, 321, 440, 490).
* Issue spec: ``epics/06-tools/06-03-runwithtokengate-middleware.md``.
* ADR-029 Wave-12 F5 row (middleware-not-decorator) and W2A1 (compact taps
  the wrapper's catch block on throw).
"""

from __future__ import annotations

import math
from typing import Any, Callable, Final, TypedDict

from lossless_hermes.plugin import result_budget
from lossless_hermes.plugin.token_state import tap_result_for_token_accounting

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Pre-call refusal threshold. When
#: ``(current_token_count + estimated_result_tokens) / token_budget``
#: exceeds this ratio, :func:`evaluate_needs_compact_gate` returns a
#: refusal payload instead of dispatching the tool. 0.92 = 8% headroom
#: above current state, which is enough room for one full-cap tool result
#: plus the agent's own response at the default cap (10K tokens).
REFUSAL_THRESHOLD: Final[float] = 0.92

#: Tools wrapped by the gate. The :class:`LCMEngine.handle_tool_call`
#: dispatch ladder consults this set to decide whether to wrap the
#: inner dispatch with :func:`run_with_token_gate`. ``lcm_expand`` and
#: ``lcm_compact`` are intentionally absent (see module docstring).
#: ``lcm_expand_query`` is included even though it's deferred to v2 per
#: ADR-012 — its estimator entry is correct so when the v2 port lands
#: the wiring is already in place. Tests pin this set.
TOKEN_GATE_TOOLS: Final[frozenset[str]] = frozenset({
    "lcm_grep",
    "lcm_describe",
    "lcm_synthesize_around",
    "lcm_expand_query",
    "lcm_get_entity",
    "lcm_search_entities",
})

#: Default ``charsPerToken`` divisor for the estimator. Matches TS.
_CHARS_PER_TOKEN: Final[int] = 4


# ---------------------------------------------------------------------------
# Refusal payload type
# ---------------------------------------------------------------------------


class NeedsCompactRefusal(TypedDict):
    """Structured refusal payload returned to the agent.

    Schema matches TS ``evaluateNeedsCompactGate`` return shape at lines
    188–197. The agent reads ``needsCompact`` programmatically; the
    ``suggested_actions`` list is concrete next-step guidance.

    Keys:

    * ``ok``: always ``False`` (refusal).
    * ``needsCompact``: always ``True`` — the trigger flag the agent
      looks for to know it should call ``lcm_compact``.
    * ``reason``: stable enum ``"context-overflow-prevention"``. Tests
      pin this string.
    * ``currentRatio``: rounded ``current / budget`` ratio (3 decimals).
    * ``estimatedResultTokens``: per-tool estimate at the requested
      params.
    * ``projectedRatio``: rounded ``(current + estimate) / budget``
      ratio (3 decimals).
    * ``note``: human-readable explanation.
    * ``suggested_actions``: list of concrete next-step suggestions.
      Always includes ``"lcm_compact then retry with same params"``;
      may add ``"retry with limit=N"`` / ``"retry with expandChildrenLimit=N"``
      / etc. when the params support narrowing.
    """

    ok: bool
    needsCompact: bool  # noqa: N815 — camelCase matches TS contract
    reason: str
    currentRatio: float  # noqa: N815
    estimatedResultTokens: int  # noqa: N815
    projectedRatio: float  # noqa: N815
    note: str
    suggested_actions: list[str]


# ---------------------------------------------------------------------------
# estimate_result_tokens — per-tool formulas
# ---------------------------------------------------------------------------


def estimate_result_tokens(tool_name: str, params: dict[str, Any]) -> int:
    """Estimate the result-token count for a tool call.

    Math from Wave-14 Agent C (calibrated against actual format strings +
    Agent A's live-DB distributions). Capped at the current
    :data:`result_budget.MAX_RESULT_TOKENS` (default 10_000 tokens / ~40K
    chars; operator-tunable via ``LCM_TOOL_RESULT_TOKEN_BUDGET``).

    Wave-12 audit W1A1 #2: the cap NOW tracks the env knob instead of
    being hard-coded at 10_000. Previously, raising the env from 10K to
    e.g. 30K let tools emit 30K but the estimator still capped its
    projection at 10K, drifting ``needsCompact`` decisions low (refusals
    missed when they should fire). The Python port reads
    :func:`result_budget.get_max_result_tokens` at call time so the live
    binding always wins.

    Per-tool formulas (verbatim from tools.md lines 124–128 plus
    per-tool sections; the literal numbers are load-bearing):

    * ``lcm_grep`` (regex / full_text):   ``200 + limit * 200`` chars
    * ``lcm_grep`` (hybrid):              ``250 + limit * 230`` chars
    * ``lcm_grep`` (semantic):            ``350 + limit * 215`` chars
    * ``lcm_grep`` (verbatim):            ``70 + min(20, limit) * 2400`` chars
    * ``lcm_describe``: ``350 + 5*250 + 3200`` base, ``+k*2000`` if
      ``expandChildren``, ``+k*600`` if ``expandMessages`` (k from the
      respective limit fields, default 20)
    * ``lcm_get_entity``: ``250 + mentionLimit * 110`` chars
    * ``lcm_search_entities``: ``420 + limit * 85`` chars
    * ``lcm_expand_query``: ``maxTokens + 200`` (caps at HARD_CAP)
    * ``lcm_compact``: ``150`` tokens (status response)
    * ``lcm_synthesize_around``: ``6_000`` tokens (Wave-12 W2A1 picks
      the midpoint of the docstring's "4K-8K tokens of LLM-generated
      rollup" range)

    Args:
        tool_name: The tool name. Unknown tools default to 1000 tokens.
        params: The tool's args dict. Defaults for missing fields match
            the TS implementation: ``limit`` -> 20, ``mode`` -> ``"regex"``,
            ``mentionLimit`` -> 20, ``expandChildrenLimit`` -> 20,
            ``expandMessagesLimit`` -> 20, ``maxTokens`` -> 2000.

    Returns:
        The estimated token count, capped at the current
        :data:`result_budget.MAX_RESULT_TOKENS`.
    """
    hard_cap_tokens = result_budget.get_max_result_tokens()
    limit_raw = params.get("limit")
    limit = int(limit_raw) if isinstance(limit_raw, (int, float)) else 20

    if tool_name == "lcm_grep":
        mode = params.get("mode") or "regex"
        if mode in ("regex", "full_text"):
            # ~200 chars header + ~200 chars/row average (45 fixed + ~150 snippet)
            chars = 200 + limit * 200
            return min(hard_cap_tokens, math.ceil(chars / _CHARS_PER_TOKEN))
        if mode == "hybrid":
            # +30 chars/row for provenance + score
            chars = 250 + limit * 230
            return min(hard_cap_tokens, math.ceil(chars / _CHARS_PER_TOKEN))
        if mode == "semantic":
            # +50 chars header (Voyage model + confidence)
            chars = 350 + limit * 215
            return min(hard_cap_tokens, math.ceil(chars / _CHARS_PER_TOKEN))
        if mode == "verbatim":
            # hard cap 20 results, full message rows; tool messages p95 = 7721 chars
            # Estimate conservatively per row: 600-2400 tokens (avg ~1000 tokens)
            cap = min(20, limit)
            chars_typical = 70 + cap * 2_400  # assistant median bias
            return min(hard_cap_tokens, math.ceil(chars_typical / _CHARS_PER_TOKEN))
        # Unknown mode — small default for the cumulative ratio.
        return 1_500

    # Wave-12 consolidation SA: lcm_semantic_recall removed; its
    # estimator coefficient (250 + limit * 215) folded into lcm_grep's
    # mode='semantic' branch above. Estimate parity preserved.

    if tool_name == "lcm_describe":
        # Base: ~5 subtree nodes * 250 chars + ~3200 chars summary content + 350 header
        chars = 350 + 5 * 250 + 3_200
        if params.get("expandChildren"):
            k_raw = params.get("expandChildrenLimit")
            k = int(k_raw) if isinstance(k_raw, (int, float)) else 20
            # Wave-12 reviewer F2 calibration: live-DB validation showed
            # typical condensed summaries are ~2000 tokens (8000 chars).
            # 20x multiplier rarely binds — agents usually expand 0-1
            # children, not 20.
            chars += k * 2_000
        if params.get("expandMessages"):
            k_raw = params.get("expandMessagesLimit")
            k = int(k_raw) if isinstance(k_raw, (int, float)) else 20
            # Wave-12 reviewer F2 calibration: real expandMessages=20
            # emits 2,551-3,604 tokens (median ~140 tokens/msg = ~560
            # chars/msg), not the original 600-tokens/msg assumption.
            chars += k * 600
        return min(hard_cap_tokens, math.ceil(chars / _CHARS_PER_TOKEN))

    if tool_name == "lcm_get_entity":
        k_raw = params.get("mentionLimit")
        k = int(k_raw) if isinstance(k_raw, (int, float)) else 20
        chars = 250 + k * 110
        return min(hard_cap_tokens, math.ceil(chars / _CHARS_PER_TOKEN))

    if tool_name == "lcm_search_entities":
        chars = 420 + limit * 85
        return min(hard_cap_tokens, math.ceil(chars / _CHARS_PER_TOKEN))

    if tool_name == "lcm_expand_query":
        max_tokens_raw = params.get("maxTokens")
        max_tokens = int(max_tokens_raw) if isinstance(max_tokens_raw, (int, float)) else 2_000
        # answer up to maxTokens, plus ~200 chars envelope (TS comment
        # mentions ~500 but the literal value is +200; preserve verbatim).
        return min(hard_cap_tokens, max_tokens + 200)

    if tool_name == "lcm_compact":
        # Status response only (~10 fields, longest note ~250 chars).
        return 150

    if tool_name == "lcm_synthesize_around":
        # Wave-12 audit W2A1: lcm_synthesize_around is now wrapped by
        # run_with_token_gate so this estimate IS consulted. Output is
        # the synthesized markdown rollup. Wave-12 retro review (L1)
        # flagged a self-contradiction with the docstring ("4K-8K
        # tokens of LLM-generated rollup"). Picked 6_000 (midpoint of
        # the docstring range) — conservative under-estimate for
        # typical synthesis, matches what the docstring documents.
        return 6_000

    # Unknown tool — small default.
    return 1_000


# ---------------------------------------------------------------------------
# evaluate_needs_compact_gate — the gate decision
# ---------------------------------------------------------------------------


def evaluate_needs_compact_gate(
    *,
    tool_name: str,
    tool_params: dict[str, Any],
    current_token_count: int | None,
    token_budget: int | None,
    refusal_threshold: float | None = None,
) -> NeedsCompactRefusal | None:
    """Decide whether a tool call should be refused for context-overflow.

    Returns ``None`` when the tool should proceed normally (no gate
    fired); otherwise returns a :class:`NeedsCompactRefusal` payload
    that the wrapper returns DIRECTLY to the agent.

    Bypass conditions (all return ``None``):

    * ``current_token_count`` is ``None`` / not finite / < 0.
    * ``token_budget`` is ``None`` / not finite / <= 0.
    * Projected ratio <= ``refusal_threshold``.

    The bypass-on-missing-telemetry rule is conservative: missing
    signal shouldn't cause refusals (early in a session, before any
    ``llm_output`` has fired, the cache is empty -> bypass -> tool
    runs).

    Args:
        tool_name: The tool name being dispatched.
        tool_params: The tool's args dict (consumed by the estimator).
        current_token_count: Cached ``input + cacheRead + cacheWrite``.
            ``None`` -> bypass.
        token_budget: Cached effective context window. ``None`` ->
            bypass.
        refusal_threshold: Override for :data:`REFUSAL_THRESHOLD`.
            Tests use this to assert behavior at custom thresholds;
            production callers should leave at default.

    Returns:
        ``None`` when the tool should run; otherwise a refusal payload.
    """
    threshold = refusal_threshold if refusal_threshold is not None else REFUSAL_THRESHOLD

    # Bypass when telemetry is missing (early in session, no llm_output yet).
    if not isinstance(current_token_count, int) or current_token_count < 0:
        return None
    if not isinstance(token_budget, int) or token_budget <= 0:
        return None

    estimated_result_tokens = estimate_result_tokens(tool_name, tool_params)
    current_ratio = current_token_count / token_budget
    projected_ratio = (current_token_count + estimated_result_tokens) / token_budget

    if projected_ratio <= threshold:
        return None  # safe — let it run

    # Build refusal with concrete suggested actions.
    suggested: list[str] = ["lcm_compact then retry with same params"]

    # Tool-specific narrowing suggestions.
    limit_raw = tool_params.get("limit")
    if isinstance(limit_raw, (int, float)) and not isinstance(limit_raw, bool):
        limit_int = int(limit_raw)
        if limit_int > 5:
            suggested.append(f"retry with limit={max(5, limit_int // 2)}")

    expand_children_limit = tool_params.get("expandChildrenLimit")
    if isinstance(expand_children_limit, (int, float)) and not isinstance(
        expand_children_limit, bool
    ):
        ecl_int = int(expand_children_limit)
        if ecl_int > 5:
            suggested.append(f"retry with expandChildrenLimit={max(5, ecl_int // 2)}")

    expand_messages_limit = tool_params.get("expandMessagesLimit")
    if isinstance(expand_messages_limit, (int, float)) and not isinstance(
        expand_messages_limit, bool
    ):
        eml_int = int(expand_messages_limit)
        if eml_int > 5:
            suggested.append(f"retry with expandMessagesLimit={max(5, eml_int // 2)}")

    if (
        tool_name == "lcm_describe"
        and tool_params.get("expandChildren")
        and tool_params.get("expandMessages")
    ):
        suggested.append(
            "retry without one of the expand flags (e.g. drop expandMessages, keep expandChildren)"
        )

    # Rounding matches TS: 3 decimal places. ``round(x * 1000) / 1000``
    # yields stable equality checks in tests; ``round`` uses banker's
    # rounding which is acceptable here.
    return NeedsCompactRefusal(
        ok=False,
        needsCompact=True,
        reason="context-overflow-prevention",
        currentRatio=round(current_ratio * 1000) / 1000,
        estimatedResultTokens=estimated_result_tokens,
        projectedRatio=round(projected_ratio * 1000) / 1000,
        note=(
            f"Serving this call would push context to "
            f"{projected_ratio * 100:.0f}% of budget (currently at "
            f"{current_ratio * 100:.0f}%, would add ~{estimated_result_tokens} "
            f"tokens). Refusing to prevent overflow. Call lcm_compact to free "
            f"space, then retry — OR narrow params to reduce expected size."
        ),
        suggested_actions=suggested,
    )


# ---------------------------------------------------------------------------
# run_with_token_gate — the middleware wrapper
# ---------------------------------------------------------------------------


def run_with_token_gate(
    *,
    tool_name: str,
    tool_params: dict[str, Any],
    session_key: str | None,
    current_token_count: int | None,
    token_budget: int | None,
    inner: Callable[[], str],
) -> str:
    """Wrap a tool dispatch with pre-call gate + post-call tap.

    Applied by :class:`LCMEngine.handle_tool_call` for tools listed in
    :data:`TOKEN_GATE_TOOLS`. The wrapper:

    1. **Pre-call gate** — calls :func:`evaluate_needs_compact_gate`. On
       refusal, returns the refusal payload as a JSON string WITHOUT
       invoking ``inner``. The refusal is also tapped into the
       :mod:`token_state` cache so subsequent gate decisions account
       for the refusal's own token cost.
    2. **Inner dispatch** — calls ``inner()`` to produce the tool's
       JSON-string result.
    3. **Post-call tap** — calls
       :func:`tap_result_for_token_accounting` with the result text so
       the next tool call in the same iteration sees cumulative state.
    4. **Wave-12 W2A1 P1: throw-tap** — if ``inner`` raises, the wrapper
       taps an error-shaped payload (matching the size of the error
       message the runtime will surface) into the cache BEFORE
       re-raising. Without this, every throw inside a tool propagated
       past the wrapper, skipping the tap entirely; the error message
       costs tokens (the runtime serializes it for the agent), and that
       cost was silently un-counted, drifting downstream gate decisions
       low by exactly the size of the error message.

    Args:
        tool_name: The tool name being dispatched (consumed by the
            estimator).
        tool_params: The tool's args dict (consumed by the estimator).
        session_key: The Hermes session-key. Required for the tap (the
            cache is keyed by session-key); a ``None`` here means the
            tap is a no-op but the gate still runs.
        current_token_count: Cached ``current_token_count`` for the
            gate decision.
        token_budget: Cached ``token_budget`` for the gate decision.
        inner: The handler thunk. Returns the tool's JSON-string result.

    Returns:
        Either the refusal payload (JSON-encoded) or the inner result.

    Raises:
        Whatever ``inner`` raises — but the cache is updated FIRST so
        the error-message token cost is accounted for.
    """
    import json

    refusal = evaluate_needs_compact_gate(
        tool_name=tool_name,
        tool_params=tool_params,
        current_token_count=current_token_count,
        token_budget=token_budget,
    )
    if refusal is not None:
        refusal_text = json.dumps(refusal, ensure_ascii=False)
        return tap_result_for_token_accounting(session_key, refusal_text)

    # Wave-12 audit (W2A1 P1): catch throws and tap before re-throwing.
    # Without this, every "throw new Error(...)" inside ``inner`` (e.g.
    # "LCM engine is unavailable" — present in 6+ tools, plus 13 throw
    # sites in lcm_expand_query) propagated past the wrapper, skipping
    # tap_result_for_token_accounting entirely.
    try:
        result = inner()
    except Exception as exc:
        # Tap an error-shaped result so the cache absorbs the size of
        # the error message the runtime will surface. Re-raise so
        # callers can still observe the failure.
        error_text = json.dumps(
            {"error": f"{tool_name}: {exc}"},
            ensure_ascii=False,
        )
        tap_result_for_token_accounting(session_key, error_text)
        raise

    return tap_result_for_token_accounting(session_key, result)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__: Final = (
    "NeedsCompactRefusal",
    "REFUSAL_THRESHOLD",
    "TOKEN_GATE_TOOLS",
    "estimate_result_tokens",
    "evaluate_needs_compact_gate",
    "run_with_token_gate",
)
