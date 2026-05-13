"""Tests for :mod:`lossless_hermes.plugin.needs_compact_gate` (issue 06-03).

Coverage:

* :func:`estimate_result_tokens` per-tool formulas (verbatim from
  tools.md lines 124–128).
* :func:`evaluate_needs_compact_gate` happy path (under threshold => None).
* :func:`evaluate_needs_compact_gate` refusal at threshold (returns
  structured payload with `ok=False`, `needsCompact=True`).
* :func:`evaluate_needs_compact_gate` skip-when-no-budget (None / 0 /
  negative => bypass).
* :func:`run_with_token_gate` post-call tap accumulates state.
* :func:`run_with_token_gate` error-path tap before re-raise (Wave-12
  W2A1 P1 fix).
* :data:`TOKEN_GATE_TOOLS` set matches the spec.

References:

* :mod:`lossless_hermes.plugin.needs_compact_gate` — implementation.
* ``/Volumes/LEXAR/Claude/lossless-claw/src/plugin/needs-compact-gate.ts`` —
  TS source.
* ``docs/porting-guides/tools.md`` lines 599–617 + per-tool subsections.
* Issue spec: ``epics/06-tools/06-03-runwithtokengate-middleware.md``.
"""

from __future__ import annotations

import json
import typing

import pytest

from lossless_hermes.plugin import result_budget, token_state
from lossless_hermes.plugin.needs_compact_gate import (
    REFUSAL_THRESHOLD,
    TOKEN_GATE_TOOLS,
    estimate_result_tokens,
    evaluate_needs_compact_gate,
    run_with_token_gate,
)


@pytest.fixture(autouse=True)
def _reset_caches() -> typing.Iterator[None]:
    """Reset token-state cache and result-budget overrides between tests."""
    token_state.__reset_token_state_for_testing()
    result_budget.__reset_result_budget_for_testing()
    yield
    token_state.__reset_token_state_for_testing()
    result_budget.__reset_result_budget_for_testing()


# ---------------------------------------------------------------------------
# Constants and the TOKEN_GATE_TOOLS set
# ---------------------------------------------------------------------------


class TestConstants:
    """Module-level constants are stable knobs."""

    def test_refusal_threshold(self) -> None:
        """The threshold is 0.92 (Wave-14 Agent A calibration)."""
        assert REFUSAL_THRESHOLD == 0.92

    def test_token_gate_tools_set(self) -> None:
        """:data:`TOKEN_GATE_TOOLS` matches the spec — 6 tools.

        Excluded: ``lcm_expand`` (sub-agent grant ledger),
        ``lcm_compact`` (status response, clears cache on success).
        Included: ``lcm_expand_query`` even though it's deferred to v2
        per ADR-012 — so when the v2 port lands, the wiring is in place.
        """
        assert TOKEN_GATE_TOOLS == frozenset({
            "lcm_grep",
            "lcm_describe",
            "lcm_synthesize_around",
            "lcm_expand_query",
            "lcm_get_entity",
            "lcm_search_entities",
        })

    def test_exempt_tools_not_in_set(self) -> None:
        """``lcm_expand`` and ``lcm_compact`` are NOT gated."""
        assert "lcm_expand" not in TOKEN_GATE_TOOLS
        assert "lcm_compact" not in TOKEN_GATE_TOOLS


# ---------------------------------------------------------------------------
# estimate_result_tokens — per-tool formulas
# ---------------------------------------------------------------------------


