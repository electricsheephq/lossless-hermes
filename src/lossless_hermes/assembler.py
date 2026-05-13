"""Context assembler — item resolution + block reconstruction (Epic 03).

Port of ``lossless-claw/src/assembler.ts`` (LCM commit ``1f07fbd``, branch
``pr-613``). This module is the **lowest-level reading layer** of the
assembler: it hydrates ordered :class:`ContextItemRecord` rows into
:class:`ResolvedItem` instances carrying a runtime-shaped ``message``
plus DB metadata, plain text rendering, and an :func:`estimate_tokens`
budget figure.

### Issue 03-04 scope

This file covers ``resolveItems`` (TS 1374-1466) plus every helper the
function transitively depends on:

* :func:`parse_json` — tolerant JSON parser (TS 188-197).
* :func:`get_original_role` / :func:`get_part_metadata` — decode the
  ``message_parts.metadata`` JSON envelope (TS 199-239).
* :func:`try_restore_openai_reasoning` — reverse the OpenClaw
  normalization of OpenAI ``rs_*`` reasoning blocks (TS 265-278).
* :func:`tool_call_block_from_part` / :func:`tool_result_block_from_part`
  — per-provider keying for tool blocks (TS 281-390).
* :func:`to_runtime_role` — DB-role + parts-metadata → runtime role
  (TS 392-418).
* :func:`block_from_part` — the big switch over part type (TS 421-535).
* :func:`content_from_parts` — assemble blocks into a content array,
  collapsing single-text-block user messages to a plain string
  (TS 538-565).
* :func:`pick_tool_call_id` / :func:`pick_tool_name` /
  :func:`pick_tool_is_error` — scan parts metadata for tool-call
  identity (TS 568-640).
* :func:`format_summary_content` — render the ``<summary>`` XML wrapper
  the model sees (TS 814-852).
* :class:`ContextAssembler._resolve_message_item` /
  :meth:`._resolve_summary_item` / :meth:`.resolve_items` —
  high-level hydration entry points (TS 1342-1468).

### #628 stub-tier deferral (ADR-030)

The :class:`ResolvedItem` dataclass exposes the stub-tier fields
(``file_id``, ``file_byte_size``, ``stub_tool_name``,
``stub_tool_call_id``, ``file_summary``) as Optional ``None``-default
attributes for forward-compatibility with v0.2.0. **In v0.1.0 these
fields are never populated** by :meth:`._resolve_message_item` — the
sidecar lookup + ``apply_stub_substitution`` arrive in v0.2.0 per
ADR-030. The tests in ``tests/test_assembler_blocks.py`` assert this
invariant explicitly.

### Key TS invariants preserved

* **Tool-result without ``tool_call_id`` is degraded to assistant**
  (TS line 1399). Anthropic-compatible APIs reject ``tool_result``
  blocks missing the call id; preserving the text via an assistant
  message is strictly better than dropping it. See
  :meth:`ContextAssembler._resolve_message_item`.
* **Provider-specific keying** for tool blocks (TS 317-326). Anthropic
  ``tool_use`` uses ``input``; OpenAI ``toolCall`` / ``functionCall``
  uses ``arguments``; OpenAI Responses ``function_call`` uses
  ``call_id`` (not ``id``).
* **OpenAI reasoning restoration** (TS 265-278). When ``metadata.raw``
  carries a ``thinkingSignature`` JSON that decodes to
  ``{type: "reasoning", id: "rs_…"}``, return the OpenAI shape so the
  Responses API gets what it expects.
* **Tolerant metadata parse** — failures return ``None``, never raise
  (TS 192-196).
* **Single text-block user-message collapse** to a plain string
  (TS 554-563) for OpenAI Chat shape compatibility.

### Source
* ``lossless-claw/src/assembler.ts`` lines 188-852 (helpers) +
  1342-1468 (item resolution), pinned to commit ``1f07fbd``.
* ``epics/03-ingest-assembly/03-04-resolve-items.md`` — issue spec.
* ``docs/porting-guides/assembler-compaction.md`` §"Step-by-step".
* ``docs/adr/030-pr-628-stub-tier-deferred.md`` — stub-tier deferral.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from lossless_hermes.estimate_tokens import estimate_tokens
from lossless_hermes.store.conversation import (
    ConversationStore,
    MessagePartRecord,
    MessageRole,
)
from lossless_hermes.store.summary import (
    ContextItemRecord,
    SummaryRecord,
    SummaryStore,
)

__all__ = [
    "ResolvedItem",
    "ContextAssembler",
    "parse_json",
    "get_original_role",
    "get_part_metadata",
    "try_restore_openai_reasoning",
    "tool_call_block_from_part",
    "tool_result_block_from_part",
    "to_runtime_role",
    "block_from_part",
    "content_from_parts",
    "pick_tool_call_id",
    "pick_tool_name",
    "pick_tool_is_error",
    "format_summary_content",
]


# ---------------------------------------------------------------------------
# ResolvedItem dataclass (per spec §"#628 stub-tier fields")
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ResolvedItem:
    """A hydrated context-item ready for the budget walk.

    Mirrors TS ``interface ResolvedItem`` (assembler.ts 856-875). Carries
    enough state for downstream selection, scoring, and orphan stripping
    without re-querying the store.

    Attributes:
        ordinal: Position in the DAG. Source field is
            ``context_items.ordinal``.
        message: Runtime-shaped message (role + content array, plus
            optional ``toolCallId`` / ``toolName`` / ``isError`` for
            tool-result rows). The content shape obeys the
            provider-compat rules in :func:`content_from_parts`.
        tokens: Result of :func:`estimate_tokens` on the serialized
            content. Used by the token-budget walk.
        is_message: ``True`` if the source is a raw message;
            ``False`` for summaries. Used to differentiate budget
            walk + orphan-stripping logic.
        text: Plain-text rendering for BM25-lite relevance scoring
            and duplicate-cluster diagnostics (TS ``contentText``).
        message_id: Source raw message primary key when
            ``is_message`` is True.
        seq: Source raw message ``seq`` (per-conversation sequence).
        source_role: Source raw message role (``MessageRole``).
        summary: Source :class:`SummaryRecord` when ``is_message`` is
            False.

    ### v0.2.0 stub-tier fields (deferred per ADR-030)

    The fields below exist on the dataclass for forward-compat but
    are **never populated** by :meth:`ContextAssembler._resolve_message_item`
    in v0.1.0. v0.2.0 will wire :meth:`SummaryStore.get_large_file` and
    populate these when ``msg.largeContent`` sidecar points at a
    ``file_*`` payload. Until then, all callers must treat them as
    ``None``.

    Attributes:
        file_id: ``file_*`` sidecar pointer (v0.2.0).
        file_byte_size: Original blob size in bytes (v0.2.0).
        stub_tool_name: Tool name preserved on the stub for
            drilldown helper text (v0.2.0).
        stub_tool_call_id: Tool call id preserved on the stub for
            drilldown helper text (v0.2.0).
        file_summary: Exploration summary text from
            ``large_files.exploration_summary`` (v0.2.0).
    """

    ordinal: int
    message: dict[str, Any]
    tokens: int
    is_message: bool
    text: str
    message_id: int | None = None
    seq: int | None = None
    source_role: MessageRole | None = None
    summary: SummaryRecord | None = None
    # v0.2.0 (#628 stub-tier; deferred per ADR-030). Fields exist for
    # forward-compat but are NOT populated by `_resolve_message_item`
    # in v0.1.0. Tests assert these stay None.
    file_id: str | None = None
    file_byte_size: int | None = None
    stub_tool_name: str | None = None
    stub_tool_call_id: str | None = None
    file_summary: str | None = None


# ---------------------------------------------------------------------------
# JSON + metadata helpers (TS 188-247)
# ---------------------------------------------------------------------------


def parse_json(value: str | None) -> Any | None:
    """Tolerant ``json.loads`` — returns ``None`` on failure.

    Mirrors TS ``parseJson`` (assembler.ts 188-197). Empty strings,
    whitespace-only strings, and malformed JSON all return ``None``
    (TS ``undefined``). The function never raises.

    Args:
        value: A string to decode, or ``None``.

    Returns:
        The decoded value (any JSON type) on success, else ``None``.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return None


