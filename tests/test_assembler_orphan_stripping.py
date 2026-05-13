"""Tests for :func:`lossless_hermes.assembler.filter_non_fresh_assistant_tool_calls`.

Ports the orphan-tool-call stripping invariants exercised by
``lossless-claw/test/assembler-blocks.test.ts`` (LCM commit ``1f07fbd``)
into standalone unit tests. Covers every branch of TS
``filterNonFreshAssistantToolCalls`` (``assembler.ts`` 687-778) plus
the helper extraction functions and the dataclass result shape.

### Source mapping

* :func:`extract_tool_call_id_from_block` (Python) ↔ ``extractToolCallId``
  (TS 642-650). Tests cover the ``id``-first-then-``call_id``
  ordering and the non-empty-string guard.
* :func:`extract_tool_result_id_from_message` (Python) ↔
  ``extractToolResultIdFromMessage`` (TS 674-685). Tests cover the
  ``toolCallId``-first-then-``toolUseId`` ordering and the
  non-empty-string guard.
* :func:`filter_non_fresh_assistant_tool_calls` (Python) ↔
  ``filterNonFreshAssistantToolCalls`` (TS 687-778). Tests derive
  from the AC matrix in
  ``epics/03-ingest-assembly/03-07-orphan-stripping.md`` plus every
  decision-tree branch in the TS source.

### Invariants verified (per spec AC)

* Selected tool_result index built correctly (one list per id,
  ordinals in input order).
* Per-block decision tree:
  - Matched tool_use with later tool_result → KEEP.
  - Orphan tool_use, ordinal < boundary → STRIP.
  - Orphan tool_use, ordinal >= boundary → KEEP (cache-marginal).
  - Orphan tool_use, ordinal < boundary, but resolved-anywhere → KEEP
    (cache-marginal fallback).
* Message-level disposition:
  - All blocks stripped → message dropped.
  - All blocks stripped but surviving text content → impossible
    under the current algorithm (the filter only removes blocks; if
    a text block is present in ``content``, ``new_content`` is
    non-empty and the message survives with reduced content). The
    "text survives" case is therefore the partial-strip case.
  - Partial strip → new message dict with reduced content; original
    untouched.
  - No-op (nothing removed) → original message reference passed
    through (no copy).
* Chronological order preserved (input order = output order).
* Provider-agnostic: ``tool_use`` (Anthropic), ``function_call``
  (OpenAI), ``toolCall`` (OpenClaw legacy), ``tool-use``,
  ``toolUse``, ``functionCall`` — all detected.
* Strict ``> item.ordinal`` for "later tool_result" — same ordinal
  is no-match.

### Reference

* Source: ``lossless-claw/src/assembler.ts`` 687-778, 642-685.
* Spec: ``epics/03-ingest-assembly/03-07-orphan-stripping.md``.
* Porting guide: ``docs/porting-guides/assembler-compaction.md``
  §"Step-by-step" step 11.
"""

from __future__ import annotations

from typing import Any

from lossless_hermes.assembler import (
    TOOL_CALL_TYPES,
    FilteredEntry,
    FilteredToolCallsResult,
    ResolvedItem,
    extract_tool_call_id_from_block,
    extract_tool_result_id_from_message,
    filter_non_fresh_assistant_tool_calls,
)


# ---------------------------------------------------------------------------
# Builders — shape the ResolvedItems the function under test consumes
# ---------------------------------------------------------------------------


def _assistant_with_tool_uses(
    ordinal: int,
    tool_uses: list[dict[str, Any]],
    text: str = "",
    block_type: str = "tool_use",
) -> ResolvedItem:
    """Build an assistant ResolvedItem with the given tool-use blocks.

    Each ``tool_uses`` dict should carry at least ``{"id": "...",
    "name": "...", "input": {...}}``. The ``block_type`` parameter
    lets a single helper produce provider-variant blocks (the type
    is keyed into each block; the dict's other fields stay the same).

    A leading text block is added when ``text`` is non-empty so we
    can exercise the "text survives" branch.
    """
    content: list[Any] = []
    if text:
        content.append({"type": "text", "text": text})
    for tu in tool_uses:
        block: dict[str, Any] = {"type": block_type, **tu}
        content.append(block)
    return ResolvedItem(
        ordinal=ordinal,
        message={"role": "assistant", "content": content},
        tokens=10,
        is_message=True,
        text=text or "assistant-turn",
        message_id=ordinal,
    )


def _tool_result(
    ordinal: int,
    tool_call_id: str,
    content: str = "result",
) -> ResolvedItem:
    """Build a tool_result ResolvedItem.

    Mirrors the runtime shape :meth:`ContextAssembler._resolve_message_item`
    emits for tool_result rows (line 1268-1275): role-tagged content +
    top-level ``toolCallId``. The body content is a single
    ``tool_result`` block keyed to the same id (Anthropic shape).
    """
    return ResolvedItem(
        ordinal=ordinal,
        message={
            "role": "tool",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_call_id,
                    "content": content,
                }
            ],
            "toolCallId": tool_call_id,
        },
        tokens=10,
        is_message=True,
        text=content,
        message_id=ordinal,
    )


