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


# ---------------------------------------------------------------------------
# Issue 03-07b additions — assembler-flow scenarios
# ---------------------------------------------------------------------------
#
# These tests exercise ``sanitize_tool_use_result_pairing`` from the
# perspective of its assembler.ts:1301 call site. The function is the
# FINAL repair pass after the budget walk + ``filter_non_fresh_assistant_tool_calls``
# (03-07a) have run, so its inputs look like:
#
#   * Cross-message tool_use ↔ tool_result pairs (Anthropic shape)
#   * Survivors of the orphan-strip pass that need synthetic pairing
#   * Mixed user/assistant/toolResult sequences shaped by budget walk
#
# Verbatim TS shape per ADR-029: orphan tool_use blocks are NOT stripped
# here — they get a synthetic placeholder toolResult (the strip path
# lives in 03-07a). Tests below assert the synthesize-not-strip
# semantics so a future executor doesn't accidentally drift toward
# strip-based behavior.


def test_assembler_flow_cross_message_pair_both_kept() -> None:
    """Cross-message pair: tool_use in msg N, tool_result in msg N+1 → both kept.

    The canonical assembler-output shape: assistant emits a tool_use
    block, the next message is a separate ``toolResult`` carrying
    ``toolCallId``. Output content equal to input content; the only
    structural change is that the user message after the toolResult
    gets re-emitted via the ``remainder`` accumulator (TS
    ``moved=true`` path).

    The TS short-circuit returns the input identity ONLY when
    ``changed_or_moved`` stays False. With the assistant-span sub-loop
    consuming both the toolResult AND the trailing user, ``moved``
    flips True even though the message order is identical to the
    input. Test pins the structural-equality semantics: ``sanitized
    != input`` reference, ``sanitized == input`` content.
    """
    messages: list[Mapping[str, Any]] = [
        {"role": "user", "content": [{"type": "text", "text": "search please"}]},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "tu_cross", "name": "search"}],
        },
        {
            "role": "toolResult",
            "toolCallId": "tu_cross",
            "content": [{"type": "text", "text": "found 3 results"}],
        },
        {"role": "user", "content": [{"type": "text", "text": "thanks"}]},
    ]

    sanitized = sanitize_tool_use_result_pairing(messages)
    # The ``moved`` flag flips True because the span sub-loop
    # vacuumed both the toolResult and the trailing user. The output
    # is a fresh list but its content equals the input.
    assert list(sanitized) == list(messages)
    # No drops, no synthetics — the wrapper sees a structural diff of 0.
    result = repair_transcript(messages)
    assert result.dropped_count == 0
    assert result.synthesized_count == 0
    assert result.repaired_count == 0


def test_assembler_flow_cross_message_pair_missing_result_synthesizes() -> None:
    """Cross-message pair: tool_use in msg N, tool_result missing → synthetic inserted.

    Verbatim TS behavior at ``transcript-repair.ts:283-289``: an orphan
    tool_use gets a synthetic placeholder ``toolResult`` (marker string
    ``[lossless-claw] missing tool result …``). It is NOT stripped — the
    strip path is the separate 03-07a function on the assembler. Test
    pins the synthesize-not-strip semantics for future executors.
    """
    messages: list[Mapping[str, Any]] = [
        {"role": "user", "content": [{"type": "text", "text": "search please"}]},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "tu_no_result", "name": "search"}],
        },
        # No toolResult for tu_no_result; next message is a fresh user turn.
        {"role": "user", "content": [{"type": "text", "text": "another question"}]},
    ]

    result = repair_transcript(messages)
    assert result.synthesized_count == 1, "orphan tool_use → synthetic placeholder"
    assert result.dropped_count == 0
    # Output order: user, assistant, synthetic toolResult, user
    roles = [m.get("role") for m in result.messages if isinstance(m, Mapping)]
    assert roles == ["user", "assistant", "toolResult", "user"]
    # The synthetic placeholder carries the deterministic marker.
    synthetic = result.messages[2]
    assert synthetic.get("isError") is True
    assert synthetic.get("toolCallId") == "tu_no_result"
    assert synthetic.get("toolName") == "search"