def get_original_role(parts: Sequence[MessagePartRecord]) -> str | None:
    """Scan parts metadata for the first ``originalRole`` hint.

    Mirrors TS ``getOriginalRole`` (assembler.ts 199-211). Walks the
    parts in order, decodes each ``metadata`` JSON envelope, and
    returns the first non-empty ``originalRole`` string. Non-object
    decodes or missing fields are skipped silently.

    Args:
        parts: Sequence of :class:`MessagePartRecord`.

    Returns:
        The first ``originalRole`` string, or ``None`` if none found.
    """
    for part in parts:
        decoded = parse_json(part.metadata)
        if not isinstance(decoded, dict):
            continue
        role = decoded.get("originalRole")
        if isinstance(role, str) and len(role) > 0:
            return role
    return None


def get_part_metadata(part: MessagePartRecord) -> dict[str, Any]:
    """Decode ``message_parts.metadata`` into a typed dict.

    Mirrors TS ``getPartMetadata`` (assembler.ts 213-239). Returns
    a dict with optional ``originalRole``, ``rawType``, ``raw`` keys.
    Missing fields are omitted (TS undefined). A non-object payload
    yields an empty dict.

    Args:
        part: A :class:`MessagePartRecord`.

    Returns:
        Dict possibly containing ``originalRole`` (non-empty str),
        ``rawType`` (non-empty str), ``raw`` (any JSON value).
    """
    decoded = parse_json(part.metadata)
    if not isinstance(decoded, dict):
        return {}

    result: dict[str, Any] = {}
    original_role = decoded.get("originalRole")
    if isinstance(original_role, str) and len(original_role) > 0:
        result["originalRole"] = original_role

    raw_type = decoded.get("rawType")
    if isinstance(raw_type, str) and len(raw_type) > 0:
        result["rawType"] = raw_type

    if "raw" in decoded:
        result["raw"] = decoded["raw"]

    return result