def _user_message(ordinal: int, text: str = "hi") -> ResolvedItem:
    """Build a plain user ResolvedItem (no tool blocks)."""
    return ResolvedItem(
        ordinal=ordinal,
        message={"role": "user", "content": text},
        tokens=5,
        is_message=True,
        text=text,
        message_id=ordinal,
    )


# ===========================================================================
# extract_tool_call_id_from_block — port of TS extractToolCallId (642-650)
# ===========================================================================


class TestExtractToolCallIdFromBlock:
    """Cover the ``id``/``call_id`` precedence and non-empty guard."""

    def test_returns_id_when_present(self) -> None:
        """Anthropic shape: ``{"id": "toolu_123", ...}`` → ``"toolu_123"``."""
        assert extract_tool_call_id_from_block({"id": "toolu_123", "name": "search"}) == "toolu_123"

    def test_returns_call_id_when_id_absent(self) -> None:
        """OpenAI Responses shape: ``{"call_id": "call_abc", ...}`` → ``"call_abc"``."""
        assert (
            extract_tool_call_id_from_block({"call_id": "call_abc", "name": "search"}) == "call_abc"
        )

    def test_returns_id_when_both_present(self) -> None:
        """TS line 643-647: ``id`` is checked first and wins on collision."""
        assert extract_tool_call_id_from_block({"id": "toolu_1", "call_id": "call_x"}) == "toolu_1"

    def test_returns_none_when_neither_present(self) -> None:
        assert extract_tool_call_id_from_block({"type": "tool_use", "name": "x"}) is None

    def test_returns_none_when_id_is_empty_string(self) -> None:
        """TS line 643 has ``block.id.length > 0`` — empty string is treated as absent."""
        assert extract_tool_call_id_from_block({"id": ""}) is None

    def test_returns_none_when_id_is_not_string(self) -> None:
        """Non-string ``id`` (e.g. int from a malformed fixture) falls through to call_id."""
        # type-check is intentional — TS uses ``typeof === "string"``.
        assert extract_tool_call_id_from_block({"id": 42}) is None
        assert extract_tool_call_id_from_block({"id": None}) is None

    def test_falls_through_empty_id_to_call_id(self) -> None:
        """When ``id`` is empty but ``call_id`` is populated, the call_id wins."""
        assert extract_tool_call_id_from_block({"id": "", "call_id": "fallback"}) == "fallback"


# ===========================================================================
# extract_tool_result_id_from_message — port of TS function (674-685)
# ===========================================================================


class TestExtractToolResultIdFromMessage:
    """Cover ``toolCallId``/``toolUseId`` precedence + non-empty guard."""

    def test_returns_tool_call_id_when_present(self) -> None:
        msg = {"role": "tool", "toolCallId": "toolu_42", "content": []}
        assert extract_tool_result_id_from_message(msg) == "toolu_42"

    def test_returns_tool_use_id_when_tool_call_id_absent(self) -> None:
        """Legacy OpenClaw paths use ``toolUseId`` as a synonym."""
        msg = {"role": "tool", "toolUseId": "tu_legacy", "content": []}
        assert extract_tool_result_id_from_message(msg) == "tu_legacy"

    def test_returns_tool_call_id_when_both_present(self) -> None:
        msg = {"role": "tool", "toolCallId": "primary", "toolUseId": "legacy"}
        assert extract_tool_result_id_from_message(msg) == "primary"

    def test_returns_none_when_neither_present(self) -> None:
        assert extract_tool_result_id_from_message({"role": "user", "content": "hi"}) is None

    def test_returns_none_when_tool_call_id_is_empty(self) -> None:
        """TS line 678: ``length > 0`` guard rejects empty strings."""
        assert extract_tool_result_id_from_message({"toolCallId": ""}) is None

    def test_returns_none_when_tool_call_id_is_not_string(self) -> None:
        """Non-string ``toolCallId`` falls through to toolUseId."""
        assert extract_tool_result_id_from_message({"toolCallId": 42}) is None

    def test_falls_through_empty_tool_call_id_to_tool_use_id(self) -> None:
        msg = {"toolCallId": "", "toolUseId": "fallback"}
        assert extract_tool_result_id_from_message(msg) == "fallback"


# ===========================================================================
# TOOL_CALL_TYPES — port of TS const (80-87)
# ===========================================================================


class TestToolCallTypesConstant:
    """The provider-variant set must include every TS literal."""

    def test_includes_all_six_provider_variants(self) -> None:
        """TS line 80-87: six string literals — verify each is present."""
        expected = {
            "toolCall",
            "toolUse",
            "tool_use",
            "tool-use",
            "functionCall",
            "function_call",
        }
        assert TOOL_CALL_TYPES == expected

    def test_is_immutable_frozenset(self) -> None:
        """The constant is a ``frozenset`` so callers can't accidentally mutate it."""
        assert isinstance(TOOL_CALL_TYPES, frozenset)


# ===========================================================================
# filter_non_fresh_assistant_tool_calls — main port (TS 687-778)
# ===========================================================================