def test_assembler_flow_orphan_tool_result_with_preserved_user_text() -> None:
    """Orphan tool_result dropped — surrounding user/assistant text preserved.

    Common in assembler output: an evicted assistant turn left a
    dangling tool_result whose tool_use is no longer in the selected
    window. The sanitize pass drops it but the surrounding text-bearing
    messages must survive unchanged.
    """
    messages: list[Mapping[str, Any]] = [
        {"role": "user", "content": [{"type": "text", "text": "user before orphan"}]},
        {
            "role": "toolResult",
            "toolCallId": "tu_evicted",
            "content": [{"type": "text", "text": "result from evicted tool_use"}],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "assistant after orphan"}]},
    ]

    result = repair_transcript(messages)
    assert result.dropped_count == 1
    assert result.synthesized_count == 0
    roles_in_order = [m.get("role") for m in result.messages if isinstance(m, Mapping)]
    assert roles_in_order == ["user", "assistant"]
    # Text content of the surviving messages is unchanged.
    surviving_texts = [
        m["content"][0]["text"]
        for m in result.messages
        if isinstance(m, Mapping) and isinstance(m.get("content"), list)
    ]
    assert surviving_texts == ["user before orphan", "assistant after orphan"]


def test_assembler_flow_single_assistant_message_no_change() -> None:
    """Single text-only assistant message → identity preserved.

    Smallest valid input: one assistant message with text content only.
    No tool calls, no tool results, nothing to repair.
    """
    messages: list[Mapping[str, Any]] = [
        {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
    ]
    sanitized = sanitize_tool_use_result_pairing(messages)
    assert sanitized is messages

    result = repair_transcript(messages)
    assert result.messages is messages
    assert result.dropped_count == 0
    assert result.synthesized_count == 0
    assert result.repaired_count == 0


def test_assembler_flow_all_empty_content_messages_pass_through() -> None:
    """All-empty-content messages pass through unchanged.

    The sanitize pass does NOT enforce non-empty content — that lives
    in the assembler's "clean empty assistant turns" step at TS 1283-1293
    (Step 13 of 03-08), which runs BEFORE this function. Sanitize sees
    a clean message list and treats empty-content turns as non-tool
    messages with nothing to repair.
    """
    messages: list[Mapping[str, Any]] = [
        {"role": "user", "content": []},
        {"role": "assistant", "content": []},
        {"role": "user", "content": []},
    ]
    sanitized = sanitize_tool_use_result_pairing(messages)
    assert sanitized is messages


def test_assembler_flow_multi_call_assistant_partial_orphan_synthesizes() -> None:
    """Multi-call assistant turn: some calls paired, others orphaned.

    An assistant message with two tool_use blocks where only one has a
    matching toolResult. The matched call's result is preserved; the
    orphan call gets a synthetic placeholder. Verbatim TS behavior at
    ``transcript-repair.ts:278-290`` — the for-each-call loop inserts
    synthetics inline at the assistant turn's span end.
    """
    messages: list[Mapping[str, Any]] = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tu_matched", "name": "search"},
                {"type": "tool_use", "id": "tu_orphan", "name": "fetch"},
            ],
        },
        {
            "role": "toolResult",
            "toolCallId": "tu_matched",
            "content": [{"type": "text", "text": "search result"}],
        },
        # No toolResult for tu_orphan.
        {"role": "user", "content": [{"type": "text", "text": "continue"}]},
    ]

    result = repair_transcript(messages)
    assert result.synthesized_count == 1, "the orphan call gets one synthetic"
    # Order: assistant, real toolResult (tu_matched), synthetic (tu_orphan), user.
    out = list(result.messages)
    assert len(out) == 4
    assert out[0].get("role") == "assistant"
    assert out[1].get("role") == "toolResult"
    assert out[1].get("toolCallId") == "tu_matched"
    assert out[2].get("role") == "toolResult"
    assert out[2].get("toolCallId") == "tu_orphan"
    assert out[2].get("isError") is True
    assert out[3].get("role") == "user"


def test_assembler_flow_long_conversation_with_multiple_spans() -> None:
    """Multi-span integration: pairs, orphans, dupes interleaved.

    A realistic post-budget-walk transcript:
      * Span 1: matched pair (keep)
      * Span 2: orphan tool_use (synthesize)
      * Floating orphan tool_result between spans (drop)
      * Span 3: matched pair (keep)
    """
    messages: list[Mapping[str, Any]] = [
        # Span 1: matched
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "id_a", "name": "search"}],
        },
        {
            "role": "toolResult",
            "toolCallId": "id_a",
            "content": [{"type": "text", "text": "result a"}],
        },
        # Span 2: orphan tool_use
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "id_b_orphan", "name": "fetch"}],
        },
        # Orphan tool_result between spans (no preceding tool_use for id_c)
        {
            "role": "toolResult",
            "toolCallId": "id_c_floating",
            "content": [{"type": "text", "text": "nobody asked"}],
        },
        # Span 3: matched
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "id_d", "name": "search"}],
        },
        {
            "role": "toolResult",
            "toolCallId": "id_d",
            "content": [{"type": "text", "text": "result d"}],
        },
    ]

    result = repair_transcript(messages)
    assert result.synthesized_count == 1, "id_b_orphan synthetic placeholder"
    assert result.dropped_count == 1, "id_c_floating orphan dropped"

    # Resulting order: assistant(a), tr(a), assistant(b), synthetic(b), assistant(d), tr(d).
    out = list(result.messages)
    roles = [m.get("role") for m in out if isinstance(m, Mapping)]
    assert roles == [
        "assistant",
        "toolResult",
        "assistant",
        "toolResult",
        "assistant",
        "toolResult",
    ]
    # And every toolResult points to an id that appears in a preceding assistant turn.
    tool_result_ids = [
        m.get("toolCallId") for m in out if isinstance(m, Mapping) and m.get("role") == "toolResult"
    ]
    assert tool_result_ids == ["id_a", "id_b_orphan", "id_d"]


