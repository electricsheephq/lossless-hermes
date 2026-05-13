"""Context assembler — item resolution + block reconstruction (Epic 03).

Port of ``lossless-claw/src/assembler.ts`` (LCM commit ``1f07fbd``, branch
``pr-613``). This module is the **lowest-level reading layer** of the
assembler: it hydrates ordered :class:`ContextItemRecord` rows into
:class:`ResolvedItem` instances carrying a runtime-shaped ``message``
plus DB metadata, plain text rendering, and an :func:`estimate_tokens`
budget figure.

### Issue 03-04 + 03-05 + 03-06 + 03-07a scope

This file covers ``resolveItems`` (TS 1374-1466) plus every helper the
function transitively depends on, the fresh-tail boundary calculation
(``resolveFreshTailOrdinal``, TS 983-1032), the three-mode token-budget
walk (``assemble`` step-4, TS 1160-1230), AND
``filterNonFreshAssistantToolCalls`` (TS 687-778). The companion
``sanitizeToolUseResultPairing`` in ``transcript_repair.py``
(03-07b) and the orchestration that calls both passes (03-08) land
in subsequent issues.

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

import hashlib
import json
import logging
import re
import sys
from dataclasses import dataclass, field
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
from lossless_hermes.transcript_repair import sanitize_tool_use_result_pairing

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
    "AssemblySegment",
    "TOOL_CALL_TYPES",
    "FilteredEntry",
    "FilteredToolCallsResult",
    "extract_tool_call_id_from_block",
    "extract_tool_result_id_from_message",
    "filter_non_fresh_assistant_tool_calls",
    # 03-08 orchestration surface.
    "AssembleInput",
    "AssembleResult",
    "AssembleStats",
    "AssembleDebug",
    "AssemblyOverflowDiagnostics",
    "AssemblyDuplicateCluster",
    "AssemblyOverflowContributor",
    "DuplicateClusterKind",
]


_LOG = logging.getLogger(__name__)


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

    def assemble(self, inp: AssembleInput) -> AssembleResult:
        """Build a token-budgeted message list ready for the provider.

        Top-level orchestrator that chains the Epic 03 assembler stages:

        1. Read context items (``summary_store.get_context_items``).
        2. Resolve into :class:`ResolvedItem` (:meth:`resolve_items`).
        3. Compute fresh-tail boundary
           (:func:`resolve_fresh_tail_ordinal`).
        4. Compute orphan-stripping ordinal (caller override or
           fresh-tail).
        5. Index all tool-result ordinals by id.
        6. Split evictable / fresh tail at the boundary.
        7. **#628 stub-tier** (deferred per ADR-030): warn-and-skip.
        8. Token-budget walk (:func:`budget_walk`).
        9. Append fresh tail → ``selected``.
        10. Build overflow diagnostics
            (:func:`_build_overflow_diagnostics`).
        11. Filter non-fresh assistant tool-calls
            (:func:`filter_non_fresh_assistant_tool_calls`).
        12. Normalize assistant content (string → array, drop blank
            blocks).
        13. Clean empty assistant turns (drop empty / blank /
            thinking-only).
        14. Pre-sanitize hashing for debug (SHA-256 of evictable,
            fresh_tail, combined).
        15. Sanitize tool-use ↔ tool-result pairing
            (:func:`sanitize_tool_use_result_pairing`).
        16. Return :class:`AssembleResult`.

        Mirrors TS ``ContextAssembler.assemble`` (``assembler.ts``
        1102-1332).

        ### #628 stub-tier deferral

        When :attr:`AssembleInput.stub_large_tool_payloads` is ``True``,
        the orchestration logs a ``warning`` per ADR-030 and runs the
        remainder of the pipeline unchanged. The actual stub
        substitution lands in v0.2.0.

        ### Empty conversation short-circuit

        TS line 1109-1115: when ``get_context_items`` returns an empty
        list, the method returns an empty :class:`AssembleResult` with
        zero counts. The caller's safe-fallback path handles
        downstream behavior (e.g. returning the live messages instead).

        ### Debug envelope

        :attr:`AssembleResult.debug` is populated only when
        :attr:`AssembleInput.capture_debug` is ``True``. The TS source
        always emits the debug envelope; the Python port gates it
        because the three SHA-256 passes (TS 1295-1300) are non-trivial
        on long conversations and the engine-side hot path doesn't
        need them every turn.

        ### estimated_tokens calculation

        Mirrors TS ``estimatedTokens = evictableTokens + tailTokens``
        (line 1235). The post-budget-walk total — does NOT account for
        any block / message drops applied by stages 11-13 (those passes
        only remove content and never inflate the token count, so the
        figure is an upper bound on what the provider actually sees).

        Args:
            inp: :class:`AssembleInput` carrying conversation id,
                budget, and selection knobs.

        Returns:
            Populated :class:`AssembleResult`. The ``messages`` field
            is post-sanitize ready for the provider; ``estimated_tokens``
            is the budget-walk total; ``stats`` is over the
            pre-selection resolved set; ``debug`` is populated when
            requested.
        """
        # Step 1 — context items (TS 1107).
        context_items = self._summary_store.get_context_items(inp.conversation_id)
        if len(context_items) == 0:
            # TS 1109-1115 — empty short-circuit. The caller's safe-fallback
            # path takes over downstream.
            return AssembleResult(
                messages=[],
                estimated_tokens=0,
                stats=AssembleStats(
                    raw_message_count=0,
                    summary_count=0,
                    total_context_items=0,
                ),
                debug=None,
            )

        # Step 2 — resolve (TS 1118).
        resolved = self.resolve_items(context_items)

        # Stats over the full pre-selection set (TS 1121-1129).
        raw_message_count = 0
        summary_count = 0
        for item in resolved:
            if item.is_message:
                raw_message_count += 1
            else:
                summary_count += 1
        stats = AssembleStats(
            raw_message_count=raw_message_count,
            summary_count=summary_count,
            total_context_items=len(resolved),
        )

        # Step 3 — fresh-tail boundary (TS 1132-1136).
        fresh_tail_ordinal = resolve_fresh_tail_ordinal(
            resolved,
            inp.fresh_tail_count,
            inp.fresh_tail_max_tokens,
        )

        # Step 4 — orphan-stripping ordinal (TS 1137-1142). When the caller
        # provides a stable ordinal (e.g. from
        # ``LCMEngine._stable_orphan_stripping_ordinals_by_conversation``),
        # use it; otherwise fall back to the fresh-tail boundary. The TS
        # source allows any non-negative finite number; the Python port
        # accepts non-negative ints (``None`` → fallback).
        if inp.orphan_stripping_ordinal is not None and inp.orphan_stripping_ordinal >= 0:
            orphan_stripping_ordinal = inp.orphan_stripping_ordinal
        else:
            orphan_stripping_ordinal = fresh_tail_ordinal

        # Step 5 — index all tool-result ordinals by id (TS 1143-1155).
        # Build the map over the *resolved* set so the orphan-strip pass
        # has visibility into whether any version of a tool-result
        # surfaced anywhere in the conversation, even if it didn't make
        # the budget-walk cut.
        all_tool_result_ordinals_by_id: dict[str, list[int]] = {}
        for item in resolved:
            tr_id = extract_tool_result_id_from_message(item.message)
            if tr_id is None:
                continue
            all_tool_result_ordinals_by_id.setdefault(tr_id, []).append(item.ordinal)

        # Step 6 — split (TS 1156-1158).
        evictable = [item for item in resolved if item.ordinal < fresh_tail_ordinal]
        fresh_tail = [item for item in resolved if item.ordinal >= fresh_tail_ordinal]

        # Step 7 — #628 stub-tier (deferred per ADR-030).
        # Per spec AC: "logs a warning and runs the rest of the pipeline
        # normally with no stub substitution". v0.2.0 will replace this
        # branch with the real ``apply_stub_substitution(evictable)``
        # call. The TS source-of-truth for v0.2.0 lives on lossless-claw
        # main @ 13780e9.
        if inp.stub_large_tool_payloads:
            _LOG.warning(
                "stub-tier deferred to v0.2.0 per ADR-030 — running pipeline without substitution"
            )

        # Step 8 — token-budget walk (TS 1160-1230). Returns
        # ``(kept_evictable, mode)``. ``budget_walk`` already encodes the
        # three selection modes; we only need to capture the mode for the
        # debug envelope.
        kept_evictable, selection_mode = budget_walk(
            evictable,
            fresh_tail,
            inp.token_budget,
            inp.prompt,
            inp.prompt_aware_eviction,
        )
        evictable_kept_tokens = sum(item.tokens for item in kept_evictable)
        tail_tokens = sum(item.tokens for item in fresh_tail)

        # Step 9 — append fresh tail (TS 1233).
        selected: list[ResolvedItem] = [*kept_evictable, *fresh_tail]

        # estimatedTokens = evictableTokens + tailTokens (TS 1235).
        estimated_tokens = evictable_kept_tokens + tail_tokens

        # Step 10 — overflow diagnostics (TS 1236, only when debug is on).
        overflow_diagnostics: AssemblyOverflowDiagnostics | None = None
        if inp.capture_debug:
            overflow_diagnostics = _build_overflow_diagnostics(
                resolved,
                selected,
                inp.token_budget,
            )

        # Step 11 — filter non-fresh assistant tool-calls (TS 1244-1248).
        fresh_tail_ordinals_set = {item.ordinal for item in fresh_tail}
        filtered = filter_non_fresh_assistant_tool_calls(
            selected,
            fresh_tail_ordinals_set,
            orphan_stripping_ordinal,
            all_tool_result_ordinals_by_id,
        )

        # Steps 12 + 13 — normalize content + clean empty/thinking-only
        # assistant turns (TS 1250-1293).
        cleaned_entries, _normalized_entries = _normalize_and_clean_assistant_content(
            filtered.entries,
        )

        # ``cleaned`` is the projected message list (TS 1294).
        cleaned: list[Mapping[str, Any]] = [entry.message for entry in cleaned_entries]

        # Step 14 — pre-sanitize hashing (TS 1295-1300). Compute the
        # three SHA-256 prefixes only when the debug envelope was
        # requested — the hashing cost is non-trivial on long
        # conversations and most assemble() calls don't need it.
        pre_sanitize_evictable_messages: list[Mapping[str, Any]] = []
        pre_sanitize_fresh_tail_messages: list[Mapping[str, Any]] = []
        if inp.capture_debug:
            for entry in cleaned_entries:
                if entry.segment == "evictable":
                    pre_sanitize_evictable_messages.append(entry.message)
                else:
                    pre_sanitize_fresh_tail_messages.append(entry.message)

        # Step 15 — sanitize tool-use ↔ tool-result pairing (TS 1301).
        # ``sanitize_tool_use_result_pairing`` returns the original
        # sequence when no repair was needed (identity preserved) — we
        # materialise to a list either way so callers can mutate the
        # result without affecting the cleaned cache.
        repaired_seq = sanitize_tool_use_result_pairing(cleaned)
        repaired: list[dict[str, Any]] = [
            dict(m) if isinstance(m, Mapping) else m for m in repaired_seq
        ]

        # Step 16 — return (TS 1302-1331).
        debug: AssembleDebug | None = None
        if inp.capture_debug:
            # The overflow_diagnostics field is guaranteed non-None when
            # capture_debug is True (built above).
            assert overflow_diagnostics is not None  # for ty narrowing
            remaining_budget = max(0, inp.token_budget - tail_tokens)
            evictable_total_tokens = sum(item.tokens for item in evictable)
            debug = AssembleDebug(
                fresh_tail_ordinal=fresh_tail_ordinal,
                orphan_stripping_ordinal=orphan_stripping_ordinal,
                base_fresh_tail_count=len(fresh_tail),
                fresh_tail_count=len(fresh_tail),
                tail_tokens=tail_tokens,
                remaining_budget=remaining_budget,
                evictable_total_tokens=evictable_total_tokens,
                selection_mode=selection_mode,
                # v0.2.0 #628 stub-tier counters — always 0 / [] in v0.1.0
                # per ADR-030.
                promoted_tool_result_count=0,
                promoted_ordinals=[],
                removed_tool_use_block_count=filtered.removed_tool_use_block_count,
                touched_assistant_message_count=filtered.touched_assistant_message_count,
                pre_sanitize_evictable_count=len(pre_sanitize_evictable_messages),
                pre_sanitize_fresh_tail_count=len(pre_sanitize_fresh_tail_messages),
                pre_sanitize_evictable_hash=_hash_messages(pre_sanitize_evictable_messages),
                pre_sanitize_fresh_tail_hash=_hash_messages(pre_sanitize_fresh_tail_messages),
                pre_sanitize_messages_hash=_hash_messages(cleaned),
                final_messages_hash=_hash_messages(repaired),
                overflow_diagnostics=overflow_diagnostics,
            )

        return AssembleResult(
            messages=repaired,
            estimated_tokens=estimated_tokens,
            stats=stats,
            debug=debug,
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


# ---------------------------------------------------------------------------
# Orphan tool-use stripping (TS 687-778)
# ---------------------------------------------------------------------------


# Segment label attached to each surviving entry by
# :func:`filter_non_fresh_assistant_tool_calls`. ``"freshTail"`` for items
# whose ordinal is in the fresh-tail set, ``"evictable"`` otherwise. The
# string literals are part of the debug envelope (TS ``preSanitize*`` hash
# fields, see ``assembler.ts`` 1295-1300) — porting them verbatim
# preserves wire compatibility with the TS debug-stream consumers.
AssemblySegment = Literal["freshTail", "evictable"]


# Set of content-block ``type`` strings that represent a tool call across
# every provider the assembler supports. Sourced verbatim from TS
# ``TOOL_CALL_TYPES`` (``assembler.ts`` 80-87). The mixed-case variants
# (``"toolCall"`` / ``"toolUse"``) cover OpenClaw's legacy normalized
# shapes; the snake_case + kebab variants cover Anthropic, OpenAI Chat,
# and OpenAI Responses. The set lookup is O(1) per block — the dominant
# cost in the inner loop is the ``content`` array length, not the type
# match.
TOOL_CALL_TYPES: Final[frozenset[str]] = frozenset({
    "toolCall",
    "toolUse",
    "tool_use",
    "tool-use",
    "functionCall",
    "function_call",
})


def extract_tool_call_id_from_block(block: Mapping[str, Any]) -> str | None:
    """Pick the tool_call_id out of a content block.

    Mirrors TS ``extractToolCallId`` (``assembler.ts`` 642-650). Anthropic
    ``tool_use`` blocks key the id as ``"id"``; OpenAI Responses
    ``function_call`` blocks key it as ``"call_id"``. The TS source
    checks ``id`` first; we preserve that order so a block carrying
    both keys (which should not happen in practice but is possible in
    test fixtures) resolves to the Anthropic key.

    Args:
        block: Mapping representing a single content block. Only the
            ``id`` and ``call_id`` keys are inspected; all other keys
            are ignored.

    Returns:
        The string id when one of the keys carries a non-empty string,
        else ``None``. Empty strings are treated as absent (TS
        ``block.id.length > 0`` guard at line 643).
    """
    id_value = block.get("id")
    if isinstance(id_value, str) and len(id_value) > 0:
        return id_value
    call_id_value = block.get("call_id")
    if isinstance(call_id_value, str) and len(call_id_value) > 0:
        return call_id_value
    return None


def extract_tool_result_id_from_message(message: Mapping[str, Any]) -> str | None:
    """Pick the tool-result id out of a runtime message dict.

    Mirrors TS ``extractToolResultIdFromMessage`` (``assembler.ts``
    674-685). The runtime message carries the id on the top-level
    ``toolCallId`` field (set by
    :meth:`ContextAssembler._resolve_message_item` when the source role
    is ``"toolResult"``) — NOT inside a ``content`` block. Some legacy
    OpenClaw paths use ``toolUseId`` as a synonym; we check both.

    Args:
        message: Runtime message dict. Only the top-level
            ``toolCallId`` and ``toolUseId`` keys are inspected.

    Returns:
        The string id when one of the keys carries a non-empty string,
        else ``None``. Empty strings are treated as absent.
    """
    tool_call_id = message.get("toolCallId")
    if isinstance(tool_call_id, str) and len(tool_call_id) > 0:
        return tool_call_id
    tool_use_id = message.get("toolUseId")
    if isinstance(tool_use_id, str) and len(tool_use_id) > 0:
        return tool_use_id
    return None


@dataclass(slots=True)
class FilteredEntry:
    """A surviving message paired with its assembly segment label.

    Mirrors the inline anonymous-object shape TS uses at
    ``assembler.ts`` 693 (``Array<{ message: AgentMessage; segment:
    AssemblySegment }>``). Promoted to a named dataclass on the Python
    side so downstream code can pattern-match on field names rather
    than tuple positions.

    Attributes:
        message: The runtime message dict (post-filter). When the
            assistant message survived with a reduced ``content``
            array, this is a freshly built dict — the input message
            is never mutated in place.
        segment: ``"freshTail"`` when the source item's ordinal was in
            the fresh-tail set, ``"evictable"`` otherwise. Used by the
            03-08 orchestration step to compute ``preSanitize*`` debug
            hashes (TS 1295-1300).
    """

    message: dict[str, Any]
    segment: AssemblySegment


@dataclass(slots=True)
class FilteredToolCallsResult:
    """Output of :func:`filter_non_fresh_assistant_tool_calls`.

    Mirrors TS's inline return type at ``assembler.ts`` 692-696:
    ``{ entries, removedToolUseBlockCount, touchedAssistantMessageCount }``.
    Promoted to a dataclass for the same reasons as :class:`FilteredEntry`.

    Attributes:
        entries: Surviving entries in input (ordinal-ascending) order.
            Each carries the message dict plus its segment label.
        removed_tool_use_block_count: Number of assistant messages
            that had at least one tool-use block removed. **NOTE**:
            this is a per-message counter, not a per-block counter —
            an assistant message with three orphan blocks removed
            counts as 1, not 3. This matches the TS source verbatim
            (``assembler.ts`` 754-764 increments at message granularity).
        touched_assistant_message_count: Number of assistant messages
            that were either filtered down to a smaller content array
            or dropped entirely. Equal to
            ``removed_tool_use_block_count`` per the TS source — both
            counters increment together on every touched message.
    """

    entries: list[FilteredEntry]
    removed_tool_use_block_count: int
    touched_assistant_message_count: int


def filter_non_fresh_assistant_tool_calls(
    items: Sequence[ResolvedItem],
    fresh_tail_ordinals: set[int],
    orphan_stripping_ordinal: int,
    all_tool_result_ordinals_by_id: Mapping[str, Sequence[int]],
) -> FilteredToolCallsResult:
    """Strip assistant tool-use blocks whose tool_result was evicted.

    Mirrors TS ``filterNonFreshAssistantToolCalls`` (``assembler.ts``
    687-778). Run between budget-walk (Step 03-06) and the final
    sanitize pass (Step 03-07b). Removes assistant content blocks of
    type ``tool_use`` (and provider variants in :data:`TOOL_CALL_TYPES`)
    when no matching ``tool_result`` survives the budget walk —
    Anthropic-compatible APIs reject such "orphan" tool-call blocks
    with a 400 error, so this pass is load-bearing for API call
    correctness.

    ### Algorithm (TS 696-772)

    1. **Build the selected-tool-result index.** Walk every input
       ``item``. If the item is a tool_result (top-level
       ``toolCallId``/``toolUseId``), append its ordinal to the list
       keyed by that id. The result is
       ``selected_tool_result_ordinals_by_id: dict[str, list[int]]``
       — one list per id, ordinals in insertion order (which equals
       ascending order when the input is sorted by ordinal, the caller's
       contract).

    2. **Filter each assistant message.** For every item:

       * Non-assistant items pass through unchanged with their segment
         label.
       * Assistant items whose ``content`` is not a list (e.g. plain
         string, or an unexpected shape) pass through unchanged. The
         TS guard at line 720 protects against ``content: undefined``
         and ``content: "string"`` — both common in OpenAI-Chat shapes.
       * For each block in the content array, decide keep-or-strip:

         a. Non-dict or missing ``type`` → KEEP (not our concern;
            could be ``text``, ``image``, ``thinking``, etc.).
         b. ``type`` not in :data:`TOOL_CALL_TYPES` → KEEP.
         c. No extractable tool_call_id (neither ``id`` nor ``call_id``
            present) → KEEP. The TS source returns ``true`` (keep)
            here because there's nothing to match against — orphan
            detection requires an id.
         d. A selected tool_result with the same id exists at an
            ordinal **strictly greater** than this item's ordinal
            (``> item.ordinal``, not ``>=``) → KEEP. The strict
            inequality is load-bearing: the tool_result must come
            AFTER the tool_use in the timeline. A tool_result with
            the same ordinal as the tool_use would be a malformed
            input (Anthropic emits tool_result on the NEXT turn) and
            we treat it as no-match.
         e. Item ordinal is **less than** the orphan-stripping
            boundary → STRIP. Items below the boundary are aggressively
            pruned because the boundary advances past them as the
            conversation grows; cache stability for those items is
            no longer load-bearing.
         f. The id has **NO** resolved tool_result anywhere
            (``all_tool_result_ordinals_by_id`` lookup returns absent
            or empty) → KEEP. The tool_use is not a stale pairing —
            there's no evidence a result ever existed, so we preserve
            the assistant's intent and let the sanitize pass
            (03-07b) handle it.
         g. The id HAS resolved tool_result evidence somewhere → STRIP.
            A subsequent ``assemble()`` call might include the result
            at a different ordinal; emitting this tool_use now would
            create a stale-pairing risk across cache reuses.

       NOTE the inversion in branches (f) vs (g): the issue spec text
       describes this branch in the OPPOSITE direction (presence-of-
       evidence → KEEP, absence → STRIP). The TS source at line 747
       (``if (!(...?.length)) return true``) reads the other way: the
       ``!`` inverts the length check, so absence-of-evidence → KEEP.
       The TS source is canonical per project policy.

    3. **Decide message-level disposition** (TS 754-771):

       * If the filtered content is empty → DROP the entire message
         (do not push). Increment both counters.
       * If nothing was filtered → push the **original** message
         (no copy) with its segment.
       * Otherwise (partial-strip with surviving content) → push a
         **new** message dict with the same fields except for
         ``content``. Increment both counters.

    ### Cache-marginal protection (load-bearing)

    The two guards at TS 743 and 747 implement the cache-stability
    strategy. The boundary check (``item.ordinal <
    orphan_stripping_ordinal``) is the strip-aggressively threshold:
    items below this ordinal are old enough that the engine no
    longer protects them across ``assemble()`` calls. The boundary
    is pinned by the engine shell's
    :attr:`LcmContextEngine._stable_orphan_stripping_ordinals_by_conversation`
    map (landing in 03-08).

    The second guard at TS 747 — ``if (!(all_ordinals.length)) keep``
    — is a sanitize-vs-strip disposition for items AT or ABOVE the
    boundary that didn't find a selected result. If the id has zero
    resolved-anywhere evidence, the tool_use is genuinely orphaned
    (the result was never emitted, or was lost from history); we
    KEEP the block and let the downstream sanitize pass handle it.
    If the id HAS resolved evidence somewhere in the full
    conversation history, the result might surface in a later
    assembly call at a different ordinal — emitting the tool_use
    now creates a stale-pairing risk across cache reuses, so we
    STRIP it.

    ### Provider-agnostic block detection

    The :data:`TOOL_CALL_TYPES` constant covers six provider variants
    (Anthropic ``tool_use``, OpenAI Chat ``functionCall`` /
    ``function_call``, OpenAI Responses ``function_call``, OpenClaw
    legacy ``toolCall`` / ``toolUse``, kebab ``tool-use``). The id
    extraction logic in :func:`extract_tool_call_id_from_block` covers
    both Anthropic-style ``id`` and OpenAI-Responses-style ``call_id``.
    See ``docs/porting-guides/assembler-compaction.md`` §"Provider
    variants" for the full matrix.

    ### Wave-N provenance

    The TS source at lines 687-778 carries **no** explicit ``Wave-N``
    audit-fix marker — the orphan-stripping algorithm is one of the
    sections that survived all 12 audit waves without a scar-tissue
    patch. (The cache-marginal protections at 743 and 747 are part of
    the original design, not a Wave-N retro-fix.) Per ADR-029, we
    omit a ``# LCM Wave-N`` comment from this function.

    ### Performance

    O(n + total_blocks) where ``n = len(items)`` and ``total_blocks``
    is the sum of content-array lengths across assistant messages.
    The index-build pass is O(n); the filter pass is O(n) outer +
    O(blocks) inner. Set membership (``fresh_tail_ordinals``) is
    O(1). Dict get on the two index maps is O(1). No quadratic
    patterns — the only mutation is ``list.append`` and dict-key
    ``list.append``, both amortised O(1).

    Args:
        items: Items selected by the budget walk, in ordinal-ascending
            order. The function expects the caller to have already
            composed ``kept_evictable + list(fresh_tail)`` (TS 1233).
            Both raw-message items and summary items may appear; only
            assistant raw-message items are eligible for filtering.
        fresh_tail_ordinals: Set of ordinals that belong to the fresh
            tail. Used only to compute the per-entry segment label.
            Items whose ordinal is in this set get ``"freshTail"``;
            others get ``"evictable"``.
        orphan_stripping_ordinal: Boundary below which orphan
            tool-use blocks are aggressively stripped. The engine
            shell's
            ``_stable_orphan_stripping_ordinals_by_conversation`` map
            pins this across hot-cache turns (03-08 wiring); for
            cold-cache assemblies the caller passes the current
            fresh-tail ordinal (the effect is "strip everything
            outside the fresh tail").
        all_tool_result_ordinals_by_id: Mapping from tool_call_id to
            **every** ordinal at which a tool_result with that id
            resolved — whether or not the budget walk selected it.
            The caller builds this from the full resolved list, not
            just the selected items. Read at TS line 747: when this
            map has resolved evidence for the id, an at-or-above-
            boundary orphan tool_use is STRIPPED (avoid stale-pairing
            risk on next assembly). When it has none, the tool_use
            is KEPT and deferred to the sanitize pass.

    Returns:
        A :class:`FilteredToolCallsResult` containing the surviving
        entries (in input order), plus per-message counters for the
        debug envelope.

        Invariant: every assistant message in
        ``result.entries`` either had no tool-call blocks to begin
        with, or every tool-call block that survived has a usable
        downstream selected tool_result (or is protected by the
        cache-marginal guards).
    """
    # ── Pass 1: index selected tool_results by id ─────────────────────
    # TS line 697-708. The TS source uses ``Map<string, number[]>``;
    # the Python equivalent is ``dict[str, list[int]]``. The TS
    # ``ordinals.push(...)`` pattern is mirrored via
    # ``selected.setdefault(id, []).append(ord)``.
    selected_tool_result_ordinals_by_id: dict[str, list[int]] = {}
    for item in items:
        tool_result_id = extract_tool_result_id_from_message(item.message)
        if tool_result_id is None:
            continue
        ordinals = selected_tool_result_ordinals_by_id.get(tool_result_id)
        if ordinals is not None:
            ordinals.append(item.ordinal)
        else:
            selected_tool_result_ordinals_by_id[tool_result_id] = [item.ordinal]

    # ── Pass 2: filter assistant content blocks ───────────────────────
    filtered_entries: list[FilteredEntry] = []
    removed_tool_use_block_count = 0
    touched_assistant_message_count = 0

    for item in items:
        # Segment label derives purely from ordinal membership in the
        # fresh-tail set (TS line 714). The label travels with the
        # entry for downstream ``preSanitize*`` hash computation.
        segment: AssemblySegment = (
            "freshTail" if item.ordinal in fresh_tail_ordinals else "evictable"
        )

        message = item.message
        # Non-assistant items pass through unchanged. TS line 715-718.
        if message.get("role") != "assistant":
            filtered_entries.append(FilteredEntry(message=message, segment=segment))
            continue

        # Assistant with non-array content (e.g. plain string, missing
        # key, or unexpected shape) passes through unchanged. TS line
        # 720-723. The downstream sanitize pass + cleanup at TS 1276
        # handles empty/blank string content; we don't touch it here.
        content = message.get("content")
        if not isinstance(content, list):
            filtered_entries.append(FilteredEntry(message=message, segment=segment))
            continue

        # Per-block filter. ``removed_any`` tracks whether at least one
        # block was stripped so we know whether to copy the message
        # dict (touched) or push the original reference (untouched).
        removed_any = False
        new_content: list[Any] = []
        for block in content:
            # Non-dict blocks (str, None, numbers — rare in practice
            # but possible from malformed fixtures) are kept. TS line
            # 727-729 short-circuits on ``!block || typeof block !==
            # "object"``; Python's ``isinstance`` is the equivalent.
            if not isinstance(block, Mapping):
                new_content.append(block)
                continue

            block_type = block.get("type")
            if not isinstance(block_type, str) or block_type not in TOOL_CALL_TYPES:
                new_content.append(block)
                continue

            tool_call_id = extract_tool_call_id_from_block(block)
            if tool_call_id is None:
                # No id to match against — keep the block. TS line
                # 735-737 returns ``true`` here.
                new_content.append(block)
                continue

            selected_ordinals = selected_tool_result_ordinals_by_id.get(tool_call_id, [])
            # TS line 739: strict ``> item.ordinal``. The strict
            # inequality is load-bearing — a tool_result at the same
            # ordinal as the tool_use is malformed input (results
            # always come on a later turn) and counts as no-match.
            has_usable_selected_result = any(ord_ > item.ordinal for ord_ in selected_ordinals)
            if has_usable_selected_result:
                new_content.append(block)
                continue

            # No usable selected result — check the boundary first.
            # Items below the boundary are aggressively stripped (the
            # boundary advances past them as the conversation grows,
            # so cache-stability concerns no longer apply).
            if item.ordinal < orphan_stripping_ordinal:
                removed_any = True
                continue

            # At/above the boundary — apply the cache-marginal
            # fallback. TS line 747-749:
            # ``if (!(allToolResultOrdinalsById.get(toolCallId)?.length))
            # return true``. The leading ``!`` inverts the length
            # check. The block is KEPT when there is NO evidence of a
            # resolved tool_result anywhere (id absent or maps to
            # empty list) — i.e. the tool_use is not a stale pairing,
            # just an unresolved call.
            #
            # Conversely, when the id DOES have resolved evidence in
            # the conversation's full history but the result didn't
            # make it into the selected set, the block is STRIPPED.
            # The rationale: a subsequent ``assemble()`` call might
            # include the result at a different ordinal, and emitting
            # the tool_use here would create a stale-pairing risk
            # across cache reuses.
            #
            # This is the inverse of the algorithm summary in the
            # issue spec text; the TS source is canonical per project
            # policy ("TS source is canonical").
            all_ordinals = all_tool_result_ordinals_by_id.get(tool_call_id)
            if all_ordinals is None or len(all_ordinals) == 0:
                new_content.append(block)
                continue

            # No selected result, at/above boundary, and the id has
            # resolved evidence elsewhere — strip. TS line 750-751.
            removed_any = True

        # ── Message-level disposition (TS 754-771) ─────────────────
        if len(new_content) == 0:
            # All blocks stripped → drop the entire message. Counters
            # increment whether or not blocks were actually removed
            # (e.g. an assistant message with ``content: []`` triggers
            # both increments per TS line 754-757 — the ``removed_any``
            # flag is not checked at this branch). We preserve that
            # quirk verbatim.
            removed_tool_use_block_count += 1
            touched_assistant_message_count += 1
            continue

        if not removed_any:
            # Nothing changed — push the original message reference,
            # avoiding the cost of dict materialization. TS line
            # 759-762 also pushes the original reference (``item.message``).
            filtered_entries.append(FilteredEntry(message=message, segment=segment))
            continue

        # Partial strip with surviving content — emit a new message
        # dict with the filtered content. We copy via ``dict(message)``
        # to preserve every other field (role, usage envelope,
        # toolCallId on the assistant turn if any, etc.) without
        # mutating the input. The TS source uses object spread
        # (``{ ...item.message, content: ... }``); ``dict(...)`` +
        # overwrite-key is the Python equivalent.
        removed_tool_use_block_count += 1
        touched_assistant_message_count += 1
        new_message: dict[str, Any] = dict(message)
        new_message["content"] = new_content
        filtered_entries.append(FilteredEntry(message=new_message, segment=segment))

    return FilteredToolCallsResult(
        entries=filtered_entries,
        removed_tool_use_block_count=removed_tool_use_block_count,
        touched_assistant_message_count=touched_assistant_message_count,
    )


# ---------------------------------------------------------------------------
# Top-level orchestration — assemble() public API (TS 1102-1332)
# ---------------------------------------------------------------------------


# Duplicate-cluster kind labels — mirror TS union literal at
# ``assembler.ts`` line 48 (``AssemblyDuplicateCluster.kind``). The string
# values are part of the debug envelope serialization and are observed by
# downstream tooling, so porting verbatim preserves wire compatibility.
DuplicateClusterKind = Literal["message-ref", "summary-ref", "message-content"]


# Blocks that count as "model-internal reasoning" and will be stripped by
# the provider layer before being sent to the API. If an assistant message
# contains ONLY these block types, it is treated as empty (TS line 92-93).
# Bedrock / Anthropic reject messages that present an empty content array,
# so the empty-assistant cleanup pass drops them.
_THINKING_LIKE_TYPES: Final[frozenset[str]] = frozenset({
    "thinking",
    "redacted_thinking",
    "reasoning",
})


def _is_blank_text_block(block: Any) -> bool:
    """Return ``True`` if ``block`` is a ``{type:"text", text:""}`` shape.

    Mirrors TS ``isBlankTextBlock`` (``assembler.ts`` 107-114). The
    whitespace-only check uses ``str.strip()`` for parity with TS
    ``text.trim()``. Non-text blocks (``tool_use``, ``thinking``, etc.)
    always return ``False`` — they are not "blank text" even when their
    text-like fields are empty.

    Args:
        block: Anything; non-Mapping inputs return ``False``.

    Returns:
        ``True`` only when ``block`` is a dict with ``type == "text"`` and
        ``text`` is a string that is empty or whitespace-only.
    """
    if not isinstance(block, Mapping):
        return False
    if block.get("type") != "text":
        return False
    text = block.get("text")
    if not isinstance(text, str):
        return False
    return text.strip() == ""


def _is_blank_content(content: Sequence[Any]) -> bool:
    """Return ``True`` if every block in ``content`` is a blank text block.

    Mirrors TS ``isBlankContent`` (``assembler.ts`` 121-124). Bedrock
    rejects assistant messages whose content array is, e.g.,
    ``[{type:"text", text:""}]`` with "The text field in the
    ContentBlock object at messages.N.content.0 is blank", so this
    predicate exists to filter those out before they hit the provider.

    Empty arrays return ``False`` — the empty-content branch in
    :meth:`ContextAssembler.assemble` checks ``len(content) == 0``
    separately. Mirrors TS line 122 ``if (content.length === 0) return
    false``.
    """
    if len(content) == 0:
        return False
    return all(_is_blank_text_block(block) for block in content)


def _is_thinking_only_content(content: Sequence[Any]) -> bool:
    """Return ``True`` if every block in ``content`` is a thinking/reasoning block.

    Mirrors TS ``isThinkingOnlyContent`` (``assembler.ts`` 97-105).
    Anthropic-side and Bedrock-side reasoning blocks are stripped by
    the provider layer before being sent to the API; if an assistant
    message's content is ONLY reasoning, the post-strip content is
    empty, which the providers then reject.

    Empty arrays return ``False`` — mirrors TS line 98 ``if
    (content.length === 0) return false``. Combined with
    :func:`_is_blank_content` and the explicit ``len(content) == 0``
    check, the empty-assistant cleanup covers all three failure modes.
    """
    if len(content) == 0:
        return False
    return all(
        isinstance(block, Mapping) and block.get("type") in _THINKING_LIKE_TYPES
        for block in content
    )


def _hash_messages(messages: Sequence[Mapping[str, Any]]) -> str:
    """SHA-256 prefix of ``messages`` serialised as JSON.

    Mirrors TS ``hashMessages`` (``assembler.ts`` 780-782):

    .. code-block:: typescript

        createHash("sha256").update(JSON.stringify(messages)).digest("hex").slice(0, 16)

    The 16-character truncation is load-bearing for cache-stability
    debug pairing: the engine-side prefix-stability snapshot reads
    these hashes to detect inter-turn drift, and stretching to the full
    64 chars would inflate every debug envelope by ~150 bytes per turn
    for no gain (the cardinality is bounded by the number of distinct
    assemble() outputs over the lifetime of a conversation, which the
    16-hex-character space comfortably covers).

    ``json.dumps`` defaults to insertion-order preservation (Python 3.7+
    dict iteration order), with no extra whitespace — matching TS
    ``JSON.stringify`` byte-for-byte for the cases we hash here
    (runtime message dicts have well-defined key orderings).

    Args:
        messages: A sequence of runtime message dicts.

    Returns:
        Lowercase hex string of length 16.
    """
    # ``separators=(",", ":")`` matches JS ``JSON.stringify`` (no spaces
    # after commas/colons). The TS source uses the default
    # ``JSON.stringify(messages)`` which produces exactly that form.
    # ``sort_keys=False`` preserves insertion order (Python 3.7+ dict
    # iteration), parity with JS object iteration order.
    serialized = json.dumps(messages, separators=(",", ":"), sort_keys=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


def _hash_text(text: str) -> str:
    """SHA-256 prefix of ``text``.

    Mirrors TS ``hashText`` (``assembler.ts`` 784-786). Used as the key
    for :func:`_build_message_content_duplicate_clusters` (clusters
    items whose ``text`` field hashes to the same value). 16 chars is
    enough cardinality to avoid collisions across a single
    conversation's history.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Overflow-diagnostics dataclasses (TS 16-78 interfaces)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AssemblyOverflowContributor:
    """Top-N token contributor for the overflow-diagnostics envelope.

    Mirrors TS ``interface AssemblyOverflowContributor`` (``assembler.ts``
    15-34). The shape is part of the public debug surface — downstream
    operators consume it to identify which items are pushing the
    context over budget.

    Attributes:
        ordinal: Position of the item in the persisted conversation
            window (``context_items.ordinal``).
        tokens: :func:`estimate_tokens` figure for the emitted item.
        selected: Whether the item survived the budget walk.
        message_id: Raw-message primary key (only set when the item is
            a message).
        seq: Raw-message conversation-sequence (only set when the item
            is a message).
        role: Raw-message role (only set when the item is a message).
        summary_id: Summary primary key (only set when the item is a
            summary).
        summary_kind: ``"leaf"`` / ``"condensed"`` (only set when the
            item is a summary).
        summary_depth: 0 / 1+ (only set when the item is a summary).
    """

    ordinal: int
    tokens: int
    selected: bool
    message_id: int | None = None
    seq: int | None = None
    role: str | None = None
    summary_id: str | None = None
    summary_kind: str | None = None
    summary_depth: int | None = None


@dataclass(slots=True)
class AssemblyDuplicateCluster:
    """A group of items that share a duplicate-detection key.

    Mirrors TS ``interface AssemblyDuplicateCluster`` (``assembler.ts``
    36-49). The duplicate-cluster diagnostics call out duplicates by
    reference (same ``messageId`` / ``summaryId`` appearing multiple
    times) and by content hash (same SHA-256 of plain-text rendering).

    Attributes:
        key: Cluster key — either ``"message:<id>"`` /
            ``"summary:<id>"`` for refs or a 16-char SHA-256 prefix
            for content.
        kind: Discriminator: ``"message-ref"``, ``"summary-ref"``, or
            ``"message-content"``.
        count: Number of items in the cluster (always ``>= 2``).
        tokens: Sum of ``item.tokens`` across the cluster.
        ordinals: Up to first 8 ordinals of cluster members.
        seqs: Up to first 8 ``seq`` values when items are messages.
            ``None`` when no member has a ``seq``.
    """

    key: str
    kind: DuplicateClusterKind
    count: int
    tokens: int
    ordinals: list[int]
    seqs: list[int] | None = None


@dataclass(slots=True)
class AssemblyOverflowDiagnostics:
    """Per-assemble overflow-diagnostics envelope.

    Mirrors TS ``interface AssemblyOverflowDiagnostics`` (``assembler.ts``
    51-78). Returned as part of :class:`AssembleDebug.overflow_diagnostics`
    when debug telemetry is requested.

    Attributes:
        token_budget: Budget used by this assembly pass.
        total_context_tokens: ``sum(item.tokens for item in resolved)``.
        raw_message_tokens: Token sum across raw-message items only.
        summary_tokens: Token sum across summary items only.
        raw_message_count: Number of resolved raw messages
            (pre-selection).
        summary_count: Number of resolved summaries (pre-selection).
        total_context_items: ``len(resolved)``.
        selected_raw_message_count: Number of raw-message items kept
            after budget walk.
        selected_summary_count: Number of summary items kept after
            budget walk.
        duplicate_ref_clusters: Reference-key clusters (e.g., same
            messageId resolved twice).
        duplicate_message_clusters: Content-hash clusters (same text
            payload across distinct messageIds).
        top_message_contributors: Up to 5 largest raw-message
            contributors.
        top_summary_contributors: Up to 5 largest summary
            contributors.
    """

    token_budget: int
    total_context_tokens: int
    raw_message_tokens: int
    summary_tokens: int
    raw_message_count: int
    summary_count: int
    total_context_items: int
    selected_raw_message_count: int
    selected_summary_count: int
    duplicate_ref_clusters: list[AssemblyDuplicateCluster]
    duplicate_message_clusters: list[AssemblyDuplicateCluster]
    top_message_contributors: list[AssemblyOverflowContributor]
    top_summary_contributors: list[AssemblyOverflowContributor]


def _top_contributors(
    items: Sequence[ResolvedItem],
    selected_ordinals: set[int],
    is_message: bool,
) -> list[AssemblyOverflowContributor]:
    """Pick the top-5 token contributors of the given kind.

    Mirrors TS ``topContributors`` (``assembler.ts`` 877-902).

    1. Filter items by ``is_message`` flag.
    2. Sort by ``(-tokens, ordinal)`` — largest token-cost first, ties
       broken by smaller ordinal (older wins on tie).
    3. Slice to first 5.
    4. Project to :class:`AssemblyOverflowContributor` with the
       per-kind optional fields set (messageId/seq/role for raw
       messages; summaryId/summaryKind/summaryDepth for summaries).

    Args:
        items: All resolved items (pre-selection).
        selected_ordinals: Set of ordinals that survived the budget
            walk. Used to mark each contributor's ``selected`` flag.
        is_message: When ``True``, project raw-message items; when
            ``False``, project summary items.

    Returns:
        Up to 5 :class:`AssemblyOverflowContributor` instances.
    """
    # Filter, sort, slice. The TS sort comparator is
    # ``b.tokens - a.tokens || a.ordinal - b.ordinal`` (TS line 885) —
    # tokens descending, ordinal ascending. Python translates to
    # ``key=lambda i: (-i.tokens, i.ordinal)`` with default ascending sort.
    kind_items = [item for item in items if item.is_message is is_message]
    kind_items.sort(key=lambda i: (-i.tokens, i.ordinal))
    selected_subset = kind_items[:5]

    contributors: list[AssemblyOverflowContributor] = []
    for item in selected_subset:
        contributor = AssemblyOverflowContributor(
            ordinal=item.ordinal,
            tokens=item.tokens,
            selected=item.ordinal in selected_ordinals,
        )
        # Per-kind optional fields — mirror TS spread (assembler.ts
        # 891-900). The TS source uses conditional spreads
        # (``...(item.messageId != null ? { messageId: item.messageId } : {})``);
        # the Python translation sets the field only when present.
        if item.message_id is not None:
            contributor.message_id = item.message_id
        if item.seq is not None:
            contributor.seq = item.seq
        if item.source_role is not None:
            contributor.role = item.source_role
        if item.summary is not None:
            contributor.summary_id = item.summary.summary_id
            contributor.summary_kind = item.summary.kind
            contributor.summary_depth = item.summary.depth
        contributors.append(contributor)

    return contributors


def _build_ref_duplicate_clusters(
    items: Sequence[ResolvedItem],
) -> list[AssemblyDuplicateCluster]:
    """Build duplicate-reference clusters keyed by messageId/summaryId.

    Mirrors TS ``buildRefDuplicateClusters`` (``assembler.ts`` 904-920).
    Items missing a usable key (e.g. an items malformed enough that
    both ``message_id`` and ``summary`` are absent) are skipped silently.

    Args:
        items: All resolved items (pre-selection).

    Returns:
        Sorted list of clusters with ``count >= 2``. Empty when no
        reference duplicates exist.
    """
    clusters: dict[str, list[ResolvedItem]] = {}
    for item in items:
        key: str | None
        if item.is_message:
            key = f"message:{item.message_id}" if item.message_id is not None else None
        else:
            key = f"summary:{item.summary.summary_id}" if item.summary is not None else None
        if key is None:
            continue
        clusters.setdefault(key, []).append(item)

    def _kind_for_key(key: str) -> DuplicateClusterKind:
        return "message-ref" if key.startswith("message:") else "summary-ref"

    return _format_duplicate_clusters(clusters, _kind_for_key)


def _build_message_content_duplicate_clusters(
    items: Sequence[ResolvedItem],
) -> list[AssemblyDuplicateCluster]:
    """Build duplicate-content clusters keyed by SHA-256 of ``item.text``.

    Mirrors TS ``buildMessageContentDuplicateClusters`` (``assembler.ts``
    922-934). Only raw-message items with non-empty text qualify —
    summaries are excluded (their text is the un-wrapped summary content,
    which by construction is unique across the DAG).

    Args:
        items: All resolved items (pre-selection).

    Returns:
        Sorted list of clusters with ``count >= 2`` where every cluster
        member is a raw message.
    """
    clusters: dict[str, list[ResolvedItem]] = {}
    for item in items:
        if not item.is_message or len(item.text) == 0:
            continue
        clusters.setdefault(_hash_text(item.text), []).append(item)

    return _format_duplicate_clusters(clusters, lambda _k: "message-content")


def _format_duplicate_clusters(
    clusters: Mapping[str, list[ResolvedItem]],
    kind_for_key: Any,  # Callable[[str], DuplicateClusterKind]
) -> list[AssemblyDuplicateCluster]:
    """Project a cluster map to sorted, capped :class:`AssemblyDuplicateCluster`.

    Mirrors TS ``formatDuplicateClusters`` (``assembler.ts`` 936-954).
    Filters singletons (TS uses ``filter(([, items]) => items.length >
    1)``), aggregates tokens/ordinals/seqs, sorts by ``(-tokens,
    -count, key)`` for stable diagnostic ordering, and caps the result
    at 5 entries.

    The per-cluster ``ordinals`` and ``seqs`` lists are capped at 8
    entries each (TS line 947, 949). The seq cap is conditional —
    omitted entirely when no cluster member has a ``seq``.

    Args:
        clusters: Map from key → cluster members.
        kind_for_key: Callable that produces a
            :data:`DuplicateClusterKind` label for each key.

    Returns:
        Up to 5 :class:`AssemblyDuplicateCluster` records.
    """
    formatted: list[AssemblyDuplicateCluster] = []
    for key, cluster_items in clusters.items():
        if len(cluster_items) <= 1:
            continue
        # Tokens accumulator + per-cluster ordinal/seq capture. The TS
        # source does this in one list-comprehension chain
        # (``items.reduce(...) ... items.map(...)``); Python's explicit
        # loops are equivalent and slightly easier to debug.
        tokens = sum(item.tokens for item in cluster_items)
        ordinals = [item.ordinal for item in cluster_items[:8]]
        has_any_seq = any(item.seq is not None for item in cluster_items)
        seqs: list[int] | None
        if has_any_seq:
            seqs = [item.seq for item in cluster_items if item.seq is not None][:8]
        else:
            seqs = None
        formatted.append(
            AssemblyDuplicateCluster(
                key=key,
                kind=kind_for_key(key),
                count=len(cluster_items),
                tokens=tokens,
                ordinals=ordinals,
                seqs=seqs,
            )
        )

    # Sort by (tokens desc, count desc, key asc). The TS source uses
    # ``b.tokens - a.tokens || b.count - a.count || a.key.localeCompare(b.key)``
    # which translates to ``key=(-tokens, -count, key)`` with default
    # ascending sort.
    formatted.sort(key=lambda c: (-c.tokens, -c.count, c.key))
    return formatted[:5]


def _build_overflow_diagnostics(
    resolved: Sequence[ResolvedItem],
    selected: Sequence[ResolvedItem],
    token_budget: int,
) -> AssemblyOverflowDiagnostics:
    """Assemble the per-assemble overflow-diagnostics envelope.

    Mirrors TS ``buildOverflowDiagnostics`` (``assembler.ts`` 956-981).
    Aggregates token totals, duplicate clusters, and top-N contributors
    for the debug-stream consumers.

    Args:
        resolved: All resolved items (pre-selection).
        selected: Items that survived the budget walk.
        token_budget: The budget used by this assemble pass.

    Returns:
        Populated :class:`AssemblyOverflowDiagnostics`.
    """
    selected_ordinals = {item.ordinal for item in selected}
    raw_message_items = [item for item in resolved if item.is_message]
    summary_items = [item for item in resolved if not item.is_message]
    return AssemblyOverflowDiagnostics(
        token_budget=token_budget,
        total_context_tokens=sum(item.tokens for item in resolved),
        raw_message_tokens=sum(item.tokens for item in raw_message_items),
        summary_tokens=sum(item.tokens for item in summary_items),
        raw_message_count=len(raw_message_items),
        summary_count=len(summary_items),
        total_context_items=len(resolved),
        selected_raw_message_count=sum(1 for item in selected if item.is_message),
        selected_summary_count=sum(1 for item in selected if not item.is_message),
        duplicate_ref_clusters=_build_ref_duplicate_clusters(resolved),
        duplicate_message_clusters=_build_message_content_duplicate_clusters(resolved),
        top_message_contributors=_top_contributors(resolved, selected_ordinals, True),
        top_summary_contributors=_top_contributors(resolved, selected_ordinals, False),
    )


# ---------------------------------------------------------------------------
# AssembleInput / AssembleResult / AssembleStats / AssembleDebug
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AssembleInput:
    """Inputs to :meth:`ContextAssembler.assemble`.

    Mirrors TS ``interface AssembleContextInput`` (``assembler.ts``
    128-141). All fields except ``conversation_id`` and ``token_budget``
    carry defaults so callers can pass a minimal struct.

    Attributes:
        conversation_id: Scope for :meth:`SummaryStore.get_context_items`.
        token_budget: Total budget for this assembly pass. Fresh tail
            may exceed the budget; the eviction loop only enforces the
            remaining budget after the tail.
        fresh_tail_count: Maximum number of raw messages to protect
            from eviction. Default 8 (TS line 132).
        fresh_tail_max_tokens: Optional token cap on the protected
            tail. The newest message is always preserved even if it
            alone exceeds this cap.
        prompt: Optional user query for BM25-lite-scored selection.
            ``None`` / whitespace falls back to chronological mode.
        prompt_aware_eviction: When ``False``, forces chronological
            mode even when a searchable prompt is present. Default
            ``True``.
        orphan_stripping_ordinal: Stable boundary for orphan
            tool-use stripping during hot-cache turns. Defaults to
            the fresh-tail ordinal at assemble time when ``None``
            (cold-cache fallback). Engine-side callers pin this
            across turns via
            ``LCMEngine._stable_orphan_stripping_ordinals_by_conversation``.
        stub_large_tool_payloads: v0.2.0 #628 stub-tier toggle. In
            v0.1.0 this is **a no-op + warning** per ADR-030.
        capture_debug: Whether to populate :attr:`AssembleResult.debug`.
            Off by default to avoid the SHA-256 hashing cost on every
            assemble call.
    """

    conversation_id: int
    token_budget: int
    fresh_tail_count: int = 8
    fresh_tail_max_tokens: int | None = None
    prompt: str | None = None
    prompt_aware_eviction: bool = True
    orphan_stripping_ordinal: int | None = None
    # Deferred per ADR-030; accepted for forward-compat but treated as
    # no-op + warning. Tests assert the warning path doesn't raise.
    stub_large_tool_payloads: bool = False
    # Off-by-default debug capture — populating ``AssembleResult.debug``
    # requires three SHA-256 passes on the assembled message list (TS
    # 1295-1300) which are non-trivial on long conversations. Engine-side
    # callers flip this on when surfacing diagnostics, off in steady-state.
    capture_debug: bool = False


@dataclass(slots=True)
class AssembleStats:
    """Stats envelope on :class:`AssembleResult`.

    Mirrors TS ``AssembleContextResult.stats`` (``assembler.ts``
    149-153). Counters are over the **pre-selection** resolved set so
    callers can spot when the budget walk dropped a lot of items.
    """

    raw_message_count: int
    summary_count: int
    total_context_items: int


@dataclass(slots=True)
class AssembleDebug:
    """Optional debug envelope on :class:`AssembleResult`.

    Mirrors TS ``AssembleContextResult.debug`` (``assembler.ts``
    154-176). Populated only when :attr:`AssembleInput.capture_debug`
    is ``True``. The three SHA-256 hashes are load-bearing for the
    engine-side prefix-stability snapshot — comparing
    ``pre_sanitize_messages_hash`` across consecutive assemble() calls
    detects inter-turn drift.

    Attributes:
        fresh_tail_ordinal: Smallest ordinal in the protected tail
            (:data:`EMPTY_FRESH_TAIL_ORDINAL` when no tail).
        orphan_stripping_ordinal: Resolved ordinal used by
            :func:`filter_non_fresh_assistant_tool_calls` (either the
            caller's override or the fresh-tail ordinal).
        base_fresh_tail_count: Tail size before any filter passes
            (always equal to ``fresh_tail_count`` in v0.1.0; the
            ``baseFreshTail``-vs-``freshTail`` distinction was for the
            #628 stub-tier path which is deferred per ADR-030).
        fresh_tail_count: Final tail size.
        tail_tokens: ``sum(item.tokens for item in fresh_tail)``.
        remaining_budget: ``max(0, token_budget - tail_tokens)``.
        evictable_total_tokens: ``sum(item.tokens for item in evictable)``.
        selection_mode: Which budget-walk branch executed.
        promoted_tool_result_count: v0.2.0 #628 stub-tier counter.
            Always ``0`` in v0.1.0 (ADR-030).
        promoted_ordinals: v0.2.0 #628 stub-tier list. Always ``[]``
            in v0.1.0.
        removed_tool_use_block_count: Per-message counter from
            :func:`filter_non_fresh_assistant_tool_calls`.
        touched_assistant_message_count: Per-message counter from
            :func:`filter_non_fresh_assistant_tool_calls`.
        pre_sanitize_evictable_count: Size of the evictable bucket
            after orphan-stripping + content-normalization, before
            :func:`sanitize_tool_use_result_pairing`.
        pre_sanitize_fresh_tail_count: Size of the fresh-tail bucket
            at the same point.
        pre_sanitize_evictable_hash: SHA-256(16-char) of the
            pre-sanitize evictable messages, JSON-serialised.
        pre_sanitize_fresh_tail_hash: SHA-256(16-char) of the
            pre-sanitize fresh-tail messages.
        pre_sanitize_messages_hash: SHA-256(16-char) of the
            pre-sanitize combined messages.
        final_messages_hash: SHA-256(16-char) of the post-sanitize
            final messages.
        overflow_diagnostics: Token/duplicate diagnostics envelope.
    """

    fresh_tail_ordinal: int
    orphan_stripping_ordinal: int
    base_fresh_tail_count: int
    fresh_tail_count: int
    tail_tokens: int
    remaining_budget: int
    evictable_total_tokens: int
    selection_mode: SelectionMode
    promoted_tool_result_count: int
    promoted_ordinals: list[int]
    removed_tool_use_block_count: int
    touched_assistant_message_count: int
    pre_sanitize_evictable_count: int
    pre_sanitize_fresh_tail_count: int
    pre_sanitize_evictable_hash: str
    pre_sanitize_fresh_tail_hash: str
    pre_sanitize_messages_hash: str
    final_messages_hash: str
    overflow_diagnostics: AssemblyOverflowDiagnostics


@dataclass(slots=True)
class AssembleResult:
    """Output of :meth:`ContextAssembler.assemble`.

    Mirrors TS ``interface AssembleContextResult`` (``assembler.ts``
    143-176).

    Attributes:
        messages: Final, sanitize-pass-clean message list ready for
            the provider. Empty list when the conversation has no
            context items.
        estimated_tokens: ``evictable_kept_tokens + fresh_tail_tokens``.
            The post-budget-walk total (NOT including any drift from
            content normalization or tool-call stripping — those passes
            do not add tokens; they only drop blocks/messages).
        stats: Pre-selection :class:`AssembleStats` envelope.
        debug: Optional :class:`AssembleDebug` envelope when the input
            requested it via :attr:`AssembleInput.capture_debug`.
    """

    messages: list[dict[str, Any]]
    estimated_tokens: int
    stats: AssembleStats
    debug: AssembleDebug | None = None


# ---------------------------------------------------------------------------
# ContextAssembler.assemble — top-level orchestration
# ---------------------------------------------------------------------------


def _normalize_and_clean_assistant_content(
    entries: Sequence[FilteredEntry],
) -> tuple[list[FilteredEntry], list[FilteredEntry]]:
    """Normalize assistant content and drop empty/thinking-only turns.

    Implements TS ``assembler.ts`` 1250-1293 in one pass: the two-stage
    cleanup runs as a single normalization pass over the entries list
    (string-content → ``[{type:"text"}]`` array + blank-text-block
    filter) followed by a drop-empty-assistants pass.

    Splits behavior across two stages purely to keep the docstring
    short — the actual code is one filter + one map.

    Returns:
        Pair ``(cleaned_entries, normalized_entries)``. The second
        item is the post-normalization-before-cleanup list (used by
        callers that want to count how many turns the cleanup pass
        dropped).
    """
    # Stage 1: normalize content (TS 1250-1274). Each entry's message
    # is rebuilt only when the content needed reshaping — otherwise the
    # original entry passes through with no allocation.
    normalized: list[FilteredEntry] = []
    for entry in entries:
        msg = entry.message
        if msg.get("role") == "assistant" and isinstance(msg.get("content"), str):
            # String-content assistant → wrap in a single text block.
            # Some providers (OpenAI Chat) emit content as a plain
            # string; Anthropic expects an array. Round-tripping through
            # the array form is byte-compatible for both.
            normalized.append(
                FilteredEntry(
                    message={
                        **msg,
                        "content": [{"type": "text", "text": msg["content"]}],
                    },
                    segment=entry.segment,
                )
            )
            continue
        if msg.get("role") == "assistant" and isinstance(msg.get("content"), list):
            content_list = msg["content"]
            filtered_blocks = [block for block in content_list if not _is_blank_text_block(block)]
            if len(filtered_blocks) != len(content_list):
                # Some blank text blocks were stripped — rebuild.
                normalized.append(
                    FilteredEntry(
                        message={**msg, "content": filtered_blocks},
                        segment=entry.segment,
                    )
                )
                continue
        normalized.append(entry)

    # Stage 2: drop empty / blank / thinking-only assistant turns
    # (TS 1283-1293). Bedrock + Anthropic reject these. The cleanup
    # is identity-preserving for non-assistant entries.
    cleaned: list[FilteredEntry] = []
    for entry in normalized:
        msg = entry.message
        if msg.get("role") != "assistant":
            cleaned.append(entry)
            continue
        content = msg.get("content")
        if isinstance(content, list):
            # Empty / thinking-only / blank-only → drop.
            if (
                len(content) == 0
                or _is_thinking_only_content(content)
                or _is_blank_content(content)
            ):
                continue
        else:
            # String content: drop when None / empty / whitespace-only.
            if not isinstance(content, str) or content.strip() == "":
                continue
        cleaned.append(entry)

    return cleaned, normalized