class TestFilterReturnShape:
    """The result is a :class:`FilteredToolCallsResult` with three fields."""

    def test_empty_input_returns_empty_result(self) -> None:
        """No items → empty entries, zero counters."""
        result = filter_non_fresh_assistant_tool_calls(
            items=[],
            fresh_tail_ordinals=set(),
            orphan_stripping_ordinal=0,
            all_tool_result_ordinals_by_id={},
        )
        assert isinstance(result, FilteredToolCallsResult)
        assert result.entries == []
        assert result.removed_tool_use_block_count == 0
        assert result.touched_assistant_message_count == 0

    def test_segment_label_freshTail_for_in_set(self) -> None:
        """Items whose ordinal is in fresh_tail_ordinals get ``"freshTail"``."""
        item = _user_message(ordinal=5)
        result = filter_non_fresh_assistant_tool_calls(
            items=[item],
            fresh_tail_ordinals={5},
            orphan_stripping_ordinal=0,
            all_tool_result_ordinals_by_id={},
        )
        assert len(result.entries) == 1
        assert result.entries[0].segment == "freshTail"

    def test_segment_label_evictable_for_out_of_set(self) -> None:
        """Items whose ordinal is not in fresh_tail_ordinals get ``"evictable"``."""
        item = _user_message(ordinal=5)
        result = filter_non_fresh_assistant_tool_calls(
            items=[item],
            fresh_tail_ordinals={10, 11},
            orphan_stripping_ordinal=0,
            all_tool_result_ordinals_by_id={},
        )
        assert len(result.entries) == 1
        assert result.entries[0].segment == "evictable"


class TestPassthroughCases:
    """Non-assistant, non-list-content, and no-tool-call items pass through."""

    def test_user_message_passes_through(self) -> None:
        """TS line 715-718: non-assistant role → push verbatim."""
        item = _user_message(ordinal=1, text="hello")
        result = filter_non_fresh_assistant_tool_calls(
            items=[item],
            fresh_tail_ordinals=set(),
            orphan_stripping_ordinal=0,
            all_tool_result_ordinals_by_id={},
        )
        assert len(result.entries) == 1
        assert result.entries[0].message is item.message
        assert result.removed_tool_use_block_count == 0
        assert result.touched_assistant_message_count == 0

    def test_tool_result_passes_through(self) -> None:
        """Tool-result messages (role != assistant) are unaffected."""
        item = _tool_result(ordinal=1, tool_call_id="t1")
        result = filter_non_fresh_assistant_tool_calls(
            items=[item],
            fresh_tail_ordinals=set(),
            orphan_stripping_ordinal=0,
            all_tool_result_ordinals_by_id={"t1": [1]},
        )
        assert len(result.entries) == 1
        assert result.entries[0].message is item.message

    def test_assistant_with_string_content_passes_through(self) -> None:
        """TS line 720-723: non-list content (plain string) → push verbatim.

        This is the OpenAI-Chat shape where assistant content is a
        single string. Orphan stripping operates on content arrays
        only.
        """
        item = ResolvedItem(
            ordinal=1,
            message={"role": "assistant", "content": "I'll help with that."},
            tokens=10,
            is_message=True,
            text="I'll help with that.",
        )
        result = filter_non_fresh_assistant_tool_calls(
            items=[item],
            fresh_tail_ordinals=set(),
            orphan_stripping_ordinal=10,
            all_tool_result_ordinals_by_id={},
        )
        assert len(result.entries) == 1
        assert result.entries[0].message is item.message
        assert result.removed_tool_use_block_count == 0

    def test_assistant_with_no_tool_use_blocks_passes_through(self) -> None:
        """Text-only assistant content → all blocks kept, no copy made."""
        item = _assistant_with_tool_uses(ordinal=1, tool_uses=[], text="just text, no tools")
        result = filter_non_fresh_assistant_tool_calls(
            items=[item],
            fresh_tail_ordinals=set(),
            orphan_stripping_ordinal=10,
            all_tool_result_ordinals_by_id={},
        )
        assert len(result.entries) == 1
        # ``removed_any`` was False, so we push the original reference.
        assert result.entries[0].message is item.message
        assert result.removed_tool_use_block_count == 0

    def test_block_without_id_is_kept(self) -> None:
        """TS line 735-737: no extractable id → keep the block."""
        item = _assistant_with_tool_uses(
            ordinal=1,
            tool_uses=[{"name": "search", "input": {}}],  # no id, no call_id
        )
        result = filter_non_fresh_assistant_tool_calls(
            items=[item],
            fresh_tail_ordinals=set(),
            orphan_stripping_ordinal=0,
            all_tool_result_ordinals_by_id={},
        )
        assert len(result.entries) == 1
        assert result.entries[0].message is item.message  # no-op
        assert result.removed_tool_use_block_count == 0