class TestEstimateResultTokens:
    """Per-tool estimator formulas pinned verbatim from TS lines 66–169."""

    def test_lcm_grep_regex_default_limit(self) -> None:
        """``lcm_grep`` regex mode with default limit=20: 200 + 20*200 = 4200 chars / 4 = 1050 tokens."""
        assert estimate_result_tokens("lcm_grep", {"mode": "regex"}) == 1_050

    def test_lcm_grep_regex_explicit_limit(self) -> None:
        """``limit=50``: 200 + 50*200 = 10200 chars / 4 = 2550 tokens."""
        assert estimate_result_tokens("lcm_grep", {"mode": "regex", "limit": 50}) == 2_550

    def test_lcm_grep_full_text(self) -> None:
        """``full_text`` mode uses the same coefficients as ``regex``."""
        assert estimate_result_tokens("lcm_grep", {"mode": "full_text"}) == 1_050

    def test_lcm_grep_hybrid(self) -> None:
        """``hybrid``: 250 + 20*230 = 4850 chars / 4 = 1213 tokens (ceil)."""
        assert estimate_result_tokens("lcm_grep", {"mode": "hybrid"}) == 1_213

    def test_lcm_grep_semantic(self) -> None:
        """``semantic``: 350 + 20*215 = 4650 chars / 4 = 1163 tokens (ceil)."""
        assert estimate_result_tokens("lcm_grep", {"mode": "semantic"}) == 1_163

    def test_lcm_grep_verbatim(self) -> None:
        """``verbatim``: 70 + min(20, limit)*2400. With limit=20 => 48070 / 4 = 12018 ceil; capped at 10000."""
        # 70 + 20*2400 = 48070 chars / 4 = 12017.5 -> ceil 12018; cap 10000.
        assert estimate_result_tokens("lcm_grep", {"mode": "verbatim", "limit": 20}) == 10_000

    def test_lcm_grep_verbatim_small_limit(self) -> None:
        """``verbatim`` with limit=5: 70 + 5*2400 = 12070 chars / 4 = 3018 ceil."""
        assert estimate_result_tokens("lcm_grep", {"mode": "verbatim", "limit": 5}) == 3_018

    def test_lcm_grep_unknown_mode(self) -> None:
        """Unknown mode falls through to 1500 token small default."""
        assert estimate_result_tokens("lcm_grep", {"mode": "made-up"}) == 1_500

    def test_lcm_describe_base(self) -> None:
        """``lcm_describe`` base (no expand flags): 350+1250+3200 = 4800 chars / 4 = 1200."""
        assert estimate_result_tokens("lcm_describe", {}) == 1_200

    def test_lcm_describe_with_expand_children(self) -> None:
        """``expandChildren=True`` with default k=20: base+k*2000 = 4800+40000 = 44800 chars / 4 = 11200; capped at 10000."""
        assert estimate_result_tokens("lcm_describe", {"expandChildren": True}) == 10_000

    def test_lcm_describe_with_expand_children_small_k(self) -> None:
        """``expandChildren=True, expandChildrenLimit=5``: base+5*2000 = 14800 / 4 = 3700."""
        assert (
            estimate_result_tokens(
                "lcm_describe",
                {"expandChildren": True, "expandChildrenLimit": 5},
            )
            == 3_700
        )

    def test_lcm_describe_with_expand_messages(self) -> None:
        """``expandMessages=True`` with default k=20: base+k*600 = 4800+12000 = 16800 / 4 = 4200."""
        assert estimate_result_tokens("lcm_describe", {"expandMessages": True}) == 4_200

    def test_lcm_describe_with_both_flags(self) -> None:
        """``expandChildren+expandMessages`` (k=20 each): base+40000+12000 = 56800 / 4 = 14200; capped at 10000."""
        assert (
            estimate_result_tokens(
                "lcm_describe",
                {"expandChildren": True, "expandMessages": True},
            )
            == 10_000
        )

    def test_lcm_get_entity_default(self) -> None:
        """``lcm_get_entity`` default mentionLimit=20: 250+20*110 = 2450 chars / 4 = 613 ceil."""
        assert estimate_result_tokens("lcm_get_entity", {}) == 613

    def test_lcm_get_entity_explicit_limit(self) -> None:
        """``mentionLimit=50``: 250+50*110 = 5750 / 4 = 1438 ceil."""
        assert estimate_result_tokens("lcm_get_entity", {"mentionLimit": 50}) == 1_438

    def test_lcm_search_entities_default(self) -> None:
        """``lcm_search_entities`` default limit=20: 420+20*85 = 2120 / 4 = 530."""
        assert estimate_result_tokens("lcm_search_entities", {}) == 530

    def test_lcm_expand_query_default(self) -> None:
        """``lcm_expand_query`` default maxTokens=2000: 2000+200 = 2200 tokens."""
        assert estimate_result_tokens("lcm_expand_query", {}) == 2_200

    def test_lcm_expand_query_explicit(self) -> None:
        """``maxTokens=5000``: 5000+200 = 5200 tokens."""
        assert estimate_result_tokens("lcm_expand_query", {"maxTokens": 5_000}) == 5_200

    def test_lcm_expand_query_capped(self) -> None:
        """``maxTokens=50000`` => capped at MAX_RESULT_TOKENS (10000)."""
        assert estimate_result_tokens("lcm_expand_query", {"maxTokens": 50_000}) == 10_000

    def test_lcm_compact(self) -> None:
        """``lcm_compact`` is a status response — flat 150 tokens."""
        assert estimate_result_tokens("lcm_compact", {}) == 150

    def test_lcm_synthesize_around(self) -> None:
        """``lcm_synthesize_around`` flat 6000 tokens (Wave-12 W2A1 midpoint)."""
        assert estimate_result_tokens("lcm_synthesize_around", {}) == 6_000

    def test_unknown_tool_default(self) -> None:
        """Unknown tool falls through to 1000 tokens."""
        assert estimate_result_tokens("does-not-exist", {}) == 1_000

    def test_cap_tracks_max_result_tokens(self) -> None:
        """Raising :data:`result_budget.MAX_RESULT_TOKENS` raises the cap.

        Wave-12 audit W1A1 #2: the cap tracks the env knob, not a
        hardcoded value. The estimator reads at call time.
        """
        result_budget.apply_result_budget_config(30_000)
        # lcm_describe with both expand flags at limit=20: 14200 tokens.
        # Now 14200 < 30000 so it's NOT capped.
        assert (
            estimate_result_tokens(
                "lcm_describe",
                {"expandChildren": True, "expandMessages": True},
            )
            == 14_200
        )


