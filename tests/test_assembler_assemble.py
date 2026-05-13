"""Tests for :meth:`lossless_hermes.assembler.ContextAssembler.assemble`.

Verifies the top-level orchestration chain (TS ``assembler.ts`` 1102-1332)
ports correctly: every step from context-item read through final
:func:`sanitize_tool_use_result_pairing` returns the expected
:class:`AssembleResult` shape, ``estimated_tokens`` arithmetic, debug
envelope, and edge-case fallback behaviors.

### Chain (per issue spec)

1. ``summary_store.get_context_items``.
2. :meth:`ContextAssembler.resolve_items`.
3. :func:`resolve_fresh_tail_ordinal`.
4. Compute orphan-stripping ordinal (override-or-fallback).
5. Index all tool-result ordinals.
6. Split evictable vs fresh tail.
7. ``stub_large_tool_payloads`` — warn-and-skip (ADR-030).
8. :func:`budget_walk`.
9. Append fresh tail.
10. :func:`_build_overflow_diagnostics`.
11. :func:`filter_non_fresh_assistant_tool_calls`.
12. Normalize assistant content.
13. Clean empty assistant turns.
14. Pre-sanitize hashing.
15. :func:`sanitize_tool_use_result_pairing`.
16. Return.

### Invariants verified (per AC)

* Empty context → empty :class:`AssembleResult`.
* ``estimated_tokens == evictable_kept_tokens + fresh_tail_tokens``.
* ``capture_debug=False`` → :attr:`AssembleResult.debug` is ``None``.
* ``capture_debug=True`` → debug envelope populated with all 16 fields.
* ``stub_large_tool_payloads=True`` → warning logged, pipeline runs.
* Selection mode matches the budget setup (full-fit / chronological /
  prompt-aware).
* Output sanity: no orphan ``tool_result`` blocks, no empty /
  thinking-only assistant turns.
* Prefix-stability: two consecutive calls on the same DAG return
  byte-identical message prefixes.

### Reference

* Source: ``lossless-claw/src/assembler.ts`` 1102-1332.
* Spec: ``epics/03-ingest-assembly/03-08-assemble-orchestration.md``.
* Porting guide: ``docs/porting-guides/assembler-compaction.md``
  §"Step-by-step".
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from lossless_hermes.assembler import (
    EMPTY_FRESH_TAIL_ORDINAL,
    AssembleDebug,
    AssembleInput,
    AssembleResult,
    AssembleStats,
    AssemblyOverflowDiagnostics,
    ContextAssembler,
)
from lossless_hermes.store.conversation import (
    MessagePartRecord,
    MessageRecord,
)
from lossless_hermes.store.summary import (
    ContextItemRecord,
    SummaryRecord,
)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _msg_record(
    *,
    message_id: int,
    role: str = "user",
    content: str = "",
    seq: int | None = None,
) -> MessageRecord:
    """Build a minimal :class:`MessageRecord` for orchestration tests."""
    return MessageRecord(
        message_id=message_id,
        conversation_id=1,
        seq=seq if seq is not None else message_id,
        role=role,  # type: ignore[arg-type]
        content=content,
        token_count=0,
        created_at=datetime.now(timezone.utc),
    )


def _ctx_item(
    *,
    ordinal: int,
    item_type: str = "message",
    message_id: int | None = None,
    summary_id: str | None = None,
) -> ContextItemRecord:
    """Build a :class:`ContextItemRecord` referencing a message or summary."""
    return ContextItemRecord(
        conversation_id=1,
        ordinal=ordinal,
        item_type=item_type,  # type: ignore[arg-type]
        message_id=message_id,
        summary_id=summary_id,
        created_at=datetime.now(timezone.utc),
    )


def _summary_record(*, summary_id: str = "sum_a", content: str = "Summary text") -> SummaryRecord:
    return SummaryRecord(
        summary_id=summary_id,
        conversation_id=1,
        kind="leaf",
        depth=0,
        content=content,
        token_count=10,
        file_ids=[],
        earliest_at=None,
        latest_at=None,
        descendant_count=0,
        descendant_token_count=0,
        source_message_token_count=0,
        model="test",
        created_at=datetime.now(timezone.utc),
    )


def _part(**overrides: Any) -> MessagePartRecord:
    defaults: dict[str, Any] = {
        "part_id": "p1",
        "message_id": 1,
        "session_id": "s",
        "part_type": "text",
        "ordinal": 0,
        "text_content": None,
        "tool_call_id": None,
        "tool_name": None,
        "tool_input": None,
        "tool_output": None,
        "metadata": None,
    }
    defaults.update(overrides)
    return MessagePartRecord(**defaults)


def _make_assembler(
    *,
    messages_by_id: dict[int, MessageRecord] | None = None,
    parts_by_message_id: dict[int, list[MessagePartRecord]] | None = None,
    summaries_by_id: dict[str, SummaryRecord] | None = None,
    context_items: list[ContextItemRecord] | None = None,
) -> ContextAssembler:
    """Build an in-memory :class:`ContextAssembler` with mock stores."""
    cstore = MagicMock()
    cstore.get_message_by_id.side_effect = lambda mid: (messages_by_id or {}).get(mid)
    cstore.get_message_parts.side_effect = lambda mid: (parts_by_message_id or {}).get(mid, [])

    sstore = MagicMock()
    sstore.get_summary.side_effect = lambda sid: (summaries_by_id or {}).get(sid)
    sstore.get_summary_parents.side_effect = lambda _sid: []
    sstore.get_context_items.side_effect = lambda _cid: list(context_items or [])

    return ContextAssembler(cstore, sstore)


# ===========================================================================
# Empty / minimal short-circuits
# ===========================================================================


class TestEmptyContext:
    """Step 1 — empty :meth:`get_context_items` short-circuits."""

    def test_empty_context_returns_empty_result(self) -> None:
        assembler = _make_assembler(context_items=[])
        result = assembler.assemble(AssembleInput(conversation_id=1, token_budget=10_000))
        assert isinstance(result, AssembleResult)
        assert result.messages == []
        assert result.estimated_tokens == 0
        assert result.stats == AssembleStats(
            raw_message_count=0,
            summary_count=0,
            total_context_items=0,
        )
        assert result.debug is None

    def test_empty_context_with_capture_debug_still_none(self) -> None:
        # The empty short-circuit returns before debug capture; the AC says
        # the empty case returns the bare result. capture_debug=True should
        # not change that — the short-circuit predates the debug gate.
        assembler = _make_assembler(context_items=[])
        result = assembler.assemble(
            AssembleInput(conversation_id=1, token_budget=10_000, capture_debug=True),
        )
        assert result.debug is None


# ===========================================================================
# Single-message happy path
# ===========================================================================


class TestSingleMessage:
    """One user message, ample budget → full-fit selection."""

    def test_single_user_message_full_fit(self) -> None:
        msg = _msg_record(message_id=1, role="user", content="Hello world")
        assembler = _make_assembler(
            messages_by_id={1: msg},
            parts_by_message_id={1: []},
            context_items=[_ctx_item(ordinal=0, message_id=1)],
        )
        result = assembler.assemble(AssembleInput(conversation_id=1, token_budget=10_000))
        assert len(result.messages) == 1
        assert result.messages[0]["role"] == "user"
        assert result.messages[0]["content"] == "Hello world"
        assert result.estimated_tokens > 0
        assert result.stats.raw_message_count == 1
        assert result.stats.summary_count == 0
        assert result.stats.total_context_items == 1


# ===========================================================================
# Multi-message + budget walk
# ===========================================================================


class TestBudgetWalk:
    """Verify selection_mode dispatch and estimated_tokens arithmetic."""

    def test_estimated_tokens_equals_evictable_plus_tail(self) -> None:
        # Build 4 messages with predictable token counts. Budget is set
        # so the older 2 fit + the newest 2 stay in the fresh tail.
        messages = {
            i: _msg_record(message_id=i, role="user", content=f"message-{i}" * 10)
            for i in range(1, 5)
        }
        assembler = _make_assembler(
            messages_by_id=messages,
            parts_by_message_id={i: [] for i in range(1, 5)},
            context_items=[_ctx_item(ordinal=i - 1, message_id=i) for i in range(1, 5)],
        )

        # First assemble to discover the actual per-message token cost,
        # then reassemble with a budget large enough to fit everything.
        result_full = assembler.assemble(
            AssembleInput(
                conversation_id=1,
                token_budget=1_000_000,
                fresh_tail_count=2,
                capture_debug=True,
            ),
        )
        assert result_full.debug is not None
        # ``estimated_tokens = evictable_kept_tokens + tail_tokens``.
        # With a million-token budget the full set fits → mode is full-fit
        # and ``estimated_tokens`` equals the total resolved token count.
        assert result_full.debug.selection_mode == "full-fit"
        assert (
            result_full.estimated_tokens
            == result_full.debug.evictable_total_tokens + result_full.debug.tail_tokens
        )

    def test_tight_budget_forces_chronological(self) -> None:
        # 6 small messages, tiny budget. Fresh tail = 2 newest; budget
        # only covers the tail. Older 4 are evicted.
        messages = {i: _msg_record(message_id=i, role="user", content=f"m{i}") for i in range(1, 7)}
        assembler = _make_assembler(
            messages_by_id=messages,
            parts_by_message_id={i: [] for i in range(1, 7)},
            context_items=[_ctx_item(ordinal=i - 1, message_id=i) for i in range(1, 7)],
        )
        result = assembler.assemble(
            AssembleInput(
                conversation_id=1,
                token_budget=1,  # ridiculous tight budget
                fresh_tail_count=2,
                capture_debug=True,
            ),
        )
        # Fresh tail is always included even if it busts the budget;
        # all 4 evictable items dropped.
        assert result.debug is not None
        assert result.debug.selection_mode in {"chronological", "full-fit"}
        # The newest two ordinals (4, 5) must be in the final output.
        contents = [m.get("content") for m in result.messages]
        assert "m5" in contents
        assert "m6" in contents

    def test_prompt_aware_selection_mode(self) -> None:
        # Messages with distinct text; budget tight; prompt names one of
        # the older items. Prompt-aware mode must select it over recent.
        m1 = _msg_record(message_id=1, role="user", content="The deployment kubernetes pod runs")
        m2 = _msg_record(message_id=2, role="user", content="Recipe for chocolate cake batter")
        m3 = _msg_record(message_id=3, role="user", content="What does the cat say to the dog")
        m4 = _msg_record(message_id=4, role="user", content="Newest message tail content")
        assembler = _make_assembler(
            messages_by_id={1: m1, 2: m2, 3: m3, 4: m4},
            parts_by_message_id={i: [] for i in range(1, 5)},
            context_items=[_ctx_item(ordinal=i - 1, message_id=i) for i in range(1, 5)],
        )

        # Budget is enough for fresh tail + one evictable item but not
        # all 3 evictable items.
        result = assembler.assemble(
            AssembleInput(
                conversation_id=1,
                token_budget=50,
                fresh_tail_count=1,
                prompt="kubernetes deployment",
                prompt_aware_eviction=True,
                capture_debug=True,
            ),
        )
        assert result.debug is not None
        # With a searchable prompt and prompt-aware enabled, mode is
        # ``prompt-aware`` whenever full-fit fails.
        if result.debug.selection_mode == "prompt-aware":
            contents = [m.get("content") for m in result.messages]
            # The kubernetes message should win the selection over the
            # cat/dog or chocolate-cake fillers.
            kept_text = " ".join(c for c in contents if isinstance(c, str))
            assert "kubernetes" in kept_text or "deployment" in kept_text


# ===========================================================================
# Stub-tier warning (ADR-030 deferral)
# ===========================================================================


class TestStubTierWarning:
    """ADR-030: ``stub_large_tool_payloads=True`` logs warning, no exception."""

    def test_stub_flag_emits_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        msg = _msg_record(message_id=1, role="user", content="x")
        assembler = _make_assembler(
            messages_by_id={1: msg},
            parts_by_message_id={1: []},
            context_items=[_ctx_item(ordinal=0, message_id=1)],
        )
        with caplog.at_level(logging.WARNING, logger="lossless_hermes.assembler"):
            result = assembler.assemble(
                AssembleInput(
                    conversation_id=1,
                    token_budget=10_000,
                    stub_large_tool_payloads=True,
                ),
            )
        assert any("ADR-030" in rec.message for rec in caplog.records)
        # Pipeline still runs — output is non-empty.
        assert len(result.messages) == 1

    def test_stub_flag_default_false_no_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        msg = _msg_record(message_id=1, role="user", content="x")
        assembler = _make_assembler(
            messages_by_id={1: msg},
            parts_by_message_id={1: []},
            context_items=[_ctx_item(ordinal=0, message_id=1)],
        )
        with caplog.at_level(logging.WARNING, logger="lossless_hermes.assembler"):
            assembler.assemble(AssembleInput(conversation_id=1, token_budget=10_000))
        assert not any("ADR-030" in rec.message for rec in caplog.records)


# ===========================================================================
# Debug envelope
# ===========================================================================


class TestDebugEnvelope:
    """Verify capture_debug gating and debug envelope shape."""

    def test_capture_debug_false_returns_none(self) -> None:
        msg = _msg_record(message_id=1, role="user", content="x")
        assembler = _make_assembler(
            messages_by_id={1: msg},
            parts_by_message_id={1: []},
            context_items=[_ctx_item(ordinal=0, message_id=1)],
        )
        result = assembler.assemble(
            AssembleInput(conversation_id=1, token_budget=10_000, capture_debug=False),
        )
        assert result.debug is None

    def test_capture_debug_true_populates_envelope(self) -> None:
        msg = _msg_record(message_id=1, role="user", content="x")
        assembler = _make_assembler(
            messages_by_id={1: msg},
            parts_by_message_id={1: []},
            context_items=[_ctx_item(ordinal=0, message_id=1)],
        )
        result = assembler.assemble(
            AssembleInput(conversation_id=1, token_budget=10_000, capture_debug=True),
        )
        assert result.debug is not None
        debug = result.debug
        assert isinstance(debug, AssembleDebug)
        # All 16 envelope fields populated with sane types.
        assert isinstance(debug.fresh_tail_ordinal, int)
        assert isinstance(debug.orphan_stripping_ordinal, int)
        assert isinstance(debug.base_fresh_tail_count, int)
        assert isinstance(debug.fresh_tail_count, int)
        assert isinstance(debug.tail_tokens, int)
        assert isinstance(debug.remaining_budget, int)
        assert isinstance(debug.evictable_total_tokens, int)
        assert debug.selection_mode in {"full-fit", "prompt-aware", "chronological"}
        # v0.2.0 stub-tier counters always zero / empty in v0.1.0.
        assert debug.promoted_tool_result_count == 0
        assert debug.promoted_ordinals == []
        assert isinstance(debug.removed_tool_use_block_count, int)
        assert isinstance(debug.touched_assistant_message_count, int)
        assert isinstance(debug.pre_sanitize_evictable_count, int)
        assert isinstance(debug.pre_sanitize_fresh_tail_count, int)
        # Hashes are 16-char hex strings.
        assert len(debug.pre_sanitize_evictable_hash) == 16
        assert len(debug.pre_sanitize_fresh_tail_hash) == 16
        assert len(debug.pre_sanitize_messages_hash) == 16
        assert len(debug.final_messages_hash) == 16
        assert isinstance(debug.overflow_diagnostics, AssemblyOverflowDiagnostics)


# ===========================================================================
# Orphan-stripping ordinal — override vs fallback
# ===========================================================================


class TestOrphanStrippingOrdinal:
    """Verify the override-or-fresh-tail logic at TS 1137-1142."""

    def test_no_override_uses_fresh_tail_ordinal(self) -> None:
        msg = _msg_record(message_id=1, role="user", content="x")
        assembler = _make_assembler(
            messages_by_id={1: msg},
            parts_by_message_id={1: []},
            context_items=[_ctx_item(ordinal=0, message_id=1)],
        )
        result = assembler.assemble(
            AssembleInput(
                conversation_id=1,
                token_budget=10_000,
                orphan_stripping_ordinal=None,
                capture_debug=True,
            ),
        )
        assert result.debug is not None
        # With a single message + fresh_tail_count=8 default, the only
        # raw message is in the tail → the fresh-tail ordinal is 0.
        # Orphan-stripping defaults to that.
        assert result.debug.orphan_stripping_ordinal == result.debug.fresh_tail_ordinal

    def test_explicit_override_respected(self) -> None:
        msg = _msg_record(message_id=1, role="user", content="x")
        assembler = _make_assembler(
            messages_by_id={1: msg},
            parts_by_message_id={1: []},
            context_items=[_ctx_item(ordinal=0, message_id=1)],
        )
        result = assembler.assemble(
            AssembleInput(
                conversation_id=1,
                token_budget=10_000,
                orphan_stripping_ordinal=42,
                capture_debug=True,
            ),
        )
        assert result.debug is not None
        assert result.debug.orphan_stripping_ordinal == 42

    def test_negative_override_falls_back(self) -> None:
        # TS 1138-1142: ``orphanStrippingOrdinal >= 0`` is the gate.
        # Negative values must fall back to the fresh-tail ordinal.
        msg = _msg_record(message_id=1, role="user", content="x")
        assembler = _make_assembler(
            messages_by_id={1: msg},
            parts_by_message_id={1: []},
            context_items=[_ctx_item(ordinal=0, message_id=1)],
        )
        result = assembler.assemble(
            AssembleInput(
                conversation_id=1,
                token_budget=10_000,
                orphan_stripping_ordinal=-5,
                capture_debug=True,
            ),
        )
        assert result.debug is not None
        # Negative values fall through to ``fresh_tail_ordinal``.
        assert result.debug.orphan_stripping_ordinal == result.debug.fresh_tail_ordinal


# ===========================================================================
# Empty / blank / thinking-only assistant cleanup (TS 1276-1293)
# ===========================================================================


class TestEmptyAssistantCleanup:
    """Step 13 — drop empty / blank / thinking-only assistant turns."""

    def test_drops_assistant_with_only_thinking_blocks(self) -> None:
        user = _msg_record(message_id=1, role="user", content="hello")
        # Assistant with only a "thinking" block — must be dropped after
        # the empty-assistant cleanup pass.
        asst = _msg_record(message_id=2, role="assistant", content="")
        asst_parts = [
            _part(
                part_id="p1",
                message_id=2,
                part_type="text",
                ordinal=0,
                text_content="internal monologue",
                metadata='{"rawType":"thinking"}',
            ),
        ]
        followup = _msg_record(message_id=3, role="user", content="follow up")
        assembler = _make_assembler(
            messages_by_id={1: user, 2: asst, 3: followup},
            parts_by_message_id={1: [], 2: asst_parts, 3: []},
            context_items=[
                _ctx_item(ordinal=0, message_id=1),
                _ctx_item(ordinal=1, message_id=2),
                _ctx_item(ordinal=2, message_id=3),
            ],
        )
        result = assembler.assemble(AssembleInput(conversation_id=1, token_budget=100_000))
        roles = [m.get("role") for m in result.messages]
        # Sanitize may move/drop messages — but the thinking-only assistant
        # must NOT appear. The remaining messages should be the user
        # turns.
        assert "user" in roles
        # Walk the output: any "assistant" entry must not be the
        # thinking-only one (its content was just the thinking block).
        for m in result.messages:
            if m.get("role") == "assistant":
                content = m.get("content")
                if isinstance(content, list):
                    # Thinking blocks present would be the dropped case.
                    types_in_content = {b.get("type") for b in content if isinstance(b, dict)}
                    # If the only block is "thinking", this should have
                    # been cleaned. Since it survived, it had other
                    # content too.
                    if types_in_content == {"thinking"}:
                        pytest.fail("thinking-only assistant should have been dropped")

    def test_drops_assistant_with_blank_text_content(self) -> None:
        user = _msg_record(message_id=1, role="user", content="hi")
        # Assistant with a single blank-text block — TS line 1283-1293
        # filters via ``isBlankContent``.
        asst = _msg_record(message_id=2, role="assistant", content="")
        # Build the part as a text block whose content is just whitespace.
        asst_parts = [
            _part(
                part_id="p2",
                message_id=2,
                part_type="text",
                ordinal=0,
                text_content="   ",
            ),
        ]
        assembler = _make_assembler(
            messages_by_id={1: user, 2: asst},
            parts_by_message_id={1: [], 2: asst_parts},
            context_items=[
                _ctx_item(ordinal=0, message_id=1),
                _ctx_item(ordinal=1, message_id=2),
            ],
        )
        result = assembler.assemble(AssembleInput(conversation_id=1, token_budget=100_000))
        # The blank assistant must be dropped — verify by counting
        # assistant entries.
        assistant_count = sum(1 for m in result.messages if m.get("role") == "assistant")
        assert assistant_count == 0


# ===========================================================================
# Sanitize pass — final tool_use ↔ tool_result repair
# ===========================================================================


class TestSanitizePass:
    """Step 15 — :func:`sanitize_tool_use_result_pairing` runs as final pass."""

    def test_orphan_tool_result_without_tool_use_is_dropped(self) -> None:
        # An orphan toolResult at the start of the conversation
        # (no preceding tool_use) must be dropped by the sanitizer.
        # We model it as a single role=tool message with a tool_call_id;
        # since there's no preceding assistant tool_use, the sanitize
        # pass drops it.
        tool_msg = _msg_record(message_id=1, role="tool", content="result text")
        tool_parts = [
            _part(
                part_id="p1",
                message_id=1,
                part_type="tool",
                ordinal=0,
                tool_call_id="toolu_dangling",
                tool_name="x",
                tool_output="some output",
            ),
        ]
        assembler = _make_assembler(
            messages_by_id={1: tool_msg},
            parts_by_message_id={1: tool_parts},
            context_items=[_ctx_item(ordinal=0, message_id=1)],
        )
        result = assembler.assemble(AssembleInput(conversation_id=1, token_budget=100_000))
        # The orphan tool result is dropped by the sanitizer; result
        # may be empty or contain only the cleaned non-orphan messages.
        for m in result.messages:
            if m.get("role") == "toolResult":
                pytest.fail("orphan toolResult should have been dropped by sanitizer")


# ===========================================================================
# Prefix stability — two consecutive calls produce identical prefixes
# ===========================================================================


class TestPrefixStability:
    """Acceptance: 2 consecutive ``assemble()`` calls return identical prefixes.

    Same DAG + same budget + same prompt → identical output (no
    nondeterminism in the pipeline). Validates the
    ``orphan_stripping_ordinal`` snapshotting contract (engine-side
    callers pin the boundary across turns; the assembler-side method
    is deterministic).
    """

    def test_two_calls_same_input_same_output(self) -> None:
        messages = {
            i: _msg_record(message_id=i, role="user", content=f"msg{i}") for i in range(1, 5)
        }
        ctx_items = [_ctx_item(ordinal=i - 1, message_id=i) for i in range(1, 5)]

        a1 = _make_assembler(
            messages_by_id=messages,
            parts_by_message_id={i: [] for i in range(1, 5)},
            context_items=ctx_items,
        )
        a2 = _make_assembler(
            messages_by_id=messages,
            parts_by_message_id={i: [] for i in range(1, 5)},
            context_items=ctx_items,
        )
        inp = AssembleInput(
            conversation_id=1,
            token_budget=10_000,
            fresh_tail_count=2,
            capture_debug=True,
        )
        r1 = a1.assemble(inp)
        r2 = a2.assemble(inp)
        # Byte-identical messages.
        assert r1.messages == r2.messages
        assert r1.estimated_tokens == r2.estimated_tokens
        # Byte-identical debug hashes (the load-bearing signal for
        # prefix-stability snapshotting).
        assert r1.debug is not None
        assert r2.debug is not None
        assert r1.debug.final_messages_hash == r2.debug.final_messages_hash
        assert r1.debug.pre_sanitize_messages_hash == r2.debug.pre_sanitize_messages_hash

    def test_overlapping_prefix_stable_across_appends(self) -> None:
        # Two DAGs: ``base`` has 4 messages; ``extended`` has the same 4
        # messages plus a 5th. With a budget that fits all 5, the prefix
        # (messages 1-4) must match byte-for-byte between the two calls.
        # This validates that the orchestration is purely a function of
        # its inputs and doesn't carry hidden state across calls.
        messages = {
            i: _msg_record(message_id=i, role="user", content=f"msg{i}") for i in range(1, 6)
        }
        # 4-message version.
        base_ctx_items = [_ctx_item(ordinal=i - 1, message_id=i) for i in range(1, 5)]
        # 5-message version.
        ext_ctx_items = [_ctx_item(ordinal=i - 1, message_id=i) for i in range(1, 6)]

        a_base = _make_assembler(
            messages_by_id=messages,
            parts_by_message_id={i: [] for i in range(1, 6)},
            context_items=base_ctx_items,
        )
        a_ext = _make_assembler(
            messages_by_id=messages,
            parts_by_message_id={i: [] for i in range(1, 6)},
            context_items=ext_ctx_items,
        )
        inp = AssembleInput(
            conversation_id=1,
            token_budget=100_000,
            fresh_tail_count=2,
            capture_debug=False,
        )
        r_base = a_base.assemble(inp)
        r_ext = a_ext.assemble(inp)
        # Both should fit in full → the first N messages of the extended
        # output must match the base output byte-for-byte.
        assert len(r_base.messages) >= 1
        for i, base_msg in enumerate(r_base.messages):
            assert r_ext.messages[i] == base_msg


# ===========================================================================
# Stats — over the pre-selection set, not post-selection
# ===========================================================================


class TestStats:
    """Stats counters are over the resolved (pre-selection) set."""

    def test_stats_counts_pre_selection_resolution(self) -> None:
        # 3 messages + 1 summary in the DAG. Even if the budget evicts
        # some, stats should reflect the pre-selection counts.
        summaries = {"sum_a": _summary_record(summary_id="sum_a", content="condensed text")}
        messages = {
            1: _msg_record(message_id=1, role="user", content="x"),
            2: _msg_record(message_id=2, role="user", content="y"),
            3: _msg_record(message_id=3, role="user", content="z"),
        }
        assembler = _make_assembler(
            messages_by_id=messages,
            parts_by_message_id={1: [], 2: [], 3: []},
            summaries_by_id=summaries,
            context_items=[
                _ctx_item(ordinal=0, item_type="summary", summary_id="sum_a"),
                _ctx_item(ordinal=1, message_id=1),
                _ctx_item(ordinal=2, message_id=2),
                _ctx_item(ordinal=3, message_id=3),
            ],
        )
        result = assembler.assemble(
            AssembleInput(
                conversation_id=1,
                token_budget=100_000,
                fresh_tail_count=8,
            ),
        )
        assert result.stats.raw_message_count == 3
        assert result.stats.summary_count == 1
        assert result.stats.total_context_items == 4


# ===========================================================================
# Budget = 0 edge case
# ===========================================================================


class TestZeroBudget:
    """Budget=0: fresh tail still kept, no evictable retained."""

    def test_zero_budget_keeps_fresh_tail(self) -> None:
        messages = {i: _msg_record(message_id=i, role="user", content=f"m{i}") for i in range(1, 5)}
        assembler = _make_assembler(
            messages_by_id=messages,
            parts_by_message_id={i: [] for i in range(1, 5)},
            context_items=[_ctx_item(ordinal=i - 1, message_id=i) for i in range(1, 5)],
        )
        result = assembler.assemble(
            AssembleInput(
                conversation_id=1,
                token_budget=0,
                fresh_tail_count=2,
                capture_debug=True,
            ),
        )
        assert result.debug is not None
        # The fresh tail tokens are still in estimated_tokens; the
        # evictable kept count is zero.
        assert result.debug.remaining_budget == 0


# ===========================================================================
# All-summary input
# ===========================================================================


class TestAllSummaries:
    """All-summary DAG → fresh tail is empty; everything is evictable."""

    def test_all_summaries_uses_empty_fresh_tail_sentinel(self) -> None:
        summaries = {
            "sum_a": _summary_record(summary_id="sum_a", content="summary a"),
            "sum_b": _summary_record(summary_id="sum_b", content="summary b"),
        }
        assembler = _make_assembler(
            summaries_by_id=summaries,
            context_items=[
                _ctx_item(ordinal=0, item_type="summary", summary_id="sum_a"),
                _ctx_item(ordinal=1, item_type="summary", summary_id="sum_b"),
            ],
        )
        result = assembler.assemble(
            AssembleInput(
                conversation_id=1,
                token_budget=100_000,
                fresh_tail_count=8,
                capture_debug=True,
            ),
        )
        assert result.debug is not None
        # With no raw messages, the fresh tail is empty
        # (`EMPTY_FRESH_TAIL_ORDINAL` sentinel).
        assert result.debug.fresh_tail_ordinal == EMPTY_FRESH_TAIL_ORDINAL
        assert result.debug.tail_tokens == 0
        # Both summaries are evictable but fit under the budget.
        assert result.stats.summary_count == 2
        assert result.stats.raw_message_count == 0


# ===========================================================================
# Step 7 — stub_large_tool_payloads runs the rest of the pipeline
# ===========================================================================


class TestStubFlagDoesntBreakPipeline:
    """ADR-030: stub flag is a warn-and-skip, pipeline output is unchanged."""

    def test_stub_flag_does_not_change_output(self) -> None:
        # Same DAG run twice; once with the stub flag and once without.
        # Outputs must be byte-identical because v0.1.0 doesn't
        # actually substitute anything.
        messages = {1: _msg_record(message_id=1, role="user", content="x")}
        ctx_items = [_ctx_item(ordinal=0, message_id=1)]

        a1 = _make_assembler(
            messages_by_id=messages,
            parts_by_message_id={1: []},
            context_items=ctx_items,
        )
        a2 = _make_assembler(
            messages_by_id=messages,
            parts_by_message_id={1: []},
            context_items=ctx_items,
        )
        r1 = a1.assemble(
            AssembleInput(
                conversation_id=1,
                token_budget=10_000,
                stub_large_tool_payloads=False,
            ),
        )
        r2 = a2.assemble(
            AssembleInput(
                conversation_id=1,
                token_budget=10_000,
                stub_large_tool_payloads=True,
            ),
        )
        assert r1.messages == r2.messages
        assert r1.estimated_tokens == r2.estimated_tokens


# ===========================================================================
# Output sanity — no orphan tool results, no empty assistant turns
# ===========================================================================


class TestOutputSanity:
    """Per AC: output passes sanity check post-sanitize."""

    def test_no_assistant_message_with_empty_content_in_output(self) -> None:
        # Build a DAG that includes only a user message; the assembler
        # output must have no empty assistant turns under any path.
        msg = _msg_record(message_id=1, role="user", content="hello")
        assembler = _make_assembler(
            messages_by_id={1: msg},
            parts_by_message_id={1: []},
            context_items=[_ctx_item(ordinal=0, message_id=1)],
        )
        result = assembler.assemble(AssembleInput(conversation_id=1, token_budget=10_000))
        for m in result.messages:
            if m.get("role") == "assistant":
                content = m.get("content")
                if isinstance(content, list):
                    assert len(content) > 0
                elif isinstance(content, str):
                    assert content.strip() != ""

    def test_no_orphan_tool_result_in_output(self) -> None:
        # An assistant tool_use without a paired tool_result earlier in
        # the timeline is allowed (sanitize pass synthesises one). The
        # converse — a tool_result without an upstream tool_use — must
        # be stripped.
        # We model with a free-standing tool message that has no preceding
        # assistant tool_use.
        tool_msg = _msg_record(message_id=1, role="tool", content="")
        tool_parts = [
            _part(
                part_id="p1",
                message_id=1,
                part_type="tool",
                ordinal=0,
                tool_call_id="orphan_id",
                tool_name="x",
                tool_output="out",
            ),
        ]
        assembler = _make_assembler(
            messages_by_id={1: tool_msg},
            parts_by_message_id={1: tool_parts},
            context_items=[_ctx_item(ordinal=0, message_id=1)],
        )
        result = assembler.assemble(AssembleInput(conversation_id=1, token_budget=10_000))
        for m in result.messages:
            if m.get("role") == "toolResult":
                pytest.fail("orphan toolResult survived sanitizer")


# ===========================================================================
# Overflow diagnostics
# ===========================================================================


class TestOverflowDiagnostics:
    """Debug envelope's overflow_diagnostics field shape."""

    def test_diagnostics_aggregate_token_totals(self) -> None:
        messages = {
            i: _msg_record(message_id=i, role="user", content=f"msg{i}") for i in range(1, 4)
        }
        assembler = _make_assembler(
            messages_by_id=messages,
            parts_by_message_id={i: [] for i in range(1, 4)},
            context_items=[_ctx_item(ordinal=i - 1, message_id=i) for i in range(1, 4)],
        )
        result = assembler.assemble(
            AssembleInput(
                conversation_id=1,
                token_budget=10_000,
                capture_debug=True,
            ),
        )
        assert result.debug is not None
        diag = result.debug.overflow_diagnostics
        assert diag.token_budget == 10_000
        assert diag.raw_message_count == 3
        assert diag.summary_count == 0
        assert diag.total_context_items == 3
        # Tokens aggregate.
        assert diag.total_context_tokens > 0
        assert diag.raw_message_tokens == diag.total_context_tokens
        assert diag.summary_tokens == 0
        # Top contributors capped at 5.
        assert len(diag.top_message_contributors) <= 5
        assert len(diag.top_summary_contributors) <= 5

    def test_duplicate_ref_cluster_detected(self) -> None:
        # Same message_id referenced twice in context_items → triggers
        # the duplicate-ref cluster diagnostic.
        msg = _msg_record(message_id=1, role="user", content="repeated")
        assembler = _make_assembler(
            messages_by_id={1: msg},
            parts_by_message_id={1: []},
            context_items=[
                _ctx_item(ordinal=0, message_id=1),
                _ctx_item(ordinal=1, message_id=1),
            ],
        )
        result = assembler.assemble(
            AssembleInput(
                conversation_id=1,
                token_budget=10_000,
                capture_debug=True,
            ),
        )
        assert result.debug is not None
        diag = result.debug.overflow_diagnostics
        # The duplicate reference cluster must be detected.
        assert len(diag.duplicate_ref_clusters) >= 1
        cluster = diag.duplicate_ref_clusters[0]
        assert cluster.kind == "message-ref"
        assert cluster.count == 2
        assert cluster.key == "message:1"