class TestMatchedToolUseKept:
    """When a selected tool_result follows the tool_use, the block is kept."""

    def test_matched_pair_in_selected_items_kept(self) -> None:
        """tool_use at ordinal 1, tool_result at ordinal 2, same id → KEEP."""
        items = [
            _assistant_with_tool_uses(
                ordinal=1, tool_uses=[{"id": "tu1", "name": "search", "input": {}}]
            ),
            _tool_result(ordinal=2, tool_call_id="tu1"),
        ]
        result = filter_non_fresh_assistant_tool_calls(
            items=items,
            fresh_tail_ordinals={1, 2},
            orphan_stripping_ordinal=0,
            all_tool_result_ordinals_by_id={"tu1": [2]},
        )
        assert len(result.entries) == 2
        # Original assistant message reference preserved (no copy).
        assert result.entries[0].message is items[0].message
        assert result.removed_tool_use_block_count == 0
        assert result.touched_assistant_message_count == 0

    def test_strict_greater_than_ordinal(self) -> None:
        """TS line 739: ``ord > item.ordinal`` — same ordinal is no-match.

        A tool_result with the SAME ordinal as the tool_use is treated
        as if it doesn't exist. (Such an input is malformed; tool
        results always come on a later turn.)
        """
        # Construct an impossible-but-defensible input: tool_use and
        # tool_result share ordinal 1. The strict inequality at TS
        # line 739 means no usable result is found.
        items = [
            _assistant_with_tool_uses(
                ordinal=1, tool_uses=[{"id": "tu_same", "name": "x", "input": {}}]
            ),
            # tool_result at SAME ordinal — should not count as a match.
            _tool_result(ordinal=1, tool_call_id="tu_same"),
        ]
        result = filter_non_fresh_assistant_tool_calls(
            items=items,
            fresh_tail_ordinals=set(),
            orphan_stripping_ordinal=10,  # well above ordinal 1
            all_tool_result_ordinals_by_id={},  # cache-marginal fallback empty
        )
        # ordinal 1 < orphan_stripping_ordinal 10, so the block is stripped.
        # The assistant message (which had only the orphan block) is dropped.
        # The tool_result passes through (different role).
        assert len(result.entries) == 1
        assert result.entries[0].message is items[1].message
        assert result.removed_tool_use_block_count == 1
        assert result.touched_assistant_message_count == 1


class TestSingleOrphanStripped:
    """Tool_use without a paired result is stripped under the right conditions."""

    def test_orphan_below_boundary_stripped_and_message_dropped(self) -> None:
        """Single orphan tool_use, no surviving text → message dropped."""
        items = [
            _assistant_with_tool_uses(
                ordinal=5, tool_uses=[{"id": "tu_orphan", "name": "x", "input": {}}]
            ),
        ]
        result = filter_non_fresh_assistant_tool_calls(
            items=items,
            fresh_tail_ordinals=set(),
            orphan_stripping_ordinal=10,
            all_tool_result_ordinals_by_id={},
        )
        assert result.entries == []
        assert result.removed_tool_use_block_count == 1
        assert result.touched_assistant_message_count == 1

    def test_orphan_below_boundary_text_survives(self) -> None:
        """Tool_use stripped but text block survives → assistant kept with text only."""
        items = [
            _assistant_with_tool_uses(
                ordinal=5,
                tool_uses=[{"id": "tu_orphan", "name": "x", "input": {}}],
                text="I will call the tool.",
            ),
        ]
        result = filter_non_fresh_assistant_tool_calls(
            items=items,
            fresh_tail_ordinals=set(),
            orphan_stripping_ordinal=10,
            all_tool_result_ordinals_by_id={},
        )
        assert len(result.entries) == 1
        new_msg = result.entries[0].message
        # Must be a NEW dict (not the original — content was filtered).
        assert new_msg is not items[0].message
        assert new_msg["role"] == "assistant"
        assert new_msg["content"] == [{"type": "text", "text": "I will call the tool."}]
        assert result.removed_tool_use_block_count == 1
        assert result.touched_assistant_message_count == 1

    def test_input_message_not_mutated_on_partial_strip(self) -> None:
        """The original ResolvedItem's message dict must be untouched."""
        items = [
            _assistant_with_tool_uses(
                ordinal=5,
                tool_uses=[{"id": "tu_orphan", "name": "x", "input": {}}],
                text="text first",
            ),
        ]
        original_content_id = id(items[0].message["content"])
        original_content_len = len(items[0].message["content"])
        result = filter_non_fresh_assistant_tool_calls(
            items=items,
            fresh_tail_ordinals=set(),
            orphan_stripping_ordinal=10,
            all_tool_result_ordinals_by_id={},
        )
        # Input dict unchanged.
        assert id(items[0].message["content"]) == original_content_id
        assert len(items[0].message["content"]) == original_content_len
        # Output dict is a new object with reduced content.
        new_msg = result.entries[0].message
        assert new_msg is not items[0].message
        assert len(new_msg["content"]) == 1