# ---------------------------------------------------------------------------
# evaluate_needs_compact_gate — gate decisions
# ---------------------------------------------------------------------------


class TestEvaluateNeedsCompactGate:
    """The gate decision honors (current+estimate)/budget threshold."""

    def test_happy_path_below_threshold(self) -> None:
        """Low current usage => gate returns None (proceed)."""
        result = evaluate_needs_compact_gate(
            tool_name="lcm_grep",
            tool_params={"mode": "regex"},
            current_token_count=10_000,
            token_budget=200_000,
        )
        assert result is None

    def test_refuses_at_threshold(self) -> None:
        """High current usage + big estimate => gate refuses."""
        # 200K budget, threshold 0.92 -> 184K headroom.
        # current=180K, estimate for lcm_describe with both expands = 10K.
        # (180+10)/200 = 0.95 > 0.92 -> refuse.
        result = evaluate_needs_compact_gate(
            tool_name="lcm_describe",
            tool_params={"expandChildren": True, "expandMessages": True},
            current_token_count=180_000,
            token_budget=200_000,
        )
        assert result is not None
        assert result["ok"] is False
        assert result["needsCompact"] is True
        assert result["reason"] == "context-overflow-prevention"
        assert result["projectedRatio"] > 0.92

    def test_refusal_includes_suggested_actions(self) -> None:
        """Refusal carries at least one ``suggested_actions`` entry."""
        result = evaluate_needs_compact_gate(
            tool_name="lcm_grep",
            tool_params={"mode": "regex", "limit": 100},
            current_token_count=180_000,
            token_budget=200_000,
        )
        assert result is not None
        # First action is always "lcm_compact then retry with same params".
        assert result["suggested_actions"][0] == "lcm_compact then retry with same params"
        # limit=100 > 5 => narrowing suggestion is added.
        assert any("limit=50" in s for s in result["suggested_actions"])

    def test_refusal_describe_both_flags_suggestion(self) -> None:
        """``lcm_describe`` with both expand flags suggests dropping one."""
        result = evaluate_needs_compact_gate(
            tool_name="lcm_describe",
            tool_params={"expandChildren": True, "expandMessages": True},
            current_token_count=180_000,
            token_budget=200_000,
        )
        assert result is not None
        assert any("drop expandMessages" in s for s in result["suggested_actions"])

    def test_refusal_note_format(self) -> None:
        """Refusal note explains the rejection with concrete percentages."""
        result = evaluate_needs_compact_gate(
            tool_name="lcm_grep",
            tool_params={"mode": "verbatim"},
            current_token_count=180_000,
            token_budget=200_000,
        )
        assert result is not None
        # The note has the words "% of budget" plus "lcm_compact" hint.
        assert "% of budget" in result["note"]
        assert "lcm_compact" in result["note"]

    def test_bypass_when_current_count_missing(self) -> None:
        """``current_token_count=None`` => bypass (early-session no anchor)."""
        result = evaluate_needs_compact_gate(
            tool_name="lcm_describe",
            tool_params={"expandChildren": True, "expandMessages": True},
            current_token_count=None,
            token_budget=200_000,
        )
        assert result is None

    def test_bypass_when_budget_missing(self) -> None:
        """``token_budget=None`` => bypass."""
        result = evaluate_needs_compact_gate(
            tool_name="lcm_describe",
            tool_params={"expandChildren": True, "expandMessages": True},
            current_token_count=180_000,
            token_budget=None,
        )
        assert result is None

    def test_bypass_when_budget_zero(self) -> None:
        """``token_budget=0`` => bypass (avoid division by zero)."""
        result = evaluate_needs_compact_gate(
            tool_name="lcm_describe",
            tool_params={"expandChildren": True, "expandMessages": True},
            current_token_count=180_000,
            token_budget=0,
        )
        assert result is None

    def test_bypass_when_count_negative(self) -> None:
        """``current_token_count=-1`` => bypass (garbage signal)."""
        result = evaluate_needs_compact_gate(
            tool_name="lcm_describe",
            tool_params={"expandChildren": True, "expandMessages": True},
            current_token_count=-1,
            token_budget=200_000,
        )
        assert result is None

    def test_custom_threshold(self) -> None:
        """Tests can override the threshold to assert different cutoffs."""
        # current=10K + estimate 1050 = 11050 / 100K = 0.1105.
        # Threshold 0.1 => refuse.
        result = evaluate_needs_compact_gate(
            tool_name="lcm_grep",
            tool_params={"mode": "regex"},
            current_token_count=10_000,
            token_budget=100_000,
            refusal_threshold=0.1,
        )
        assert result is not None
        # Threshold 0.5 => proceed.
        result2 = evaluate_needs_compact_gate(
            tool_name="lcm_grep",
            tool_params={"mode": "regex"},
            current_token_count=10_000,
            token_budget=100_000,
            refusal_threshold=0.5,
        )
        assert result2 is None


