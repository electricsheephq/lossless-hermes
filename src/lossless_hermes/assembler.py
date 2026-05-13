"""Context assembler — item resolution + block reconstruction (Epic 03).

Port of ``lossless-claw/src/assembler.ts`` (LCM commit ``1f07fbd``, branch
``pr-613``). This module is the **lowest-level reading layer** of the
assembler: it hydrates ordered :class:`ContextItemRecord` rows into
:class:`ResolvedItem` instances carrying a runtime-shaped ``message``
plus DB metadata, plain text rendering, and an :func:`estimate_tokens`
budget figure.

### Issue 03-04 + 03-05 scope

This file covers ``resolveItems`` (TS 1374-1466) plus every helper the
function transitively depends on, AND the fresh-tail boundary
calculation (``resolveFreshTailOrdinal``, TS 983-1032). The budget
walk (03-06), orphan stripping (03-07), and orchestration (03-08)
land in subsequent issues.

Helpers covered by 03-04:

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

Helpers covered by 03-05:

* :func:`resolve_fresh_tail_ordinal` — walks raw-message items
  newest→oldest, protecting up to ``fresh_tail_count`` (and
  optionally ``fresh_tail_max_tokens``) before declaring the
  boundary. Returns :data:`EMPTY_FRESH_TAIL_ORDINAL` when no
  protection applies.

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
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal, Mapping, Sequence
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
    "resolve_fresh_tail_ordinal",
    "EMPTY_FRESH_TAIL_ORDINAL",
    "tokenize_text",
    "score_relevance",
    "has_searchable_prompt",
    "budget_walk",
    "SelectionMode",
]


# Tokens used by the BM25-lite selection mode label. Matches the
# ``selectionMode`` field of TS ``debug`` envelope (assembler.ts 1180).
SelectionMode = Literal["full-fit", "prompt-aware", "chronological"]


# ---------------------------------------------------------------------------
# Fresh-tail boundary computation (TS 983-1032)
# ---------------------------------------------------------------------------


# Sentinel for "no fresh tail" — the TS source returns ``Infinity`` (a
# ``number`` in JS); Python's downstream stores accept ``int`` ordinals
# only, so we use ``sys.maxsize`` which (a) is an int, (b) always exceeds
# any real ordinal value emitted by ``context_items.ordinal`` (SQLite
# INTEGER, max 2**63-1), and (c) makes the splitter predicates
# ``ordinal < EMPTY_FRESH_TAIL_ORDINAL`` and ``ordinal >= EMPTY_FRESH_TAIL_ORDINAL``
# behave identically to TS ``ordinal < Infinity`` / ``ordinal >= Infinity``.
#
# Behavioral contract: when this value is returned, the fresh tail is
# empty and *every* resolved item is evictable. Downstream splitters
# already use the ``ordinal >= boundary`` predicate, so this drops in
# without further work in 03-06 / 03-08.
EMPTY_FRESH_TAIL_ORDINAL = sys.maxsize


def resolve_fresh_tail_ordinal(
    resolved: Sequence[ResolvedItem],
    fresh_tail_count: int,
    fresh_tail_max_tokens: int | None = None,
) -> int:
    """Compute the boundary ordinal separating the fresh tail from evictable.

    Mirrors TS ``resolveFreshTailOrdinal`` (``assembler.ts`` 983-1032).
    Walks raw-message items in ``resolved`` from newest to oldest,
    protecting up to ``fresh_tail_count`` messages (and stopping early
    if adding the next message would push the protected total past
    ``fresh_tail_max_tokens``). The newest message is **always
    protected**, even if it alone exceeds the cap — the user's current
    turn must never be evicted.

    The returned ordinal is the **smallest ordinal in the fresh tail**.
    Downstream code uses:

    * ``item.ordinal >= boundary`` → membership in the fresh tail (kept).
    * ``item.ordinal < boundary``  → evictable prefix (subject to budget).

    ### Edge cases (matches TS verbatim)

    * **No raw messages** (empty input, or all-summaries): return
      :data:`EMPTY_FRESH_TAIL_ORDINAL`. Every resolved item is
      classified as evictable; the fresh tail is empty.
    * **``fresh_tail_count <= 0``** (or non-finite): return
      :data:`EMPTY_FRESH_TAIL_ORDINAL`. Mirrors TS line 988
      (``if (!Number.isFinite(freshTailCount) || freshTailCount <= 0)
      return Infinity;``). This contradicts the issue spec AC bullet
      "fresh_tail_count = 0 still keeps the newest" — the spec author
      was wrong about TS behavior; ``test/lcm-integration.test.ts``
      exercises ``freshTailCount: 0`` configurations that depend on the
      no-protection path (lines 1510, 1567, 1628, 1688, 1761). Ports
      MUST match TS, not the spec prose, per the project's
      "TS source is canonical" principle.
    * **``fresh_tail_max_tokens`` smaller than the newest message**:
      the newest is still protected (``protectedCount > 0`` guard at
      TS line 1019 means the token-cap check is skipped on the first
      iteration).
    * **``fresh_tail_count`` larger than the message count**: every
      raw message is protected; the boundary is the ordinal of the
      oldest raw message.
    * **Summaries between raw messages**: they are *skipped* during the
      newest-to-oldest walk (only ``is_message=True`` items count
      against ``fresh_tail_count``), but they end up *included* in the
      fresh-tail slice if their ordinal happens to be ``>=`` the
      boundary, because the splitter uses ``ordinal >= boundary``
      rather than ``is_message AND ordinal >= boundary``. The 03-08
      integration test validates this end-to-end.

    ### Implementation notes

    The TS source uses a numeric-cast token-cap (``Math.floor`` on a
    float-typed input); Python types ``fresh_tail_max_tokens`` as
    ``int | None`` so the floor is implicit. We still guard against
    negative values (TS ``freshTailMaxTokens >= 0`` check at line
    1000) — a negative cap collapses to "no cap" behavior, matching
    TS.

    Args:
        resolved: Hydrated context items in ordinal order
            (i.e. oldest first). Only items with ``is_message=True``
            count toward the protection budget; summaries are skipped.
        fresh_tail_count: Maximum number of raw messages to protect
            from eviction. Default callers should pass ``8`` (see
            :class:`AssembleInput` defaults — TS ``AssembleContextInput``
            line 128).
        fresh_tail_max_tokens: Optional cap on protected-tail tokens.
            When ``None``, only ``fresh_tail_count`` gates the walk.
            Negative or non-finite values collapse to "no cap" per TS
            line 1000.

    Returns:
        The ordinal of the oldest message that fits the protection
        window, or :data:`EMPTY_FRESH_TAIL_ORDINAL` when no message
        qualifies (empty input, all-summaries, or
        ``fresh_tail_count <= 0``). The returned value is always
        usable in ``item.ordinal >= boundary`` / ``< boundary``
        comparisons.
    """
    # TS: if (!Number.isFinite(freshTailCount) || freshTailCount <= 0)
    #         return Infinity;
    # Python ints can't be NaN/Inf, but we still guard against negative or
    # zero counts — they should disable the fresh-tail entirely.
    if fresh_tail_count <= 0:
        return EMPTY_FRESH_TAIL_ORDINAL

    raw_messages = [item for item in resolved if item.is_message]
    if not raw_messages:
        # TS line 994: empty raw-message set → no fresh tail.
        return EMPTY_FRESH_TAIL_ORDINAL

    # TS line 997-1002: a finite, non-negative ``freshTailMaxTokens`` is
    # the active cap; anything else collapses to ``undefined`` (no cap).
    # ``int | None`` makes the finite/integer check trivial in Python.
    token_cap: int | None = (
        fresh_tail_max_tokens
        if fresh_tail_max_tokens is not None and fresh_tail_max_tokens >= 0
        else None
    )

    protected_count = 0
    protected_tokens = 0
    tail_start_ordinal: int = EMPTY_FRESH_TAIL_ORDINAL

    # Walk newest → oldest (reverse the message list).
    for item in reversed(raw_messages):
        if protected_count >= fresh_tail_count:
            break

        # TS lines 1018-1024: the newest item is always kept regardless
        # of the token cap (``protectedCount > 0`` gate). Subsequent
        # items respect the cap.
        would_exceed_budget = (
            protected_count > 0
            and token_cap is not None
            and protected_tokens + item.tokens > token_cap
        )
        if would_exceed_budget:
            break

        tail_start_ordinal = item.ordinal
        protected_count += 1
        protected_tokens += item.tokens

    return tail_start_ordinal


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
    this Python port ships the hydration surface (:meth:`resolve_items`
    + the two ``_resolve_*`` helpers) and the fresh-tail boundary
    (:func:`resolve_fresh_tail_ordinal`, module-level). The remaining
    steps (budget walk, orphan stripping, sanitize) land in 03-06..03-08.

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


# ---------------------------------------------------------------------------
# BM25-lite relevance scorer (TS 1037-1080)
# ---------------------------------------------------------------------------


# Pre-compiled tokenizer pattern. The TS source builds a fresh RegExp on each
# ``text.split(...)`` call; ``re.compile`` once at module load avoids paying
# the compilation cost on the ~O(evictable_count * average_text_length)
# tokenizer churn the budget walk inflicts during prompt-aware selection.
# The pattern is a verbatim copy of the TS literal ``/[^a-z0-9]+/``.
_TOKENIZE_SPLIT_RE: Final = re.compile(r"[^a-z0-9]+")


def tokenize_text(text: str) -> list[str]:
    """Tokenize ``text`` into lowercase alphanumeric terms.

    Mirrors TS ``tokenizeText`` (``assembler.ts`` 1037-1042):

    * Lowercase the entire input.
    * Split on the regex ``/[^a-z0-9]+/`` — every contiguous run of
      non-alphanumeric ASCII characters is a delimiter.
    * Filter out tokens with length ``<= 1`` (the TS source uses
      ``.length > 1``). This drops single-character noise like ``"i"``,
      ``"a"``, plus the empty-string artifact that ``re.split`` emits
      at the start/end of an all-delimiter string.

    ### Why the length filter

    BM25-lite's TF accumulator is normalized by ``len(item_tokens)``;
    flooding the item-token set with single-character noise would
    artificially deflate every per-term score. The TS source's
    ``len > 1`` filter excludes both stop-words like ``"a"``/``"i"`` AND
    the spurious empty string that ``"".split(re)`` and trailing
    delimiters produce. We match that exact behavior.

    ### Edge cases (parity with TS)

    * Empty string → ``[]`` (the regex split returns ``[""]``; the
      length filter strips it).
    * Whitespace-only / all-punctuation string → ``[]`` (same path).
    * Unicode letters (``"über"``, ``"café"``, CJK) → unsplittable by
      ASCII regex, so the whole input passes through. The TS source
      uses ``/[^a-z0-9]+/`` too — Unicode handling is identical
      (mostly: it preserves the whole non-ASCII run as one token).
      This is BM25-lite, not BM25 — accuracy on non-English text is
      knowingly degraded.
    * Numbers: ``"v3.1"`` → ``["v3", "1"]`` after length-filter →
      ``["v3"]``; ``"auth2"`` survives intact.

    Args:
        text: The text to tokenize.

    Returns:
        A list of lowercase alphanumeric tokens, length-1+
        characters each. Order matches the TS source (left-to-right).
    """
    # ``re.split`` on an empty string returns ``[""]`` (length 1). The
    # length-filter below strips it, matching TS ``"".split(re).filter`` →
    # ``[]``.
    return [t for t in _TOKENIZE_SPLIT_RE.split(text.lower()) if len(t) > 1]


def score_relevance(item_text: str, prompt: str) -> float:
    """BM25-lite relevance score for ``item_text`` against ``prompt``.

    Mirrors TS ``scoreRelevance`` (``assembler.ts`` 1049-1075). Computes a
    normalized term-frequency overlap score using a simplified BM25 model:

    1. Tokenize the prompt; return ``0.0`` if empty.
    2. Tokenize the item; return ``0.0`` if empty.
    3. Build a per-term frequency map of the item's tokens.
    4. For each **unique** prompt term (deduped), if it appears in the
       item, add ``item_freq[term] / len(item_tokens)`` to the score.

    ### Why "BM25-lite", not BM25

    Full BM25 multiplies TF by an IDF factor and applies saturation
    (``k1``, ``b`` hyper-parameters). LCM's TS source intentionally
    omits both — the eviction-mode use-case calls this on tiny corpora
    (typically <100 evictable items, often <20) where IDF would be
    statistically meaningless and saturation tuning is overkill. The
    ``// BM25-lite saturation skipped for simplicity`` comment in the TS
    source at line 1070 makes this explicit.

    ### Float-precision parity with TS

    TS's ``+=`` accumulator is identical to Python's; both languages
    store doubles (IEEE 754). Order of summation matters because
    floating-point addition is not associative. We preserve the TS
    iteration order (left-to-right over ``prompt_tokens``, skipping
    duplicates via a ``seen`` set) — this is the residual 90%-confidence
    risk noted in the spec, mitigated in tests via tolerance bounds
    rather than exact equality.

    Args:
        item_text: The item's plain-text rendering (
            :attr:`ResolvedItem.text`).
        prompt: The user's current query, used as the scoring corpus.

    Returns:
        A non-negative float. ``0.0`` when there is no overlap, the
        prompt has no searchable terms, or the item has no searchable
        terms. Higher values indicate stronger keyword overlap.

    Examples:
        >>> score_relevance("authentication login", "authentication")
        0.5
        >>> score_relevance("painting canvas", "authentication")
        0.0
        >>> score_relevance("", "anything") == score_relevance("anything", "")
        True
    """
    # Match TS dispatch order: prompt-tokenize first, then item-tokenize.
    # Both functions short-circuit on empty; reordering changes nothing
    # observable, but preserves comment alignment for review parity.
    prompt_tokens = tokenize_text(prompt)
    if len(prompt_tokens) == 0:
        return 0.0

    item_tokens = tokenize_text(item_text)
    if len(item_tokens) == 0:
        return 0.0

    # Build item TF map. ``dict.get(k, 0) + 1`` is the same pattern as
    # TS's ``map.set(k, (map.get(k) ?? 0) + 1)``.
    freq: dict[str, int] = {}
    for term in item_tokens:
        freq[term] = freq.get(term, 0) + 1

    item_term_count = len(item_tokens)
    seen: set[str] = set()
    score = 0.0
    for term in prompt_tokens:
        # The dedup step matches TS line 1066-1067 — repeating "foo foo
        # bar" in the prompt must score identically to "foo bar".
        if term in seen:
            continue
        seen.add(term)
        tf = freq.get(term, 0)
        if tf > 0:
            # Normalized TF: tf / item_term_count. The TS comment at
            # line 1070 notes "BM25-lite saturation skipped for
            # simplicity" — we mirror.
            score += tf / item_term_count
    return score


def has_searchable_prompt(prompt: str | None) -> bool:
    """Return ``True`` when ``prompt`` has at least one searchable token.

    Mirrors TS ``hasSearchablePrompt`` (``assembler.ts`` 1078-1080). The
    TS source uses a type-guard ``prompt is string`` predicate so the
    compiler narrows ``input.prompt`` to ``string`` inside the
    prompt-aware branch. Python has no equivalent narrowing; the
    branch's caller (:func:`budget_walk`) just guards the
    ``score_relevance`` call with this predicate.

    The predicate enforces three conditions:

    * ``prompt`` is a string (``None`` and non-string values fall back
      to chronological mode).
    * ``prompt`` is non-empty (empty string short-circuits).
    * ``tokenize_text(prompt)`` yields ≥ 1 token (whitespace-only and
      single-character-only strings fall back to chronological).

    Args:
        prompt: Optional user query string.

    Returns:
        ``True`` if BM25-lite scoring is safe to attempt;
        ``False`` otherwise (caller must fall to chronological mode).
    """
    if not isinstance(prompt, str):
        return False
    # Short-circuit empty + whitespace-only without tokenizing — saves a
    # regex split + lowercase pass on a hot path. The tokenizer length
    # check still catches edge cases like ``"!!!"`` which is non-empty
    # but yields zero tokens.
    if len(prompt) == 0:
        return False
    return len(tokenize_text(prompt)) > 0


# ---------------------------------------------------------------------------
# Token-budget walk — three selection modes (TS 1160-1230)
# ---------------------------------------------------------------------------


def budget_walk(
    evictable: Sequence[ResolvedItem],
    fresh_tail: Sequence[ResolvedItem],
    token_budget: int,
    prompt: str | None,
    prompt_aware_eviction: bool = True,
) -> tuple[list[ResolvedItem], SelectionMode]:
    """Select which evictable items to keep under ``token_budget``.

    Mirrors TS ``assemble`` step-4 (``assembler.ts`` 1160-1230). The fresh
    tail is **always** included by the caller — this function returns
    only the kept *evictable* items plus the mode label. The caller is
    expected to compose the final list as
    ``kept_evictable + list(fresh_tail)`` in chronological order.

    ### Three selection modes

    #### 1. ``"full-fit"`` (TS 1181-1184)

    When ``sum(item.tokens for item in evictable) <= remaining_budget``,
    everything fits — return all evictable items in input (ordinal)
    order. No scoring, no sorting, no walk.

    Gate: ``evictable_total_tokens <= remaining_budget``. Boundary
    case: equality is full-fit (the ``<=`` is load-bearing for the
    "exactly-fits" property the spec calls out).

    #### 2. ``"prompt-aware"`` (TS 1185-1209)

    When ``prompt_aware_eviction`` is truthy AND
    :func:`has_searchable_prompt` returns ``True`` AND the full-fit
    gate failed, score each evictable item by relevance and greedy-fill
    the budget:

    1. For each evictable, compute ``score_relevance(item.text, prompt)``
       and pair with its original index (``idx``). Higher ``idx`` =
       newer, used as recency tiebreaker.
    2. Sort by ``(-score, -idx)`` — highest score first; on ties, newer
       wins (TS ``b.score - a.score || b.idx - a.idx``).
    3. Walk sorted list; for each item, **append if it fits**
       (``accum + item.tokens <= remaining_budget``). Note this is a
       **skip-and-continue** walk — unlike chronological, prompt-aware
       does NOT bail on the first non-fit. A small high-score item
       AFTER a large high-score item can still be picked up.
    4. Re-sort kept items by ``item.ordinal`` to restore chronological
       order before the caller appends the fresh tail.

    #### 3. ``"chronological"`` (TS 1210-1230, default fallback)

    The fallback when prompt is missing/whitespace OR
    ``prompt_aware_eviction is False``. Walk evictable from
    **newest → oldest**, accumulating tokens. On the **first**
    non-fitting item, **stop entirely** — drop that item AND all older
    items. This is a *monotonic* walk: unlike prompt-aware, the
    chronological mode cannot "skip and continue" past a non-fit.

    Reverse the kept list to restore chronological (oldest-first) order
    before the caller appends the fresh tail.

    The strict-stop semantic preserves conversational coherence — once
    the budget breaks the timeline, all older context is gone (no
    Frankenstein-history gap). The TS comment at line 1223 spells this
    out: ``"Once an item doesn't fit we stop — all older items are
    also dropped"``.

    ### Tail-tokens & remaining budget (TS 1162-1170)

    The fresh tail is **always included**, even if its sum exceeds the
    full budget. The walk operates on
    ``remaining_budget = max(0, token_budget - tail_tokens)``. When
    ``tail_tokens >= token_budget``, ``remaining_budget`` becomes 0 and
    the only mode that emits a non-empty kept list is ``full-fit`` (and
    only when ``evictable_total_tokens == 0``).

    ### Edge cases the spec covers

    * **Empty evictable + non-empty fresh tail** → mode is
      ``"full-fit"`` (the gate ``0 <= remaining_budget`` is always
      true), kept list is ``[]``.
    * **``token_budget = 0``** → ``remaining_budget = max(0, -tail) = 0``
      → if any evictable has tokens, the mode is whichever the gate
      hits (full-fit only if all items are zero-token). When the
      gate fails, prompt-aware/chronological emit ``[]``.
    * **Fresh tail alone exceeds budget** → see above (zero remaining).
    * **Empty prompt** → fall to chronological.
    * **``prompt_aware_eviction=False`` with non-empty prompt** → fall
      to chronological (the AND-guard at TS 1185 short-circuits on
      ``promptAwareEviction !== false``).

    ### Wave-N provenance

    The TS source at this line range (1162-1230) carries **no**
    explicit ``Wave-N`` audit-fix marker — the budget-walk algorithm is
    one of the few sections that survived all 12 audit waves without a
    scar-tissue patch. (The fresh-tail computation at 988 carries
    Wave-4; the budget walk does not.) Per ADR-029, we omit a
    ``# LCM Wave-N`` comment from this function.

    ### Performance

    All three branches are O(n) on item count, with the prompt-aware
    branch contributing one O(n log n) sort. ``list.append`` is
    amortised O(1); the spec's "no quadratic patterns" AC is satisfied
    by avoiding ``selected = selected + [item]`` (which copies, costing
    O(n²) cumulative).

    Args:
        evictable: Items with ``ordinal < fresh_tail_ordinal``.
            Must be sorted by ascending ordinal (the caller is
            :meth:`ContextAssembler.assemble`'s split step at TS 1157,
            which filters a pre-sorted resolved list).
        fresh_tail: Items with ``ordinal >= fresh_tail_ordinal``.
            Used only to compute ``tail_tokens``; never returned.
        token_budget: The total token budget from
            ``AssembleContextInput.tokenBudget`` (TS 1103).
        prompt: Optional user query for prompt-aware mode.
        prompt_aware_eviction: When ``False``, forces chronological
            mode even if the prompt is searchable. Mirrors TS
            ``promptAwareEviction`` default ``true``; the AND-guard at
            TS 1185 short-circuits on ``!== false``, so any truthy
            value enables prompt-aware.

    Returns:
        A pair ``(kept_evictable, mode)`` where ``kept_evictable`` is
        a list of :class:`ResolvedItem` in ordinal-ascending order
        (chronological), and ``mode`` is the
        :data:`SelectionMode` label that describes which branch
        executed. The caller appends ``list(fresh_tail)`` to
        ``kept_evictable`` to build the final selected list.

        The token-budget invariant
        ``sum(item.tokens for item in kept_evictable) <= remaining_budget``
        holds for every mode. (Note this is *remaining_budget*, not
        ``token_budget`` — the fresh tail is allowed to overflow.)
    """
    # Compute tail tokens. ``sum(..., 0)`` covers the empty-iterable case
    # without raising. The TS source uses an in-place loop (line 1163);
    # ``sum()`` over a generator is the idiomatic Python equivalent and
    # avoids materializing an intermediate list.
    tail_tokens = sum(item.tokens for item in fresh_tail)

    # ``max(0, ...)`` matches TS line 1170 — when the fresh tail alone
    # busts the budget, remaining is clamped to 0, not allowed to go
    # negative (which would let large negative budgets accept arbitrary
    # items in the gate check below).
    remaining_budget = max(0, token_budget - tail_tokens)

    # Compute evictable token total once. The TS source uses
    # ``Array.prototype.reduce`` at line 1178; ``sum(generator)`` is
    # equivalent and avoids the materialization cost of a list comp.
    evictable_total_tokens = sum(item.tokens for item in evictable)

    # ── Mode 1: full-fit (TS 1181-1184) ────────────────────────────────
    # The ``<=`` is load-bearing — equality is still full-fit (everything
    # exactly fits, no eviction needed).
    if evictable_total_tokens <= remaining_budget:
        # ``list(evictable)`` materializes a copy so the caller's
        # downstream mutations don't reach into the input. The TS source
        # uses ``selected.push(...evictable)`` which has the same
        # semantics (spread copies the array elements).
        return list(evictable), "full-fit"

    # ── Mode 2: prompt-aware (TS 1185-1209) ────────────────────────────
    # Gate: ``promptAwareEviction !== false && hasSearchablePrompt``.
    # TS's ``!== false`` short-circuits on any truthy value; Python's
    # ``is False`` check is the precise translation. Using a plain
    # ``not prompt_aware_eviction`` would erroneously fall through on
    # truthy non-``True`` values (e.g. ``1``, non-empty strings) which
    # the TS source would have honored.
    if prompt_aware_eviction is not False and has_searchable_prompt(prompt):
        # ``prompt`` is narrowed to ``str`` by ``has_searchable_prompt``
        # in practice — Python's type checker doesn't see the narrowing,
        # so we cast via a local rebind for ``ty``'s benefit.
        assert prompt is not None  # narrowed by has_searchable_prompt
        return _budget_walk_prompt_aware(evictable, remaining_budget, prompt)

    # ── Mode 3: chronological (TS 1210-1230, fallback) ─────────────────
    return _budget_walk_chronological(evictable, remaining_budget)


def _budget_walk_prompt_aware(
    evictable: Sequence[ResolvedItem],
    remaining_budget: int,
    prompt: str,
) -> tuple[list[ResolvedItem], SelectionMode]:
    """Prompt-aware greedy-fill (BM25-lite scoring + token cap).

    Mirrors TS ``assembler.ts`` 1186-1209. Extracted to a helper to
    keep :func:`budget_walk`'s top-level dispatch readable. Returns the
    same ``(kept, mode)`` shape as the parent.

    ### Algorithm

    1. Score every evictable item once. The TS source builds an array
       of ``{item, score, idx}`` triples (line 1190-1194); Python's
       tuple lets us pack the same data with less overhead.
    2. Sort by ``(-score, -idx)`` so the highest-scoring items come
       first, with newer items winning ties. The recency tiebreaker
       is critical — when two items match the prompt equally, we
       prefer the more recent one because it's more likely to
       reflect the current conversational context.
    3. Greedy-fill ``remaining_budget``: walk the sorted list, append
       each item that fits, **skip-and-continue** on items that don't
       fit. This is the load-bearing difference from chronological
       mode — a small relevant item can still slip in after a large
       relevant item didn't.
    4. Re-sort kept items by ``ordinal`` so the output is in
       chronological order (the caller appends fresh tail without
       re-sorting).

    ### Recency tiebreaker rationale

    The TS source uses original-index (``idx``) as the recency proxy,
    not ``item.ordinal``. The two are equivalent when ``evictable`` is
    sorted by ordinal (which it always is, per the caller's
    contract) — index 0 = oldest, index ``len-1`` = newest. We use
    ``enumerate()`` to match.

    Args:
        evictable: Sorted-by-ordinal evictable items.
        remaining_budget: Budget left after fresh-tail allocation.
        prompt: Non-empty, searchable user query.

    Returns:
        ``(kept_evictable, "prompt-aware")``.
    """
    # Pre-score all items in one pass — ``score_relevance`` is pure, no
    # observable side effect from doing this eagerly. The (-score, -idx)
    # double-negation sort key is the idiomatic Python translation of
    # TS's ``b.score - a.score || b.idx - a.idx`` descending sort.
    scored: list[tuple[float, int, ResolvedItem]] = [
        (score_relevance(item.text, prompt), idx, item) for idx, item in enumerate(evictable)
    ]
    # Sort in descending order. Note: we sort by ``(-score, -idx)``
    # rather than passing ``reverse=True`` because we want a *stable*
    # descending sort by score AND a descending tiebreaker by idx.
    # Python's ``sorted(..., reverse=True)`` would flip BOTH keys; the
    # negation form gives us the asymmetric direction TS expects.
    scored.sort(key=lambda triple: (-triple[0], -triple[1]))

    kept: list[ResolvedItem] = []
    accum = 0
    for _score, _idx, item in scored:
        # TS line 1201: ``if (accum + item.tokens <= remainingBudget)``.
        # The ``<=`` is load-bearing for the exact-fit boundary.
        if accum + item.tokens <= remaining_budget:
            kept.append(item)
            accum += item.tokens
        # else: SKIP (not break) — this is the difference vs chronological.

    # Restore chronological order. ``list.sort`` is in-place + O(n log n);
    # the TS source uses the same ``kept.sort(a.ordinal - b.ordinal)``
    # pattern at line 1207.
    kept.sort(key=lambda item: item.ordinal)
    return kept, "prompt-aware"


def _budget_walk_chronological(
    evictable: Sequence[ResolvedItem],
    remaining_budget: int,
) -> tuple[list[ResolvedItem], SelectionMode]:
    """Chronological newest-first walk with strict-stop semantics.

    Mirrors TS ``assembler.ts`` 1211-1229. Extracted to a helper to
    parallel :func:`_budget_walk_prompt_aware`. Returns the same
    ``(kept, mode)`` shape.

    ### Algorithm

    Walk the evictable list from index ``len-1`` (newest) down to
    ``0`` (oldest), appending each item that fits. **On the first
    non-fit, break** — drop that item and every older item too.
    Reverse the kept list to restore chronological (oldest-first)
    order.

    ### Strict-stop vs skip-and-continue

    This is the load-bearing distinction from prompt-aware. The TS
    comment at line 1223 ``"Once an item doesn't fit we stop — all
    older items are also dropped"`` makes this explicit. The motivation
    is conversational coherence: if the budget tears the timeline at
    an older boundary, the gap between the kept fresh tail and the
    kept oldest item must be contiguous. A "skip and continue" walk
    could leave a 5-message-old gap and then resume picking up
    20-message-old messages, which confuses the model.

    Args:
        evictable: Sorted-by-ordinal evictable items.
        remaining_budget: Budget left after fresh-tail allocation.

    Returns:
        ``(kept_evictable, "chronological")``.
    """
    # Walk newest → oldest. The TS source uses a numeric reverse-index
    # loop (``for (let i = evictable.length - 1; i >= 0; i--)``);
    # ``reversed(evictable)`` is the idiomatic Python equivalent.
    kept: list[ResolvedItem] = []
    accum = 0
    for item in reversed(evictable):
        if accum + item.tokens <= remaining_budget:
            kept.append(item)
            accum += item.tokens
        else:
            # Strict-stop: the TS comment at 1223 spells this out.
            # We break, NOT continue. Skipping past a too-big item would
            # let a small old item sneak in and create a discontiguous
            # history.
            break

    # ``list.reverse`` is in-place + O(n); the TS source uses
    # ``kept.reverse()`` at line 1227. The reversal restores
    # chronological (oldest-first) order from our newest-first walk.
    kept.reverse()
    return kept, "chronological"
