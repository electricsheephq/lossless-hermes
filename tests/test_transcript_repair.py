"""Tests for ``lossless_hermes.transcript_repair``.

Ports the three cases in ``lossless-claw/test/transcript-repair.test.ts``
(commit ``1f07fbd``) 1:1 to pytest plus the additional structural-counts
test mandated by ``epics/01-storage/01-14-transcript-repair.md``
§"Acceptance criteria".

Test naming mirrors the TS ``it("...")`` descriptions so a reviewer
cross-referencing the two suites can locate each case by description.
"""

from __future__ import annotations

from typing import Any, Mapping

import pytest

from lossless_hermes.transcript_repair import (
    RepairResult,
    repair_transcript,
    sanitize_tool_use_result_pairing,
)


# ---------------------------------------------------------------------------
# Ported cases — lossless-claw/test/transcript-repair.test.ts 1:1
# ---------------------------------------------------------------------------


def test_moves_openai_reasoning_blocks_before_function_call_blocks() -> None:
    """Ports TS case "moves OpenAI reasoning blocks before function_call blocks".

    Single ``function_call`` followed by a ``reasoning`` block: the
    reasoning block must be hoisted to the front of ``content``.
    """
    repaired = sanitize_tool_use_result_pairing([
        {
            "role": "assistant",
            "content": [
                {
                    "type": "function_call",
                    "call_id": "fc_1",
                    "name": "bash",
                    "arguments": '{"cmd":"pwd"}',
                },
                {"type": "reasoning", "text": "Need tool output first."},
            ],
        },
    ])

    assistant = repaired[0]
    assert isinstance(assistant, Mapping)
    content = assistant.get("content")
    assert isinstance(content, list)
    assert [block.get("type") for block in content] == ["reasoning", "function_call"]


def test_preserves_interleaved_reasoning_when_assistant_turn_has_multiple_function_calls() -> None:
    """Ports TS case "preserves interleaved reasoning when an assistant turn has multiple function calls".

    Two ``function_call`` blocks with a ``reasoning`` block between them:
    the reasoning placement is left alone — interleaved reasoning may
    be intentional in multi-call OpenAI turns.
    """
    input_messages: list[Mapping[str, Any]] = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "function_call",
                    "call_id": "fc_1",
                    "name": "bash",
                    "arguments": '{"cmd":"pwd"}',
                },
                {"type": "reasoning", "text": "Reasoning for the second call."},
                {
                    "type": "function_call",
                    "call_id": "fc_2",
                    "name": "bash",
                    "arguments": '{"cmd":"ls"}',
                },
            ],
        },
    ]

    repaired = sanitize_tool_use_result_pairing(input_messages)

    assistant = repaired[0]
    assert isinstance(assistant, Mapping)
    assert assistant.get("content") == [
        {
            "type": "function_call",
            "call_id": "fc_1",
            "name": "bash",
            "arguments": '{"cmd":"pwd"}',
        },
        {"type": "reasoning", "text": "Reasoning for the second call."},
        {
            "type": "function_call",
            "call_id": "fc_2",
            "name": "bash",
            "arguments": '{"cmd":"ls"}',
        },
    ]


def test_creates_deterministic_synthetic_tool_results_for_missing_calls() -> None:
    """Ports TS case "creates deterministic synthetic tool results for missing calls".

    A toolCall with no matching toolResult: a synthetic placeholder
    tool-result is inserted. Running the function twice on the same
    input must produce structurally identical output.
    """
    messages: list[Mapping[str, Any]] = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "toolCall",
                    "id": "call_missing",
                    "name": "update_plan",
                    "input": {"step": "x"},
                },
            ],
        },
    ]

    first = sanitize_tool_use_result_pairing(messages)
    second = sanitize_tool_use_result_pairing(messages)

    assert list(first) == list(second)
    assert first[1] == {
        "role": "toolResult",
        "toolCallId": "call_missing",
        "toolName": "update_plan",
        "content": [
            {
                "type": "text",
                "text": (
                    "[lossless-claw] missing tool result in session history; "
                    "inserted synthetic error result for transcript repair."
                ),
            },
        ],
        "isError": True,
    }


# ---------------------------------------------------------------------------
# Additional AC: pairing — orphans / duplicates / Anthropic shape
# ---------------------------------------------------------------------------


