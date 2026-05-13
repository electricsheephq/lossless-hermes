"""Tests for the ``_on_pre_llm_call`` hook body (issue 03-10).

Covers the recall-policy injection per ADR-014: that the hook returns
``{"context": LOSSLESS_RECALL_POLICY_PROMPT}`` and that Hermes's
plugin plumbing routes this to user-message content (not system
prompt) at API-call time.

The complementary ``tests/test_hook_registrations.py`` covers the
wiring (``register_hook("pre_llm_call", ...)`` happens). This file
covers the body (what the registered callback returns).

See:

* ``docs/adr/014-recall-policy-injection.md`` — Option A decision.
* ``docs/spike-results/002-hermes-pre-llm-call.md`` — Hermes routes
  ``pre_llm_call`` returns to user-message content, not system prompt.
* ``epics/03-ingest-assembly/03-10-recall-policy-injection.md`` — AC.
"""

from __future__ import annotations

import logging

import pytest

from lossless_hermes.engine import LCMEngine
from lossless_hermes.recall_policy import LOSSLESS_RECALL_POLICY_PROMPT


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def engine() -> LCMEngine:
    """A freshly constructed :class:`LCMEngine` with no DB / migrations
    run. The ``_on_pre_llm_call`` body is stateless w.r.t. engine state
    (it returns the same policy text every turn) so the bare engine
    suffices."""
    return LCMEngine()


# ---------------------------------------------------------------------------
# Return-shape contract (ADR-014 §Decision)
# ---------------------------------------------------------------------------


def test_pre_llm_call_returns_context_dict(engine: LCMEngine) -> None:
    """ADR-014 §Decision: the hook returns ``{"context": <text>}``
    where Hermes's ``invoke_hook`` plumbing appends the ``context``
    value to the current turn's user-message content
    (``hermes_cli/plugins.py:1218-1232``).
    """
    result = engine._on_pre_llm_call(
        session_id="sess-1",
        user_message="hello",
        conversation_history=[],
        is_first_turn=True,
        model="claude-sonnet-4-5",
        platform="anthropic",
    )
    assert isinstance(result, dict), (
        f"pre_llm_call must return a dict per ADR-014; got {type(result).__name__}"
    )
    assert "context" in result, (
        "pre_llm_call return must carry a ``context`` key — Hermes's "
        "plumbing routes this value to user-message content"
    )


def test_pre_llm_call_context_is_policy_prompt(engine: LCMEngine) -> None:
    """The injected context is exactly :data:`LOSSLESS_RECALL_POLICY_PROMPT`.
    Pinning the equality (not just substring) guards against partial
    injection or accidental wrapping with extra text."""
    result = engine._on_pre_llm_call(
        session_id="sess-1",
        user_message="hello",
        conversation_history=[],
        is_first_turn=True,
        model="claude-sonnet-4-5",
        platform="anthropic",
    )
    assert result == {"context": LOSSLESS_RECALL_POLICY_PROMPT}


def test_pre_llm_call_never_returns_none(engine: LCMEngine) -> None:
    """Issue 03-10 lifts the hook from no-op (returns None at 02-07) to
    always-on injection. Pinning ``is not None`` is the deliberate
    regression-detection invariant — if a future change accidentally
    short-circuits to None, this test surfaces the regression."""
    result = engine._on_pre_llm_call(
        session_id="sess-1",
        user_message="anything",
        conversation_history=[],
        is_first_turn=False,
        model="claude-sonnet-4-5",
        platform="anthropic",
    )
    assert result is not None


# ---------------------------------------------------------------------------
# First-turn vs subsequent-turn parity (spike 002 line 38)
# ---------------------------------------------------------------------------


def test_pre_llm_call_fires_on_first_turn(engine: LCMEngine) -> None:
    """The hook injects on the first turn. Spike 002 line 38: "All
    non-None returns are concatenated" — Hermes does not gate the
    injection on ``is_first_turn``."""
    result = engine._on_pre_llm_call(
        session_id="sess-1",
        user_message="opening turn",
        conversation_history=[],
        is_first_turn=True,
        model="claude-sonnet-4-5",
        platform="anthropic",
    )
    assert result == {"context": LOSSLESS_RECALL_POLICY_PROMPT}


def test_pre_llm_call_fires_on_subsequent_turn(engine: LCMEngine) -> None:
    """The hook injects on every turn, not just the first. The policy
    text ships every turn so the agent retains it after compaction
    windows or long histories that drop earlier turns. Same shape as
    the first-turn case — invariant across turns."""
    history = [
        {"role": "user", "content": "earlier user turn"},
        {"role": "assistant", "content": "earlier assistant turn"},
    ]
    result = engine._on_pre_llm_call(
        session_id="sess-1",
        user_message="follow-up turn",
        conversation_history=history,
        is_first_turn=False,
        model="claude-sonnet-4-5",
        platform="anthropic",
    )
    assert result == {"context": LOSSLESS_RECALL_POLICY_PROMPT}


def test_pre_llm_call_first_and_subsequent_returns_are_identical(
    engine: LCMEngine,
) -> None:
    """The injection is stateless: the policy text is the same on turn
    1, turn N, and turn N+1. Pinning this guards against any future
    "first-turn-only" logic that would silently drop the policy on
    later turns."""
    first = engine._on_pre_llm_call(
        session_id="sess-1",
        user_message="turn 1",
        conversation_history=[],
        is_first_turn=True,
        model="claude-sonnet-4-5",
        platform="anthropic",
    )
    later = engine._on_pre_llm_call(
        session_id="sess-1",
        user_message="turn 17",
        conversation_history=[{"role": "user", "content": "x"}] * 16,
        is_first_turn=False,
        model="claude-sonnet-4-5",
        platform="anthropic",
    )
    assert first == later


