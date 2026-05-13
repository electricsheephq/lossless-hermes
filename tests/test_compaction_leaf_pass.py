"""Tests for :class:`CompactionEngine` leaf-pass body (issue 04-02).

Covers issue 04-02 acceptance criteria
(``epics/04-compaction/04-02-compaction-leaf-pass.md``):

* :meth:`CompactionEngine._select_oldest_leaf_chunk` — selects exactly
  the contiguous raw-message chunk outside the fresh tail, capped by
  ``leaf_chunk_tokens`` (always-include-≥1 invariant).
* Chunk-selection terminates on any non-message item — guards future
  item types.
* Chunk stops STRICTLY BEFORE ``fresh_tail_ordinal`` (uses ``<``, not
  ``<=``).
* Prior-summary context is exactly the last 2 summary items before
  chunk start, joined with ``"\\n\\n"``.
* Media annotation: pure-media messages become ``"[Media attachment]"``;
  mixed parts get ``" [with media attachment]"`` suffix.
* Reasoning / thinking blocks stripped from message text before
  concatenation (the load-bearing operator behavior — encrypted
  signatures must never reach the summarizer).
* Summary insert uses ``"sum_" + sha256(content + str(now_ms))[:16]``
  ID format.
* :meth:`SummaryStore.link_summary_to_messages` invoked with the
  message IDs in chunk order.
* :meth:`SummaryStore.replace_context_range_with_summary` invoked
  inside the same transaction as :meth:`SummaryStore.insert_summary`.
* Auth-failure path returns ``None`` (NOT raising) — caller handles
  via :class:`CompactionResult` ``auth_failure=True``.

### Test design

We use TWO levels of fixture:

1. **In-memory protocol stand-ins** — minimal classes satisfying
   ``_SummaryStoreLike`` + ``_ConversationStoreLike``. Used for unit-
   level coverage of selection, concatenation, escalation paths.
2. **Real :class:`SummaryStore` + :class:`ConversationStore`** —
   migrated SQLite for the persistence-integrity tests (DAG link rows,
   atomic transaction commit, context-range swap).

### Source references

* TS source: ``lossless-claw/src/compaction.ts`` lines 1005-1057
  (selectOldestLeafChunk), 1065-1104 (resolvePriorLeafSummaryContext),
  1457-1485 (annotateMediaContent), 1492-1607 (leafPass).
* LCM commit ``1f07fbd`` on branch ``pr-613``.
* Spec: ``epics/04-compaction/04-02-compaction-leaf-pass.md``.
* Porting guide: ``docs/porting-guides/assembler-compaction.md``
  §"Leaf-pass algorithm".
* ADR-017 (sync stores), ADR-024 (project layout), ADR-029 (Wave-N
  provenance).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

import pytest

from lossless_hermes.compaction import (
    CompactionConfig,
    CompactionEngine,
    LcmProviderAuthError,
    LeafChunkSelection,
    LeafPassOutcome,
    LeafPassResult,
    SummarizeFn,
    _dedupe_ordered_ids,
    _extract_meaningful_message_text,
    _format_timestamp,
    _generate_summary_id,
    _is_media_attachment_part,
    _looks_like_binary_payload,
    _strip_embedded_media_payloads,
)
from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.store.conversation import ConversationStore
from lossless_hermes.store.summary import (
    CreateSummaryInput,
    ReplaceContextRangeInput,
    SummaryStore,
)


# =============================================================================
# In-memory stand-ins
# =============================================================================


@dataclass
class _StubContextItem:
    ordinal: int
    item_type: str
    message_id: int | None = None
    summary_id: str | None = None


@dataclass
class _StubMessage:
    message_id: int
    content: str
    token_count: int
    created_at: datetime = field(default_factory=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))


@dataclass
class _StubMessagePart:
    part_type: str
    text_content: str | None = None
    metadata: str | None = None


@dataclass
class _StubSummaryRecord:
    content: str


class _StubSummaryStore:
    """Minimal :class:`_SummaryStoreLike` stand-in with transaction tracking.

    Records every call made inside :meth:`with_transaction` so tests can
    assert atomic ordering — "insert + link + replace ran together, not
    separately".
    """

    def __init__(
        self,
        *,
        context_token_count: int = 0,
        context_items: list[_StubContextItem] | None = None,
        summaries: dict[str, _StubSummaryRecord] | None = None,
    ) -> None:
        self.context_token_count = context_token_count
        self.context_items: list[_StubContextItem] = list(context_items or [])
        self.summaries: dict[str, _StubSummaryRecord] = dict(summaries or {})
        # Persistence sinks — tests assert on these.
        self.inserted: list[CreateSummaryInput] = []
        self.linked: list[tuple[str, list[int]]] = []
        self.replaced: list[ReplaceContextRangeInput] = []
        # Atomicity tracking — call log inside-transaction.
        self.transaction_depth = 0
        self.in_transaction_calls: list[str] = []

    def get_context_token_count(self, conversation_id: int) -> int:
        return self.context_token_count

    def get_context_items(self, conversation_id: int) -> list[_StubContextItem]:
        return list(self.context_items)

    def get_summary(self, summary_id: str) -> _StubSummaryRecord | None:
        return self.summaries.get(summary_id)

    def get_distinct_depths_in_context(
        self,
        conversation_id: int,
        *,
        max_ordinal_exclusive: int | None = None,
    ) -> list[int]:
        # Leaf-pass tests don't exercise condensation; phase-2 reaches
        # the depth picker via the production ``_run_condensed_pass``
        # but never qualifies for a candidate (no summary rows have
        # been written in these scenarios). Returning the empty list
        # short-circuits the depth picker to ``None``.
        del conversation_id, max_ordinal_exclusive
        return []

    def link_summary_to_parents(
        self,
        summary_id: str,
        parent_summary_ids: list[str],
    ) -> None:
        # 04-03 condensed-pass DAG link; leaf-pass scenarios never
        # exercise this path but the structural Protocol requires the
        # method to exist on the stub.
        if self.transaction_depth > 0:
            self.in_transaction_calls.append("link_summary_to_parents")

    @contextmanager
    def with_transaction(self) -> Iterator[None]:
        self.transaction_depth += 1
        try:
            yield
        finally:
            self.transaction_depth -= 1

    def insert_summary(self, input_: CreateSummaryInput) -> None:
        if self.transaction_depth > 0:
            self.in_transaction_calls.append("insert_summary")
        self.inserted.append(input_)

    def link_summary_to_messages(
        self,
        summary_id: str,
        message_ids: list[int],
    ) -> None:
        if self.transaction_depth > 0:
            self.in_transaction_calls.append("link_summary_to_messages")
        self.linked.append((summary_id, list(message_ids)))

    def replace_context_range_with_summary(
        self,
        input_: ReplaceContextRangeInput,
    ) -> None:
        if self.transaction_depth > 0:
            self.in_transaction_calls.append("replace_context_range_with_summary")
        self.replaced.append(input_)
        # Mutate context_items to mirror the real store's behavior: drop
        # the [start_ordinal..end_ordinal] range, insert a summary item
        # at start_ordinal, resequence the survivors so ordinals are
        # contiguous. Without this, full-sweep tests would loop forever
        # because the same chunk would be re-selected every pass.
        survivors = [
            item
            for item in self.context_items
            if item.ordinal < input_.start_ordinal or item.ordinal > input_.end_ordinal
        ]
        replacement = _StubContextItem(
            ordinal=input_.start_ordinal,
            item_type="summary",
            summary_id=input_.summary_id,
        )
        combined = sorted([*survivors, replacement], key=lambda it: it.ordinal)
        # Resequence to contiguous 0..N-1.
        for new_ord, item in enumerate(combined):
            item.ordinal = new_ord
        self.context_items = combined


class _StubConversationStore:
    def __init__(
        self,
        *,
        messages: dict[int, _StubMessage] | None = None,
        parts: dict[int, list[_StubMessagePart]] | None = None,
    ) -> None:
        self.messages: dict[int, _StubMessage] = dict(messages or {})
        self.parts: dict[int, list[_StubMessagePart]] = dict(parts or {})

    def get_message_by_id(
        self,
        message_id: int,
        *,
        include_suppressed: bool = False,
    ) -> _StubMessage | None:
        del include_suppressed
        return self.messages.get(message_id)

    def get_message_parts(self, message_id: int) -> list[_StubMessagePart]:
        return list(self.parts.get(message_id, []))


def _make_engine(
    *,
    context_token_count: int = 0,
    context_items: list[_StubContextItem] | None = None,
    messages: dict[int, _StubMessage] | None = None,
    parts: dict[int, list[_StubMessagePart]] | None = None,
    summaries: dict[str, _StubSummaryRecord] | None = None,
    config: CompactionConfig | None = None,
) -> tuple[CompactionEngine, _StubSummaryStore, _StubConversationStore]:
    summary_store = _StubSummaryStore(
        context_token_count=context_token_count,
        context_items=context_items,
        summaries=summaries,
    )
    conversation_store = _StubConversationStore(
        messages=messages,
        parts=parts,
    )
    engine = CompactionEngine(
        conversation_store=conversation_store,
        summary_store=summary_store,
        config=config or CompactionConfig(),
    )
    return engine, summary_store, conversation_store


# =============================================================================
# Helper-function tests
# =============================================================================


class TestStripEmbeddedMediaPayloads:
    """:func:`_strip_embedded_media_payloads` — TS lines 245-265."""

    def test_strips_data_url(self) -> None:
        """A ``data:<mime>;base64,...`` run is replaced with the placeholder.

        Note: the TS regex character class ``[A-Za-z0-9+/=\\s]`` includes
        whitespace, so the match is greedy across trailing text. Tests
        terminate the data URL run with a non-class char (``!`` here) so
        the boundary is explicit.
        """
        text = "hello data:image/png;base64,ABCD!suffix"
        assert _strip_embedded_media_payloads(text) == "hello [embedded media omitted]!suffix"

    def test_drops_media_path_lines(self) -> None:
        text = "before\nMEDIA:/path/to/file.jpg\nafter"
        assert _strip_embedded_media_payloads(text) == "before\nafter"

    def test_drops_pure_base64_block(self) -> None:
        # A long pure-base64 string with no punctuation → dropped.
        b64 = "A" * 256
        text = f"prefix\n{b64}\nsuffix"
        result = _strip_embedded_media_payloads(text)
        assert "AAAA" not in result
        assert result == "prefix\nsuffix"

    def test_empty_returns_empty(self) -> None:
        assert _strip_embedded_media_payloads("") == ""

    def test_non_str_returns_empty(self) -> None:
        # The function must accept non-strings without raising.
        assert _strip_embedded_media_payloads(None) == ""  # type: ignore[arg-type]
        assert _strip_embedded_media_payloads(123) == ""  # type: ignore[arg-type]


class TestLooksLikeBinaryPayload:
    """:func:`_looks_like_binary_payload` — TS lines 225-242."""

    def test_data_url_is_binary(self) -> None:
        assert _looks_like_binary_payload("data:image/png;base64,ABCD") is True

    def test_short_string_not_binary(self) -> None:
        assert _looks_like_binary_payload("A" * 100) is False

    def test_pure_base64_long_block_is_binary(self) -> None:
        # 256+ chars, multiple of 4, no punctuation.
        assert _looks_like_binary_payload("A" * 256) is True

    def test_long_string_with_punctuation_is_not_binary(self) -> None:
        # Even a long alphanumeric block with punctuation = prose.
        prose = "A" * 100 + ", " + "B" * 200
        assert _looks_like_binary_payload(prose) is False

    def test_empty_not_binary(self) -> None:
        assert _looks_like_binary_payload("") is False


class TestExtractMeaningfulMessageText:
    """:func:`_extract_meaningful_message_text` — TS lines 313-331."""

    def test_plain_text_passes_through_sanitized(self) -> None:
        assert _extract_meaningful_message_text("hello world") == "hello world"

    def test_strips_reasoning_block(self) -> None:
        """Reasoning blocks MUST be stripped — AC: encrypted signatures.

        TS line 286 ``PROVIDER_REASONING_RAW_TYPES`` set
        (``"reasoning"``, ``"thinking"``).
        """
        content = (
            '[{"type": "text", "text": "visible text"},'
            ' {"type": "thinking", "thinkingSignature": "sig123"}]'
        )
        result = _extract_meaningful_message_text(content)
        assert result == "visible text"
        assert "sig123" not in result
        assert "thinking" not in result

    def test_strips_thinking_block(self) -> None:
        content = (
            '[{"type": "text", "text": "real prose"}, {"type": "reasoning", "encrypted": "abc"}]'
        )
        result = _extract_meaningful_message_text(content)
        assert result == "real prose"

    def test_extracts_from_dict_with_text_key(self) -> None:
        content = '{"text": "extracted", "irrelevant": "x"}'
        assert _extract_meaningful_message_text(content) == "extracted"

    def test_invalid_json_falls_back_to_strip(self) -> None:
        content = "[not valid json"
        # Falls through to _strip_embedded_media_payloads on raw string.
        assert _extract_meaningful_message_text(content) == "[not valid json"

    def test_empty_string_returns_empty(self) -> None:
        assert _extract_meaningful_message_text("") == ""

    def test_image_record_only_returns_direct_text(self) -> None:
        """``MEDIA_ATTACHMENT_RAW_TYPES`` record stops at direct keys."""
        content = '{"type": "image", "alt": "a cat", "content": {"text": "should be ignored"}}'
        result = _extract_meaningful_message_text(content)
        # We get "a cat" but not "should be ignored" — nested keys are not walked.
        assert "a cat" in result
        assert "should be ignored" not in result


class TestFormatTimestamp:
    """:func:`_format_timestamp` — TS lines 125-150."""

    def test_utc_format(self) -> None:
        dt = datetime(2026, 4, 22, 14, 35, tzinfo=timezone.utc)
        assert _format_timestamp(dt, "UTC") == "2026-04-22 14:35 UTC"

    def test_invalid_tz_falls_back_to_utc(self) -> None:
        dt = datetime(2026, 4, 22, 14, 35, tzinfo=timezone.utc)
        # Bogus timezone name → fall back to UTC per TS lines 142-149.
        result = _format_timestamp(dt, "Not/A/Zone")
        assert result == "2026-04-22 14:35 UTC"

    def test_naive_datetime_treated_as_utc(self) -> None:
        dt = datetime(2026, 4, 22, 14, 35)
        # Naive → assumed UTC.
        assert _format_timestamp(dt, "UTC") == "2026-04-22 14:35 UTC"

    def test_pads_single_digits(self) -> None:
        dt = datetime(2026, 1, 5, 3, 7, tzinfo=timezone.utc)
        assert _format_timestamp(dt, "UTC") == "2026-01-05 03:07 UTC"


class TestGenerateSummaryId:
    """:func:`_generate_summary_id` — TS lines 168-176."""

    def test_id_format(self) -> None:
        sid = _generate_summary_id("some content")
        assert sid.startswith("sum_")
        assert len(sid) == 4 + 16  # "sum_" + 16 hex chars

    def test_id_is_hex_after_prefix(self) -> None:
        sid = _generate_summary_id("abc")
        suffix = sid[len("sum_") :]
        # All chars must be valid hex.
        int(suffix, 16)  # raises if non-hex

    def test_distinct_content_different_id(self) -> None:
        sid_a = _generate_summary_id("alpha")
        sid_b = _generate_summary_id("beta")
        assert sid_a != sid_b


class TestDedupeOrderedIds:
    """:func:`_dedupe_ordered_ids` — TS lines 197-207."""

    def test_basic_dedupe_preserves_order(self) -> None:
        assert _dedupe_ordered_ids(["a", "b", "a", "c", "b"]) == ["a", "b", "c"]

    def test_empty(self) -> None:
        assert _dedupe_ordered_ids([]) == []


class TestIsMediaAttachmentPart:
    """:func:`_is_media_attachment_part` — TS lines 333-347."""

    def test_file_part_type_is_media(self) -> None:
        assert _is_media_attachment_part(_StubMessagePart(part_type="file")) is True

    def test_snapshot_part_type_is_media(self) -> None:
        assert _is_media_attachment_part(_StubMessagePart(part_type="snapshot")) is True

    def test_text_part_with_image_rawtype_is_media(self) -> None:
        part = _StubMessagePart(
            part_type="text",
            metadata='{"rawType": "image"}',
        )
        assert _is_media_attachment_part(part) is True

    def test_text_part_with_nested_raw_type(self) -> None:
        part = _StubMessagePart(
            part_type="text",
            metadata='{"raw": {"type": "file"}}',
        )
        assert _is_media_attachment_part(part) is True

    def test_text_part_no_metadata_is_not_media(self) -> None:
        assert _is_media_attachment_part(_StubMessagePart(part_type="text")) is False

    def test_text_part_malformed_metadata_is_not_media(self) -> None:
        part = _StubMessagePart(part_type="text", metadata="not json {{")
        assert _is_media_attachment_part(part) is False


# =============================================================================
# _select_oldest_leaf_chunk tests
# =============================================================================


class TestSelectOldestLeafChunk:
    """``_select_oldest_leaf_chunk`` — issue 04-02 AC.

    TS source: ``compaction.ts`` lines 1005-1057.
    """

    def test_includes_contiguous_raw_messages(self) -> None:
        """The selected chunk is exactly the contiguous raw-message block.

        AC: ``_select_oldest_leaf_chunk produces the same chunk set as
        TS for the same context_items input``.
        """
        context_items = [
            _StubContextItem(ordinal=0, item_type="message", message_id=1),
            _StubContextItem(ordinal=1, item_type="message", message_id=2),
            _StubContextItem(ordinal=2, item_type="message", message_id=3),
            # Fresh tail starts here — at most 8 protected by default;
            # so 0/1/2 are outside-tail (8 raw msgs is the default count).
            # Pad with enough items so the fresh tail bites correctly.
        ]
        for i in range(3, 15):
            context_items.append(_StubContextItem(ordinal=i, item_type="message", message_id=i + 1))
        messages = {
            i + 1: _StubMessage(message_id=i + 1, content=f"m{i}", token_count=100)
            for i in range(len(context_items))
        }
        engine, _, _ = _make_engine(
            context_items=context_items,
            messages=messages,
            config=CompactionConfig(fresh_tail_count=8, leaf_chunk_tokens=10_000),
        )
        result = engine._select_oldest_leaf_chunk(conversation_id=1)
        assert isinstance(result, LeafChunkSelection)
        # With fresh_tail_count=8 and 15 messages total → fresh tail
        # starts at ordinal 7 (the 8 newest are protected). So outside-
        # tail = ordinals 0..6 (7 items).
        assert [item.ordinal for item in result.items] == [0, 1, 2, 3, 4, 5, 6]

    def test_terminates_on_summary_item(self) -> None:
        """Mid-chunk summary stops the walk.

        AC: ``Chunk-selection terminates on any non-message item``.
        TS lines 1037-1039.
        """
        context_items = [
            _StubContextItem(ordinal=0, item_type="message", message_id=1),
            _StubContextItem(ordinal=1, item_type="message", message_id=2),
            _StubContextItem(ordinal=2, item_type="summary", summary_id="sum_x"),
            _StubContextItem(ordinal=3, item_type="message", message_id=3),
            # Fresh-tail padding.
        ]
        for i in range(4, 16):
            context_items.append(_StubContextItem(ordinal=i, item_type="message", message_id=i + 1))
        messages = {
            mid: _StubMessage(message_id=mid, content="m", token_count=100)
            for mid in (1, 2, 3, *range(5, 17))
        }
        engine, _, _ = _make_engine(
            context_items=context_items,
            messages=messages,
            config=CompactionConfig(fresh_tail_count=8, leaf_chunk_tokens=10_000),
        )
        result = engine._select_oldest_leaf_chunk(conversation_id=1)
        # Chunk should stop AT the summary (ordinal 2) — items 0,1 only.
        assert [item.ordinal for item in result.items] == [0, 1]

    def test_terminates_on_arbitrary_non_message_type(self) -> None:
        """ANY non-message item terminates — guards future item types.

        AC: ``terminates on any non-message item (NOT just summaries)``.
        """
        context_items = [
            _StubContextItem(ordinal=0, item_type="message", message_id=1),
            _StubContextItem(ordinal=1, item_type="future_type"),
            _StubContextItem(ordinal=2, item_type="message", message_id=2),
        ]
        for i in range(3, 15):
            context_items.append(_StubContextItem(ordinal=i, item_type="message", message_id=i + 1))
        messages = {1: _StubMessage(message_id=1, content="m", token_count=100)}
        for i in range(2, 17):
            messages[i] = _StubMessage(message_id=i, content="m", token_count=100)
        engine, _, _ = _make_engine(
            context_items=context_items,
            messages=messages,
            config=CompactionConfig(fresh_tail_count=8, leaf_chunk_tokens=10_000),
        )
        result = engine._select_oldest_leaf_chunk(conversation_id=1)
        assert [item.ordinal for item in result.items] == [0]

    def test_skips_leading_non_messages_before_starting(self) -> None:
        """Pre-chunk non-messages are skipped, walk continues.

        TS lines 1033-1035 — ``if (!started) continue``.
        """
        context_items = [
            _StubContextItem(ordinal=0, item_type="summary", summary_id="sum_a"),
            _StubContextItem(ordinal=1, item_type="message", message_id=1),
            _StubContextItem(ordinal=2, item_type="message", message_id=2),
        ]
        for i in range(3, 15):
            context_items.append(_StubContextItem(ordinal=i, item_type="message", message_id=i + 1))
        messages = {
            i: _StubMessage(message_id=i, content="m", token_count=100) for i in range(1, 17)
        }
        engine, _, _ = _make_engine(
            context_items=context_items,
            messages=messages,
            config=CompactionConfig(fresh_tail_count=8, leaf_chunk_tokens=10_000),
        )
        result = engine._select_oldest_leaf_chunk(conversation_id=1)
        # Started at ordinal 1, contiguous raw run until fresh tail.
        assert result.items[0].ordinal == 1

    def test_respects_token_cap(self) -> None:
        """Chunk size ≤ ``leaf_chunk_tokens`` when multiple messages exist.

        AC: ``Chunk size ≤ leaf_chunk_tokens unless single message
        exceeds``.
        """
        context_items = [
            _StubContextItem(ordinal=0, item_type="message", message_id=1),
            _StubContextItem(ordinal=1, item_type="message", message_id=2),
            _StubContextItem(ordinal=2, item_type="message", message_id=3),
        ]
        for i in range(3, 15):
            context_items.append(_StubContextItem(ordinal=i, item_type="message", message_id=i + 1))
        messages = {
            mid: _StubMessage(message_id=mid, content="m", token_count=400) for mid in range(1, 17)
        }
        engine, _, _ = _make_engine(
            context_items=context_items,
            messages=messages,
            config=CompactionConfig(fresh_tail_count=8, leaf_chunk_tokens=900),
        )
        result = engine._select_oldest_leaf_chunk(conversation_id=1)
        # 400 + 400 = 800 ≤ 900; adding a third (1200) would exceed →
        # break, only 2 items in chunk.
        assert len(result.items) == 2
        assert result.threshold == 900

    def test_always_includes_at_least_one_message_even_if_over_cap(self) -> None:
        """Always-include-≥1 invariant.

        AC: ``Chunk always includes ≥1 message even if it alone
        exceeds leaf_chunk_tokens``.
        TS lines 1045-1046 (``chunk.length > 0`` gate).
        """
        context_items = [
            _StubContextItem(ordinal=0, item_type="message", message_id=1),
        ]
        for i in range(1, 12):
            context_items.append(_StubContextItem(ordinal=i, item_type="message", message_id=i + 1))
        messages = {
            1: _StubMessage(message_id=1, content="big", token_count=99_999),
        }
        for i in range(2, 13):
            messages[i] = _StubMessage(message_id=i, content="m", token_count=100)
        engine, _, _ = _make_engine(
            context_items=context_items,
            messages=messages,
            config=CompactionConfig(fresh_tail_count=8, leaf_chunk_tokens=500),
        )
        result = engine._select_oldest_leaf_chunk(conversation_id=1)
        # First message alone is 99k tokens >> 500 cap, but we MUST
        # include it (the chunk-length gate guarantees forward
        # progress even on oversize singletons).
        assert len(result.items) == 1
        assert result.items[0].message_id == 1

    def test_strict_less_than_fresh_tail_ordinal(self) -> None:
        """Chunk stops STRICTLY BEFORE the fresh-tail boundary.

        AC: ``Chunk stops STRICTLY BEFORE fresh_tail_ordinal (uses
        <, not <=)``.
        TS lines 1015-1017, 1028-1030.
        """
        # 9 messages with fresh_tail_count=8 → fresh tail covers
        # ordinals 1..8. Outside-tail = ordinal 0 only.
        context_items = [
            _StubContextItem(ordinal=i, item_type="message", message_id=i + 1) for i in range(9)
        ]
        messages = {
            i + 1: _StubMessage(message_id=i + 1, content="m", token_count=100) for i in range(9)
        }
        engine, _, _ = _make_engine(
            context_items=context_items,
            messages=messages,
            config=CompactionConfig(fresh_tail_count=8, leaf_chunk_tokens=10_000),
        )
        result = engine._select_oldest_leaf_chunk(conversation_id=1)
        # Only ordinal 0 is strictly below the fresh-tail boundary.
        assert [item.ordinal for item in result.items] == [0]

    def test_empty_when_no_raw_messages_outside_tail(self) -> None:
        """``items`` is empty when all messages are inside the fresh tail."""
        # 3 messages, fresh_tail_count=8 → all 3 protected.
        context_items = [
            _StubContextItem(ordinal=i, item_type="message", message_id=i + 1) for i in range(3)
        ]
        messages = {
            i + 1: _StubMessage(message_id=i + 1, content="m", token_count=100) for i in range(3)
        }
        engine, _, _ = _make_engine(
            context_items=context_items,
            messages=messages,
            config=CompactionConfig(fresh_tail_count=8),
        )
        result = engine._select_oldest_leaf_chunk(conversation_id=1)
        assert result.items == []

    def test_override_threshold_takes_precedence(self) -> None:
        context_items = [
            _StubContextItem(ordinal=0, item_type="message", message_id=1),
            _StubContextItem(ordinal=1, item_type="message", message_id=2),
        ]
        for i in range(2, 15):
            context_items.append(_StubContextItem(ordinal=i, item_type="message", message_id=i + 1))
        messages = {
            i: _StubMessage(message_id=i, content="m", token_count=400) for i in range(1, 17)
        }
        engine, _, _ = _make_engine(
            context_items=context_items,
            messages=messages,
            config=CompactionConfig(fresh_tail_count=8, leaf_chunk_tokens=10_000),
        )
        result = engine._select_oldest_leaf_chunk(
            conversation_id=1,
            leaf_chunk_tokens_override=500,
        )
        assert result.threshold == 500
        # 400 + 400 = 800 > 500 cap; always-one says include first only.
        assert len(result.items) == 1


# =============================================================================
# _resolve_prior_leaf_summary_context tests
# =============================================================================


class TestResolvePriorLeafSummaryContext:
    """``_resolve_prior_leaf_summary_context`` — TS lines 1065-1104."""

    def test_returns_none_for_empty_message_items(self) -> None:
        engine, _, _ = _make_engine()
        assert engine._resolve_prior_leaf_summary_context(1, []) is None

    def test_joins_last_two_prior_summaries(self) -> None:
        """The result joins the most-recent 2 prior summaries with ``\\n\\n``.

        AC: ``Prior-summary context is exactly the last 2 summary items
        before chunk start, joined `\\n\\n```.
        """
        context_items = [
            _StubContextItem(ordinal=0, item_type="summary", summary_id="sum_a"),
            _StubContextItem(ordinal=1, item_type="summary", summary_id="sum_b"),
            _StubContextItem(ordinal=2, item_type="summary", summary_id="sum_c"),
            _StubContextItem(ordinal=3, item_type="message", message_id=10),
        ]
        summaries = {
            "sum_a": _StubSummaryRecord(content="A content"),
            "sum_b": _StubSummaryRecord(content="B content"),
            "sum_c": _StubSummaryRecord(content="C content"),
        }
        engine, _, _ = _make_engine(
            context_items=context_items,
            summaries=summaries,
        )
        # The "chunk" starts at ordinal 3.
        chunk = [context_items[3]]
        result = engine._resolve_prior_leaf_summary_context(1, chunk)
        # Should be the last 2 — "B content" + "C content".
        assert result == "B content\n\nC content"

    def test_strips_whitespace_from_summary_content(self) -> None:
        context_items = [
            _StubContextItem(ordinal=0, item_type="summary", summary_id="sum_a"),
            _StubContextItem(ordinal=1, item_type="message", message_id=10),
        ]
        summaries = {"sum_a": _StubSummaryRecord(content="  trimmed  \n")}
        engine, _, _ = _make_engine(
            context_items=context_items,
            summaries=summaries,
        )
        result = engine._resolve_prior_leaf_summary_context(1, [context_items[1]])
        assert result == "trimmed"

    def test_filters_empty_summaries(self) -> None:
        context_items = [
            _StubContextItem(ordinal=0, item_type="summary", summary_id="sum_a"),
            _StubContextItem(ordinal=1, item_type="summary", summary_id="sum_b"),
            _StubContextItem(ordinal=2, item_type="message", message_id=10),
        ]
        summaries = {
            "sum_a": _StubSummaryRecord(content="   "),
            "sum_b": _StubSummaryRecord(content="real"),
        }
        engine, _, _ = _make_engine(
            context_items=context_items,
            summaries=summaries,
        )
        result = engine._resolve_prior_leaf_summary_context(1, [context_items[2]])
        # Empty-content summaries are dropped from the join.
        assert result == "real"

    def test_returns_none_when_all_priors_empty(self) -> None:
        context_items = [
            _StubContextItem(ordinal=0, item_type="summary", summary_id="sum_a"),
            _StubContextItem(ordinal=1, item_type="message", message_id=10),
        ]
        summaries = {"sum_a": _StubSummaryRecord(content="")}
        engine, _, _ = _make_engine(
            context_items=context_items,
            summaries=summaries,
        )
        assert engine._resolve_prior_leaf_summary_context(1, [context_items[1]]) is None

    def test_returns_none_when_no_prior_summaries(self) -> None:
        context_items = [
            _StubContextItem(ordinal=0, item_type="message", message_id=10),
        ]
        engine, _, _ = _make_engine(context_items=context_items)
        assert engine._resolve_prior_leaf_summary_context(1, [context_items[0]]) is None


# =============================================================================
# _annotate_media_content tests
# =============================================================================


class TestAnnotateMediaContent:
    """``_annotate_media_content`` — TS lines 1457-1485."""

    def test_passthrough_when_no_media_parts(self) -> None:
        engine, _, _ = _make_engine(
            parts={1: [_StubMessagePart(part_type="text", text_content="hi")]},
        )
        assert engine._annotate_media_content(1, "hello") == "hello"

    def test_pure_media_becomes_media_attachment(self) -> None:
        """AC: pure-media messages become ``[Media attachment]``."""
        engine, _, _ = _make_engine(
            parts={1: [_StubMessagePart(part_type="file")]},
        )
        # Content empty / pure-media → no prose remains.
        assert engine._annotate_media_content(1, "") == "[Media attachment]"

    def test_mixed_parts_get_suffix(self) -> None:
        """AC: media-mostly messages keep text + ``[with media attachment]``."""
        engine, _, _ = _make_engine(
            parts={
                1: [
                    _StubMessagePart(part_type="text", text_content="actual prose"),
                    _StubMessagePart(part_type="file"),
                ]
            },
        )
        result = engine._annotate_media_content(1, "actual prose")
        assert result.endswith("[with media attachment]")
        assert "actual prose" in result

    def test_no_double_annotation(self) -> None:
        engine, _, _ = _make_engine(
            parts={
                1: [
                    _StubMessagePart(
                        part_type="text",
                        text_content="prose [with media attachment]",
                    ),
                    _StubMessagePart(part_type="file"),
                ]
            },
        )
        result = engine._annotate_media_content(1, "prose [with media attachment]")
        # Suffix appears exactly once.
        assert result.count("[with media attachment]") == 1

    def test_falls_back_to_content_when_parts_empty(self) -> None:
        """When parts list is empty (no media), no annotation happens."""
        engine, _, _ = _make_engine(parts={1: []})
        assert engine._annotate_media_content(1, "original") == "original"


# =============================================================================
# Leaf-pass body — _run_leaf_pass with stub stores
# =============================================================================


def _scripted_summarize(output: str) -> SummarizeFn:
    """Build a :data:`SummarizeFn` that returns ``output`` for any call."""

    def _fn(text: str, aggressive: bool = False, options: dict[str, Any] | None = None) -> str:
        del text, aggressive, options
        return output

    return _fn


class TestLeafPassBody:
    """``_run_leaf_pass`` / ``_leaf_pass`` — end-to-end leaf-pass body."""

    def _setup_engine_with_messages(
        self,
    ) -> tuple[
        CompactionEngine,
        _StubSummaryStore,
        _StubConversationStore,
    ]:
        # 12 raw messages, fresh_tail_count=8 → outside-tail = 0..3.
        context_items = [
            _StubContextItem(ordinal=i, item_type="message", message_id=i + 1) for i in range(12)
        ]
        messages = {
            i + 1: _StubMessage(
                message_id=i + 1,
                content=f"message-{i}",
                token_count=100,
                created_at=datetime(2026, 4, 22, 10, i, tzinfo=timezone.utc),
            )
            for i in range(12)
        }
        return _make_engine(
            context_items=context_items,
            messages=messages,
            config=CompactionConfig(fresh_tail_count=8, leaf_chunk_tokens=10_000),
        )

    def test_returns_none_when_no_compactable_chunk(self) -> None:
        """Empty chunk → ``summary=None, auth_failure=False`` (clean break)."""
        engine, _, _ = _make_engine(context_items=[])
        outcome = engine._run_leaf_pass(
            conversation_id=1,
            summarize=_scripted_summarize("ignored"),
            previous_summary_content=None,
            summary_model=None,
        )
        assert isinstance(outcome, LeafPassOutcome)
        assert outcome.summary is None
        # CRITICAL: empty chunk is NOT an auth failure — protocol
        # split per PR #81 reviewer MAJOR.
        assert outcome.auth_failure is False

    def test_persists_summary_with_correct_dag_edges(self) -> None:
        """AC: ``link_summary_to_messages`` called with chunk message IDs.

        AC: ``Summary insert uses sum_ + sha256(content + str(now_ms))[:16]``.
        AC: ``replace_context_range_with_summary called inside same
        transaction as insert_summary``.
        """
        engine, summary_store, _ = self._setup_engine_with_messages()
        outcome = engine._run_leaf_pass(
            conversation_id=1,
            summarize=_scripted_summarize("Summary text"),
            previous_summary_content=None,
            summary_model=None,
        )
        assert outcome.auth_failure is False
        assert outcome.summary is not None
        assert isinstance(outcome.summary, LeafPassResult)
        result = outcome.summary
        # ID format check.
        assert result.summary_id.startswith("sum_")
        assert len(result.summary_id) == 4 + 16
        # All three persistence calls happened inside one transaction.
        assert summary_store.in_transaction_calls == [
            "insert_summary",
            "link_summary_to_messages",
            "replace_context_range_with_summary",
        ]
        # Linked messages are the chunk's message_ids in order.
        assert len(summary_store.linked) == 1
        linked_summary_id, linked_msgs = summary_store.linked[0]
        assert linked_summary_id == result.summary_id
        # Outside-tail = ordinals 0..3, message_ids 1..4.
        assert linked_msgs == [1, 2, 3, 4]
        # Replace call spans the full chunk range.
        assert len(summary_store.replaced) == 1
        replace = summary_store.replaced[0]
        assert replace.conversation_id == 1
        assert replace.start_ordinal == 0
        assert replace.end_ordinal == 3
        assert replace.summary_id == result.summary_id
        # Insert payload looks reasonable.
        assert len(summary_store.inserted) == 1
        insert = summary_store.inserted[0]
        assert insert.kind == "leaf"
        assert insert.depth == 0
        assert insert.content == "Summary text"
        assert insert.source_message_token_count == 400  # 4 × 100

    def test_auth_failure_returns_outcome_with_flag_set(self) -> None:
        """AC: Auth-failure path returns ``LeafPassOutcome(summary=None, auth_failure=True)``.

        The flag is what lets :meth:`compact_full_sweep` set
        ``CompactionResult.auth_failure=True`` (the PR #81 reviewer
        MAJOR fix). Before 04-02 the protocol was just ``None`` and
        the sweep could not distinguish auth-failure from empty-chunk.
        """
        engine, summary_store, _ = self._setup_engine_with_messages()

        def auth_failing(text: str, aggressive: bool = False, options: dict | None = None) -> str:
            del text, aggressive, options
            raise LcmProviderAuthError("provider down")

        outcome = engine._run_leaf_pass(
            conversation_id=1,
            summarize=auth_failing,
            previous_summary_content=None,
            summary_model=None,
        )
        assert outcome.summary is None
        assert outcome.auth_failure is True
        # CRITICAL: nothing got persisted on auth failure.
        assert summary_store.inserted == []
        assert summary_store.linked == []
        assert summary_store.replaced == []

    def test_empty_summarizer_output_returns_outcome_without_auth_flag(self) -> None:
        """An empty summarizer return is a voluntary skip (TS lines 1544-1549).

        Must NOT trip ``auth_failure`` — only ``LcmProviderAuthError``
        does that.
        """
        engine, summary_store, _ = self._setup_engine_with_messages()
        outcome = engine._run_leaf_pass(
            conversation_id=1,
            summarize=_scripted_summarize(""),
            previous_summary_content=None,
            summary_model=None,
        )
        assert outcome.summary is None
        assert outcome.auth_failure is False
        assert summary_store.inserted == []

    def test_strips_reasoning_blocks_from_message_text(self) -> None:
        """AC: Reasoning blocks stripped before concatenation.

        Verifies the load-bearing behavior — encrypted reasoning
        content NEVER reaches the summarizer's prompt.
        """
        # Messages with embedded thinking blocks.
        context_items = [
            _StubContextItem(ordinal=i, item_type="message", message_id=i + 1) for i in range(10)
        ]
        messages = {
            i + 1: _StubMessage(
                message_id=i + 1,
                content=(
                    '[{"type": "text", "text": "real text"},'
                    f' {{"type": "thinking", "thinkingSignature": "SECRET-SIG-{i}"}}]'
                ),
                token_count=50,
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
            for i in range(10)
        }
        captured_text: list[str] = []

        def capture(text: str, aggressive: bool = False, options: dict | None = None) -> str:
            del aggressive, options
            captured_text.append(text)
            return "summary"

        engine, _, _ = _make_engine(
            context_items=context_items,
            messages=messages,
            config=CompactionConfig(fresh_tail_count=8),
        )
        outcome = engine._run_leaf_pass(
            conversation_id=1,
            summarize=capture,
            previous_summary_content=None,
            summary_model=None,
        )
        assert outcome.summary is not None
        assert outcome.auth_failure is False
        assert len(captured_text) == 1
        # No SECRET-SIG bytes in what we send to the summarizer.
        assert "SECRET-SIG" not in captured_text[0]
        assert "thinkingSignature" not in captured_text[0]
        # But the real text DID make it.
        assert "real text" in captured_text[0]

    def test_pure_media_message_becomes_media_attachment(self) -> None:
        """AC: Pure-media messages become ``[Media attachment]`` in input."""
        context_items = [
            _StubContextItem(ordinal=i, item_type="message", message_id=i + 1) for i in range(10)
        ]
        messages = {
            i + 1: _StubMessage(
                message_id=i + 1,
                content="",  # pure media — no prose
                token_count=50,
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
            for i in range(10)
        }
        parts = {i + 1: [_StubMessagePart(part_type="file")] for i in range(10)}
        captured: list[str] = []

        def capture(text: str, aggressive: bool = False, options: dict | None = None) -> str:
            del aggressive, options
            captured.append(text)
            return "summary"

        engine, _, _ = _make_engine(
            context_items=context_items,
            messages=messages,
            parts=parts,
            config=CompactionConfig(fresh_tail_count=8),
        )
        outcome = engine._run_leaf_pass(
            conversation_id=1,
            summarize=capture,
            previous_summary_content=None,
            summary_model=None,
        )
        assert outcome.summary is not None
        assert outcome.auth_failure is False
        assert "[Media attachment]" in captured[0]

    def test_passes_previous_summary_to_summarizer(self) -> None:
        """AC: Iterative continuity — caller's previous_summary wins."""
        engine, _, _ = self._setup_engine_with_messages()
        captured_options: list[dict[str, Any] | None] = []

        def capture(text: str, aggressive: bool = False, options: dict | None = None) -> str:
            del text, aggressive
            captured_options.append(options)
            return "summary"

        engine._run_leaf_pass(
            conversation_id=1,
            summarize=capture,
            previous_summary_content="prior pass content",
            summary_model=None,
        )
        assert captured_options[0] is not None
        assert captured_options[0]["previous_summary"] == "prior pass content"

    def test_falls_back_to_context_walk_when_no_continuity(self) -> None:
        """When ``previous_summary_content`` is None, walk context items."""
        context_items = [
            _StubContextItem(ordinal=0, item_type="summary", summary_id="sum_old"),
            *(
                _StubContextItem(ordinal=i + 1, item_type="message", message_id=i + 1)
                for i in range(11)
            ),
        ]
        summaries = {"sum_old": _StubSummaryRecord(content="historical context")}
        messages = {
            i + 1: _StubMessage(
                message_id=i + 1,
                content=f"m{i}",
                token_count=50,
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
            for i in range(11)
        }
        captured: list[dict[str, Any] | None] = []

        def capture(text: str, aggressive: bool = False, options: dict | None = None) -> str:
            del text, aggressive
            captured.append(options)
            return "summary"

        engine, _, _ = _make_engine(
            context_items=context_items,
            messages=messages,
            summaries=summaries,
            config=CompactionConfig(fresh_tail_count=8),
        )
        engine._run_leaf_pass(
            conversation_id=1,
            summarize=capture,
            previous_summary_content=None,
            summary_model=None,
        )
        # Resolved from context walk.
        assert captured[0] is not None
        assert captured[0]["previous_summary"] == "historical context"

    def test_summary_id_format_is_sha256_prefixed(self) -> None:
        """AC: Summary ID has the ``sum_<16 hex chars>`` format."""
        engine, _, _ = self._setup_engine_with_messages()
        outcome = engine._run_leaf_pass(
            conversation_id=1,
            summarize=_scripted_summarize("summary"),
            previous_summary_content=None,
            summary_model=None,
        )
        assert outcome.summary is not None
        result = outcome.summary
        assert result.summary_id.startswith("sum_")
        # Suffix is valid hex of length 16.
        suffix = result.summary_id[len("sum_") :]
        assert len(suffix) == 16
        int(suffix, 16)

    def test_concatenation_uses_timestamp_header(self) -> None:
        """Each message gets a ``[YYYY-MM-DD HH:mm TZ]`` header."""
        # Build a 9-message context so fresh_tail_count=8 protects 8
        # leaving ordinal 0 outside-tail.
        context_items = [
            _StubContextItem(ordinal=i, item_type="message", message_id=i + 1) for i in range(9)
        ]
        messages = {
            i + 1: _StubMessage(
                message_id=i + 1,
                content=f"text-{i}",
                token_count=50,
                created_at=datetime(2026, 4, 22, 10, i, tzinfo=timezone.utc),
            )
            for i in range(9)
        }
        captured: list[str] = []

        def capture(text: str, aggressive: bool = False, options: dict | None = None) -> str:
            del aggressive, options
            captured.append(text)
            return "summary"

        engine, _, _ = _make_engine(
            context_items=context_items,
            messages=messages,
            config=CompactionConfig(fresh_tail_count=8, timezone="UTC"),
        )
        engine._run_leaf_pass(
            conversation_id=1,
            summarize=capture,
            previous_summary_content=None,
            summary_model=None,
        )
        # First message at 10:00 UTC.
        assert "[2026-04-22 10:00 UTC]" in captured[0]
        assert "text-0" in captured[0]

    def test_extracts_file_ids_into_insert(self) -> None:
        """The summary's ``file_ids`` carries file references from chunk content."""
        context_items = [
            _StubContextItem(ordinal=i, item_type="message", message_id=i + 1) for i in range(9)
        ]
        messages = {
            1: _StubMessage(
                message_id=1,
                content="references file_aaaaaaaaaaaaaaaa and file_bbbbbbbbbbbbbbbb",
                token_count=50,
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ),
        }
        for i in range(1, 9):
            messages[i + 1] = _StubMessage(
                message_id=i + 1,
                content="no refs",
                token_count=50,
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        engine, summary_store, _ = _make_engine(
            context_items=context_items,
            messages=messages,
            config=CompactionConfig(fresh_tail_count=8),
        )
        engine._run_leaf_pass(
            conversation_id=1,
            summarize=_scripted_summarize("ok"),
            previous_summary_content=None,
            summary_model=None,
        )
        insert = summary_store.inserted[0]
        assert insert.file_ids is not None
        assert "file_aaaaaaaaaaaaaaaa" in insert.file_ids
        assert "file_bbbbbbbbbbbbbbbb" in insert.file_ids

    def test_passes_summary_model_through_to_insert(self) -> None:
        engine, summary_store, _ = self._setup_engine_with_messages()
        engine._run_leaf_pass(
            conversation_id=1,
            summarize=_scripted_summarize("ok"),
            previous_summary_content=None,
            summary_model="custom-model",
        )
        assert summary_store.inserted[0].model == "custom-model"


# =============================================================================
# Compact-full-sweep integration with the real leaf-pass body
# =============================================================================


class TestCompactFullSweepUsesLeafPass:
    """End-to-end: ``compact_full_sweep`` drives the real ``_run_leaf_pass``."""

    def test_full_sweep_runs_leaf_pass_when_triggered(self) -> None:
        """Triggered sweep runs the real ``_leaf_pass`` once and stops.

        Uses ``force=False`` so the under-threshold short-circuit (TS
        lines 705-708) fires after one successful pass. With ``tokens
        _before = 6000`` against a ``threshold = 750`` (10k × 0.075),
        the soft trigger fires; the post-pass running count is below
        threshold because the stub summarize returns a tiny string,
        which is enough to short-circuit out cleanly.
        """
        context_items = [
            _StubContextItem(ordinal=i, item_type="message", message_id=i + 1) for i in range(12)
        ]
        messages = {
            i + 1: _StubMessage(
                message_id=i + 1,
                content=f"msg{i}",
                token_count=100,
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
            for i in range(12)
        }
        # context_token_count > threshold so evaluate() returns
        # should_compact=True; pass-1 delta drops the running count
        # well below threshold for clean phase-1 exit.
        engine, summary_store, _ = _make_engine(
            context_token_count=6000,
            context_items=context_items,
            messages=messages,
            config=CompactionConfig(
                fresh_tail_count=8,
                leaf_chunk_tokens=10_000,
                context_threshold=0.075,  # threshold = 750
            ),
        )

        call_count = {"n": 0}

        def stepping(text: str, aggressive: bool = False, options: dict | None = None) -> str:
            del text, aggressive, options
            call_count["n"] += 1
            return f"summary-{call_count['n']}"

        result = engine.compact_full_sweep(
            conversation_id=1,
            token_budget=10_000,
            summarize=stepping,
            force=False,  # honor under-threshold short-circuit
        )
        # At least one leaf pass actually ran.
        assert result.action_taken is True
        assert result.created_summary_id is not None
        assert call_count["n"] >= 1
        assert len(summary_store.inserted) >= 1
        # The under-threshold short-circuit should have fired — the
        # running token count drops below threshold after pass-1's
        # 400-token chunk got replaced by a ~10-token summary, so
        # the loop should exit quickly. No condensed pass attempted
        # since phase-2 only runs while previous_tokens > threshold.
        assert result.condensed is False


# =============================================================================
# Integration test against the real SummaryStore
# =============================================================================


def _migrated_conn() -> sqlite3.Connection:
    """In-memory SQLite with LCM migrations applied."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(conn, fts5_available=False)
    return conn


def _seed_conversation(conn: sqlite3.Connection, session_id: str = "s1") -> int:
    cur = conn.execute(
        "INSERT INTO conversations (session_id, session_key, title) VALUES (?, ?, ?)",
        (session_id, f"sk_{session_id}", "Test"),
    )
    return int(cur.lastrowid)  # type: ignore[arg-type]


def _seed_message(
    conn: sqlite3.Connection,
    *,
    conv_id: int,
    seq: int,
    content: str = "msg",
    token_count: int = 100,
    role: str = "user",
) -> int:
    """Insert a message row + a context_items row pointing at it."""
    msg_cur = conn.execute(
        "INSERT INTO messages (conversation_id, seq, role, content, token_count) "
        "VALUES (?, ?, ?, ?, ?)",
        (conv_id, seq, role, content, token_count),
    )
    msg_id = int(msg_cur.lastrowid)  # type: ignore[arg-type]
    # Insert a corresponding context_items row.
    conn.execute(
        "INSERT INTO context_items (conversation_id, ordinal, item_type, message_id) "
        "VALUES (?, ?, 'message', ?)",
        (conv_id, seq, msg_id),
    )
    return msg_id


@pytest.fixture
def real_db() -> Iterator[sqlite3.Connection]:
    conn = _migrated_conn()
    try:
        yield conn
    finally:
        conn.close()


class TestLeafPassRealStoreIntegration:
    """End-to-end with real SummaryStore + ConversationStore."""

    def test_persists_summary_row_with_correct_dag_links(self, real_db: sqlite3.Connection) -> None:
        """The leaf-pass commits a summaries row + summary_messages edges.

        Verifies the atomic transaction landed:

        * ``summaries.summary_id`` row exists
        * ``summary_messages`` row per source message_id
        * ``context_items`` swapped: original raw-message rows GONE,
          one new ``item_type='summary'`` row at the start ordinal
        """
        conv_id = _seed_conversation(real_db)
        # 12 raw messages → fresh_tail_count=8 protects 4..11; outside-tail = 0..3.
        msg_ids = [
            _seed_message(real_db, conv_id=conv_id, seq=i, content=f"msg{i}") for i in range(12)
        ]

        conv_store = ConversationStore(real_db)
        summary_store = SummaryStore(real_db, fts5_available=False)
        engine = CompactionEngine(
            conversation_store=conv_store,
            summary_store=summary_store,
            config=CompactionConfig(fresh_tail_count=8, leaf_chunk_tokens=10_000),
        )

        outcome = engine._run_leaf_pass(
            conversation_id=conv_id,
            summarize=_scripted_summarize("Compacted summary"),
            previous_summary_content=None,
            summary_model="test-model",
        )
        assert outcome.summary is not None
        assert outcome.auth_failure is False
        result = outcome.summary
        # Summaries row exists.
        row = real_db.execute(
            "SELECT summary_id, conversation_id, kind, depth, content, model "
            "FROM summaries WHERE summary_id = ?",
            (result.summary_id,),
        ).fetchone()
        assert row is not None
        assert row[0] == result.summary_id
        assert row[1] == conv_id
        assert row[2] == "leaf"
        assert row[3] == 0
        assert row[4] == "Compacted summary"
        assert row[5] == "test-model"
        # summary_messages has rows for ordinals 0..3 → message_ids[0..3].
        link_rows = real_db.execute(
            "SELECT message_id, ordinal FROM summary_messages WHERE summary_id = ? "
            "ORDER BY ordinal",
            (result.summary_id,),
        ).fetchall()
        # 4 messages outside fresh tail (0..3).
        assert [r[0] for r in link_rows] == msg_ids[:4]
        # context_items shows the swap: ordinal 0 is the new summary,
        # the old message rows at ordinals 0..3 are gone.
        ctx_rows = real_db.execute(
            "SELECT ordinal, item_type, message_id, summary_id "
            "FROM context_items WHERE conversation_id = ? ORDER BY ordinal",
            (conv_id,),
        ).fetchall()
        # First item is the new summary at ordinal 0.
        assert ctx_rows[0][1] == "summary"
        assert ctx_rows[0][3] == result.summary_id
        # No leftover raw-message rows from ordinals 0..3 (they were
        # collapsed and the surviving rows were resequenced).
        # Total ctx rows = 1 (summary) + 8 (fresh-tail messages) = 9.
        assert len(ctx_rows) == 9

    def test_token_delta_reflects_running_replace(self, real_db: sqlite3.Connection) -> None:
        """``removed_tokens`` matches sum of source-message tokens."""
        conv_id = _seed_conversation(real_db)
        for i in range(12):
            _seed_message(
                real_db,
                conv_id=conv_id,
                seq=i,
                content=f"msg{i}",
                token_count=200,
            )

        conv_store = ConversationStore(real_db)
        summary_store = SummaryStore(real_db, fts5_available=False)
        engine = CompactionEngine(
            conversation_store=conv_store,
            summary_store=summary_store,
            config=CompactionConfig(fresh_tail_count=8, leaf_chunk_tokens=10_000),
        )
        outcome = engine._run_leaf_pass(
            conversation_id=conv_id,
            summarize=_scripted_summarize("Compacted"),
            previous_summary_content=None,
            summary_model=None,
        )
        assert outcome.summary is not None
        assert outcome.auth_failure is False
        result = outcome.summary
        # 4 messages outside tail × 200 tokens = 800.
        assert result.removed_tokens == 800
        # ``added_tokens`` is estimate_tokens("Compacted") — small.
        assert result.added_tokens >= 1
        assert result.added_tokens < result.removed_tokens


# =============================================================================
# Auth-failure propagation — _run_leaf_pass → compact_full_sweep (PR #81)
# =============================================================================
#
# These regression tests close the PR #81 reviewer MAJOR finding:
# before 04-02, the ``_run_leaf_pass`` protocol returned
# ``LeafPassResult | None`` and could not distinguish "empty chunk" from
# "auth failure". As a result ``compact_full_sweep`` would never set
# ``CompactionResult.auth_failure=True`` even when the summarizer
# raised :class:`LcmProviderAuthError`, and ``compact_until_under``
# would retry across a provider outage.
#
# The fix introduces :class:`LeafPassOutcome` with an explicit
# ``auth_failure`` flag (TS parity: ``compaction.ts:685-687`` —
# ``hadAuthFailure = true; break``). The phase-1 leaf-pass loop and
# phase-2 condensed-pass loop both consume the flag.


class _AuthFailingEngine(CompactionEngine):
    """Subclass whose ``_run_leaf_pass`` simulates a provider-auth failure.

    Used to drive the auth path through ``compact_full_sweep`` without
    standing up a real summarizer.
    """

    def __init__(self, **engine_kwargs: object) -> None:
        super().__init__(**engine_kwargs)  # type: ignore[arg-type]
        self.leaf_pass_calls = 0

    def _run_leaf_pass(
        self,
        *,
        conversation_id: int,
        summarize: SummarizeFn,
        previous_summary_content: str | None,
        summary_model: str | None,
    ) -> LeafPassOutcome:
        del conversation_id, summarize, previous_summary_content, summary_model
        self.leaf_pass_calls += 1
        return LeafPassOutcome(summary=None, auth_failure=True)


class _EmptyChunkEngine(CompactionEngine):
    """Subclass whose ``_run_leaf_pass`` simulates an empty-chunk return.

    Used to verify ``auth_failure`` STAYS False on the empty-chunk
    path — the bug before 04-02 was that both paths conflated.
    """

    def __init__(self, **engine_kwargs: object) -> None:
        super().__init__(**engine_kwargs)  # type: ignore[arg-type]
        self.leaf_pass_calls = 0

    def _run_leaf_pass(
        self,
        *,
        conversation_id: int,
        summarize: SummarizeFn,
        previous_summary_content: str | None,
        summary_model: str | None,
    ) -> LeafPassOutcome:
        del conversation_id, summarize, previous_summary_content, summary_model
        self.leaf_pass_calls += 1
        return LeafPassOutcome(summary=None, auth_failure=False)


class TestAuthFailurePropagation:
    """``_run_leaf_pass`` → ``compact_full_sweep`` auth-failure split (PR #81).

    Closes reviewer MAJOR finding: before this split, ``compact_full_sweep``
    could not distinguish "empty chunk" from "auth failure" because both
    funneled through the same ``None`` return. The TS source at
    ``compaction.ts:685-687`` explicitly sets ``hadAuthFailure = true``
    on the auth path, and ``compact_until_under`` (TS lines 831-838)
    reads it to short-circuit the round loop instead of retrying across
    a provider outage.
    """

    def _setup_stores(
        self,
    ) -> tuple[_StubSummaryStore, _StubConversationStore]:
        """Build stores with enough context to trigger compaction."""
        context_items = [
            _StubContextItem(ordinal=i, item_type="message", message_id=i + 1) for i in range(12)
        ]
        messages = {
            i + 1: _StubMessage(
                message_id=i + 1,
                content=f"msg{i}",
                token_count=100,
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
            for i in range(12)
        }
        # 100k token count, threshold = 10k × 0.85 = 8500 → trigger fires.
        summary_store = _StubSummaryStore(
            context_token_count=100_000,
            context_items=context_items,
        )
        conversation_store = _StubConversationStore(messages=messages)
        return summary_store, conversation_store

    def test_auth_failure_propagates_to_compact_full_sweep_result(self) -> None:
        """AC: ``CompactionResult.auth_failure=True`` when leaf-pass auth-fails.

        Mirror of TS ``hadAuthFailure = true; break`` (compaction.ts:686).
        The sweep stops after the first pass and surfaces the auth flag
        through the result. Before the protocol split the sweep would
        complete normally with ``auth_failure=False`` — the bug PR #81
        reviewer caught.
        """
        summary_store, conversation_store = self._setup_stores()
        engine = _AuthFailingEngine(
            conversation_store=conversation_store,
            summary_store=summary_store,
            config=CompactionConfig(fresh_tail_count=8),
        )
        result = engine.compact_full_sweep(
            conversation_id=1,
            token_budget=10_000,
            summarize=_scripted_summarize("ignored"),
        )
        # CRITICAL: the auth flag is now set on the CompactionResult.
        assert result.auth_failure is True
        # Sweep stopped after exactly 1 pass (the auth break).
        assert engine.leaf_pass_calls == 1
        # Auth failure happens BEFORE any pass body could run, so no
        # passes are completed and no summary was created.
        assert result.passes_completed == 0
        assert result.action_taken is False
        assert result.created_summary_id is None

    def test_empty_chunk_does_not_set_auth_failure(self) -> None:
        """AC: ``CompactionResult.auth_failure`` stays False on empty-chunk break.

        The empty-chunk path is TS lines 673-675 — a clean break that
        leaves ``hadAuthFailure`` at its initial ``false``. Before the
        protocol split, the empty-chunk path was indistinguishable from
        the auth-failure path in Python because both returned ``None``.
        This test pins the disambiguation.
        """
        summary_store, conversation_store = self._setup_stores()
        engine = _EmptyChunkEngine(
            conversation_store=conversation_store,
            summary_store=summary_store,
            config=CompactionConfig(fresh_tail_count=8),
        )
        result = engine.compact_full_sweep(
            conversation_id=1,
            token_budget=10_000,
            summarize=_scripted_summarize("ignored"),
        )
        # CRITICAL: empty-chunk does NOT set auth_failure.
        assert result.auth_failure is False
        # One call to _run_leaf_pass (returned empty → phase-1 broke).
        assert engine.leaf_pass_calls == 1
        # No pass actually produced a summary.
        assert result.passes_completed == 0
        assert result.action_taken is False

    def test_auth_failure_short_circuits_compact_until_under(self) -> None:
        """End-to-end: ``compact_until_under`` returns ``auth_failure=True`` after 1 round.

        This is the integration the protocol split exists to support:
        ``compact_until_under`` reads ``CompactionResult.auth_failure``
        at TS lines 831-838 (Python lines 2034-2042) to short-circuit
        the round loop instead of burning through ``max_rounds``
        retries against a dead provider.
        """
        summary_store, conversation_store = self._setup_stores()
        engine = _AuthFailingEngine(
            conversation_store=conversation_store,
            summary_store=summary_store,
            config=CompactionConfig(fresh_tail_count=8, max_rounds=5),
        )
        result = engine.compact_until_under(
            conversation_id=1,
            token_budget=10_000,
            summarize=_scripted_summarize("ignored"),
            target_tokens=1_000,
        )
        # The round loop broke after exactly 1 round on the auth flag.
        assert result.success is False
        assert result.auth_failure is True
        assert result.rounds == 1
        # And the engine only attempted one leaf-pass call total
        # (not max_rounds × N).
        assert engine.leaf_pass_calls == 1

    def test_leaf_pass_auth_failure_sets_outcome_flag_directly(self) -> None:
        """The :meth:`_run_leaf_pass` body itself catches and signals auth.

        Unit-level coverage of the catch-and-signal path inside
        :meth:`_leaf_pass`: when the summarizer raises
        :class:`LcmProviderAuthError`, the body returns
        ``LeafPassOutcome(summary=None, auth_failure=True)`` directly —
        the flag is set inside the engine, NOT only by the test
        subclass.
        """
        # Build the same 12-message context as TestLeafPassBody.
        context_items = [
            _StubContextItem(ordinal=i, item_type="message", message_id=i + 1) for i in range(12)
        ]
        messages = {
            i + 1: _StubMessage(
                message_id=i + 1,
                content=f"msg{i}",
                token_count=100,
                created_at=datetime(2026, 4, 22, 10, i, tzinfo=timezone.utc),
            )
            for i in range(12)
        }
        engine, _, _ = _make_engine(
            context_items=context_items,
            messages=messages,
            config=CompactionConfig(fresh_tail_count=8, leaf_chunk_tokens=10_000),
        )

        def auth_failing(text: str, aggressive: bool = False, options: dict | None = None) -> str:
            del text, aggressive, options
            raise LcmProviderAuthError("provider down")

        outcome = engine._run_leaf_pass(
            conversation_id=1,
            summarize=auth_failing,
            previous_summary_content=None,
            summary_model=None,
        )
        # The auth_failure flag is set BY the engine body (not by a
        # test override).
        assert outcome.summary is None
        assert outcome.auth_failure is True