def _parse_stored_value(value: str | None) -> Any | None:
    """Best-effort JSON decode, else return the raw string.

    Mirrors TS ``parseStoredValue`` (assembler.ts 241-247). Used for
    ``part.toolInput`` / ``part.toolOutput`` columns that are
    JSON-encoded when feasible but may carry a plain string.

    Returns ``None`` for empty inputs (TS undefined).
    """
    if not isinstance(value, str) or len(value) == 0:
        return None
    parsed = parse_json(value)
    return parsed if parsed is not None else value


def _reasoning_block_from_part(
    part: MessagePartRecord,
    raw_type: str | None,
) -> dict[str, Any]:
    """Reconstruct a reasoning/thinking block from a part row.

    Mirrors TS ``reasoningBlockFromPart`` (assembler.ts 249-257).
    Honors the ``rawType=thinking`` override to emit the Anthropic
    thinking shape; default is the OpenAI ``reasoning`` shape.
    """
    block_type = "thinking" if raw_type == "thinking" else "reasoning"
    if isinstance(part.text_content, str) and len(part.text_content) > 0:
        if block_type == "thinking":
            return {"type": block_type, "thinking": part.text_content}
        return {"type": block_type, "text": part.text_content}
    return {"type": block_type}


def try_restore_openai_reasoning(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    """Reverse OpenClaw normalization of an OpenAI ``rs_*`` reasoning block.

    Mirrors TS ``tryRestoreOpenAIReasoning`` (assembler.ts 265-278).
    OpenClaw converts the OpenAI Responses API shape
    ``{type: "reasoning", id: "rs_…", encrypted_content: "…"}``
    into ``{type: "thinking", thinking: "", thinkingSignature: "{…}"}``
    for storage. When we reassemble for the OpenAI provider we need
    the original back.

    Args:
        raw: The decoded ``metadata.raw`` value.

    Returns:
        The restored OpenAI ``reasoning`` block if ``raw`` is a
        recognisable normalised form, else ``None``.
    """
    if raw.get("type") != "thinking":
        return None
    sig = raw.get("thinkingSignature")
    if not isinstance(sig, str) or not sig.startswith("{"):
        return None
    try:
        parsed = json.loads(sig)
    except (ValueError, TypeError):
        # not valid JSON — leave as-is
        return None
    if (
        isinstance(parsed, dict)
        and parsed.get("type") == "reasoning"
        and isinstance(parsed.get("id"), str)
    ):
        return parsed
    return None


# ---------------------------------------------------------------------------
# Tool call / tool result block reconstruction (TS 281-390)
# ---------------------------------------------------------------------------


def tool_call_block_from_part(
    part: MessagePartRecord,
    raw_type: str | None = None,
) -> dict[str, Any]:
    """Reconstruct a tool_use / toolCall / function_call block.

    Mirrors TS ``toolCallBlockFromPart`` (assembler.ts 281-328).
    Handles provider-specific keying — ``tool_use`` (Anthropic)
    uses ``input``, ``toolCall`` / ``functionCall`` (OpenAI Chat)
    uses ``arguments``, ``function_call`` (OpenAI Responses) uses
    ``call_id`` instead of ``id``.

    Args:
        part: A :class:`MessagePartRecord` carrying tool-call data.
        raw_type: Optional rawType hint from
            :func:`get_part_metadata` (e.g. ``"tool_use"``).

    Returns:
        A dict representing the runtime tool-call block. Always has a
        ``type`` and an id (synthetic ``toolu_lcm_<part_id>`` if the
        DB column is empty — downstream providers crash on undefined).
    """
    if raw_type in {
        "function_call",
        "functionCall",
        "tool_use",
        "tool-use",
        "toolUse",
        "toolCall",
    }:
        block_type = raw_type
    else:
        block_type = "toolCall"

    input_value = _parse_stored_value(part.tool_input)
    block: dict[str, Any] = {"type": block_type}

    if block_type == "function_call":
        if isinstance(part.tool_call_id, str) and len(part.tool_call_id) > 0:
            block["call_id"] = part.tool_call_id
        if isinstance(part.tool_name, str) and len(part.tool_name) > 0:
            block["name"] = part.tool_name
        if input_value is not None:
            block["arguments"] = input_value
        return block

    # Always set id — downstream providers (e.g. Anthropic) call
    # normalizeToolCallId(block.id) which crashes on undefined.
    if isinstance(part.tool_call_id, str) and len(part.tool_call_id) > 0:
        block["id"] = part.tool_call_id
    else:
        synthetic_part_id = part.part_id if part.part_id is not None else "unknown"
        block["id"] = f"toolu_lcm_{synthetic_part_id}"

    if isinstance(part.tool_name, str) and len(part.tool_name) > 0:
        block["name"] = part.tool_name

    if input_value is not None:
        # toolCall and functionCall use "arguments" (consumed by OpenAI/xAI Chat
        # Completions extractToolCalls and Responses API paths in OpenClaw).
        # tool_use and variants use "input" (Anthropic native format).
        if block_type in {"functionCall", "toolCall"}:
            block["arguments"] = input_value
        else:
            block["input"] = input_value
    return block


def tool_result_block_from_part(
    part: MessagePartRecord,
    raw_type: str | None = None,
    raw: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Reconstruct a tool_result / function_call_output block.

    Mirrors TS ``toolResultBlockFromPart`` (assembler.ts 331-390).
    Preserves the externalized-text fast-path (TS 336-348), the
    column → ``raw.output`` → ``raw.content`` fallback chain, and
    ``is_error`` / ``isError`` preservation.

    Args:
        part: A :class:`MessagePartRecord` carrying tool-result data.
        raw_type: Optional rawType hint.
        raw: Optional decoded ``metadata.raw`` value.

    Returns:
        A dict representing the runtime tool-result block. The
        externalized-text fast-path may instead return a
        ``{type: "text", text}`` block when the stored payload is an
        externalized stub reference (TS 336-348).
    """
    # Externalized plain-text fast-path: if raw has a `text` field but no
    # `output`/`content`, and the DB columns are empty (or textContent
    # matches), surface a plain text block. This preserves stub-reference
    # rendering from `large-files.ts.formatToolOutputReference`.
    if (
        raw is not None
        and isinstance(raw.get("text"), str)
        and raw.get("output") is None
        and raw.get("content") is None
        and (part.tool_output is None or part.tool_output == "")
        and (part.text_content is None or part.text_content == raw["text"])
    ):
        return {
            "type": "text",
            "text": raw["text"],
        }

    if raw_type in {"function_call_output", "toolResult", "tool_result"}:
        block_type = raw_type
    else:
        block_type = "tool_result"

    output_value = _parse_stored_value(part.tool_output)
    block: dict[str, Any] = {"type": block_type}

    if isinstance(part.tool_name, str) and len(part.tool_name) > 0:
        block["name"] = part.tool_name

    if output_value is not None:
        block["output"] = output_value
    elif isinstance(part.text_content, str):
        block["output"] = part.text_content
    elif raw is not None and raw.get("output") is not None:
        block["output"] = raw["output"]
    elif raw is not None and raw.get("content") is not None:
        block["content"] = raw["content"]
    else:
        block["output"] = ""

    if raw is not None and isinstance(raw.get("is_error"), bool):
        block["is_error"] = raw["is_error"]
    elif raw is not None and isinstance(raw.get("isError"), bool):
        block["isError"] = raw["isError"]

    if block_type == "function_call_output":
        if isinstance(part.tool_call_id, str) and len(part.tool_call_id) > 0:
            block["call_id"] = part.tool_call_id
        return block

    if isinstance(part.tool_call_id, str) and len(part.tool_call_id) > 0:
        block["tool_use_id"] = part.tool_call_id
    return block


# ---------------------------------------------------------------------------
# Runtime role mapping + block dispatch (TS 392-565)
# ---------------------------------------------------------------------------


def to_runtime_role(
    db_role: MessageRole,
    parts: Sequence[MessagePartRecord],
) -> str:
    """Map a DB role + parts metadata to a runtime role.

    Mirrors TS ``toRuntimeRole`` (assembler.ts 392-418). The
    ``originalRole`` hint in ``parts[*].metadata`` takes precedence
    over the DB column; this lets us round-trip tool-result rows
    that were stored with role ``tool`` but originate from an
    assistant turn.

    Returns one of ``"user"``, ``"assistant"``, ``"toolResult"``.
    System prompts collapse to ``"user"`` because runtime system
    prompts are managed via ``set_system_prompt``, not message
    history.
    """
    original_role = get_original_role(parts)
    if original_role == "toolResult":
        return "toolResult"
    if original_role == "assistant":
        return "assistant"
    if original_role == "user":
        return "user"
    if original_role == "system":
        # Runtime system prompts are managed via setSystemPrompt(), not
        # message history.
        return "user"

    if db_role == "tool":
        return "toolResult"
    if db_role == "assistant":
        return "assistant"
    return "user"  # user | system


def block_from_part(part: MessagePartRecord) -> dict[str, Any]:
    """Reconstruct one content block from a stored part row.

    Mirrors TS ``blockFromPart`` (assembler.ts 421-535). The dispatch
    order (load-bearing):

    1. If ``metadata.raw`` looks like an OpenClaw-normalised OpenAI
       reasoning block, restore the original ``{type: "reasoning",
       id: "rs_…"}`` form.
    2. If ``metadata.raw`` is a non-tool block, return it verbatim
       (preserves provider-specific shapes the assembler doesn't
       understand).
    3. For tool blocks (``tool_use`` / ``toolCall`` /
       ``function_call`` / ``tool_result`` / ``function_call_output``),
       route through :func:`tool_call_block_from_part` or
       :func:`tool_result_block_from_part`. **Do not** return raw
       directly: providers (xAI/OpenAI Chat Completions) reject
       tool-call arguments passed as JS objects instead of JSON
       strings (TS 429-447).
    4. For ``partType == "reasoning"``, build a reasoning/thinking
       block from columns.
    5. For ``partType == "tool"``, dispatch to call vs result via
       ``originalRole`` / ``rawType``.
    6. For ``partType == "text"``, emit ``{type: "text", text}``.
    7. Fallbacks: any other type with non-empty ``text_content`` →
       text block; else stringified metadata; else empty text block.

    Args:
        part: A :class:`MessagePartRecord` row.

    Returns:
        A dict representing one runtime content block.
    """
    metadata = get_part_metadata(part)
    raw_value = metadata.get("raw")

    # Mutable shadow of `part` — we may backfill toolCallId/toolName/toolInput
    # from `metadata.raw` so subsequent calls to tool_call_block_from_part
    # have data to render. TS does this by mutating the part record in
    # place (assembler.ts 461-478); Python dataclasses are frozen, so we
    # build a working dict instead.
    part_view: MessagePartRecord = part

    if isinstance(raw_value, dict):
        # If this is an OpenClaw-normalised OpenAI reasoning block, restore
        # the original OpenAI format so the Responses API gets the
        # {type: "reasoning", id: "rs_…"} it expects.
        restored = try_restore_openai_reasoning(raw_value)
        if restored is not None:
            return restored

        # Don't return raw for tool call/result blocks — they need to go
        # through tool_call_block_from_part / tool_result_block_from_part
        # which properly normalize arguments (stringify if object) and format
        # for the target provider. Returning raw here causes arguments to be
        # passed as a JS object instead of a JSON string, which breaks
        # xAI/OpenAI Chat Completions API (422).
        raw_type = raw_value.get("type") if isinstance(raw_value.get("type"), str) else None
        is_tool_block = raw_type in {
            "toolCall",
            "tool_use",
            "tool-use",
            "toolUse",
            "functionCall",
            "function_call",
            "function_call_output",
            "toolResult",
            "tool_result",
        }
        if not is_tool_block:
            # Coerce dict → return verbatim (deep-copy not required for
            # ports — the consumer treats the block as read-only).
            return dict(raw_value)

        # When tool blocks are routed through tool_call_block_from_part
        # below instead of returning raw directly, the function reads
        # part.tool_call_id / part.tool_name from the DB columns. For rows
        # stored as part_type='text' those columns are often NULL — the
        # values only live inside metadata.raw. Backfill them here so the
        # reconstructed block keeps the original id/name.
        raw_tool_call_id: str | None = None
        raw_id = raw_value.get("id")
        raw_call_id = raw_value.get("call_id")
        if isinstance(raw_id, str) and len(raw_id) > 0:
            raw_tool_call_id = raw_id
        elif isinstance(raw_call_id, str) and len(raw_call_id) > 0:
            raw_tool_call_id = raw_call_id

        backfilled_tool_call_id = part.tool_call_id
        if raw_tool_call_id is not None and (
            not isinstance(part.tool_call_id, str) or len(part.tool_call_id) == 0
        ):
            backfilled_tool_call_id = raw_tool_call_id

        backfilled_tool_name = part.tool_name
        raw_name = raw_value.get("name")
        if (
            isinstance(raw_name, str)
            and len(raw_name) > 0
            and (not isinstance(part.tool_name, str) or len(part.tool_name) == 0)
        ):
            backfilled_tool_name = raw_name

        backfilled_tool_input = part.tool_input
        if part.tool_input is None or part.tool_input == "":
            raw_args = raw_value.get("arguments")
            if raw_args is None:
                raw_args = raw_value.get("input")
            if raw_args is not None:
                if isinstance(raw_args, str):
                    backfilled_tool_input = raw_args
                else:
                    backfilled_tool_input = json.dumps(raw_args)

        # Build a shadow record only if at least one column was changed.
        if (
            backfilled_tool_call_id is not part.tool_call_id
            or backfilled_tool_name is not part.tool_name
            or backfilled_tool_input is not part.tool_input
        ):
            # Replace by constructing a new frozen record; dataclasses.replace
            # would also work, but we use direct construction for clarity.
            part_view = MessagePartRecord(
                part_id=part.part_id,
                message_id=part.message_id,
                session_id=part.session_id,
                part_type=part.part_type,
                ordinal=part.ordinal,
                text_content=part.text_content,
                tool_call_id=backfilled_tool_call_id,
                tool_name=backfilled_tool_name,
                tool_input=backfilled_tool_input,
                tool_output=part.tool_output,
                metadata=part.metadata,
            )

    if part_view.part_type == "reasoning":
        return _reasoning_block_from_part(part_view, metadata.get("rawType"))

    if part_view.part_type == "tool":
        if (
            metadata.get("originalRole") == "toolResult"
            or metadata.get("rawType") == "function_call_output"
        ):
            return tool_result_block_from_part(
                part_view,
                metadata.get("rawType"),
                raw_value if isinstance(raw_value, dict) else None,
            )
        return tool_call_block_from_part(part_view, metadata.get("rawType"))

    raw_type = metadata.get("rawType")
    if raw_type in {
        "function_call",
        "functionCall",
        "tool_use",
        "tool-use",
        "toolUse",
        "toolCall",
    }:
        return tool_call_block_from_part(part_view, raw_type)
    if raw_type in {"function_call_output", "tool_result", "toolResult"}:
        return tool_result_block_from_part(
            part_view,
            raw_type,
            raw_value if isinstance(raw_value, dict) else None,
        )

    if part_view.part_type == "text":
        return {
            "type": "text",
            "text": part_view.text_content if part_view.text_content is not None else "",
        }

    if isinstance(part_view.text_content, str) and len(part_view.text_content) > 0:
        return {"type": "text", "text": part_view.text_content}

    decoded_fallback = parse_json(part_view.metadata)
    if isinstance(decoded_fallback, dict):
        return {"type": "text", "text": json.dumps(decoded_fallback)}

    return {"type": "text", "text": ""}


def content_from_parts(
    parts: Sequence[MessagePartRecord],
    role: str,
    fallback_content: str,
) -> Any:
    """Assemble parts into a content array (or plain string for some users).

    Mirrors TS ``contentFromParts`` (assembler.ts 538-565). Special-case:
    a single text-block user message collapses to a plain string so
    OpenAI Chat Completions accepts it.

    Args:
        parts: Sequence of :class:`MessagePartRecord`.
        role: Runtime role from :func:`to_runtime_role`.
        fallback_content: The ``messages.content`` column (used when
            parts are empty).

    Returns:
        Either a ``list[dict]`` of content blocks, or a plain
        ``str`` for the single-text-block user-message collapse.
    """
    if len(parts) == 0:
        if role == "assistant":
            return [{"type": "text", "text": fallback_content}] if fallback_content else []
        if role == "toolResult":
            return [{"type": "text", "text": fallback_content}]
        return fallback_content

    blocks = [block_from_part(part) for part in parts]
    if (
        role == "user"
        and len(blocks) == 1
        and isinstance(blocks[0], dict)
        and blocks[0].get("type") == "text"
        and isinstance(blocks[0].get("text"), str)
    ):
        return blocks[0]["text"]
    return blocks


# ---------------------------------------------------------------------------
# Tool-identity pickers (TS 568-640)
# ---------------------------------------------------------------------------


def pick_tool_call_id(parts: Sequence[MessagePartRecord]) -> str | None:
    """Scan parts for a tool_call_id.

    Mirrors TS ``pickToolCallId`` (assembler.ts 568-595). Search order:
    DB column, then ``metadata.toolCallId``, then ``metadata.raw.toolCallId``,
    then ``metadata.raw.tool_call_id`` (snake_case). First non-empty
    string wins.
    """
    for part in parts:
        if isinstance(part.tool_call_id, str) and len(part.tool_call_id) > 0:
            return part.tool_call_id
        decoded = parse_json(part.metadata)
        if not isinstance(decoded, dict):
            continue
        metadata_tool_call_id = decoded.get("toolCallId")
        if isinstance(metadata_tool_call_id, str) and len(metadata_tool_call_id) > 0:
            return metadata_tool_call_id
        raw = decoded.get("raw")
        if not isinstance(raw, dict):
            continue
        maybe = raw.get("toolCallId")
        if isinstance(maybe, str) and len(maybe) > 0:
            return maybe
        maybe_snake = raw.get("tool_call_id")
        if isinstance(maybe_snake, str) and len(maybe_snake) > 0:
            return maybe_snake
    return None


def pick_tool_name(parts: Sequence[MessagePartRecord]) -> str | None:
    """Scan parts for a tool name.

    Mirrors TS ``pickToolName`` (assembler.ts 597-625). Search order:
    DB column, then ``metadata.toolName``, then ``metadata.raw.name``,
    then ``metadata.raw.toolName``.
    """
    for part in parts:
        if isinstance(part.tool_name, str) and len(part.tool_name) > 0:
            return part.tool_name
        decoded = parse_json(part.metadata)
        if not isinstance(decoded, dict):
            continue
        metadata_tool_name = decoded.get("toolName")
        if isinstance(metadata_tool_name, str) and len(metadata_tool_name) > 0:
            return metadata_tool_name
        raw = decoded.get("raw")
        if not isinstance(raw, dict):
            continue
        maybe = raw.get("name")
        if isinstance(maybe, str) and len(maybe) > 0:
            return maybe
        maybe_camel = raw.get("toolName")
        if isinstance(maybe_camel, str) and len(maybe_camel) > 0:
            return maybe_camel
    return None


def pick_tool_is_error(parts: Sequence[MessagePartRecord]) -> bool | None:
    """Scan parts metadata for an ``isError`` boolean.

    Mirrors TS ``pickToolIsError`` (assembler.ts 627-640).
    """
    for part in parts:
        decoded = parse_json(part.metadata)
        if not isinstance(decoded, dict):
            continue
        metadata_is_error = decoded.get("isError")
        if isinstance(metadata_is_error, bool):
            return metadata_is_error
    return None


# ---------------------------------------------------------------------------
# Summary XML wrapper (TS 789-852)
# ---------------------------------------------------------------------------


def _format_date_for_attribute(date: datetime, timezone: str | None) -> str:
    """Format ``date`` for an XML attribute in ``timezone`` (default UTC).

    Mirrors TS ``formatDateForAttribute`` (assembler.ts 789-809). Uses
    ``en-CA``-style ``YYYY-MM-DDTHH:MM:SS`` output (24-hour, zero-padded).
    Falls back to UTC ISO 8601 if the zone is unrecognised.

    Note: TS reads ``timezone ?? "UTC"`` and uses
    ``Intl.DateTimeFormat("en-CA", { timeZone: tz, ... })`` which throws
    on bad ``timeZone`` strings — the ``catch`` returns
    ``date.toISOString()``. Python's :class:`ZoneInfo` raises
    :class:`zoneinfo.ZoneInfoNotFoundError` on bad names; we catch it
    and fall back to UTC ISO.
    """
    try:
        tz_name = timezone if timezone else "UTC"
        zone = ZoneInfo(tz_name)
        localized = (
            date.astimezone(zone)
            if date.tzinfo
            else date.replace(tzinfo=ZoneInfo("UTC")).astimezone(zone)
        )
        return localized.strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        # zoneinfo lookup failure → fall back to UTC ISO 8601.
        try:
            if date.tzinfo is None:
                date = date.replace(tzinfo=ZoneInfo("UTC"))
            return date.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        except Exception:
            return date.isoformat()


def format_summary_content(
    summary: SummaryRecord,
    summary_store: SummaryStore,
    timezone: str | None = None,
) -> str:
    """Render a summary record as the ``<summary>`` XML wrapper the model sees.

    Mirrors TS ``formatSummaryContent`` (assembler.ts 814-852). Always
    emits ``id``, ``kind``, ``depth``, ``descendant_count`` attributes;
    appends ``earliest_at`` / ``latest_at`` when present. For
    ``kind == "condensed"`` summaries with parents, emits a
    ``<parents>`` block of ``<summary_ref>`` child elements.

    Args:
        summary: The :class:`SummaryRecord` to render.
        summary_store: Used to look up parents for condensed
            summaries.
        timezone: IANA tz name (e.g. ``"America/Los_Angeles"``).
            Defaults to UTC.

    Returns:
        A multi-line XML string ready for inclusion in a user message.
    """
    attributes = [
        f'id="{summary.summary_id}"',
        f'kind="{summary.kind}"',
        f'depth="{summary.depth}"',
        f'descendant_count="{summary.descendant_count}"',
    ]
    if summary.earliest_at is not None:
        attributes.append(
            f'earliest_at="{_format_date_for_attribute(summary.earliest_at, timezone)}"',
        )
    if summary.latest_at is not None:
        attributes.append(
            f'latest_at="{_format_date_for_attribute(summary.latest_at, timezone)}"',
        )

    lines: list[str] = []
    lines.append(f"<summary {' '.join(attributes)}>")

    # For condensed summaries, include parent references.
    if summary.kind == "condensed":
        parents = summary_store.get_summary_parents(summary.summary_id)
        if len(parents) > 0:
            lines.append("  <parents>")
            for parent in parents:
                lines.append(f'    <summary_ref id="{parent.summary_id}" />')
            lines.append("  </parents>")

    lines.append("  <content>")
    lines.append(summary.content)
    lines.append("  </content>")
    lines.append("</summary>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# ContextAssembler — high-level orchestration (TS 1084-1469, partial)
# ---------------------------------------------------------------------------


class ContextAssembler:
    """Hydrate context_items into ResolvedItems for budget-walk + sanitize.

    Mirrors TS ``ContextAssembler`` (assembler.ts 1084-1469). v0.1.0 of
    this Python port ships only the hydration surface (:meth:`resolve_items`
    + the two ``_resolve_*`` helpers). The remaining steps
    (fresh-tail boundary, budget walk, orphan stripping, sanitize) land
    in 03-05..03-08.

    Args:
        conversation_store: Source of :class:`MessageRecord` /
            :class:`MessagePartRecord` rows.
        summary_store: Source of :class:`SummaryRecord` rows and
            ``get_context_items`` ordering.
        timezone: IANA tz name (e.g. ``"America/Los_Angeles"``) for
            summary-XML ``earliest_at`` / ``latest_at`` attributes.
            Defaults to UTC.
    """

    __slots__ = ("_conversation_store", "_summary_store", "_timezone")

    def __init__(
        self,
        conversation_store: ConversationStore,
        summary_store: SummaryStore,
        timezone: str | None = None,
    ) -> None:
        self._conversation_store = conversation_store
        self._summary_store = summary_store
        self._timezone = timezone

    def resolve_items(
        self,
        context_items: Sequence[ContextItemRecord],
    ) -> list[ResolvedItem]:
        """Resolve a list of context items into hydrated ResolvedItems.

        Mirrors TS ``resolveItems`` (assembler.ts 1342-1353). Items that
        cannot be resolved (deleted message, suppressed summary,
        malformed item type) are silently skipped — they don't appear
        in the output list, but they also don't raise.

        Args:
            context_items: Ordered context-item records from
                :meth:`SummaryStore.get_context_items`.

        Returns:
            A list of :class:`ResolvedItem` in input order. May be
            shorter than ``context_items`` if some were skipped.
        """
        resolved: list[ResolvedItem] = []
        for item in context_items:
            result = self._resolve_item(item)
            if result is not None:
                resolved.append(result)
        return resolved

    def _resolve_item(self, item: ContextItemRecord) -> ResolvedItem | None:
        """Dispatch to :meth:`_resolve_message_item` or :meth:`_resolve_summary_item`.

        Mirrors TS ``resolveItem`` (assembler.ts 1358-1369). Malformed
        rows (item_type without a matching id column) return ``None``.
        """
        if item.item_type == "message" and item.message_id is not None:
            return self._resolve_message_item(item)
        if item.item_type == "summary" and item.summary_id is not None:
            return self._resolve_summary_item(item)
        # Malformed item — skip.
        return None

    def _resolve_message_item(self, item: ContextItemRecord) -> ResolvedItem | None:
        """Hydrate a context-item that references a raw message.

        Mirrors TS ``resolveMessageItem`` (assembler.ts 1374-1448).
        Skips empty assistant messages (no parts AND empty content),
        degrades tool-result-without-toolCallId to assistant role (TS
        line 1399, Anthropic-compat invariant), and computes
        ``tokens`` via :func:`estimate_tokens`.

        ### v0.1.0 stub-tier deferral

        Per ADR-030, **the stub-tier fields on :class:`ResolvedItem`
        (``file_id``, ``file_byte_size``, ``stub_tool_name``,
        ``stub_tool_call_id``, ``file_summary``) are NEVER populated
        here**. The sidecar lookup + :meth:`SummaryStore.get_large_file`
        wiring ships in v0.2.0 (PR #628 port). Until then, every
        :class:`ResolvedItem` for a message has those fields ``None``.

        Args:
            item: A :class:`ContextItemRecord` with ``item_type="message"``.

        Returns:
            A :class:`ResolvedItem` or ``None`` if the message is
            missing / empty.
        """
        assert item.message_id is not None  # narrowed by `_resolve_item`.
        msg = self._conversation_store.get_message_by_id(item.message_id)
        if msg is None:
            return None

        parts = self._conversation_store.get_message_parts(msg.message_id)

        # Skip empty assistant messages left by error/aborted responses.
        # These waste context tokens and can confuse models that reject
        # consecutive empty assistant turns. Only skip when both the stored
        # content text AND the message_parts table are empty — assistant
        # messages that contain tool calls have empty text content but
        # non-empty parts and must be preserved.
        if msg.role == "assistant":
            content_text = msg.content.strip() if isinstance(msg.content, str) else ""
            if not content_text and len(parts) == 0:
                return None

        role_from_store = to_runtime_role(msg.role, parts)
        is_tool_result = role_from_store == "toolResult"
        tool_call_id = pick_tool_call_id(parts) if is_tool_result else None
        tool_name: str | None = (pick_tool_name(parts) or "unknown") if is_tool_result else None
        tool_is_error = pick_tool_is_error(parts) if is_tool_result else None
        # Tool results without a call id cannot be serialized for
        # Anthropic-compatible APIs. This happens for legacy/bootstrap
        # rows that have role=tool but no message_parts. Preserve the
        # text by degrading to assistant content instead of emitting
        # invalid toolResult. (TS assembler.ts line 1399)
        role: str = "assistant" if (is_tool_result and not tool_call_id) else role_from_store
        content = content_from_parts(parts, role, msg.content)
        if isinstance(content, str):
            content_text_for_tokens = content
        else:
            try:
                serialized = json.dumps(content)
            except (TypeError, ValueError):
                serialized = None
            content_text_for_tokens = serialized if serialized is not None else msg.content
        token_count = estimate_tokens(content_text_for_tokens)

        # Build the runtime message dict. Assistant messages include a
        # usage envelope so downstream cache-token accounting has a
        # well-shaped target; non-assistant messages may carry tool
        # metadata.
        message: dict[str, Any]
        if role == "assistant":
            message = {
                "role": role,
                "content": content,
                "usage": {
                    "input": 0,
                    "output": token_count,
                    "cacheRead": 0,
                    "cacheWrite": 0,
                    "totalTokens": token_count,
                    "cost": {
                        "input": 0,
                        "output": 0,
                        "cacheRead": 0,
                        "cacheWrite": 0,
                        "total": 0,
                    },
                },
            }
        else:
            message = {"role": role, "content": content}
            if tool_call_id:
                message["toolCallId"] = tool_call_id
            if tool_name:
                message["toolName"] = tool_name
            if role == "toolResult" and tool_is_error is not None:
                message["isError"] = tool_is_error

        return ResolvedItem(
            ordinal=item.ordinal,
            message=message,
            tokens=token_count,
            is_message=True,
            text=content_text_for_tokens,
            message_id=msg.message_id,
            seq=msg.seq,
            source_role=msg.role,
            # v0.2.0 stub-tier fields stay None per ADR-030.
        )

    def _resolve_summary_item(self, item: ContextItemRecord) -> ResolvedItem | None:
        """Hydrate a context-item that references a summary.

        Mirrors TS ``resolveSummaryItem`` (assembler.ts 1450-1468).
        Renders the summary as a synthetic user message wrapping
        :func:`format_summary_content`. Sets ``is_message=False`` so
        downstream code can distinguish raw messages from summaries.
        """
        assert item.summary_id is not None  # narrowed by `_resolve_item`.
        summary = self._summary_store.get_summary(item.summary_id)
        if summary is None:
            return None

        content = format_summary_content(
            summary,
            self._summary_store,
            self._timezone,
        )
        tokens = estimate_tokens(content)

        return ResolvedItem(
            ordinal=item.ordinal,
            message={"role": "user", "content": content},
            tokens=tokens,
            is_message=False,
            text=summary.content,
            summary=summary,
        )