def test_anthropic_shape_pairs_tool_use_and_tool_result_by_id() -> None:
    """``repair_transcript(provider="anthropic")`` pairs by ``tool_use_id``.

    Acceptance criterion #1. A matching ``toolResult`` after the
    assistant turn is preserved in place; no synthetic insertion.
    """
    messages: list[Mapping[str, Any]] = [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "tu_anth_1", "name": "search"}],
        },
        {
            "role": "toolResult",
            "toolCallId": "tu_anth_1",
            "content": [{"type": "text", "text": "result body"}],
        },
    ]

    result = repair_transcript(messages, provider="anthropic")

    # Output is identity-preserved when nothing changes.
    assert result.messages is messages
    assert result.dropped_count == 0
    assert result.synthesized_count == 0
    assert result.repaired_count == 0


def test_openai_shape_pairs_function_call_and_tool_result_by_call_id() -> None:
    """``repair_transcript(provider="openai")`` translates OpenAI shape.

    Acceptance criterion #2. ``function_call`` uses ``call_id``; the
    matching ``toolResult`` references it via ``toolCallId``.
    """
    messages: list[Mapping[str, Any]] = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "function_call",
                    "call_id": "fc_openai_1",
                    "name": "bash",
                    "arguments": '{"cmd":"echo hi"}',
                },
            ],
        },
        {
            "role": "toolResult",
            "toolCallId": "fc_openai_1",
            "content": [{"type": "text", "text": "hi"}],
        },
    ]

    result = repair_transcript(messages, provider="openai")
    assert result.messages is messages
    assert result.synthesized_count == 0


def test_orphan_tool_result_with_no_matching_tool_use_is_dropped() -> None:
    """Acceptance criterion #4: orphaned ``toolResult`` → dropped."""
    messages: list[Mapping[str, Any]] = [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {
            "role": "toolResult",
            "toolCallId": "orphan_id",
            "content": [{"type": "text", "text": "nobody asked"}],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
    ]

    result = repair_transcript(messages)

    # The orphan toolResult is dropped; everything else remains.
    roles = [m.get("role") for m in result.messages if isinstance(m, Mapping)]
    assert "toolResult" not in roles
    assert result.dropped_count == 1
    assert result.synthesized_count == 0


def test_duplicate_tool_result_within_same_assistant_span_is_silently_handled() -> None:
    """Verbatim TS behavior: dupes within ONE assistant turn's span are silent.

    Faithful port of ``transcript-repair.ts:253-260``: when a second
    ``toolResult`` for the same id arrives within the SAME assistant
    turn's span, the inner state silently skips it (does NOT increment
    ``droppedDuplicateCount``, does NOT set ``changed``). Because
    ``changed`` stays False, the function's ``changedOrMoved`` short-
    circuit returns the input array as-is — so both duplicates survive
    in the output despite the inner ``out`` array dropping the second.

    This is a quirk/bug of the upstream TS implementation. ADR-029
    §"Wave-N provenance comments" + the executor's "verbatim shape —
    don't simplify or re-architect" instruction means we preserve it
    rather than silently diverging. A cross-span duplicate (separate
    test below) DOES surface as ``dropped_count``.
    """
    messages: list[Mapping[str, Any]] = [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "dup_1", "name": "search"}],
        },
        {
            "role": "toolResult",
            "toolCallId": "dup_1",
            "content": [{"type": "text", "text": "first"}],
        },
        {
            "role": "toolResult",
            "toolCallId": "dup_1",
            "content": [{"type": "text", "text": "second (duplicate)"}],
        },
    ]

    # Run via the verbatim port — the identity short-circuit is the load-bearing
    # assertion (returns input when ``changed`` is never set, even though ``out``
    # internally dropped the dupe).
    sanitized = sanitize_tool_use_result_pairing(messages)
    assert sanitized is messages, (
        "TS short-circuit: changed stays False for in-span dupes, so input is returned"
    )

    # The wrapper sees identity → all counts zero (it cannot recover the
    # discarded dupe from a no-op output).
    result = repair_transcript(messages)
    assert result.messages is messages
    assert result.dropped_count == 0
    assert result.synthesized_count == 0