class TestCacheMarginalProtections:
    """The two guards at TS lines 743, 747 protect hot-cache stability."""

    def test_orphan_at_boundary_is_kept(self) -> None:
        """``item.ordinal >= orphan_stripping_ordinal`` → KEEP (line 743 strict <)."""
        items = [
            _assistant_with_tool_uses(
                ordinal=10,  # equal to boundary
                tool_uses=[{"id": "tu_orphan", "name": "x", "input": {}}],
            ),
        ]
        # No selected match, no resolved-anywhere match, but ordinal is AT
        # the boundary (not below). The strict ``<`` at TS 743 means the
        # boundary check fails → we fall through to the all_tool_result_*
        # check, which is also empty → strip.
        # Wait — re-reading: line 743 ``if (item.ordinal < orphanStrippingOrdinal)``
        # ALWAYS strips. If NOT below, fall to 747. 747's else-if-empty path:
        # ``if (!(allToolResult.get(id)?.length))`` returns true to KEEP.
        # So a block AT the boundary with NO anywhere-result is KEPT.
        result = filter_non_fresh_assistant_tool_calls(
            items=items,
            fresh_tail_ordinals=set(),
            orphan_stripping_ordinal=10,
            all_tool_result_ordinals_by_id={},
        )
        # At-boundary orphan with no resolved result → KEEP per the
        # "cache-marginal protection" branch (TS line 747-749).
        assert len(result.entries) == 1
        assert result.entries[0].message is items[0].message
        assert result.removed_tool_use_block_count == 0

    def test_orphan_above_boundary_is_kept(self) -> None:
        """``ordinal > boundary`` and no resolved result → KEEP (line 747)."""
        items = [
            _assistant_with_tool_uses(
                ordinal=20,
                tool_uses=[{"id": "tu_orphan", "name": "x", "input": {}}],
            ),
        ]
        result = filter_non_fresh_assistant_tool_calls(
            items=items,
            fresh_tail_ordinals=set(),
            orphan_stripping_ordinal=10,
            all_tool_result_ordinals_by_id={},
        )
        assert len(result.entries) == 1
        assert result.entries[0].message is items[0].message
        assert result.removed_tool_use_block_count == 0

    def test_orphan_below_boundary_with_resolved_anywhere_still_stripped(self) -> None:
        """Below-boundary orphan strips even if resolved-anywhere has the id.

        TS line 743's strict ``<`` short-circuits BEFORE the
        all_tool_result_ordinals_by_id check at line 747. The fallback
        guard only fires for items AT or ABOVE the boundary.
        """
        items = [
            _assistant_with_tool_uses(
                ordinal=5,  # below boundary 10
                tool_uses=[{"id": "tu_orphan", "name": "x", "input": {}}],
            ),
        ]
        result = filter_non_fresh_assistant_tool_calls(
            items=items,
            fresh_tail_ordinals=set(),
            orphan_stripping_ordinal=10,
            # resolved-anywhere has the id — but below-boundary strips first.
            all_tool_result_ordinals_by_id={"tu_orphan": [100]},
        )
        # Message dropped (only orphan block, no text).
        assert result.entries == []
        assert result.removed_tool_use_block_count == 1

    def test_orphan_above_boundary_with_resolved_anywhere_stripped(self) -> None:
        """``ordinal >= boundary`` AND id in resolved-anywhere → STRIP.

        TS line 747's leading ``!`` inverts the length check.
        ``allToolResultOrdinalsById.get(id)?.length`` returns 2 for
        ``[50, 60]``; ``!2 = false`` → fall through to the strip path
        at line 750. The semantic: when resolved-anywhere has evidence
        of the tool_result existing in the full history, emitting the
        tool_use here risks a stale pairing on the next assemble call
        (the result might surface at a different ordinal). Better to
        strip the tool_use and let the conversation reassemble cleanly.
        """
        items = [
            _assistant_with_tool_uses(
                ordinal=15,
                tool_uses=[{"id": "tu_orphan", "name": "x", "input": {}}],
            ),
        ]
        result = filter_non_fresh_assistant_tool_calls(
            items=items,
            fresh_tail_ordinals=set(),
            orphan_stripping_ordinal=10,
            all_tool_result_ordinals_by_id={"tu_orphan": [50, 60]},
        )
        # Block stripped; assistant message dropped (only block was orphan).
        assert result.entries == []
        assert result.removed_tool_use_block_count == 1
        assert result.touched_assistant_message_count == 1

    def test_orphan_above_boundary_with_empty_resolved_list_kept(self) -> None:
        """``ordinal >= boundary`` AND id maps to EMPTY list → KEEP.

        TS line 747: ``if (!(allToolResult.get(id)?.length)) return true``.
        An empty list has ``length === 0``; ``!0 === true``, so the
        condition fires and the block is KEPT. An explicit empty list
        is treated EXACTLY like an absent key — both KEEP the block.
        This is the "no evidence of a result anywhere" branch (vs the
        "evidence exists somewhere else" branch which strips).
        """
        items = [
            _assistant_with_tool_uses(
                ordinal=15,
                tool_uses=[{"id": "tu_orphan", "name": "x", "input": {}}],
            ),
        ]
        result = filter_non_fresh_assistant_tool_calls(
            items=items,
            fresh_tail_ordinals=set(),
            orphan_stripping_ordinal=10,
            all_tool_result_ordinals_by_id={"tu_orphan": []},
        )
        assert len(result.entries) == 1
        assert result.entries[0].message is items[0].message
        assert result.removed_tool_use_block_count == 0


