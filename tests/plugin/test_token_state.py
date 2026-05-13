"""Tests for :mod:`lossless_hermes.plugin.token_state` (issue 06-03).

Coverage:

* :func:`record_llm_output` anchors the cache with ground truth.
* Different provider usage shapes are tolerated (OpenAI / Anthropic /
  Hermes-normalized / Codex).
* :func:`accumulate_tool_result_tokens` adds estimated tokens per
  result.
* :func:`tap_result_for_token_accounting` returns its input unchanged.
* :func:`get_runtime_context` reads the live cache.
* :func:`note_successful_compact` (Wave-12 W2A1 P0) clears the cache.

References:

* :mod:`lossless_hermes.plugin.token_state` — implementation.
* ``/Volumes/LEXAR/Claude/lossless-claw/src/plugin/token-state.ts`` — TS source.
* Issue spec: ``epics/06-tools/06-03-runwithtokengate-middleware.md``.
"""

from __future__ import annotations

import typing

import pytest

from lossless_hermes.plugin import token_state


@pytest.fixture(autouse=True)
def _reset_state() -> typing.Iterator[None]:
    """Clear the cache between tests."""
    token_state.__reset_token_state_for_testing()
    yield
    token_state.__reset_token_state_for_testing()


# ---------------------------------------------------------------------------
# record_llm_output
# ---------------------------------------------------------------------------


class TestRecordLlmOutput:
    """The anchor write captures input + cache fields."""

    def test_openai_chat_shape(self) -> None:
        """OpenAI Chat ``prompt_tokens`` is the input source."""
        token_state.record_llm_output(
            session_key="sess",
            usage={"prompt_tokens": 5_000, "completion_tokens": 1_000},
            token_budget=100_000,
        )
        ctx = token_state.get_runtime_context("sess")
        # Composition: input + cache_read + cache_write. No cache fields => 5000.
        assert ctx["current_token_count"] == 5_000
        assert ctx["token_budget"] == 100_000
        assert ctx["last_update_source"] == "llm_output"

    def test_anthropic_native_shape(self) -> None:
        """Anthropic native fields: ``input_tokens`` + cache reads/writes."""
        token_state.record_llm_output(
            session_key="sess",
            usage={
                "input_tokens": 2_000,
                "cache_read_input_tokens": 8_000,
                "cache_creation_input_tokens": 1_000,
            },
            token_budget=200_000,
        )
        ctx = token_state.get_runtime_context("sess")
        # 2000 + 8000 + 1000 = 11000.
        assert ctx["current_token_count"] == 11_000

    def test_hermes_normalized_shape(self) -> None:
        """Hermes-normalized fields (ADR-015 patch #4) take precedence."""
        token_state.record_llm_output(
            session_key="sess",
            usage={
                "input_tokens": 2_000,
                "cache_read_tokens": 10_000,
                "cache_write_tokens": 500,
            },
            token_budget=200_000,
        )
        ctx = token_state.get_runtime_context("sess")
        assert ctx["current_token_count"] == 12_500

    def test_openai_responses_codex_shape(self) -> None:
        """OpenAI Responses ``prompt_tokens_details.cached_tokens``."""
        token_state.record_llm_output(
            session_key="sess",
            usage={
                "prompt_tokens": 3_000,
                "prompt_tokens_details": {"cached_tokens": 9_000},
            },
            token_budget=128_000,
        )
        ctx = token_state.get_runtime_context("sess")
        # 3000 + 9000 = 12000.
        assert ctx["current_token_count"] == 12_000

    def test_empty_usage_zero_count(self) -> None:
        """Empty ``usage`` dict => 0 token count (no fields present)."""
        token_state.record_llm_output(
            session_key="sess",
            usage={},
            token_budget=100_000,
        )
        ctx = token_state.get_runtime_context("sess")
        assert ctx["current_token_count"] == 0

    def test_empty_session_key_no_op(self) -> None:
        """Empty session_key is a no-op (matches TS guard)."""
        token_state.record_llm_output(
            session_key="",
            usage={"prompt_tokens": 5_000},
            token_budget=100_000,
        )
        assert token_state.get_runtime_context("") == {}

    def test_none_session_key_no_op(self) -> None:
        """``session_key=None`` is also a no-op."""
        token_state.record_llm_output(
            session_key=None,
            usage={"prompt_tokens": 5_000},
            token_budget=100_000,
        )
        assert token_state.get_runtime_context(None) == {}

    def test_subsequent_anchor_replaces(self) -> None:
        """A second anchor write replaces the first count + source."""
        token_state.record_llm_output(
            session_key="sess",
            usage={"prompt_tokens": 1_000},
            token_budget=100_000,
        )
        token_state.record_llm_output(
            session_key="sess",
            usage={"prompt_tokens": 5_000},
            token_budget=100_000,
        )
        ctx = token_state.get_runtime_context("sess")
        assert ctx["current_token_count"] == 5_000
        assert ctx["last_update_source"] == "llm_output"

    def test_token_budget_preserved_when_none(self) -> None:
        """``token_budget=None`` keeps the prior anchor's budget."""
        token_state.record_llm_output(
            session_key="sess",
            usage={"prompt_tokens": 1_000},
            token_budget=100_000,
        )
        token_state.record_llm_output(
            session_key="sess",
            usage={"prompt_tokens": 2_000},
            token_budget=None,
        )
        ctx = token_state.get_runtime_context("sess")
        assert ctx["token_budget"] == 100_000


