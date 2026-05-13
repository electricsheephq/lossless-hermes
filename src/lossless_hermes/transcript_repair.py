"""Tool use/result pairing repair for assembled context.

Verbatim port of ``lossless-claw/src/transcript-repair.ts`` (300 LOC,
commit ``1f07fbd``). The TS module is itself a copy of openclaw core
(``src/agents/session-transcript-repair.ts`` +
``src/agents/tool-call-id.ts``) carried forward to avoid depending on
unexported internals. When the Hermes plugin SDK exports an equivalent
helper, this file can be removed in favor of the SDK import.

### Scope

Pure-function transcript repair used at assembly time. Operates on
in-memory message lists — **no I/O, no DB access, no JSONL rewrites**.
Per ``docs/reference/lcm-source-map.md`` open-question #5 and the
Hermes session-DB design: the original ``engine.ts`` JSONL bootstrap
path drops on Hermes, but the in-memory pairing logic ports verbatim.

### What it does

Given an array of assembled messages that came out of
``context_items`` + ``freshTail``, the module:

1. **Pairs ``tool_use`` with ``tool_result``** — finds orphaned
   ``tool_use`` blocks (no matching ``tool_result``) and orphaned
   ``tool_results`` (no matching ``tool_use``). Behavior:

   - Anthropic/Claude Code Assist: ``tool_use`` and ``tool_result``
     live in separate messages; pairing is by ``tool_use_id``.
   - OpenAI: ``function_call`` blocks live inside an assistant
     message; the helper extracts ids from ``id`` *or* ``call_id``.

   Resolution:

   - Move a matching ``toolResult`` message directly after its
     assistant ``toolCall`` turn.
   - Insert a **synthetic error ``toolResult``** for missing ids
     (``isError=True``, ``content=[{type: text, text: "[lossless-claw]
     missing tool result…"}]``).
   - Drop **duplicate** ``toolResult`` messages for the same id.
   - Drop **orphaned** ``toolResult`` messages (no matching tool call).

2. **Sanitizes OpenAI reasoning placement** — OpenAI ``o1``/``o3``/``o4``
   reasoning blocks have specific placement rules. The helper only
   repairs the narrow case of a single ``function_call`` followed by
   one or more ``reasoning``/``thinking`` blocks; multi-call turns may
   use interleaved reasoning intentionally, so they are left alone.

### What this is NOT

- It does **NOT** rewrite session JSONL files on disk. That entire
  surface (the openclaw ``engine.ts`` JSONL bootstrap path) drops on
  Hermes per ``lcm-source-map.md`` §"DROP list".
- It does **NOT** touch the DB.

### Public surface

The verbatim TS export is :func:`sanitize_tool_use_result_pairing`.
A higher-level wrapper :func:`repair_transcript` exposes a
:class:`RepairResult` dataclass with structural counts
(``dropped_count`` / ``synthesized_count`` / ``repaired_count``) per
the issue spec §"Pure-function interface". Both call the same
underlying logic — the wrapper just instruments counts.

See:

* ``epics/01-storage/01-14-transcript-repair.md`` — issue spec + AC.
* ``docs/porting-guides/storage.md`` row 21 — porting guide reference.
* ``docs/reference/lcm-source-map.md`` open-question #5 — "no JSONL"
  clarification.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Literal, Mapping, Sequence, cast

__all__ = [
    "RepairResult",
    "repair_transcript",
    "sanitize_tool_use_result_pairing",
]


# ---------------------------------------------------------------------------
# Module constants — direct translation of the TS Sets
# ---------------------------------------------------------------------------

# The set of content-block ``type`` strings that the TS code treats as
# tool calls. Mirrors ``TOOL_CALL_TYPES`` in ``transcript-repair.ts`` line 30.
TOOL_CALL_TYPES: frozenset[str] = frozenset({
    "toolCall",
    "toolUse",
    "tool_use",
    "tool-use",
    "functionCall",
    "function_call",
})

# Subset of ``TOOL_CALL_TYPES`` that are OpenAI-shape. Used to gate the
# reasoning-block hoist (line 38 in the TS source).
OPENAI_FUNCTION_CALL_TYPES: frozenset[str] = frozenset({"functionCall", "function_call"})


# ---------------------------------------------------------------------------
# Extraction helpers — direct port of tool-call-id.ts
# ---------------------------------------------------------------------------


def _extract_tool_call_id(block: Mapping[str, Any]) -> str | None:
    """Return the tool-call id from a content block, or ``None``.

    Ports ``extractToolCallId`` from ``transcript-repair.ts:40-48``.
    Both ``id`` and ``call_id`` are accepted because the TS code carries
    OpenAI shape (``call_id``) and Anthropic shape (``id``) side by
    side. The function returns ``None`` for empty strings to match the
    TS truthiness check (``typeof === "string" && id``).
    """
    raw_id = block.get("id")
    if isinstance(raw_id, str) and raw_id:
        return raw_id
    raw_call_id = block.get("call_id")
    if isinstance(raw_call_id, str) and raw_call_id:
        return raw_call_id
    return None


def _normalize_assistant_reasoning_blocks(message: Mapping[str, Any]) -> Mapping[str, Any]:
    """Hoist OpenAI ``reasoning`` blocks to the front of an assistant message.

    Ports ``normalizeAssistantReasoningBlocks`` from
    ``transcript-repair.ts:50-103``. Only the narrow case is repaired:
    a single ``function_call`` followed by one or more
    ``reasoning``/``thinking`` blocks. Multi-call turns may use
    interleaved reasoning intentionally and are returned untouched.

    The function returns the same object when no repair is needed
    (identity comparison drives the ``changed`` flag in the caller).
    """
    content = message.get("content")
    if not isinstance(content, list):
        return message

    saw_tool_call = False
    reasoning_after_tool_call = False
    function_call_count = 0

    for block in content:
        if not isinstance(block, Mapping):
            return message

        block_type = block.get("type")
        if block_type in ("reasoning", "thinking"):
            if saw_tool_call:
                reasoning_after_tool_call = True
            continue

        if isinstance(block_type, str) and block_type in TOOL_CALL_TYPES:
            saw_tool_call = True
            if block_type in OPENAI_FUNCTION_CALL_TYPES:
                function_call_count += 1
            continue

        return message

    # Only repair the specific OpenAI shape we need: a single function
    # call that has one or more reasoning blocks after it. Multi-call
    # turns may use interleaved reasoning intentionally, so leave them
    # untouched. (Mirrors transcript-repair.ts:85-88.)
    if not reasoning_after_tool_call or function_call_count != 1:
        return message

    reasoning_blocks = [
        block for block in content if block.get("type") in ("reasoning", "thinking")
    ]
    tool_call_blocks = [
        block
        for block in content
        if isinstance(block.get("type"), str) and block.get("type") in TOOL_CALL_TYPES
    ]

    # Spread shallow-copy the message and replace ``content``. Mirrors the
    # TS ``{...message, content: [...reasoning, ...toolCalls]}`` idiom.
    normalized = dict(message)
    normalized["content"] = [*reasoning_blocks, *tool_call_blocks]
    return normalized


def _extract_tool_calls_from_assistant(msg: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the list of ``{id, name?}`` tool-calls embedded in an assistant turn.

    Ports ``extractToolCallsFromAssistant`` from
    ``transcript-repair.ts:105-129``. Returns an empty list when
    ``content`` is not a list, when no blocks are tool calls, or when
    every candidate block lacks an extractable id.
    """
    content = msg.get("content")
    if not isinstance(content, list):
        return []

    tool_calls: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, Mapping):
            continue
        call_id = _extract_tool_call_id(block)
        if not call_id:
            continue
        block_type = block.get("type")
        if isinstance(block_type, str) and block_type in TOOL_CALL_TYPES:
            name = block.get("name")
            entry: dict[str, Any] = {"id": call_id}
            if isinstance(name, str):
                entry["name"] = name
            tool_calls.append(entry)
    return tool_calls