def test_cross_span_duplicate_tool_result_is_dropped_and_counted() -> None:
    """A duplicate ``toolResult`` for an id ALREADY EMITTED in a prior span.

    ``seenToolResultIds`` is the cross-span guard (line 251 in TS). Once
    a tool-result for id X has been ``_push_tool_result``'d (so it lives
    in ``seenToolResultIds``), any later ``toolResult`` for X within a
    DIFFERENT assistant span gets dropped AND counted.
    """
    messages: list[Mapping[str, Any]] = [
        # Span 1: assistant tool_use(dup_x) + matching toolResult
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "dup_x", "name": "search"}],
        },
        {
            "role": "toolResult",
            "toolCallId": "dup_x",
            "content": [{"type": "text", "text": "first"}],
        },
        # Span 2: another assistant turn that also references dup_x.
        # The matching toolResult for span 2 is a duplicate id; the
        # cross-span dedupe path inside ``_push_tool_result`` fires.
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "dup_x", "name": "search"}],
        },
        {
            "role": "toolResult",
            "toolCallId": "dup_x",
            "content": [{"type": "text", "text": "second (duplicate cross-span)"}],
        },
    ]

    result = repair_transcript(messages)

    # Verbatim TS behavior (transcript-repair.ts:251-260 + 278-290): the
    # duplicate is dropped at the span-scan step (NEVER added to
    # spanResultsById). The for-call loop then falls into the
    # makeMissingToolResult branch, creating a synthetic with the same
    # id. _push_tool_result then ALSO drops the synthetic because the
    # id is in seenToolResultIds. End result: span 2's assistant turn
    # is emitted with NO accompanying tool-result.
    tool_results = [
        m for m in result.messages if isinstance(m, Mapping) and m.get("role") == "toolResult"
    ]
    assert len(tool_results) == 1, "span 1's real toolResult survives; span 2 has none"
    # Synthesized count = 0 because the would-be synthetic also got
    # dedupe-dropped before reaching the output.
    assert result.synthesized_count == 0
    # Diff: input had 2 toolResults, output has 1, synthesized 0
    # → dropped_count = max(0, 2 - (1 - 0)) = 1.
    assert result.dropped_count == 1


def test_orphan_tool_use_synthesizes_placeholder_tool_result() -> None:
    """Acceptance criterion #3: orphan ``tool_use`` → synthetic placeholder.

    The synthetic placeholder is identifiable via ``isError=True`` and
    the deterministic marker string.
    """
    messages: list[Mapping[str, Any]] = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tu_missing", "name": "compute"},
            ],
        },
    ]

    result = repair_transcript(messages)

    assert result.synthesized_count == 1
    assert result.dropped_count == 0
    # The synthetic placeholder is appended after the assistant turn.
    assert len(result.messages) == 2
    synthetic = result.messages[1]
    assert synthetic.get("isError") is True
    assert synthetic.get("toolName") == "compute"
    text_block = synthetic["content"][0]
    assert "[lossless-claw]" in text_block["text"]


def test_aborted_assistant_message_skips_synthetic_tool_result_creation() -> None:
    """``stopReason == 'aborted'`` suppresses synthetic insertion.

    Mirrors the explicit guard in ``transcript-repair.ts:216-222``: when
    ``stopReason`` is ``"error"`` or ``"aborted"``, the tool_use blocks
    may be incomplete and we must not synthesize placeholder results.
    """
    messages: list[Mapping[str, Any]] = [
        {
            "role": "assistant",
            "stopReason": "aborted",
            "content": [
                {"type": "tool_use", "id": "tu_partial", "name": "compute"},
            ],
        },
    ]

    result = repair_transcript(messages)
    assert result.synthesized_count == 0
    # Identity preserved — no repair triggered.
    assert result.messages is messages


def test_errored_assistant_message_skips_synthetic_tool_result_creation() -> None:
    """``stopReason == 'error'`` also suppresses synthetic insertion."""
    messages: list[Mapping[str, Any]] = [
        {
            "role": "assistant",
            "stopReason": "error",
            "content": [
                {"type": "tool_use", "id": "tu_errored", "name": "compute"},
            ],
        },
    ]

    result = repair_transcript(messages)
    assert result.synthesized_count == 0
    assert result.messages is messages


# ---------------------------------------------------------------------------
# Additional AC: structural counts populated correctly
# ---------------------------------------------------------------------------