# ---------------------------------------------------------------------------
# run_with_token_gate — middleware wrapper
# ---------------------------------------------------------------------------


class TestRunWithTokenGate:
    """The middleware wrapper applies gate -> inner -> tap."""

    def test_inner_invoked_when_under_threshold(self) -> None:
        """Below threshold: inner is called and result is returned verbatim."""
        sentinel = '{"data": "tool ran"}'
        called = []

        def inner() -> str:
            called.append(True)
            return sentinel

        result = run_with_token_gate(
            tool_name="lcm_grep",
            tool_params={"mode": "regex"},
            session_key="sess",
            current_token_count=10_000,
            token_budget=200_000,
            inner=inner,
        )
        assert called == [True]
        assert result == sentinel

    def test_inner_skipped_when_refused(self) -> None:
        """At threshold: inner is NOT invoked; refusal JSON is returned."""
        called = []

        def inner() -> str:
            called.append(True)
            return '{"data": "should not run"}'

        result = run_with_token_gate(
            tool_name="lcm_describe",
            tool_params={"expandChildren": True, "expandMessages": True},
            session_key="sess",
            current_token_count=180_000,
            token_budget=200_000,
            inner=inner,
        )
        # Inner skipped.
        assert called == []
        # Refusal JSON returned.
        payload = json.loads(result)
        assert payload["ok"] is False
        assert payload["needsCompact"] is True

    def test_tap_accumulates_state(self) -> None:
        """Post-call tap updates the token-state cache."""
        # Seed the cache with an anchor.
        token_state.record_llm_output(
            session_key="sess",
            usage={"input_tokens": 1000},
            token_budget=200_000,
        )
        # Pre-call state.
        before = token_state.get_runtime_context("sess")
        assert before["current_token_count"] == 1000

        # Call the wrapper with a result text.
        run_with_token_gate(
            tool_name="lcm_grep",
            tool_params={"mode": "regex"},
            session_key="sess",
            current_token_count=1000,
            token_budget=200_000,
            inner=lambda: "x" * 400,  # 400 chars = 100 tokens
        )

        # Tap accumulated.
        after = token_state.get_runtime_context("sess")
        assert after["current_token_count"] == 1000 + 100
        assert after["last_update_source"] == "tool-self-report"

    def test_tap_fires_on_refusal(self) -> None:
        """The refusal payload's text is also tapped (counts against budget)."""
        token_state.record_llm_output(
            session_key="sess",
            usage={"input_tokens": 1000},
            token_budget=200_000,
        )
        # Trigger refusal.
        result = run_with_token_gate(
            tool_name="lcm_describe",
            tool_params={"expandChildren": True, "expandMessages": True},
            session_key="sess",
            current_token_count=180_000,
            token_budget=200_000,
            inner=lambda: "should-not-run",
        )
        # Cache should reflect the size of the refusal payload.
        after = token_state.get_runtime_context("sess")
        expected_delta = (len(result) + 3) // 4
        assert after["current_token_count"] == 1000 + expected_delta

    def test_error_tap_then_reraise(self) -> None:
        """Wave-12 W2A1 P1: inner raises => wrapper taps then re-raises.

        Without this, every "throw new Error(...)" inside ``inner``
        propagated past the wrapper, skipping tap entirely; the error
        message costs tokens (the runtime serializes it for the agent),
        and that cost was silently un-counted, drifting downstream gate
        decisions low.
        """
        token_state.record_llm_output(
            session_key="sess",
            usage={"input_tokens": 1000},
            token_budget=200_000,
        )

        def inner() -> str:
            raise RuntimeError("LCM engine is unavailable")

        with pytest.raises(RuntimeError, match="LCM engine is unavailable"):
            run_with_token_gate(
                tool_name="lcm_grep",
                tool_params={"mode": "regex"},
                session_key="sess",
                current_token_count=1000,
                token_budget=200_000,
                inner=inner,
            )

        # Cache should reflect the size of the error-shaped JSON.
        after = token_state.get_runtime_context("sess")
        # error_text shape: {"error": "lcm_grep: LCM engine is unavailable"}
        # Length ~55 chars / 4 = ~14 tokens (ceil).
        assert after["current_token_count"] > 1000
        # Source remains 'tool-self-report' (the error tap counts as a tap).
        assert after["last_update_source"] == "tool-self-report"

    def test_no_session_key_no_tap(self) -> None:
        """``session_key=None`` => tap is a no-op but inner still runs."""
        # No anchor exists for any session-key; cache is empty.
        result = run_with_token_gate(
            tool_name="lcm_grep",
            tool_params={"mode": "regex"},
            session_key=None,
            current_token_count=1000,
            token_budget=200_000,
            inner=lambda: '{"data": "ok"}',
        )
        assert result == '{"data": "ok"}'
        # Cache still empty.
        assert token_state.get_runtime_context(None) == {}