def _extract_tool_result_id(msg: Mapping[str, Any]) -> str | None:
    """Return the tool-result id (``toolCallId`` or ``toolUseId``) or ``None``.

    Ports ``extractToolResultId`` from ``transcript-repair.ts:131-139``.
    ``toolCallId`` is preferred (newer LCM/Hermes shape); ``toolUseId``
    is accepted as a fallback to keep backwards compatibility with
    older session payloads.
    """
    tool_call_id = msg.get("toolCallId")
    if isinstance(tool_call_id, str) and tool_call_id:
        return tool_call_id
    tool_use_id = msg.get("toolUseId")
    if isinstance(tool_use_id, str) and tool_use_id:
        return tool_use_id
    return None


# ---------------------------------------------------------------------------
# Repair logic — direct port of session-transcript-repair.ts
# ---------------------------------------------------------------------------


def _make_missing_tool_result(*, tool_call_id: str, tool_name: str | None) -> dict[str, Any]:
    """Build a synthetic ``toolResult`` message for a missing tool-call id.

    Ports ``makeMissingToolResult`` from ``transcript-repair.ts:143-159``.
    The deterministic body (same id ⇒ same payload) is load-bearing for
    one of the AC tests ("creates deterministic synthetic tool results
    for missing calls"). Keep the marker string byte-identical to the
    TS source — ``[lossless-claw]`` prefix included — so downstream
    consumers grepping for the marker can match either implementation.
    """
    return {
        "role": "toolResult",
        "toolCallId": tool_call_id,
        "toolName": tool_name if tool_name is not None else "unknown",
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


def sanitize_tool_use_result_pairing(
    messages: Sequence[Mapping[str, Any]],
) -> Sequence[Mapping[str, Any]]:
    """Repair tool use/result pairing in an assembled message transcript.

    Verbatim port of :py:`sanitizeToolUseResultPairing` from
    ``transcript-repair.ts:171-300``. Anthropic (and Cloud Code Assist)
    reject transcripts where assistant tool calls are not immediately
    followed by matching tool results. This function:

    - Moves matching ``toolResult`` messages directly after their
      assistant ``toolCall`` turn.
    - Inserts synthetic error ``toolResults`` for missing ids.
    - Drops duplicate ``toolResults`` for the same id.
    - Drops orphaned ``toolResults`` with no matching tool call.

    Returns the original ``messages`` sequence when no change was
    needed (identity preserved); otherwise returns a freshly built
    ``list``. This matches the TS code's ``return changedOrMoved ? out
    : messages`` short-circuit (line 299), which lets callers compare
    references to detect whether a repair happened.

    Args:
        messages: A sequence of ``AgentMessageLike`` mappings. Items
            are not mutated — the synthetic ``toolResult`` insertion
            creates fresh dicts. Reasoning-block normalization shallow-
            copies the message before reshaping ``content``.

    Returns:
        Either the original ``messages`` (when nothing changed) or a
        new ``list`` with the repairs applied.
    """
    out: list[Mapping[str, Any]] = []
    seen_tool_result_ids: set[str] = set()
    # Local counters mirror the TS implementation's locals
    # (droppedDuplicateCount, droppedOrphanCount, moved, changed). They
    # are not exposed by ``sanitize_tool_use_result_pairing`` because the
    # TS export does not expose them; :func:`repair_transcript` is the
    # public wrapper that surfaces counts.
    dropped_duplicate_count = 0
    dropped_orphan_count = 0
    moved = False
    changed = False

    def _push_tool_result(msg: Mapping[str, Any]) -> None:
        nonlocal dropped_duplicate_count, changed
        result_id = _extract_tool_result_id(msg)
        if result_id and result_id in seen_tool_result_ids:
            dropped_duplicate_count += 1
            changed = True
            return
        if result_id:
            seen_tool_result_ids.add(result_id)
        out.append(msg)

    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        if not isinstance(msg, Mapping):
            out.append(msg)
            i += 1
            continue

        role = msg.get("role")
        if role != "assistant":
            if role != "toolResult":
                out.append(msg)
            else:
                dropped_orphan_count += 1
                changed = True
            i += 1
            continue

        normalized_assistant = _normalize_assistant_reasoning_blocks(msg)
        if normalized_assistant is not msg:
            changed = True

        # Skip tool call extraction for aborted or errored assistant
        # messages. When stopReason is "error" or "aborted", the
        # tool_use blocks may be incomplete and should not have
        # synthetic tool_results created. (Mirrors transcript-repair.ts:216-222.)
        stop_reason = normalized_assistant.get("stopReason")
        if stop_reason == "error" or stop_reason == "aborted":
            out.append(normalized_assistant)
            i += 1
            continue

        tool_calls = _extract_tool_calls_from_assistant(normalized_assistant)
        if not tool_calls:
            out.append(normalized_assistant)
            i += 1
            continue

        tool_call_ids = {call["id"] for call in tool_calls}

        # ``span_results_by_id``: tool-results we've vacuumed up from the
        # current assistant turn's "span" of following non-assistant
        # messages, keyed by tool-call id.
        # ``remainder``: non-toolResult messages that were interleaved
        # within the span; they get re-emitted after the paired
        # tool-results so we preserve user/system messages.
        span_results_by_id: dict[str, Mapping[str, Any]] = {}
        remainder: list[Mapping[str, Any]] = []

        j = i + 1
        while j < n:
            nxt = messages[j]
            if not isinstance(nxt, Mapping):
                remainder.append(nxt)
                j += 1
                continue

            nxt_role = nxt.get("role")
            if nxt_role == "assistant":
                break

            if nxt_role == "toolResult":
                result_id = _extract_tool_result_id(nxt)
                if result_id and result_id in tool_call_ids:
                    if result_id in seen_tool_result_ids:
                        dropped_duplicate_count += 1
                        changed = True
                        j += 1
                        continue
                    if result_id not in span_results_by_id:
                        span_results_by_id[result_id] = nxt
                    j += 1
                    continue

            if nxt.get("role") != "toolResult":
                remainder.append(nxt)
            else:
                dropped_orphan_count += 1
                changed = True
            j += 1

        out.append(normalized_assistant)

        if len(span_results_by_id) > 0 and len(remainder) > 0:
            moved = True
            changed = True

        for call in tool_calls:
            existing = span_results_by_id.get(call["id"])
            if existing is not None:
                _push_tool_result(existing)
            else:
                missing = _make_missing_tool_result(
                    tool_call_id=call["id"],
                    tool_name=call.get("name"),
                )
                changed = True
                _push_tool_result(missing)

        for rem in remainder:
            out.append(rem)

        # Skip past the span we just processed. (TS: ``i = j - 1`` then
        # the for-loop's ``i += 1`` advances; here we set ``i = j``.)
        i = j

    changed_or_moved = changed or moved
    return out if changed_or_moved else messages


# ---------------------------------------------------------------------------
# Higher-level wrapper — issue spec §"Pure-function interface"
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RepairResult:
    """Structured result for :func:`repair_transcript`.

    Mirrors the dataclass shape in
    ``epics/01-storage/01-14-transcript-repair.md`` §"Pure-function
    interface". Counts are derived by re-scanning the input/output
    arrays after the verbatim TS logic runs — the TS export does not
    surface these counters, so we recompute them by structural diff.

    Attributes:
        messages: The repaired message list. When no change was needed,
            this is the same ``list[dict]`` reference as the input
            (callers that rely on ``messages is input`` to skip work
            see the optimization).
        dropped_count: Number of orphaned/duplicate ``toolResult``
            messages removed from the output.
        synthesized_count: Number of synthetic placeholder
            ``toolResult`` messages inserted for missing ids.
        repaired_count: Number of assistant turns whose ``content``
            ordering or downstream pairing was structurally changed
            (reasoning-hoist OR span-reorder).
    """

    messages: list[Mapping[str, Any]]
    dropped_count: int
    synthesized_count: int
    repaired_count: int


_SYNTHETIC_MARKER = (
    "[lossless-claw] missing tool result in session history; "
    "inserted synthetic error result for transcript repair."
)


def _is_synthetic_tool_result(msg: Mapping[str, Any]) -> bool:
    """Return True iff ``msg`` was produced by :func:`_make_missing_tool_result`.

    The marker string is deterministic (see
    :func:`_make_missing_tool_result`) so a downstream diff can
    distinguish synthesized from authentic tool-results without an
    extra side-channel. Used by :func:`repair_transcript` to populate
    ``RepairResult.synthesized_count`` without re-running the repair.
    """
    if msg.get("role") != "toolResult" or msg.get("isError") is not True:
        return False
    content = msg.get("content")
    if not isinstance(content, list) or len(content) != 1:
        return False
    only_block = content[0]
    if not isinstance(only_block, Mapping):
        return False
    return only_block.get("type") == "text" and only_block.get("text") == _SYNTHETIC_MARKER


def _count_tool_results(messages: Iterable[Mapping[str, Any]]) -> int:
    """Count ``toolResult`` messages in a sequence (excluding non-mappings)."""
    return sum(1 for m in messages if isinstance(m, Mapping) and m.get("role") == "toolResult")


def _count_reasoning_normalized_assistants(
    before: Sequence[Mapping[str, Any]], after: Sequence[Mapping[str, Any]]
) -> int:
    """Count assistant messages whose ``content`` ordering changed.

    A coarse structural diff: an assistant message is "repaired" if its
    index-aligned ``content`` types differ between input and output.
    This is the cheapest way to surface the reasoning-hoist count
    without re-implementing the TS logic. It is best-effort — if the
    output drops or adds messages, the diff falls back to "all
    assistant turns at differing positions".
    """
    repaired = 0
    # Walk by index so we compare the same logical message slot. When
    # the lists differ in length the loop terminates at the shorter
    # one; the dropped/added counts capture the rest.
    for original, repaired_msg in zip(before, after):
        if not isinstance(original, Mapping) or not isinstance(repaired_msg, Mapping):
            continue
        if original.get("role") != "assistant" or repaired_msg.get("role") != "assistant":
            continue
        if original is repaired_msg:
            continue
        original_content = original.get("content")
        repaired_content = repaired_msg.get("content")
        if not isinstance(original_content, list) or not isinstance(repaired_content, list):
            continue
        original_types = [b.get("type") for b in original_content if isinstance(b, Mapping)]
        repaired_types = [b.get("type") for b in repaired_content if isinstance(b, Mapping)]
        if original_types != repaired_types:
            repaired += 1
    return repaired


def repair_transcript(
    messages: Sequence[Mapping[str, Any]],
    *,
    provider: Literal["anthropic", "openai"] = "anthropic",
) -> RepairResult:
    """Repair an assembled message transcript and surface structural counts.

    Higher-level wrapper around :func:`sanitize_tool_use_result_pairing`
    that exposes the :class:`RepairResult` shape mandated by the issue
    spec (``epics/01-storage/01-14-transcript-repair.md`` §"Pure-function
    interface"). The underlying logic auto-detects provider shape
    (Anthropic vs OpenAI) based on content-block types, so the
    ``provider`` argument is informational only — it is accepted so
    future provider-specific divergence can land without an API change.

    The ``RepairResult`` counts are recomputed via a structural diff on
    the output:

    - ``synthesized_count``: tool-result messages whose body matches
      the deterministic synthetic marker (see
      :func:`_make_missing_tool_result`).
    - ``dropped_count``: the difference in ``toolResult`` cardinality
      between input and output, minus ``synthesized_count`` (synthetic
      results add to the output, so they offset the drop count).
    - ``repaired_count``: assistant turns whose ``content`` block-type
      ordering changed (the reasoning-hoist path).

    Args:
        messages: The assembled message list to repair. Items are not
            mutated; the output ``messages`` field is either the same
            reference (no change) or a freshly built ``list``.
        provider: ``"anthropic"`` (default) or ``"openai"``.
            Informational — the underlying repair auto-detects shape.

    Returns:
        A :class:`RepairResult` carrying the repaired list plus the
        three structural counts.
    """
    if provider not in ("anthropic", "openai"):
        raise ValueError(
            f"repair_transcript: unknown provider {provider!r}; expected 'anthropic' or 'openai'."
        )

    repaired = sanitize_tool_use_result_pairing(messages)

    # Normalize the output to ``list`` so the dataclass field type is
    # stable. When the TS short-circuit fires (no change) the input
    # sequence is re-used verbatim — preserve that identity by
    # returning a list wrapping the same elements iff the caller passed
    # a list; otherwise materialize.
    if repaired is messages:
        # Preserve identity for the no-change case where the input was
        # already a list. Other Sequence inputs (tuples) get a list
        # copy so the dataclass field type is honored. The ``cast``
        # keeps ty happy: the sanitizer's return type is ``Sequence``
        # for variance reasons (the no-change branch returns the input
        # verbatim), but we know it is a ``list`` here when ``messages``
        # itself was a list.
        result_messages: list[Mapping[str, Any]] = (
            cast("list[Mapping[str, Any]]", messages)
            if isinstance(messages, list)
            else list(messages)
        )
        return RepairResult(
            messages=result_messages,
            dropped_count=0,
            synthesized_count=0,
            repaired_count=0,
        )

    # When ``repaired is not messages``, the sanitizer materialized a
    # fresh ``list`` (see its docstring + return contract).
    result_messages = cast("list[Mapping[str, Any]]", repaired)

    # Surface structural counts via diff between input and output.
    synthesized_count = sum(1 for m in result_messages if _is_synthetic_tool_result(m))

    input_tool_results = _count_tool_results(messages)
    output_tool_results = _count_tool_results(result_messages)
    # Net drop: tool-results we lost from the input, offset by the
    # synthesized ones we inserted. ``max(0, …)`` defends against the
    # pathological case where reordering produces a count we don't
    # expect (it never should — but the guard keeps the field semantic).
    dropped_count = max(
        0,
        input_tool_results - (output_tool_results - synthesized_count),
    )

    repaired_count = _count_reasoning_normalized_assistants(messages, result_messages)

    return RepairResult(
        messages=result_messages,
        dropped_count=dropped_count,
        synthesized_count=synthesized_count,
        repaired_count=repaired_count,
    )