class TestMultiBlockPartialStrip:
    """Within one assistant message, some blocks may strip and others survive."""

    def test_partial_strip_keeps_matched_and_text(self) -> None:
        """Two tool_uses: one matched + one orphan. Only the orphan is removed."""
        items = [
            _assistant_with_tool_uses(
                ordinal=1,
                tool_uses=[
                    {"id": "tu_matched", "name": "search", "input": {}},
                    {"id": "tu_orphan", "name": "fetch", "input": {}},
                ],
                text="I'll call two tools.",
            ),
            _tool_result(ordinal=2, tool_call_id="tu_matched"),
        ]
        result = filter_non_fresh_assistant_tool_calls(
            items=items,
            fresh_tail_ordinals=set(),
            orphan_stripping_ordinal=10,
            all_tool_result_ordinals_by_id={"tu_matched": [2]},
        )
        # Assistant message kept with text + matched tool_use; orphan stripped.
        assert len(result.entries) == 2
        new_assistant_msg = result.entries[0].message
        assert new_assistant_msg is not items[0].message  # was copied
        content = new_assistant_msg["content"]
        assert len(content) == 2
        # Text block first, then the matched tool_use.
        assert content[0] == {"type": "text", "text": "I'll call two tools."}
        assert content[1]["id"] == "tu_matched"
        # The tool_result passed through.
        assert result.entries[1].message is items[1].message
        assert result.removed_tool_use_block_count == 1
        assert result.touched_assistant_message_count == 1


class TestProviderVariants:
    """The filter works on every variant in TOOL_CALL_TYPES."""

    def test_provider_tool_use_anthropic(self) -> None:
        """``"tool_use"`` (Anthropic) — orphan stripped."""
        items = [
            _assistant_with_tool_uses(
                ordinal=5,
                tool_uses=[{"id": "tu1", "name": "x", "input": {}}],
                block_type="tool_use",
            ),
        ]
        result = filter_non_fresh_assistant_tool_calls(
            items=items,
            fresh_tail_ordinals=set(),
            orphan_stripping_ordinal=10,
            all_tool_result_ordinals_by_id={},
        )
        assert result.entries == []
        assert result.removed_tool_use_block_count == 1

    def test_provider_function_call_openai(self) -> None:
        """``"function_call"`` (OpenAI Responses) — orphan stripped."""
        items = [
            _assistant_with_tool_uses(
                ordinal=5,
                tool_uses=[{"call_id": "call_x", "name": "x", "arguments": "{}"}],
                block_type="function_call",
            ),
        ]
        result = filter_non_fresh_assistant_tool_calls(
            items=items,
            fresh_tail_ordinals=set(),
            orphan_stripping_ordinal=10,
            all_tool_result_ordinals_by_id={},
        )
        assert result.entries == []
        assert result.removed_tool_use_block_count == 1

    def test_provider_tool_call_legacy_openclaw(self) -> None:
        """``"toolCall"`` (OpenClaw legacy normalized) — orphan stripped."""
        items = [
            _assistant_with_tool_uses(
                ordinal=5,
                tool_uses=[{"id": "tc1", "name": "x", "input": {}}],
                block_type="toolCall",
            ),
        ]
        result = filter_non_fresh_assistant_tool_calls(
            items=items,
            fresh_tail_ordinals=set(),
            orphan_stripping_ordinal=10,
            all_tool_result_ordinals_by_id={},
        )
        assert result.entries == []
        assert result.removed_tool_use_block_count == 1

    def test_provider_tool_use_kebab(self) -> None:
        """``"tool-use"`` kebab variant — orphan stripped."""
        items = [
            _assistant_with_tool_uses(
                ordinal=5,
                tool_uses=[{"id": "tu1", "name": "x", "input": {}}],
                block_type="tool-use",
            ),
        ]
        result = filter_non_fresh_assistant_tool_calls(
            items=items,
            fresh_tail_ordinals=set(),
            orphan_stripping_ordinal=10,
            all_tool_result_ordinals_by_id={},
        )
        assert result.entries == []
        assert result.removed_tool_use_block_count == 1

    def test_provider_tool_use_camel(self) -> None:
        """``"toolUse"`` camelCase variant — orphan stripped."""
        items = [
            _assistant_with_tool_uses(
                ordinal=5,
                tool_uses=[{"id": "tu1", "name": "x", "input": {}}],
                block_type="toolUse",
            ),
        ]
        result = filter_non_fresh_assistant_tool_calls(
            items=items,
            fresh_tail_ordinals=set(),
            orphan_stripping_ordinal=10,
            all_tool_result_ordinals_by_id={},
        )
        assert result.entries == []
        assert result.removed_tool_use_block_count == 1

    def test_provider_function_call_camel(self) -> None:
        """``"functionCall"`` camelCase variant — orphan stripped."""
        items = [
            _assistant_with_tool_uses(
                ordinal=5,
                tool_uses=[{"id": "fc1", "name": "x", "input": {}}],
                block_type="functionCall",
            ),
        ]
        result = filter_non_fresh_assistant_tool_calls(
            items=items,
            fresh_tail_ordinals=set(),
            orphan_stripping_ordinal=10,
            all_tool_result_ordinals_by_id={},
        )
        assert result.entries == []
        assert result.removed_tool_use_block_count == 1

    def test_unknown_block_type_passes_through(self) -> None:
        """A block with ``type: "text"`` is never touched by the filter."""
        items = [
            _assistant_with_tool_uses(
                ordinal=1,
                tool_uses=[],
                text="Just text here, no tool calls.",
            ),
        ]
        result = filter_non_fresh_assistant_tool_calls(
            items=items,
            fresh_tail_ordinals=set(),
            orphan_stripping_ordinal=100,
            all_tool_result_ordinals_by_id={},
        )
        assert len(result.entries) == 1
        assert result.entries[0].message is items[0].message
        assert result.removed_tool_use_block_count == 0

    def test_thinking_block_passes_through(self) -> None:
        """A block with ``type: "thinking"`` is unaffected — not in TOOL_CALL_TYPES."""
        items = [
            ResolvedItem(
                ordinal=1,
                message={
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "..."},
                        {"type": "text", "text": "answer"},
                    ],
                },
                tokens=10,
                is_message=True,
                text="answer",
            ),
        ]
        result = filter_non_fresh_assistant_tool_calls(
            items=items,
            fresh_tail_ordinals=set(),
            orphan_stripping_ordinal=100,
            all_tool_result_ordinals_by_id={},
        )
        assert len(result.entries) == 1
        assert result.entries[0].message is items[0].message
        # Content unchanged.
        assert len(result.entries[0].message["content"]) == 2