# ---------------------------------------------------------------------------
# tap_result_for_token_accounting (token-state surface, exercised here too)
# ---------------------------------------------------------------------------


class TestTapResultForTokenAccounting:
    """The tap helper is idempotent across re-calls.

    Issue 06-03 AC explicitly mentions "idempotent across re-calls" — each
    call accumulates additively, NOT replaces the previous value.
    """

    def test_calling_twice_sums_twice(self) -> None:
        """Two taps with the same text add the same delta twice."""
        token_state.record_llm_output(
            session_key="sess",
            usage={"input_tokens": 1000},
            token_budget=200_000,
        )
        token_state.tap_result_for_token_accounting("sess", "x" * 400)
        first = token_state.get_runtime_context("sess")["current_token_count"]
        token_state.tap_result_for_token_accounting("sess", "x" * 400)
        second = token_state.get_runtime_context("sess")["current_token_count"]
        assert second - first == 100  # +400 chars / 4 chars-per-token

    def test_returns_result_text_unchanged(self) -> None:
        """The helper returns its input so it can sit in a ``return``."""
        token_state.record_llm_output(
            session_key="sess",
            usage={"input_tokens": 1000},
            token_budget=200_000,
        )
        result = token_state.tap_result_for_token_accounting("sess", "abc")
        assert result == "abc"