# ---------------------------------------------------------------------------
# Idempotency on second call (defensive — see AC line 5 of dispatcher)
# ---------------------------------------------------------------------------


def test_pre_llm_call_second_call_returns_same_payload(engine: LCMEngine) -> None:
    """Calling the hook twice produces the same payload. The hook is
    stateless w.r.t. prior calls — the engine does not track or filter
    previous injections. This is the AC-line-5 invariant (defensive
    idempotency on repeated calls)."""
    kwargs = dict(
        session_id="sess-1",
        user_message="anything",
        conversation_history=[],
        is_first_turn=True,
        model="claude-sonnet-4-5",
        platform="anthropic",
    )
    result_1 = engine._on_pre_llm_call(**kwargs)
    result_2 = engine._on_pre_llm_call(**kwargs)
    assert result_1 == result_2, (
        "pre_llm_call must be stateless across calls — the engine has "
        "no signal about prior injections (Hermes builds the joined "
        "user context, not the engine)"
    )


# ---------------------------------------------------------------------------
# Signature tolerance — every documented kwarg + forward-compat **kwargs
# ---------------------------------------------------------------------------


def test_pre_llm_call_accepts_all_documented_kwargs(engine: LCMEngine) -> None:
    """The hook accepts every kwarg documented in
    ``docs/reference/hermes-hooks.md`` line 91. Mirrors the wiring-
    side check in ``test_hook_registrations.py`` but on the engine
    method directly (so a future engine refactor cannot regress the
    signature without surfacing here).
    """
    result = engine._on_pre_llm_call(
        session_id="sess-1",
        user_message="hello",
        conversation_history=[{"role": "user", "content": "hello"}],
        is_first_turn=True,
        model="claude-sonnet-4-5",
        platform="anthropic",
        sender_id="user-42",
    )
    assert isinstance(result, dict)


def test_pre_llm_call_tolerates_extra_kwargs(engine: LCMEngine) -> None:
    """Forward-compat: ``**kwargs`` swallows future Hermes signature
    additions. Same pattern as the ``post_llm_call`` tolerance test in
    ``test_hook_registrations.py``."""
    result = engine._on_pre_llm_call(
        session_id="sess-1",
        user_message="hello",
        conversation_history=[],
        is_first_turn=True,
        model="claude-sonnet-4-5",
        platform="anthropic",
        sender_id="",
        # Future-only kwargs that 03-10 must tolerate.
        future_field_added_in_hermes_v999="ignored",
        another_thing=42,
    )
    assert result == {"context": LOSSLESS_RECALL_POLICY_PROMPT}


def test_pre_llm_call_tolerates_none_conversation_history(
    engine: LCMEngine,
) -> None:
    """02-07 declared ``conversation_history: Optional[...] = None`` —
    callers that pass ``None`` (forward-compat / partial-kwarg callers)
    must not crash the hook."""
    result = engine._on_pre_llm_call(
        session_id="sess-1",
        user_message="hello",
        conversation_history=None,
        is_first_turn=True,
        model="claude-sonnet-4-5",
        platform="anthropic",
    )
    assert result == {"context": LOSSLESS_RECALL_POLICY_PROMPT}


# ---------------------------------------------------------------------------
# Debug-log breadcrumb
# ---------------------------------------------------------------------------


def test_pre_llm_call_emits_debug_breadcrumb(
    engine: LCMEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """The hook emits a DEBUG-level breadcrumb so operators scanning
    logs can confirm the policy fires each turn. The 02-07 stub
    already did this; 03-10 keeps the breadcrumb because the
    injection itself is invisible in any normal log."""
    with caplog.at_level(logging.DEBUG, logger="lossless_hermes.engine.assemble"):
        engine._on_pre_llm_call(
            session_id="sess-debug",
            user_message="hello",
            conversation_history=[{"role": "user", "content": "x"}],
            is_first_turn=False,
            model="claude-sonnet-4-5",
            platform="anthropic",
        )
    assert any(
        "pre_llm_call inject-policy" in rec.getMessage() and "sess-debug" in rec.getMessage()
        for rec in caplog.records
    ), (
        f"expected debug breadcrumb with 'pre_llm_call inject-policy' + session, "
        f"got: {[r.getMessage() for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# ADR-014 invariant: text is intended for user-message position, NOT system
# ---------------------------------------------------------------------------


def test_pre_llm_call_return_shape_is_context_not_system_prompt(
    engine: LCMEngine,
) -> None:
    """ADR-014 §Decision rules out injecting the policy into the system
    prompt (would invalidate Anthropic prompt cache every turn). The
    return dict's key must be ``"context"`` (Hermes routes to user
    message) and explicitly NOT something like ``"system"`` /
    ``"system_prompt"`` / ``"prependSystemContext"`` (which would
    signal system-prompt injection if Hermes had such a key).
    """
    result = engine._on_pre_llm_call(
        session_id="sess-1",
        user_message="hello",
        conversation_history=[],
        is_first_turn=True,
        model="claude-sonnet-4-5",
        platform="anthropic",
    )
    assert isinstance(result, dict)
    # Required key
    assert "context" in result
    # Forbidden keys — pinning these as the regression-detection
    # invariant per ADR-014 §"Open questions" item 5 ("future Hermes
    # change routes pre_llm_call returns to system prompt").
    for forbidden in ("system", "system_prompt", "prependSystemContext"):
        assert forbidden not in result, (
            f"pre_llm_call return must not carry a {forbidden!r} key "
            f"— that would signal system-prompt injection, breaking "
            f"ADR-014 §Decision (cache preservation)"
        )