class TestSelectedToolResultIndexing:
    """The internal selected_tool_result_ordinals_by_id index is built correctly."""

    def test_multiple_results_same_id_indexed(self) -> None:
        """Two tool_results sharing an id are both indexed.

        Pathological but possible (e.g. retry-with-same-id). The
        filter picks the first ordinal strictly greater than the
        tool_use, so multiple entries are OK.
        """
        items = [
            _assistant_with_tool_uses(
                ordinal=1,
                tool_uses=[{"id": "tu1", "name": "x", "input": {}}],
            ),
            _tool_result(ordinal=2, tool_call_id="tu1", content="result-1"),
            _tool_result(ordinal=3, tool_call_id="tu1", content="result-2"),
        ]
        result = filter_non_fresh_assistant_tool_calls(
            items=items,
            fresh_tail_ordinals=set(),
            orphan_stripping_ordinal=10,
            all_tool_result_ordinals_by_id={"tu1": [2, 3]},
        )
        # Assistant message + two tool_results all pass through.
        assert len(result.entries) == 3
        assert result.entries[0].message is items[0].message
        assert result.removed_tool_use_block_count == 0

    def test_result_before_tool_use_does_not_count(self) -> None:
        """A tool_result at an ordinal LESS than the tool_use's ordinal is not a match.

        TS line 739: ``ord > item.ordinal``. A result at ordinal 1 does
        NOT match a tool_use at ordinal 5.
        """
        items = [
            _tool_result(ordinal=1, tool_call_id="tu1"),
            _assistant_with_tool_uses(
                ordinal=5,
                tool_uses=[{"id": "tu1", "name": "x", "input": {}}],
            ),
        ]
        result = filter_non_fresh_assistant_tool_calls(
            items=items,
            fresh_tail_ordinals=set(),
            orphan_stripping_ordinal=10,
            all_tool_result_ordinals_by_id={},
        )
        # tool_result passes through (different role). Assistant orphan
        # ordinal 5 < boundary 10 → STRIP → message dropped.
        assert len(result.entries) == 1
        assert result.entries[0].message is items[0].message
        assert result.removed_tool_use_block_count == 1


class TestChronologicalOrderPreserved:
    """Output entries match input ordinal order."""

    def test_mixed_kept_and_stripped_preserves_order(self) -> None:
        items = [
            _user_message(ordinal=1, text="first"),
            _assistant_with_tool_uses(
                ordinal=2,
                tool_uses=[{"id": "tu_kept", "name": "x", "input": {}}],
            ),
            _tool_result(ordinal=3, tool_call_id="tu_kept"),
            _user_message(ordinal=4, text="next"),
            _assistant_with_tool_uses(
                ordinal=5,
                tool_uses=[{"id": "tu_orphan", "name": "x", "input": {}}],
            ),
            _user_message(ordinal=6, text="last"),
        ]
        result = filter_non_fresh_assistant_tool_calls(
            items=items,
            fresh_tail_ordinals={4, 5, 6},
            orphan_stripping_ordinal=10,
            all_tool_result_ordinals_by_id={"tu_kept": [3]},
        )
        # ordinal 5 (orphan) is dropped; others survive in order.
        msgs = [entry.message for entry in result.entries]
        assert len(msgs) == 5
        assert msgs[0]["content"] == "first"
        assert msgs[1]["role"] == "assistant"
        assert msgs[2]["role"] == "tool"  # tool_result
        assert msgs[3]["content"] == "next"
        assert msgs[4]["content"] == "last"
        # The dropped message was at ordinal 5; segment of the kept
        # entries reflects the fresh-tail set correctly.
        segments = [entry.segment for entry in result.entries]
        assert segments == ["evictable", "evictable", "evictable", "freshTail", "freshTail"]