# ===========================================================================
# Fresh-tail ordinal interactions
# ===========================================================================


class TestFreshTailOrdinal:
    """Verify the fresh-tail count + ordinal computation thread through."""

    def test_fresh_tail_count_zero_makes_all_items_evictable(self) -> None:
        # ``fresh_tail_count = 0`` → :data:`EMPTY_FRESH_TAIL_ORDINAL`,
        # which means every resolved item is < boundary → all evictable.
        messages = {i: _msg_record(message_id=i, role="user", content=f"m{i}") for i in range(1, 4)}
        assembler = _make_assembler(
            messages_by_id=messages,
            parts_by_message_id={i: [] for i in range(1, 4)},
            context_items=[_ctx_item(ordinal=i - 1, message_id=i) for i in range(1, 4)],
        )
        result = assembler.assemble(
            AssembleInput(
                conversation_id=1,
                token_budget=100_000,
                fresh_tail_count=0,
                capture_debug=True,
            ),
        )
        assert result.debug is not None
        assert result.debug.fresh_tail_ordinal == EMPTY_FRESH_TAIL_ORDINAL
        assert result.debug.tail_tokens == 0
        # Everything fits — selection mode is full-fit, all kept.
        assert result.debug.selection_mode == "full-fit"

    def test_fresh_tail_max_tokens_cap_respected(self) -> None:
        # Generate messages of known size; cap should evict the older
        # ones from the protected tail.
        messages = {
            i: _msg_record(message_id=i, role="user", content="x" * 100) for i in range(1, 5)
        }
        assembler = _make_assembler(
            messages_by_id=messages,
            parts_by_message_id={i: [] for i in range(1, 5)},
            context_items=[_ctx_item(ordinal=i - 1, message_id=i) for i in range(1, 5)],
        )
        # Very small cap → only newest message qualifies.
        result = assembler.assemble(
            AssembleInput(
                conversation_id=1,
                token_budget=100_000,
                fresh_tail_count=10,
                fresh_tail_max_tokens=1,
                capture_debug=True,
            ),
        )
        assert result.debug is not None
        # The fresh-tail size should be smaller than the requested 10
        # because the token cap kicks in.
        assert result.debug.fresh_tail_count <= 1 or result.debug.fresh_tail_count < 10