def test_repair_result_counts_match_structural_diff_mixed_fixture() -> None:
    """Counts (``dropped`` / ``synthesized`` / ``repaired``) sum coherently.

    Acceptance criterion: ``RepairResult.dropped_count``,
    ``synthesized_count``, ``repaired_count`` are correctly populated
    and the sum matches the structural diff between input and output
    messages.

    Fixture mixes:
      * one assistant turn with reasoning-after-function_call (→ 1 repaired)
      * one orphan tool_use (→ 1 synthesized)
      * one orphan toolResult (→ 1 dropped)
    """
    messages: list[Mapping[str, Any]] = [
        # 1. Reasoning hoist — assistant with single function_call + trailing reasoning
        {
            "role": "assistant",
            "content": [
                {
                    "type": "function_call",
                    "call_id": "fc_hoist",
                    "name": "noop",
                    "arguments": "{}",
                },
                {"type": "reasoning", "text": "after the call"},
            ],
        },
        # The matching toolResult exists so this assistant turn is not synthetic.
        {
            "role": "toolResult",
            "toolCallId": "fc_hoist",
            "content": [{"type": "text", "text": "noop result"}],
        },
        # 2. Orphan tool_use — assistant turn with no matching toolResult after it
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tu_synthesize", "name": "compute"},
            ],
        },
        # 3. Orphan toolResult — no preceding assistant tool_use with this id
        {
            "role": "toolResult",
            "toolCallId": "tu_orphan",
            "content": [{"type": "text", "text": "nobody"}],
        },
    ]

    result = repair_transcript(messages)

    assert isinstance(result, RepairResult)
    assert result.repaired_count == 1, "one assistant turn had reasoning hoisted"
    assert result.synthesized_count == 1, "one orphan tool_use → one synthetic placeholder"
    assert result.dropped_count == 1, "one orphan toolResult dropped"


def test_repair_result_no_change_returns_identity_with_zero_counts() -> None:
    """No-change input must preserve identity and zero out all counts."""
    messages: list[Mapping[str, Any]] = [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
    ]

    result = repair_transcript(messages)

    assert result.messages is messages
    assert result.dropped_count == 0
    assert result.synthesized_count == 0
    assert result.repaired_count == 0


def test_repair_transcript_rejects_unknown_provider() -> None:
    """``provider`` is restricted to ``"anthropic" | "openai"``."""
    with pytest.raises(ValueError, match="unknown provider"):
        repair_transcript([], provider="gemini")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Edge cases — input shape robustness
# ---------------------------------------------------------------------------


def test_non_mapping_entries_pass_through_unchanged() -> None:
    """Non-mapping items (e.g. ``None``, strings) are passed through.

    Mirrors the TS guard ``if (!msg || typeof msg !== "object")`` —
    callers occasionally pass partially constructed sequences and the
    repair should not crash.
    """
    messages: list[Any] = [
        None,
        "not a mapping",
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
    ]

    repaired = sanitize_tool_use_result_pairing(messages)
    # Identity preserved when nothing repaired.
    assert repaired is messages


def test_empty_input_returns_empty_unchanged() -> None:
    """An empty input is a no-op (identity preserved)."""
    messages: list[Mapping[str, Any]] = []
    repaired = sanitize_tool_use_result_pairing(messages)
    assert repaired is messages


def test_tool_use_id_fallback_is_accepted() -> None:
    """``toolUseId`` is accepted alongside the newer ``toolCallId``.

    Older session payloads carry ``toolUseId``; the extractor falls
    back when ``toolCallId`` is absent (transcript-repair.ts:131-139).
    """
    messages: list[Mapping[str, Any]] = [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "tu_fallback", "name": "search"}],
        },
        {
            "role": "toolResult",
            "toolUseId": "tu_fallback",  # legacy-shape field
            "content": [{"type": "text", "text": "result"}],
        },
    ]

    result = repair_transcript(messages)
    assert result.synthesized_count == 0
    assert result.dropped_count == 0


def test_interleaved_user_message_is_re_emitted_after_tool_result() -> None:
    """A user message interleaved within the tool-result span gets re-emitted.

    Mirrors the TS ``remainder`` accumulator — when we vacuum up tool-
    results to place them adjacent to the assistant turn, any non-
    tool-result messages we encountered get re-emitted after the
    paired tool-results.
    """
    messages: list[Mapping[str, Any]] = [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "tu_span", "name": "search"}],
        },
        # An interleaved user message between the assistant and its
        # tool-result. The repair preserves it but re-emits it after
        # the paired tool-result.
        {"role": "user", "content": [{"type": "text", "text": "mid-span comment"}]},
        {
            "role": "toolResult",
            "toolCallId": "tu_span",
            "content": [{"type": "text", "text": "result"}],
        },
    ]

    result = repair_transcript(messages)
    # All three messages survive; their order is assistant -> toolResult -> user.
    roles_in_order = [m.get("role") for m in result.messages if isinstance(m, Mapping)]
    assert roles_in_order == ["assistant", "toolResult", "user"]