class TestEmptyContentEdgeCases:
    """Edge cases around empty content arrays and dropped messages."""

    def test_empty_content_array_increments_counters(self) -> None:
        """TS line 754-757: ``content.length === 0`` increments counters unconditionally.

        Even though no blocks were actually removed (the array was empty
        to begin with), the message is dropped and both counters
        increment. This is a TS quirk preserved verbatim.
        """
        items = [
            ResolvedItem(
                ordinal=1,
                message={"role": "assistant", "content": []},
                tokens=0,
                is_message=True,
                text="",
            ),
        ]
        result = filter_non_fresh_assistant_tool_calls(
            items=items,
            fresh_tail_ordinals=set(),
            orphan_stripping_ordinal=100,
            all_tool_result_ordinals_by_id={},
        )
        assert result.entries == []
        # Quirky-but-verbatim: empty content drops the message + bumps counters.
        assert result.removed_tool_use_block_count == 1
        assert result.touched_assistant_message_count == 1

    def test_all_orphan_blocks_no_text_drops_message(self) -> None:
        """Multiple orphan blocks, no text → message dropped."""
        items = [
            _assistant_with_tool_uses(
                ordinal=5,
                tool_uses=[
                    {"id": "tu_a", "name": "x", "input": {}},
                    {"id": "tu_b", "name": "y", "input": {}},
                ],
            ),
        ]
        result = filter_non_fresh_assistant_tool_calls(
            items=items,
            fresh_tail_ordinals=set(),
            orphan_stripping_ordinal=10,
            all_tool_result_ordinals_by_id={},
        )
        assert result.entries == []
        # Both blocks stripped but counter is per-message, not per-block.
        assert result.removed_tool_use_block_count == 1
        assert result.touched_assistant_message_count == 1

    def test_single_message_no_blocks_no_change(self) -> None:
        items = [_user_message(ordinal=1, text="only message")]
        result = filter_non_fresh_assistant_tool_calls(
            items=items,
            fresh_tail_ordinals={1},
            orphan_stripping_ordinal=0,
            all_tool_result_ordinals_by_id={},
        )
        assert len(result.entries) == 1
        assert result.entries[0].segment == "freshTail"
        assert result.removed_tool_use_block_count == 0

    def test_non_dict_block_passes_through(self) -> None:
        """TS line 727-729: ``!block || typeof block !== "object"`` → KEEP.

        A string or None block is preserved verbatim. (Pathological
        input, but the TS guard exists; we match it.)
        """
        items = [
            ResolvedItem(
                ordinal=1,
                message={
                    "role": "assistant",
                    "content": [
                        "stray string",  # non-dict, kept
                        {"type": "text", "text": "real block"},
                    ],
                },
                tokens=5,
                is_message=True,
                text="real block",
            ),
        ]
        result = filter_non_fresh_assistant_tool_calls(
            items=items,
            fresh_tail_ordinals=set(),
            orphan_stripping_ordinal=100,
            all_tool_result_ordinals_by_id={},
        )
        assert len(result.entries) == 1
        # No tool_use blocks at all → nothing removed → original ref.
        assert result.entries[0].message is items[0].message

    def test_block_with_non_string_type_passes_through(self) -> None:
        """A block with ``type: 42`` (non-string) is not a tool-call → KEEP."""
        items = [
            ResolvedItem(
                ordinal=1,
                message={
                    "role": "assistant",
                    "content": [
                        {"type": 42, "id": "tu1"},  # weird, but pass-through
                        {"type": "text", "text": "hello"},
                    ],
                },
                tokens=5,
                is_message=True,
                text="hello",
            ),
        ]
        result = filter_non_fresh_assistant_tool_calls(
            items=items,
            fresh_tail_ordinals=set(),
            orphan_stripping_ordinal=100,
            all_tool_result_ordinals_by_id={},
        )
        assert len(result.entries) == 1
        assert result.entries[0].message is items[0].message


class TestRemovedAnyBranchSemantics:
    """The ``removed_any`` flag controls copy-vs-pass-through behavior."""

    def test_no_op_passes_original_reference(self) -> None:
        """When nothing is stripped, the original message dict is returned.

        This is a memory + identity-stability optimization that mirrors
        the TS source's ``filteredEntries.push({ message: item.message, ... })``
        pattern at line 716, 721, 760 (all push the original reference).
        """
        items = [
            _assistant_with_tool_uses(
                ordinal=1,
                tool_uses=[{"id": "tu_kept", "name": "x", "input": {}}],
                text="text",
            ),
            _tool_result(ordinal=2, tool_call_id="tu_kept"),
        ]
        result = filter_non_fresh_assistant_tool_calls(
            items=items,
            fresh_tail_ordinals=set(),
            orphan_stripping_ordinal=10,
            all_tool_result_ordinals_by_id={"tu_kept": [2]},
        )
        # Identity preserved on no-op.
        assert result.entries[0].message is items[0].message
        assert result.entries[1].message is items[1].message