# ---------------------------------------------------------------------------
# accumulate_tool_result_tokens
# ---------------------------------------------------------------------------


class TestAccumulateToolResultTokens:
    """The additive tool-side update accumulates per call."""

    def test_accumulates_after_anchor(self) -> None:
        """After an anchor, tool result tokens add to the count."""
        token_state.record_llm_output(
            session_key="sess",
            usage={"prompt_tokens": 1_000},
            token_budget=100_000,
        )
        # 800 chars / 4 = 200 tokens.
        token_state.accumulate_tool_result_tokens("sess", "x" * 800)
        ctx = token_state.get_runtime_context("sess")
        assert ctx["current_token_count"] == 1_200
        assert ctx["last_update_source"] == "tool-self-report"

    def test_accumulates_idempotent_across_recalls(self) -> None:
        """Calling twice accumulates additively."""
        token_state.record_llm_output(
            session_key="sess",
            usage={"prompt_tokens": 1_000},
            token_budget=100_000,
        )
        token_state.accumulate_tool_result_tokens("sess", "x" * 400)
        token_state.accumulate_tool_result_tokens("sess", "x" * 400)
        ctx = token_state.get_runtime_context("sess")
        assert ctx["current_token_count"] == 1_000 + 100 + 100

    def test_no_op_without_anchor(self) -> None:
        """No anchor => the tap is a no-op (matches TS guard)."""
        token_state.accumulate_tool_result_tokens("sess", "x" * 800)
        assert token_state.get_runtime_context("sess") == {}

    def test_no_op_empty_text(self) -> None:
        """Empty result text => no-op."""
        token_state.record_llm_output(
            session_key="sess",
            usage={"prompt_tokens": 1_000},
            token_budget=100_000,
        )
        token_state.accumulate_tool_result_tokens("sess", "")
        ctx = token_state.get_runtime_context("sess")
        assert ctx["current_token_count"] == 1_000  # unchanged

    def test_ceil_rounding(self) -> None:
        """Lengths not divisible by 4 ceil-round."""
        token_state.record_llm_output(
            session_key="sess",
            usage={"prompt_tokens": 0},
            token_budget=100_000,
        )
        # 5 chars / 4 = 1.25 -> ceil 2.
        token_state.accumulate_tool_result_tokens("sess", "x" * 5)
        ctx = token_state.get_runtime_context("sess")
        assert ctx["current_token_count"] == 2


# ---------------------------------------------------------------------------
# tap_result_for_token_accounting
# ---------------------------------------------------------------------------


class TestTapResultForTokenAccounting:
    """The tap helper is a convenience wrapper around accumulate."""

    def test_returns_input_unchanged(self) -> None:
        """The helper returns its input so it can sit in a ``return``."""
        token_state.record_llm_output(
            session_key="sess",
            usage={"prompt_tokens": 1_000},
            token_budget=100_000,
        )
        result = token_state.tap_result_for_token_accounting("sess", "abc")
        assert result == "abc"

    def test_accumulates_state_like_raw_call(self) -> None:
        """Same cache effect as :func:`accumulate_tool_result_tokens`."""
        token_state.record_llm_output(
            session_key="sess",
            usage={"prompt_tokens": 1_000},
            token_budget=100_000,
        )
        token_state.tap_result_for_token_accounting("sess", "x" * 400)
        ctx = token_state.get_runtime_context("sess")
        assert ctx["current_token_count"] == 1_100


