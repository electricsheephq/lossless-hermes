"""Tests for :mod:`lossless_hermes.assembler` block reconstruction.

Ports ``lossless-claw/test/assembler-blocks.test.ts`` (~650 LOC at LCM
commit ``1f07fbd``) to pytest, plus additional fixtures covering:

* The line-1399 invariant — tool-result-without-toolCallId is degraded
  to assistant.
* Provider-specific keying for tool blocks (Anthropic ``input`` vs
  OpenAI ``arguments``).
* OpenAI reasoning restoration round-trip.
* Tolerant ``metadata`` JSON parse — malformed JSON returns ``None``,
  never raises.
* Summary XML wrapper with parents (condensed kind).
* Empty-content message edge case.
* ``is_message`` flag set correctly.
* ``tokens`` field computed via :func:`estimate_tokens`.
* ``ResolvedItem`` v0.2.0 stub-tier fields stay ``None`` in v0.1.0
  (ADR-030).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

from lossless_hermes.assembler import (
    ContextAssembler,
    ResolvedItem,
    block_from_part,
    content_from_parts,
    format_summary_content,
    get_original_role,
    get_part_metadata,
    parse_json,
    pick_tool_call_id,
    pick_tool_is_error,
    pick_tool_name,
    to_runtime_role,
    tool_call_block_from_part,
    tool_result_block_from_part,
    try_restore_openai_reasoning,
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
# Helpers
# ---------------------------------------------------------------------------


def make_part(**overrides: Any) -> MessagePartRecord:
    """Build a minimal :class:`MessagePartRecord` for testing.

    Defaults mirror TS ``makePart`` in ``assembler-blocks.test.ts``.
    """
    defaults: dict[str, Any] = {
        "part_id": "test-part-1",
        "message_id": 1,
        "session_id": "test-session",
        "part_type": "tool",
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


def make_message(**overrides: Any) -> MessageRecord:
    """Build a minimal :class:`MessageRecord` for testing."""
    defaults: dict[str, Any] = {
        "message_id": 1,
        "conversation_id": 1,
        "seq": 1,
        "role": "user",
        "content": "",
        "token_count": 0,
        "created_at": datetime.now(timezone.utc),
    }
    defaults.update(overrides)
    return MessageRecord(**defaults)


def make_summary(**overrides: Any) -> SummaryRecord:
    """Build a minimal :class:`SummaryRecord` for testing."""
    defaults: dict[str, Any] = {
        "summary_id": "sum_abc123",
        "conversation_id": 1,
        "kind": "leaf",
        "depth": 0,
        "content": "Summary text",
        "token_count": 10,
        "file_ids": [],
        "earliest_at": None,
        "latest_at": None,
        "descendant_count": 0,
        "descendant_token_count": 0,
        "source_message_token_count": 0,
        "model": "test-model",
        "created_at": datetime.now(timezone.utc),
    }
    defaults.update(overrides)
    return SummaryRecord(**defaults)


def make_context_item(**overrides: Any) -> ContextItemRecord:
    """Build a minimal :class:`ContextItemRecord` for testing."""
    defaults: dict[str, Any] = {
        "conversation_id": 1,
        "ordinal": 0,
        "item_type": "message",
        "message_id": 1,
        "summary_id": None,
        "created_at": datetime.now(timezone.utc),
    }
    defaults.update(overrides)
    return ContextItemRecord(**defaults)


# ═══════════════════════════════════════════════════════════════════════════
# parse_json
# ═══════════════════════════════════════════════════════════════════════════


class TestParseJson:
    """Cover the tolerant JSON parser (TS assembler.ts 188-197)."""

    def test_returns_none_for_none(self) -> None:
        assert parse_json(None) is None

    def test_returns_none_for_empty_string(self) -> None:
        assert parse_json("") is None

    def test_returns_none_for_whitespace_only(self) -> None:
        assert parse_json("   ") is None

    def test_returns_none_for_malformed_json(self) -> None:
        # CRITICAL: must not raise — this is a tolerance invariant for
        # callers that pass arbitrary `metadata` strings.
        assert parse_json("{not valid}") is None
        assert parse_json("trailing comma,") is None

    def test_decodes_valid_object(self) -> None:
        assert parse_json('{"a": 1}') == {"a": 1}

    def test_decodes_valid_array(self) -> None:
        assert parse_json("[1, 2, 3]") == [1, 2, 3]

    def test_decodes_valid_primitive(self) -> None:
        assert parse_json('"hello"') == "hello"
        assert parse_json("42") == 42
        assert parse_json("true") is True


# ═══════════════════════════════════════════════════════════════════════════
# get_original_role
# ═══════════════════════════════════════════════════════════════════════════


class TestGetOriginalRole:
    """Cover originalRole metadata scan (TS assembler.ts 199-211)."""

    def test_returns_none_when_no_parts(self) -> None:
        assert get_original_role([]) is None

    def test_returns_none_when_no_metadata(self) -> None:
        parts = [make_part(metadata=None)]
        assert get_original_role(parts) is None

    def test_returns_first_non_empty_role(self) -> None:
        parts = [
            make_part(metadata=json.dumps({"originalRole": "assistant"})),
            make_part(metadata=json.dumps({"originalRole": "user"})),
        ]
        assert get_original_role(parts) == "assistant"

    def test_skips_empty_role_string(self) -> None:
        # Empty string must NOT be returned — must continue to next part.
        parts = [
            make_part(metadata=json.dumps({"originalRole": ""})),
            make_part(metadata=json.dumps({"originalRole": "user"})),
        ]
        assert get_original_role(parts) == "user"

    def test_tolerates_malformed_metadata(self) -> None:
        # Tolerance invariant: malformed JSON must not raise.
        parts = [
            make_part(metadata="not json"),
            make_part(metadata=json.dumps({"originalRole": "assistant"})),
        ]
        assert get_original_role(parts) == "assistant"


# ═══════════════════════════════════════════════════════════════════════════
# get_part_metadata
# ═══════════════════════════════════════════════════════════════════════════


class TestGetPartMetadata:
    """Cover metadata envelope decoding (TS assembler.ts 213-239)."""

    def test_returns_empty_dict_for_no_metadata(self) -> None:
        assert get_part_metadata(make_part(metadata=None)) == {}

    def test_returns_empty_dict_for_malformed_metadata(self) -> None:
        # Tolerance: malformed JSON yields empty dict, no raise.
        assert get_part_metadata(make_part(metadata="{bad}")) == {}

    def test_extracts_all_three_fields(self) -> None:
        part = make_part(
            metadata=json.dumps(
                {
                    "originalRole": "assistant",
                    "rawType": "toolCall",
                    "raw": {"key": "value"},
                },
            ),
        )
        result = get_part_metadata(part)
        assert result["originalRole"] == "assistant"
        assert result["rawType"] == "toolCall"
        assert result["raw"] == {"key": "value"}

    def test_omits_empty_strings(self) -> None:
        # Empty-string roles/types must be omitted, not preserved.
        part = make_part(
            metadata=json.dumps(
                {"originalRole": "", "rawType": "", "raw": "preserved"},
            ),
        )
        result = get_part_metadata(part)
        assert "originalRole" not in result
        assert "rawType" not in result
        assert result["raw"] == "preserved"


# ═══════════════════════════════════════════════════════════════════════════
# try_restore_openai_reasoning
# ═══════════════════════════════════════════════════════════════════════════


class TestTryRestoreOpenAIReasoning:
    """Cover OpenAI rs_* reasoning restoration (TS assembler.ts 265-278)."""

    def test_restores_well_formed_signature(self) -> None:
        raw = {
            "type": "thinking",
            "thinking": "",
            "thinkingSignature": json.dumps(
                {
                    "type": "reasoning",
                    "id": "rs_abc123",
                    "encrypted_content": "...",
                },
            ),
        }
        result = try_restore_openai_reasoning(raw)
        assert result is not None
        assert result["type"] == "reasoning"
        assert result["id"] == "rs_abc123"

    def test_returns_none_for_non_thinking_type(self) -> None:
        raw = {"type": "text", "thinkingSignature": '{"type":"reasoning","id":"rs_x"}'}
        assert try_restore_openai_reasoning(raw) is None

    def test_returns_none_for_missing_signature(self) -> None:
        raw = {"type": "thinking", "thinking": ""}
        assert try_restore_openai_reasoning(raw) is None

    def test_returns_none_for_non_json_signature(self) -> None:
        raw = {"type": "thinking", "thinkingSignature": "plain string"}
        assert try_restore_openai_reasoning(raw) is None

    def test_returns_none_for_malformed_signature_json(self) -> None:
        # CRITICAL: malformed JSON inside thinkingSignature must NOT raise.
        raw = {"type": "thinking", "thinkingSignature": "{broken"}
        assert try_restore_openai_reasoning(raw) is None

    def test_returns_none_when_parsed_lacks_id(self) -> None:
        raw = {
            "type": "thinking",
            "thinkingSignature": json.dumps({"type": "reasoning"}),
        }
        assert try_restore_openai_reasoning(raw) is None


# ═══════════════════════════════════════════════════════════════════════════
# tool_call_block_from_part — ported from TS describe("toolCallBlockFromPart")
# ═══════════════════════════════════════════════════════════════════════════


class TestToolCallBlockFromPart:
    """Cover :func:`tool_call_block_from_part` (TS assembler.ts 281-328)."""

    def test_emits_arguments_for_toolCall_default(self) -> None:
        part = make_part(
            tool_call_id="call-123",
            tool_name="read",
            tool_input='{"path":"SOUL.md"}',
        )
        block = tool_call_block_from_part(part)
        assert block["type"] == "toolCall"
        assert block["id"] == "call-123"
        assert block["name"] == "read"
        assert block["arguments"] == {"path": "SOUL.md"}
        assert "input" not in block

    def test_emits_arguments_for_explicit_toolCall_rawType(self) -> None:
        part = make_part(
            tool_call_id="call-456",
            tool_name="exec",
            tool_input='{"command":"ls"}',
        )
        block = tool_call_block_from_part(part, "toolCall")
        assert block["type"] == "toolCall"
        assert block["arguments"] == {"command": "ls"}
        assert "input" not in block

    def test_emits_arguments_for_functionCall_rawType(self) -> None:
        part = make_part(
            tool_call_id="call-789",
            tool_name="bash",
            tool_input='{"cmd":"pwd"}',
        )
        block = tool_call_block_from_part(part, "functionCall")
        assert block["type"] == "functionCall"
        assert block["arguments"] == {"cmd": "pwd"}
        assert "input" not in block

    def test_emits_call_id_for_function_call_rawType(self) -> None:
        part = make_part(
            tool_call_id="fc_1",
            tool_name="read",
            tool_input='{"path":"test.md"}',
        )
        block = tool_call_block_from_part(part, "function_call")
        assert block["type"] == "function_call"
        assert block["call_id"] == "fc_1"
        assert block["name"] == "read"
        assert block["arguments"] == {"path": "test.md"}
        assert "id" not in block
        assert "input" not in block

    def test_emits_input_for_tool_use_rawType_anthropic(self) -> None:
        # CRITICAL: Anthropic tool_use uses "input", not "arguments".
        part = make_part(
            tool_call_id="toolu_abc",
            tool_name="read",
            tool_input='{"path":"USER.md"}',
        )
        block = tool_call_block_from_part(part, "tool_use")
        assert block["type"] == "tool_use"
        assert block["id"] == "toolu_abc"
        assert block["name"] == "read"
        assert block["input"] == {"path": "USER.md"}
        assert "arguments" not in block

    def test_emits_input_for_toolUse_rawType(self) -> None:
        part = make_part(
            tool_call_id="toolu_def",
            tool_name="write",
            tool_input='{"path":"out.txt","content":"hello"}',
        )
        block = tool_call_block_from_part(part, "toolUse")
        assert block["type"] == "toolUse"
        assert block["input"] == {"path": "out.txt", "content": "hello"}
        assert "arguments" not in block

    def test_emits_input_for_tool_use_hyphenated_rawType(self) -> None:
        part = make_part(
            tool_call_id="id-1",
            tool_name="search",
            tool_input='{"query":"test"}',
        )
        block = tool_call_block_from_part(part, "tool-use")
        assert block["type"] == "tool-use"
        assert block["input"] == {"query": "test"}
        assert "arguments" not in block

    def test_handles_non_json_tool_input_string(self) -> None:
        part = make_part(
            tool_call_id="call-str",
            tool_name="bash",
            tool_input="echo hello",
        )
        block = tool_call_block_from_part(part)
        assert block["arguments"] == "echo hello"

    def test_omits_arguments_when_tool_input_is_none(self) -> None:
        part = make_part(
            tool_call_id="call-nil",
            tool_name="read",
            tool_input=None,
        )
        block = tool_call_block_from_part(part)
        assert "arguments" not in block
        assert "input" not in block

    def test_generates_synthetic_id_for_empty_tool_call_id(self) -> None:
        part = make_part(
            tool_call_id="",
            tool_name="read",
            tool_input='{"path":"a.txt"}',
        )
        block = tool_call_block_from_part(part)
        assert block["id"] == "toolu_lcm_test-part-1"
        assert block["name"] == "read"

    def test_generates_synthetic_id_for_none_tool_call_id(self) -> None:
        part = make_part(
            tool_call_id=None,
            tool_name="read",
            tool_input='{"path":"a.txt"}',
        )
        block = tool_call_block_from_part(part)
        assert block["id"] == "toolu_lcm_test-part-1"


# ═══════════════════════════════════════════════════════════════════════════
# tool_result_block_from_part — ported from TS describe("toolResultBlockFromPart")
# ═══════════════════════════════════════════════════════════════════════════


class TestToolResultBlockFromPart:
    """Cover :func:`tool_result_block_from_part` (TS assembler.ts 331-390)."""

    def test_defaults_to_tool_result_with_tool_use_id(self) -> None:
        part = make_part(
            tool_call_id="toolu_abc",
            tool_name="read",
            tool_output='"file contents here"',
        )
        block = tool_result_block_from_part(part)
        assert block["type"] == "tool_result"
        assert block["tool_use_id"] == "toolu_abc"
        assert block["output"] == "file contents here"
        assert block["name"] == "read"

    def test_uses_function_call_output_type_with_call_id(self) -> None:
        part = make_part(
            tool_call_id="fc_1",
            tool_name="bash",
            tool_output='"ok"',
        )
        block = tool_result_block_from_part(part, "function_call_output")
        assert block["type"] == "function_call_output"
        assert block["call_id"] == "fc_1"
        assert block["output"] == "ok"
        assert "tool_use_id" not in block

    def test_falls_back_to_text_content_when_tool_output_none(self) -> None:
        part = make_part(
            tool_call_id="call-1",
            text_content="fallback text",
            tool_output=None,
        )
        block = tool_result_block_from_part(part)
        assert block["output"] == "fallback text"

    def test_falls_back_to_empty_string_when_both_none(self) -> None:
        part = make_part(
            tool_call_id="call-1",
            text_content=None,
            tool_output=None,
        )
        block = tool_result_block_from_part(part)
        assert block["output"] == ""

    def test_preserves_raw_content_array_when_columns_empty(self) -> None:
        # Multi-block tool_result content MUST stay an array — Anthropic
        # rejects plain-string tool_result content (P1 invariant from
        # the ADR-030 risk #5 + porting guide §"Critical invariants").
        part = make_part(
            tool_call_id="toolu_content",
            tool_name="read",
            text_content=None,
            tool_output=None,
        )
        block = tool_result_block_from_part(
            part,
            "tool_result",
            {
                "type": "tool_result",
                "tool_use_id": "toolu_content",
                "content": [{"type": "text", "text": "command output"}],
            },
        )
        assert block["type"] == "tool_result"
        assert block["tool_use_id"] == "toolu_content"
        assert block["content"] == [{"type": "text", "text": "command output"}]
        assert "output" not in block

    def test_restores_externalized_plain_text_as_text_block(self) -> None:
        # Fast-path: stub references surface as text blocks (TS 336-348).
        part = make_part(
            part_type="tool",
            tool_call_id="toolu_externalized",
            tool_name="exec",
            text_content="[LCM Tool Output: file_deadbeef12345678 tool=exec]",
            tool_output=None,
        )
        block = tool_result_block_from_part(
            part,
            "tool_result",
            {
                "type": "tool_result",
                "text": "[LCM Tool Output: file_deadbeef12345678 tool=exec]",
                "externalizedFileId": "file_deadbeef12345678",
                "toolOutputExternalized": True,
            },
        )
        assert block == {
            "type": "text",
            "text": "[LCM Tool Output: file_deadbeef12345678 tool=exec]",
        }

    def test_preserves_is_error_boolean(self) -> None:
        part = make_part(tool_call_id="t1", tool_output='"err"')
        block = tool_result_block_from_part(
            part,
            "tool_result",
            {"type": "tool_result", "is_error": True},
        )
        assert block["is_error"] is True

    def test_preserves_isError_camelCase_when_no_snake_case(self) -> None:
        part = make_part(tool_call_id="t1", tool_output='"err"')
        block = tool_result_block_from_part(
            part,
            "tool_result",
            {"type": "tool_result", "isError": True},
        )
        assert block["isError"] is True


# ═══════════════════════════════════════════════════════════════════════════
# block_from_part — integration through main dispatch
# ═══════════════════════════════════════════════════════════════════════════


class TestBlockFromPart:
    """Cover :func:`block_from_part` (TS assembler.ts 421-535)."""

    def test_routes_toolCall_through_call_block(self) -> None:
        part = make_part(
            part_type="tool",
            tool_call_id="call-1",
            tool_name="read",
            tool_input='{"path":"SOUL.md"}',
            metadata=json.dumps(
                {
                    "rawType": "toolCall",
                    "originalRole": "assistant",
                    "raw": {
                        "type": "toolCall",
                        "id": "call-1",
                        "name": "read",
                        "arguments": {"path": "SOUL.md"},
                    },
                },
            ),
        )
        block = block_from_part(part)
        # Must use "arguments" not "input" — this is the bug we're fixing
        assert block["type"] == "toolCall"
        assert block["arguments"] == {"path": "SOUL.md"}
        assert "input" not in block

    def test_routes_tool_use_through_call_block_with_input(self) -> None:
        # Anthropic invariant: tool_use → input keying.
        part = make_part(
            part_type="tool",
            tool_call_id="toolu_1",
            tool_name="read",
            tool_input='{"path":"USER.md"}',
            metadata=json.dumps(
                {
                    "rawType": "tool_use",
                    "originalRole": "assistant",
                    "raw": {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "read",
                        "input": {"path": "USER.md"},
                    },
                },
            ),
        )
        block = block_from_part(part)
        assert block["type"] == "tool_use"
        assert block["input"] == {"path": "USER.md"}
        assert "arguments" not in block

    def test_does_not_return_raw_for_tool_call_types(self) -> None:
        # Tests the early-return guard for tool blocks (TS 429-447).
        # Returning raw would bypass argument normalization.
        raw_obj = {
            "type": "toolCall",
            "id": "call-raw",
            "name": "read",
            "arguments": {"path": "test.md"},  # object, not string
        }
        part = make_part(
            part_type="tool",
            tool_call_id="call-raw",
            tool_name="read",
            tool_input='{"path":"test.md"}',
            metadata=json.dumps(
                {
                    "rawType": "toolCall",
                    "originalRole": "assistant",
                    "raw": raw_obj,
                },
            ),
        )
        block = block_from_part(part)
        # Should go through tool_call_block_from_part, not return raw
        assert block["type"] == "toolCall"
        assert block["id"] == "call-raw"
        assert block["name"] == "read"
        # arguments should come from tool_input column, not raw
        assert block["arguments"] == {"path": "test.md"}

    def test_returns_raw_block_verbatim_for_non_tool_types(self) -> None:
        raw_obj = {"type": "custom_block", "data": "something"}
        part = make_part(
            part_type="text",
            metadata=json.dumps({"raw": raw_obj}),
        )
        block = block_from_part(part)
        assert block == raw_obj

    def test_restores_openai_reasoning_blocks(self) -> None:
        # Round-trip: OpenClaw-normalized → original OpenAI form.
        part = make_part(
            part_type="reasoning",
            metadata=json.dumps(
                {
                    "raw": {
                        "type": "thinking",
                        "thinking": "",
                        "thinkingSignature": json.dumps(
                            {
                                "type": "reasoning",
                                "id": "rs_abc123",
                                "encrypted_content": "...",
                            },
                        ),
                    },
                },
            ),
        )
        block = block_from_part(part)
        assert block["type"] == "reasoning"
        assert block["id"] == "rs_abc123"

    def test_routes_tool_result_parts_correctly(self) -> None:
        part = make_part(
            part_type="tool",
            tool_call_id="call-1",
            tool_name="read",
            tool_output='"file contents"',
            metadata=json.dumps(
                {
                    "rawType": "function_call_output",
                    "originalRole": "toolResult",
                },
            ),
        )
        block = block_from_part(part)
        assert block["type"] == "function_call_output"
        assert block["call_id"] == "call-1"
        assert block["output"] == "file contents"

    def test_preserves_raw_tool_result_content_when_columns_empty(self) -> None:
        # Multi-block content array stays an array (Anthropic-compat).
        part = make_part(
            part_type="tool",
            tool_call_id="call-content",
            tool_name="read",
            tool_output=None,
            text_content=None,
            metadata=json.dumps(
                {
                    "rawType": "tool_result",
                    "originalRole": "toolResult",
                    "raw": {
                        "type": "tool_result",
                        "tool_use_id": "call-content",
                        "content": [{"type": "text", "text": "command output"}],
                        "metadata": {"raw": "ignored"},
                    },
                },
            ),
        )
        block = block_from_part(part)
        assert block["type"] == "tool_result"
        assert block["tool_use_id"] == "call-content"
        assert block["content"] == [{"type": "text", "text": "command output"}]
        assert "metadata" not in block

    def test_falls_back_to_text_block_for_text_parts(self) -> None:
        part = make_part(
            part_type="text",
            text_content="Hello, world!",
        )
        block = block_from_part(part)
        assert block == {"type": "text", "text": "Hello, world!"}

    def test_falls_back_to_empty_text_block_for_no_content(self) -> None:
        part = make_part(
            part_type="text",
            text_content=None,
        )
        block = block_from_part(part)
        assert block == {"type": "text", "text": ""}

    # ─── Regression: #158 — tool call id backfill from metadata.raw ──────────

    def test_backfills_tool_call_id_from_raw_for_text_type(self) -> None:
        # The exact scenario that crashes downstream providers:
        # text-type rows with tool call data only in metadata.raw.
        part = make_part(
            part_type="text",
            tool_call_id=None,
            tool_name=None,
            tool_input=None,
            metadata=json.dumps(
                {
                    "rawType": "toolCall",
                    "originalRole": "assistant",
                    "raw": {
                        "type": "toolCall",
                        "id": "toolu_01114sYtk4SBgj4gPvTmLrzX",
                        "name": "exec",
                        "arguments": {"command": "ls"},
                    },
                },
            ),
        )
        block = block_from_part(part)
        assert block["type"] == "toolCall"
        assert block["id"] == "toolu_01114sYtk4SBgj4gPvTmLrzX"
        assert block["name"] == "exec"
        assert block["arguments"] == {"command": "ls"}

    def test_backfills_tool_call_id_from_raw_for_tool_use_type(self) -> None:
        part = make_part(
            part_type="text",
            tool_call_id=None,
            tool_name=None,
            tool_input=None,
            metadata=json.dumps(
                {
                    "rawType": "tool_use",
                    "originalRole": "assistant",
                    "raw": {
                        "type": "tool_use",
                        "id": "toolu_abc123",
                        "name": "read",
                        "input": {"path": "USER.md"},
                    },
                },
            ),
        )
        block = block_from_part(part)
        assert block["type"] == "tool_use"
        assert block["id"] == "toolu_abc123"
        assert block["name"] == "read"

    def test_backfills_call_id_for_function_call_type(self) -> None:
        part = make_part(
            part_type="text",
            tool_call_id=None,
            tool_name=None,
            tool_input=None,
            metadata=json.dumps(
                {
                    "rawType": "function_call",
                    "originalRole": "assistant",
                    "raw": {
                        "type": "function_call",
                        "call_id": "fc_legacy_123",
                        "name": "bash",
                        "arguments": {"cmd": "pwd"},
                    },
                },
            ),
        )
        block = block_from_part(part)
        assert block["type"] == "function_call"
        assert block["call_id"] == "fc_legacy_123"
        assert block["name"] == "bash"
        assert block["arguments"] == {"cmd": "pwd"}

    def test_prefers_db_column_over_raw_when_both_present(self) -> None:
        part = make_part(
            part_type="text",
            tool_call_id="db-column-id",
            tool_name="db-tool-name",
            metadata=json.dumps(
                {
                    "rawType": "toolCall",
                    "originalRole": "assistant",
                    "raw": {
                        "type": "toolCall",
                        "id": "raw-id",
                        "name": "raw-name",
                        "arguments": {"x": 1},
                    },
                },
            ),
        )
        block = block_from_part(part)
        assert block["id"] == "db-column-id"
        assert block["name"] == "db-tool-name"

    def test_generates_synthetic_id_when_neither_has_id(self) -> None:
        part = make_part(
            part_type="text",
            tool_call_id=None,
            metadata=json.dumps(
                {
                    "rawType": "toolCall",
                    "originalRole": "assistant",
                    "raw": {
                        "type": "toolCall",
                        "name": "exec",
                        "arguments": {"command": "ls"},
                    },
                },
            ),
        )
        block = block_from_part(part)
        assert block["id"] == "toolu_lcm_test-part-1"

    def test_tolerant_metadata_parse_does_not_raise(self) -> None:
        # Malformed JSON in `metadata` must not raise; falls through to
        # part-type-based dispatch. Tests the tolerance invariant.
        part = make_part(part_type="text", text_content="hello", metadata="{not json")
        block = block_from_part(part)
        # Falls through to text dispatch.
        assert block == {"type": "text", "text": "hello"}


# ═══════════════════════════════════════════════════════════════════════════
# content_from_parts
# ═══════════════════════════════════════════════════════════════════════════


class TestContentFromParts:
    """Cover :func:`content_from_parts` (TS assembler.ts 538-565)."""

    def test_collapses_single_user_text_block_to_string(self) -> None:
        # OpenAI Chat shape compatibility.
        part = make_part(part_type="text", text_content="hello world")
        result = content_from_parts([part], "user", "")
        assert result == "hello world"

    def test_does_not_collapse_user_multi_blocks(self) -> None:
        # Multi-block user content must stay as array.
        parts = [
            make_part(part_id="p1", part_type="text", text_content="first"),
            make_part(part_id="p2", part_type="text", text_content="second"),
        ]
        result = content_from_parts(parts, "user", "")
        assert isinstance(result, list)
        assert len(result) == 2

    def test_does_not_collapse_assistant_single_text_block(self) -> None:
        # Assistant single-block content stays as array (Anthropic shape).
        part = make_part(part_type="text", text_content="hello")
        result = content_from_parts([part], "assistant", "")
        assert isinstance(result, list)

    def test_empty_parts_assistant_with_fallback(self) -> None:
        result = content_from_parts([], "assistant", "fallback text")
        assert result == [{"type": "text", "text": "fallback text"}]

    def test_empty_parts_assistant_without_fallback(self) -> None:
        result = content_from_parts([], "assistant", "")
        assert result == []

    def test_empty_parts_tool_result_with_fallback(self) -> None:
        result = content_from_parts([], "toolResult", "fallback")
        assert result == [{"type": "text", "text": "fallback"}]

    def test_empty_parts_user_returns_string(self) -> None:
        result = content_from_parts([], "user", "user text")
        assert result == "user text"


# ═══════════════════════════════════════════════════════════════════════════
# to_runtime_role
# ═══════════════════════════════════════════════════════════════════════════


class TestToRuntimeRole:
    """Cover :func:`to_runtime_role` (TS assembler.ts 392-418)."""

    def test_original_role_tool_result_wins(self) -> None:
        parts = [make_part(metadata=json.dumps({"originalRole": "toolResult"}))]
        assert to_runtime_role("user", parts) == "toolResult"

    def test_original_role_assistant_wins(self) -> None:
        parts = [make_part(metadata=json.dumps({"originalRole": "assistant"}))]
        assert to_runtime_role("user", parts) == "assistant"

    def test_original_role_user_wins(self) -> None:
        parts = [make_part(metadata=json.dumps({"originalRole": "user"}))]
        assert to_runtime_role("assistant", parts) == "user"

    def test_original_role_system_collapses_to_user(self) -> None:
        parts = [make_part(metadata=json.dumps({"originalRole": "system"}))]
        assert to_runtime_role("assistant", parts) == "user"

    def test_db_tool_maps_to_tool_result(self) -> None:
        assert to_runtime_role("tool", []) == "toolResult"

    def test_db_assistant_maps_to_assistant(self) -> None:
        assert to_runtime_role("assistant", []) == "assistant"

    def test_db_user_maps_to_user(self) -> None:
        assert to_runtime_role("user", []) == "user"

    def test_db_system_maps_to_user(self) -> None:
        assert to_runtime_role("system", []) == "user"


# ═══════════════════════════════════════════════════════════════════════════
# pick_tool_call_id / pick_tool_name / pick_tool_is_error
# ═══════════════════════════════════════════════════════════════════════════


class TestPickToolCallId:
    """Cover :func:`pick_tool_call_id` (TS assembler.ts 568-595)."""

    def test_prefers_db_column(self) -> None:
        parts = [
            make_part(
                tool_call_id="db-id",
                metadata=json.dumps({"toolCallId": "metadata-id"}),
            ),
        ]
        assert pick_tool_call_id(parts) == "db-id"

    def test_falls_back_to_metadata_camelCase(self) -> None:
        parts = [
            make_part(
                tool_call_id=None,
                metadata=json.dumps({"toolCallId": "metadata-id"}),
            ),
        ]
        assert pick_tool_call_id(parts) == "metadata-id"

    def test_falls_back_to_raw_camelCase(self) -> None:
        parts = [
            make_part(
                tool_call_id=None,
                metadata=json.dumps({"raw": {"toolCallId": "raw-camel"}}),
            ),
        ]
        assert pick_tool_call_id(parts) == "raw-camel"

    def test_falls_back_to_raw_snake_case(self) -> None:
        parts = [
            make_part(
                tool_call_id=None,
                metadata=json.dumps({"raw": {"tool_call_id": "raw-snake"}}),
            ),
        ]
        assert pick_tool_call_id(parts) == "raw-snake"

    def test_returns_none_when_no_match(self) -> None:
        parts = [make_part(tool_call_id=None, metadata=None)]
        assert pick_tool_call_id(parts) is None


class TestPickToolName:
    """Cover :func:`pick_tool_name` (TS assembler.ts 597-625)."""

    def test_prefers_db_column(self) -> None:
        parts = [
            make_part(
                tool_name="db-name",
                metadata=json.dumps({"toolName": "metadata-name"}),
            ),
        ]
        assert pick_tool_name(parts) == "db-name"

    def test_falls_back_to_raw_name(self) -> None:
        parts = [
            make_part(
                tool_name=None,
                metadata=json.dumps({"raw": {"name": "raw-name"}}),
            ),
        ]
        assert pick_tool_name(parts) == "raw-name"

    def test_falls_back_to_raw_toolName_camelCase(self) -> None:
        parts = [
            make_part(
                tool_name=None,
                metadata=json.dumps({"raw": {"toolName": "raw-camel"}}),
            ),
        ]
        assert pick_tool_name(parts) == "raw-camel"


class TestPickToolIsError:
    """Cover :func:`pick_tool_is_error` (TS assembler.ts 627-640)."""

    def test_returns_true_when_set(self) -> None:
        parts = [make_part(metadata=json.dumps({"isError": True}))]
        assert pick_tool_is_error(parts) is True

    def test_returns_false_when_set(self) -> None:
        parts = [make_part(metadata=json.dumps({"isError": False}))]
        assert pick_tool_is_error(parts) is False

    def test_returns_none_when_missing(self) -> None:
        parts = [make_part(metadata=json.dumps({}))]
        assert pick_tool_is_error(parts) is None


# ═══════════════════════════════════════════════════════════════════════════
# format_summary_content
# ═══════════════════════════════════════════════════════════════════════════


class TestFormatSummaryContent:
    """Cover :func:`format_summary_content` (TS assembler.ts 814-852)."""

    def test_renders_leaf_summary_with_minimum_attributes(self) -> None:
        summary = make_summary(
            summary_id="sum_x",
            kind="leaf",
            depth=0,
            content="Hello world",
            descendant_count=0,
            earliest_at=None,
            latest_at=None,
        )
        store = MagicMock()
        store.get_summary_parents.return_value = []
        result = format_summary_content(summary, store)
        # Must include 4 baseline attributes.
        assert 'id="sum_x"' in result
        assert 'kind="leaf"' in result
        assert 'depth="0"' in result
        assert 'descendant_count="0"' in result
        # Must wrap content.
        assert "Hello world" in result
        assert "<content>" in result
        assert "</content>" in result
        assert "</summary>" in result
        # No parents block for leaves.
        assert "<parents>" not in result

    def test_renders_condensed_summary_with_parents(self) -> None:
        summary = make_summary(
            summary_id="sum_cond",
            kind="condensed",
            depth=1,
            content="Condensed content",
            descendant_count=8,
        )
        parents = [
            make_summary(summary_id="sum_p1"),
            make_summary(summary_id="sum_p2"),
        ]
        store = MagicMock()
        store.get_summary_parents.return_value = parents
        result = format_summary_content(summary, store)
        assert 'kind="condensed"' in result
        assert "<parents>" in result
        assert '<summary_ref id="sum_p1" />' in result
        assert '<summary_ref id="sum_p2" />' in result
        assert "</parents>" in result
        store.get_summary_parents.assert_called_once_with("sum_cond")

    def test_renders_condensed_summary_without_parents_omits_block(self) -> None:
        summary = make_summary(summary_id="sum_orphan", kind="condensed", depth=1)
        store = MagicMock()
        store.get_summary_parents.return_value = []
        result = format_summary_content(summary, store)
        assert "<parents>" not in result

    def test_renders_earliest_and_latest_at(self) -> None:
        summary = make_summary(
            earliest_at=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            latest_at=datetime(2024, 1, 1, 13, 0, 0, tzinfo=timezone.utc),
        )
        store = MagicMock()
        store.get_summary_parents.return_value = []
        result = format_summary_content(summary, store, timezone="UTC")
        assert 'earliest_at="2024-01-01T12:00:00"' in result
        assert 'latest_at="2024-01-01T13:00:00"' in result

    def test_renders_with_explicit_timezone(self) -> None:
        # 2024-01-01T12:00 UTC → 04:00 in America/Los_Angeles (winter, PST).
        summary = make_summary(
            earliest_at=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            latest_at=None,
        )
        store = MagicMock()
        store.get_summary_parents.return_value = []
        result = format_summary_content(summary, store, timezone="America/Los_Angeles")
        assert 'earliest_at="2024-01-01T04:00:00"' in result


# ═══════════════════════════════════════════════════════════════════════════
# ContextAssembler.resolve_items — high-level hydration
# ═══════════════════════════════════════════════════════════════════════════


class TestResolveItems:
    """Cover :meth:`ContextAssembler.resolve_items` and related (TS 1342-1468)."""

    def _make_assembler(
        self,
        *,
        message_by_id: dict[int, MessageRecord | None] | None = None,
        parts_by_message_id: dict[int, list[MessagePartRecord]] | None = None,
        summary_by_id: dict[str, SummaryRecord | None] | None = None,
        parents_by_summary_id: dict[str, list[SummaryRecord]] | None = None,
        timezone_name: str | None = None,
    ) -> ContextAssembler:
        cstore = MagicMock()
        cstore.get_message_by_id.side_effect = lambda mid: (message_by_id or {}).get(mid)
        cstore.get_message_parts.side_effect = lambda mid: (parts_by_message_id or {}).get(mid, [])

        sstore = MagicMock()
        sstore.get_summary.side_effect = lambda sid: (summary_by_id or {}).get(sid)
        sstore.get_summary_parents.side_effect = lambda sid: (parents_by_summary_id or {}).get(
            sid, []
        )

        return ContextAssembler(cstore, sstore, timezone=timezone_name)

    def test_returns_empty_list_for_no_items(self) -> None:
        assembler = self._make_assembler()
        assert assembler.resolve_items([]) == []

    def test_resolves_simple_user_message(self) -> None:
        msg = make_message(message_id=1, role="user", content="Hello")
        assembler = self._make_assembler(
            message_by_id={1: msg},
            parts_by_message_id={1: []},
        )
        ctx_items = [make_context_item(ordinal=0, item_type="message", message_id=1)]
        resolved = assembler.resolve_items(ctx_items)
        assert len(resolved) == 1
        item = resolved[0]
        assert item.ordinal == 0
        assert item.is_message is True
        assert item.message["role"] == "user"
        # Empty parts + user → content collapses to fallback string.
        assert item.message["content"] == "Hello"
        assert item.tokens > 0
        assert item.text == "Hello"
        assert item.message_id == 1
        assert item.seq == 1
        assert item.source_role == "user"

    def test_resolves_multi_part_assistant_message(self) -> None:
        msg = make_message(message_id=2, role="assistant", content="")
        parts = [
            make_part(
                part_id="p1",
                message_id=2,
                part_type="text",
                ordinal=0,
                text_content="reasoning then...",
            ),
            make_part(
                part_id="p2",
                message_id=2,
                part_type="tool",
                ordinal=1,
                tool_call_id="toolu_1",
                tool_name="read",
                tool_input='{"path":"a.txt"}',
                metadata=json.dumps(
                    {"rawType": "tool_use", "originalRole": "assistant"},
                ),
            ),
        ]
        assembler = self._make_assembler(
            message_by_id={2: msg},
            parts_by_message_id={2: parts},
        )
        ctx_items = [make_context_item(ordinal=0, message_id=2)]
        resolved = assembler.resolve_items(ctx_items)
        assert len(resolved) == 1
        item = resolved[0]
        assert item.message["role"] == "assistant"
        content = item.message["content"]
        assert isinstance(content, list)
        assert len(content) == 2
        assert content[0] == {"type": "text", "text": "reasoning then..."}
        assert content[1]["type"] == "tool_use"
        assert content[1]["input"] == {"path": "a.txt"}
        # Assistant always carries a usage envelope.
        assert "usage" in item.message
        assert item.message["usage"]["output"] == item.tokens

    def test_resolves_tool_result_with_call_id(self) -> None:
        msg = make_message(message_id=3, role="tool", content="")
        parts = [
            make_part(
                part_id="p1",
                message_id=3,
                part_type="tool",
                ordinal=0,
                tool_call_id="toolu_abc",
                tool_name="read",
                tool_output='"contents"',
                metadata=json.dumps(
                    {"rawType": "tool_result", "originalRole": "toolResult"},
                ),
            ),
        ]
        assembler = self._make_assembler(
            message_by_id={3: msg},
            parts_by_message_id={3: parts},
        )
        ctx_items = [make_context_item(ordinal=0, message_id=3)]
        resolved = assembler.resolve_items(ctx_items)
        assert len(resolved) == 1
        item = resolved[0]
        assert item.message["role"] == "toolResult"
        assert item.message["toolCallId"] == "toolu_abc"
        assert item.message["toolName"] == "read"

    def test_tool_result_without_call_id_degrades_to_assistant(self) -> None:
        # CRITICAL invariant from TS line 1399: tool_result without
        # tool_call_id MUST be degraded to assistant role. Anthropic-
        # compatible APIs reject tool_result blocks missing the call id.
        msg = make_message(
            message_id=4,
            role="tool",
            content="orphan output",
        )
        # No parts → pick_tool_call_id returns None → degrades.
        assembler = self._make_assembler(
            message_by_id={4: msg},
            parts_by_message_id={4: []},
        )
        ctx_items = [make_context_item(ordinal=0, message_id=4)]
        resolved = assembler.resolve_items(ctx_items)
        assert len(resolved) == 1
        item = resolved[0]
        assert item.message["role"] == "assistant"
        # Text content is preserved (not dropped).
        assert "orphan output" in str(item.message["content"])
        # No toolCallId on degraded message.
        assert "toolCallId" not in item.message

    def test_tool_result_with_is_error_metadata(self) -> None:
        msg = make_message(message_id=5, role="tool", content="")
        parts = [
            make_part(
                part_id="p1",
                message_id=5,
                part_type="tool",
                ordinal=0,
                tool_call_id="toolu_err",
                tool_name="exec",
                tool_output='"failed"',
                metadata=json.dumps(
                    {
                        "rawType": "tool_result",
                        "originalRole": "toolResult",
                        "isError": True,
                    },
                ),
            ),
        ]
        assembler = self._make_assembler(
            message_by_id={5: msg},
            parts_by_message_id={5: parts},
        )
        ctx_items = [make_context_item(ordinal=0, message_id=5)]
        resolved = assembler.resolve_items(ctx_items)
        assert len(resolved) == 1
        assert resolved[0].message["isError"] is True

    def test_resolves_summary_item(self) -> None:
        summary = make_summary(
            summary_id="sum_1",
            kind="leaf",
            content="Summary body",
            descendant_count=5,
        )
        assembler = self._make_assembler(summary_by_id={"sum_1": summary})
        ctx_items = [
            make_context_item(
                ordinal=0,
                item_type="summary",
                message_id=None,
                summary_id="sum_1",
            ),
        ]
        resolved = assembler.resolve_items(ctx_items)
        assert len(resolved) == 1
        item = resolved[0]
        assert item.is_message is False
        assert item.summary is summary
        assert item.message["role"] == "user"
        # text field is the bare summary content, not the XML wrapper.
        assert item.text == "Summary body"
        # message content IS the XML wrapper.
        assert 'id="sum_1"' in item.message["content"]
        assert "<content>" in item.message["content"]
        assert "Summary body" in item.message["content"]

    def test_resolves_condensed_summary_with_parents(self) -> None:
        summary = make_summary(
            summary_id="sum_cond",
            kind="condensed",
            depth=1,
            content="Aggregated",
        )
        parents = [make_summary(summary_id="sum_leaf1")]
        assembler = self._make_assembler(
            summary_by_id={"sum_cond": summary},
            parents_by_summary_id={"sum_cond": parents},
        )
        ctx_items = [
            make_context_item(
                ordinal=0,
                item_type="summary",
                message_id=None,
                summary_id="sum_cond",
            ),
        ]
        resolved = assembler.resolve_items(ctx_items)
        assert len(resolved) == 1
        content_xml = resolved[0].message["content"]
        assert 'kind="condensed"' in content_xml
        assert '<summary_ref id="sum_leaf1" />' in content_xml

    def test_skips_missing_message(self) -> None:
        # get_message_by_id returns None → item is dropped, no raise.
        assembler = self._make_assembler(message_by_id={1: None})
        ctx_items = [make_context_item(ordinal=0, message_id=1)]
        resolved = assembler.resolve_items(ctx_items)
        assert resolved == []

    def test_skips_missing_summary(self) -> None:
        assembler = self._make_assembler(summary_by_id={"sum_missing": None})
        ctx_items = [
            make_context_item(
                ordinal=0,
                item_type="summary",
                message_id=None,
                summary_id="sum_missing",
            ),
        ]
        resolved = assembler.resolve_items(ctx_items)
        assert resolved == []

    def test_skips_empty_assistant_message_with_no_parts(self) -> None:
        # Empty content + zero parts → assistant message skipped.
        msg = make_message(message_id=7, role="assistant", content="   ")
        assembler = self._make_assembler(
            message_by_id={7: msg},
            parts_by_message_id={7: []},
        )
        ctx_items = [make_context_item(ordinal=0, message_id=7)]
        resolved = assembler.resolve_items(ctx_items)
        assert resolved == []

    def test_preserves_assistant_with_tool_use_and_empty_content(self) -> None:
        # Assistant with empty content text BUT non-empty parts is preserved.
        msg = make_message(message_id=8, role="assistant", content="")
        parts = [
            make_part(
                message_id=8,
                part_type="tool",
                ordinal=0,
                tool_call_id="toolu_1",
                tool_name="read",
                tool_input='{"path":"a.txt"}',
                metadata=json.dumps(
                    {"rawType": "tool_use", "originalRole": "assistant"},
                ),
            ),
        ]
        assembler = self._make_assembler(
            message_by_id={8: msg},
            parts_by_message_id={8: parts},
        )
        ctx_items = [make_context_item(ordinal=0, message_id=8)]
        resolved = assembler.resolve_items(ctx_items)
        assert len(resolved) == 1
        assert resolved[0].message["role"] == "assistant"

    def test_resolves_items_in_input_order(self) -> None:
        # Output preserves input ordering by construction (no sort).
        msg1 = make_message(message_id=10, content="first")
        msg2 = make_message(message_id=20, content="second")
        msg3 = make_message(message_id=30, content="third")
        assembler = self._make_assembler(
            message_by_id={10: msg1, 20: msg2, 30: msg3},
            parts_by_message_id={10: [], 20: [], 30: []},
        )
        ctx_items = [
            make_context_item(ordinal=5, message_id=10),
            make_context_item(ordinal=12, message_id=20),
            make_context_item(ordinal=20, message_id=30),
        ]
        resolved = assembler.resolve_items(ctx_items)
        assert [r.ordinal for r in resolved] == [5, 12, 20]
        assert [r.text for r in resolved] == ["first", "second", "third"]

    def test_skips_malformed_item_with_no_id(self) -> None:
        # item_type="message" but message_id=None → silently skipped.
        assembler = self._make_assembler()
        ctx_items = [
            ContextItemRecord(
                conversation_id=1,
                ordinal=0,
                item_type="message",
                message_id=None,
                summary_id=None,
                created_at=datetime.now(timezone.utc),
            ),
        ]
        resolved = assembler.resolve_items(ctx_items)
        assert resolved == []

    def test_resolves_openai_reasoning_round_trip(self) -> None:
        # Round-trip: assistant message with a reasoning part survives.
        msg = make_message(message_id=20, role="assistant", content="")
        parts = [
            make_part(
                message_id=20,
                part_type="reasoning",
                ordinal=0,
                metadata=json.dumps(
                    {
                        "raw": {
                            "type": "thinking",
                            "thinking": "",
                            "thinkingSignature": json.dumps(
                                {
                                    "type": "reasoning",
                                    "id": "rs_xyz",
                                    "encrypted_content": "...",
                                },
                            ),
                        },
                    },
                ),
            ),
            make_part(
                part_id="p2",
                message_id=20,
                part_type="text",
                ordinal=1,
                text_content="follow-up",
            ),
        ]
        assembler = self._make_assembler(
            message_by_id={20: msg},
            parts_by_message_id={20: parts},
        )
        ctx_items = [make_context_item(ordinal=0, message_id=20)]
        resolved = assembler.resolve_items(ctx_items)
        assert len(resolved) == 1
        content = resolved[0].message["content"]
        assert isinstance(content, list)
        # OpenAI reasoning shape restored.
        assert content[0] == {"type": "reasoning", "id": "rs_xyz", "encrypted_content": "..."}
        assert content[1] == {"type": "text", "text": "follow-up"}

    def test_resolves_provider_keying_anthropic_vs_openai(self) -> None:
        # Both shapes survive in a single conversation.
        # Anthropic assistant uses input; OpenAI uses arguments.
        msg_an = make_message(message_id=30, role="assistant", content="")
        parts_an = [
            make_part(
                message_id=30,
                part_type="tool",
                ordinal=0,
                tool_call_id="toolu_an",
                tool_name="read",
                tool_input='{"path":"x.txt"}',
                metadata=json.dumps(
                    {"rawType": "tool_use", "originalRole": "assistant"},
                ),
            ),
        ]
        msg_oa = make_message(message_id=31, role="assistant", content="")
        parts_oa = [
            make_part(
                message_id=31,
                part_type="tool",
                ordinal=0,
                tool_call_id="call_oa",
                tool_name="exec",
                tool_input='{"cmd":"ls"}',
                metadata=json.dumps(
                    {"rawType": "toolCall", "originalRole": "assistant"},
                ),
            ),
        ]
        assembler = self._make_assembler(
            message_by_id={30: msg_an, 31: msg_oa},
            parts_by_message_id={30: parts_an, 31: parts_oa},
        )
        ctx_items = [
            make_context_item(ordinal=0, message_id=30),
            make_context_item(ordinal=1, message_id=31),
        ]
        resolved = assembler.resolve_items(ctx_items)
        an_content = resolved[0].message["content"]
        oa_content = resolved[1].message["content"]
        assert an_content[0]["type"] == "tool_use"
        assert an_content[0]["input"] == {"path": "x.txt"}
        assert "arguments" not in an_content[0]
        assert oa_content[0]["type"] == "toolCall"
        assert oa_content[0]["arguments"] == {"cmd": "ls"}
        assert "input" not in oa_content[0]

    def test_tolerant_metadata_parse_in_resolve(self) -> None:
        # Whole resolve_items path tolerates malformed metadata on a part.
        msg = make_message(message_id=40, role="user", content="hi")
        parts = [
            make_part(
                message_id=40,
                part_type="text",
                ordinal=0,
                text_content="hi there",
                metadata="{not json",
            ),
        ]
        assembler = self._make_assembler(
            message_by_id={40: msg},
            parts_by_message_id={40: parts},
        )
        ctx_items = [make_context_item(ordinal=0, message_id=40)]
        # Must not raise.
        resolved = assembler.resolve_items(ctx_items)
        assert len(resolved) == 1
        assert resolved[0].text == "hi there"


# ═══════════════════════════════════════════════════════════════════════════
# ResolvedItem #628 stub-tier deferral (ADR-030)
# ═══════════════════════════════════════════════════════════════════════════


class TestStubTierDeferral:
    """v0.2.0 stub-tier fields exist on the dataclass but stay None.

    Per ADR-030, the Python port ships v0.1.0 with the stub-tier fields
    on :class:`ResolvedItem` but NOT populated by
    :meth:`ContextAssembler._resolve_message_item`. v0.2.0 will wire the
    sidecar lookup; until then these MUST be ``None``.
    """

    def test_resolved_item_has_all_five_stub_fields(self) -> None:
        # The dataclass surface area: forward-compat per ADR-030.
        item = ResolvedItem(
            ordinal=0,
            message={"role": "user", "content": "hi"},
            tokens=1,
            is_message=True,
            text="hi",
        )
        # Spec says they MUST exist as fields with default None.
        assert item.file_id is None
        assert item.file_byte_size is None
        assert item.stub_tool_name is None
        assert item.stub_tool_call_id is None
        assert item.file_summary is None

    def test_resolved_message_item_never_populates_stub_fields(self) -> None:
        msg = make_message(message_id=50, role="tool", content="")
        parts = [
            make_part(
                message_id=50,
                part_type="tool",
                ordinal=0,
                tool_call_id="toolu_real",
                tool_name="read",
                tool_output='"contents"',
                metadata=json.dumps(
                    {"rawType": "tool_result", "originalRole": "toolResult"},
                ),
            ),
        ]
        cstore = MagicMock()
        cstore.get_message_by_id.return_value = msg
        cstore.get_message_parts.return_value = parts
        sstore = MagicMock()
        assembler = ContextAssembler(cstore, sstore)
        ctx_items = [make_context_item(ordinal=0, message_id=50)]
        resolved = assembler.resolve_items(ctx_items)
        assert len(resolved) == 1
        item = resolved[0]
        # ADR-030 invariant: v0.1.0 NEVER populates these.
        assert item.file_id is None
        assert item.file_byte_size is None
        assert item.stub_tool_name is None
        assert item.stub_tool_call_id is None
        assert item.file_summary is None

    def test_resolved_summary_item_never_populates_stub_fields(self) -> None:
        summary = make_summary(summary_id="sum_x")
        cstore = MagicMock()
        sstore = MagicMock()
        sstore.get_summary.return_value = summary
        sstore.get_summary_parents.return_value = []
        assembler = ContextAssembler(cstore, sstore)
        ctx_items = [
            make_context_item(
                ordinal=0,
                item_type="summary",
                message_id=None,
                summary_id="sum_x",
            ),
        ]
        resolved = assembler.resolve_items(ctx_items)
        assert len(resolved) == 1
        item = resolved[0]
        assert item.file_id is None
        assert item.file_byte_size is None
        assert item.stub_tool_name is None
        assert item.stub_tool_call_id is None
        assert item.file_summary is None


# ═══════════════════════════════════════════════════════════════════════════
# Token counts
# ═══════════════════════════════════════════════════════════════════════════


class TestTokenCounts:
    """Verify :func:`estimate_tokens` is the source of `tokens` field."""

    def test_tokens_match_estimate_for_message_text(self) -> None:
        from lossless_hermes.estimate_tokens import estimate_tokens

        msg = make_message(message_id=1, role="user", content="Hello world!")
        cstore = MagicMock()
        cstore.get_message_by_id.return_value = msg
        cstore.get_message_parts.return_value = []
        assembler = ContextAssembler(cstore, MagicMock())
        ctx_items = [make_context_item(ordinal=0, message_id=1)]
        resolved = assembler.resolve_items(ctx_items)
        # Empty parts + user role → content collapses to fallback string.
        assert resolved[0].text == "Hello world!"
        assert resolved[0].tokens == estimate_tokens("Hello world!")

    def test_tokens_match_estimate_for_summary_xml(self) -> None:
        from lossless_hermes.estimate_tokens import estimate_tokens

        summary = make_summary(
            summary_id="sum_t",
            content="Tokens body",
            descendant_count=2,
        )
        cstore = MagicMock()
        sstore = MagicMock()
        sstore.get_summary.return_value = summary
        sstore.get_summary_parents.return_value = []
        assembler = ContextAssembler(cstore, sstore)
        ctx_items = [
            make_context_item(
                ordinal=0,
                item_type="summary",
                message_id=None,
                summary_id="sum_t",
            ),
        ]
        resolved = assembler.resolve_items(ctx_items)
        # Tokens computed on the XML wrapper, not bare content.
        xml_content = resolved[0].message["content"]
        assert resolved[0].tokens == estimate_tokens(xml_content)
        # Verify the XML wrapper is materially larger than bare content.
        assert resolved[0].tokens > estimate_tokens("Tokens body")