def test_assembler_flow_invariant_every_tool_result_has_preceding_tool_use() -> None:
    """Assembler acceptance criterion: final output has no unpaired tool_result.

    Per the 03-07b spec §"Critical invariant": every tool_result in
    the sanitize output must have a preceding matching tool_use in
    the same output. This integration assertion walks the output and
    checks the invariant on a fuzzy-shaped fixture.
    """
    messages: list[Mapping[str, Any]] = [
        # Orphan tool_result at the front (nothing precedes it)
        {
            "role": "toolResult",
            "toolCallId": "leading_orphan",
            "content": [{"type": "text", "text": "no preceding tool_use"}],
        },
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "matched_one", "name": "tool"}],
        },
        {
            "role": "toolResult",
            "toolCallId": "matched_one",
            "content": [{"type": "text", "text": "result"}],
        },
        # Trailing orphan (assistant turn already closed)
        {
            "role": "toolResult",
            "toolCallId": "trailing_orphan",
            "content": [{"type": "text", "text": "stray"}],
        },
    ]

    result = repair_transcript(messages)

    # Walk the output: every toolResult must have a tool_use with the
    # same id at an earlier index. (Synthetic placeholders count too —
    # they ride alongside the assistant turn that owns their id.)
    out = list(result.messages)
    seen_tool_use_ids: set[str] = set()
    for msg in out:
        if not isinstance(msg, Mapping):
            continue
        role = msg.get("role")
        if role == "assistant":
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, Mapping):
                        continue
                    block_id = block.get("id") or block.get("call_id")
                    if isinstance(block_id, str):
                        seen_tool_use_ids.add(block_id)
        elif role == "toolResult":
            tr_id = msg.get("toolCallId") or msg.get("toolUseId")
            assert tr_id in seen_tool_use_ids, (
                f"toolResult with id={tr_id!r} has no preceding matching tool_use — "
                f"invariant violation in sanitize output"
            )

    # And the structural counts match what we expect.
    assert result.dropped_count == 2, "both leading and trailing orphans dropped"
    assert result.synthesized_count == 0, "matched_one has a real toolResult; no synthetic"


def test_assembler_flow_synthetic_marker_is_byte_identical_to_ts_source() -> None:
    """Synthetic placeholder marker string must match TS source byte-for-byte.

    Per the 03-07b spec §"Acceptance criteria": "Synthesized tool_use
    blocks (when generated to pair a lone tool_result) carry a
    synthetic-marker comment in the TS source — preserve the marker
    (used by debug diagnostics)."

    The marker is checked at ``transcript-repair.ts:154``. Downstream
    consumers grep for the ``[lossless-claw]`` prefix; if this drifts
    in the Python port, the diagnostics break silently.
    """
    messages: list[Mapping[str, Any]] = [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "tu_marker", "name": "noop"}],
        },
    ]

    result = repair_transcript(messages)
    assert result.synthesized_count == 1
    synthetic = result.messages[1]
    content = synthetic.get("content")
    assert isinstance(content, list) and len(content) == 1
    text_block = content[0]
    assert isinstance(text_block, Mapping)
    assert text_block.get("text") == (
        "[lossless-claw] missing tool result in session history; "
        "inserted synthetic error result for transcript repair."
    ), "synthetic marker must be byte-identical to transcript-repair.ts:154"
    assert text_block.get("type") == "text"
    assert synthetic.get("isError") is True