# ---------------------------------------------------------------------------
# get_runtime_context
# ---------------------------------------------------------------------------


class TestGetRuntimeContext:
    """The accessor returns the cached fields as a dict."""

    def test_empty_when_no_anchor(self) -> None:
        """No anchor => empty dict (the gate's bypass signal)."""
        assert token_state.get_runtime_context("sess") == {}

    def test_empty_when_session_key_empty(self) -> None:
        """Empty session_key => empty dict regardless of cache state."""
        token_state.record_llm_output(
            session_key="real-sess",
            usage={"prompt_tokens": 1_000},
            token_budget=100_000,
        )
        assert token_state.get_runtime_context("") == {}
        assert token_state.get_runtime_context(None) == {}

    def test_returns_all_fields(self) -> None:
        """All four documented keys are present."""
        token_state.record_llm_output(
            session_key="sess",
            usage={"prompt_tokens": 1_000},
            token_budget=100_000,
        )
        ctx = token_state.get_runtime_context("sess")
        assert "current_token_count" in ctx
        assert "token_budget" in ctx
        assert "last_update_at" in ctx
        assert "last_update_source" in ctx


# ---------------------------------------------------------------------------
# note_successful_compact — Wave-12 W2A1 P0
# ---------------------------------------------------------------------------


class TestNoteSuccessfulCompact:
    """Post-compact cache reset is the W2A1 P0 fix."""

    def test_clears_cache_entry(self) -> None:
        """After ``note_successful_compact``, the next read sees empty."""
        token_state.record_llm_output(
            session_key="sess",
            usage={"prompt_tokens": 180_000},
            token_budget=200_000,
        )
        token_state.note_successful_compact("sess")
        assert token_state.get_runtime_context("sess") == {}

    def test_other_sessions_unaffected(self) -> None:
        """Only the specified session-key is cleared."""
        token_state.record_llm_output(
            session_key="sess-A",
            usage={"prompt_tokens": 100_000},
            token_budget=200_000,
        )
        token_state.record_llm_output(
            session_key="sess-B",
            usage={"prompt_tokens": 50_000},
            token_budget=200_000,
        )
        token_state.note_successful_compact("sess-A")
        assert token_state.get_runtime_context("sess-A") == {}
        ctx_b = token_state.get_runtime_context("sess-B")
        assert ctx_b["current_token_count"] == 50_000

    def test_no_op_when_session_key_empty(self) -> None:
        """``session_key=None`` / empty is a no-op (matches the contract)."""
        token_state.record_llm_output(
            session_key="sess",
            usage={"prompt_tokens": 1_000},
            token_budget=100_000,
        )
        token_state.note_successful_compact(None)
        token_state.note_successful_compact("")
        ctx = token_state.get_runtime_context("sess")
        assert ctx["current_token_count"] == 1_000

    def test_tap_recreates_entry_post_clear(self) -> None:
        """After clear, the next tap with an anchor seeds fresh state.

        This is the documented behavior in the module docstring: "the next
        wrapped tool call sees no snapshot -> gate bypasses -> tool runs
        -> tap_result_for_token_accounting recreates the entry with the
        size of the new result (small)". Without a NEW anchor, tap stays
        a no-op.
        """
        token_state.record_llm_output(
            session_key="sess",
            usage={"prompt_tokens": 180_000},
            token_budget=200_000,
        )
        token_state.note_successful_compact("sess")
        # Without a NEW anchor, tap stays a no-op.
        token_state.accumulate_tool_result_tokens("sess", "x" * 800)
        assert token_state.get_runtime_context("sess") == {}
        # After a new anchor, tap accumulates normally.
        token_state.record_llm_output(
            session_key="sess",
            usage={"prompt_tokens": 70_000},
            token_budget=200_000,
        )
        token_state.accumulate_tool_result_tokens("sess", "x" * 800)
        ctx = token_state.get_runtime_context("sess")
        assert ctx["current_token_count"] == 70_000 + 200