def test_assembler_flow_anthropic_tool_use_kebab_case_variant() -> None:
    """``tool-use`` kebab-case is one of the six provider variants.

    The :data:`TOOL_CALL_TYPES` set (transcript-repair.ts:30) covers
    six block-type strings. This test pins behavior for the
    ``tool-use`` (kebab) variant; sister tests above cover ``tool_use``
    (snake) and ``function_call``. Cross-provider parity is critical
    because the assembler does NOT normalize block types — it passes
    them through as the upstream message store stored them.
    """
    messages: list[Mapping[str, Any]] = [
        {
            "role": "assistant",
            "content": [{"type": "tool-use", "id": "tu_kebab", "name": "search"}],
        },
        {
            "role": "toolResult",
            "toolCallId": "tu_kebab",
            "content": [{"type": "text", "text": "kebab works"}],
        },
    ]

    result = repair_transcript(messages)
    # No change needed — matched pair.
    assert result.messages is messages
    assert result.dropped_count == 0
    assert result.synthesized_count == 0


def test_assembler_flow_function_call_camelcase_variant() -> None:
    """``functionCall`` (camelCase) provider variant — OpenAI Chat shape."""
    messages: list[Mapping[str, Any]] = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "functionCall",
                    "id": "fc_camel",
                    "name": "search",
                    "arguments": "{}",
                },
            ],
        },
        {
            "role": "toolResult",
            "toolCallId": "fc_camel",
            "content": [{"type": "text", "text": "camel works"}],
        },
    ]

    result = repair_transcript(messages)
    assert result.messages is messages
    assert result.dropped_count == 0
    assert result.synthesized_count == 0


def test_assembler_flow_toolcall_legacy_variant() -> None:
    """``toolCall`` legacy (OpenClaw) variant pairs correctly with toolResult."""
    messages: list[Mapping[str, Any]] = [
        {
            "role": "assistant",
            "content": [{"type": "toolCall", "id": "tc_legacy", "name": "search"}],
        },
        {
            "role": "toolResult",
            "toolCallId": "tc_legacy",
            "content": [{"type": "text", "text": "legacy works"}],
        },
    ]

    result = repair_transcript(messages)
    assert result.messages is messages
    assert result.dropped_count == 0
    assert result.synthesized_count == 0


def test_assembler_flow_three_consecutive_assistant_turns_no_tool_use() -> None:
    """Three back-to-back assistant turns with text-only content.

    Tests the loop's handling of consecutive assistants — each
    advances the outer index without triggering the span-vacuum
    sub-loop because ``tool_calls`` is empty. Output must equal
    input (identity preserved).
    """
    messages: list[Mapping[str, Any]] = [
        {"role": "assistant", "content": [{"type": "text", "text": "first"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "second"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "third"}]},
    ]

    sanitized = sanitize_tool_use_result_pairing(messages)
    assert sanitized is messages

    result = repair_transcript(messages)
    assert result.messages is messages


def test_assembler_flow_assistant_with_text_and_tool_use_then_result() -> None:
    """Assistant turn with mixed text + tool_use content blocks.

    Realistic Anthropic assistant shape: one ``text`` block followed
    by one ``tool_use`` block. The matching toolResult comes next.
    No repair triggered; output identity-preserved.
    """
    messages: list[Mapping[str, Any]] = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Let me search for that."},
                {"type": "tool_use", "id": "tu_mixed", "name": "search"},
            ],
        },
        {
            "role": "toolResult",
            "toolCallId": "tu_mixed",
            "content": [{"type": "text", "text": "found"}],
        },
    ]

    result = repair_transcript(messages)
    assert result.messages is messages
    assert result.dropped_count == 0
    assert result.synthesized_count == 0
    assert result.repaired_count == 0


def test_assembler_flow_orphan_tool_use_aborted_skips_synthesis() -> None:
    """An aborted assistant turn's orphan tool_use does NOT trigger synthesis.

    Cross-references ``test_aborted_assistant_message_skips_synthetic_tool_result_creation``
    above but in the assembler-flow context: a budget-walked transcript
    may include partial/aborted assistant turns. The TS guard at
    ``transcript-repair.ts:216-222`` prevents creating synthetic
    placeholders for them — the assistant turn passes through as-is,
    no synthetic appended.
    """
    messages: list[Mapping[str, Any]] = [
        {"role": "user", "content": [{"type": "text", "text": "before"}]},
        {
            "role": "assistant",
            "stopReason": "aborted",
            "content": [{"type": "tool_use", "id": "tu_aborted_orphan", "name": "compute"}],
        },
        {"role": "user", "content": [{"type": "text", "text": "after"}]},
    ]

    result = repair_transcript(messages)
    assert result.synthesized_count == 0
    # Identity preserved — no synthetic was inserted because of the guard.
    assert result.messages is messages
