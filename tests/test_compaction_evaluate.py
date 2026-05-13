"""Tests for :class:`lossless_hermes.compaction.CompactionEngine` trigger evaluation.

Covers issue 04-01 acceptance criteria (``epics/04-compaction/04-01-compaction-evaluate.md``):

* :class:`CompactionDecision` dataclass shape: ``should_compact``,
  ``reason``, ``current_tokens``, ``threshold``.
* :meth:`CompactionEngine.evaluate` returns ``should_compact=True`` iff
  ``current_tokens > threshold`` (strict ``>``, NOT ``>=``).
* :meth:`CompactionEngine.evaluate` uses ``max(stored, live)`` —
  observed token count overrides stored when provided and larger.
* ``threshold = floor(context_threshold * token_budget)`` (Python
  ``int()`` truncates toward zero same as ``Math.floor`` for non-
  negative inputs).
* :meth:`CompactionEngine.evaluate_leaf_trigger` sums tokens strictly
  *outside* the fresh tail (``ordinal < fresh_tail_ordinal``).
* :meth:`evaluate_leaf_trigger` uses ``>=`` (NOT strict ``>``) — soft
  trigger fires AT the boundary.
* Both methods are sync (``def``, not ``async def``) per ADR-017.

The TS source counterparts are
``lossless-claw/test/lcm-integration.test.ts`` lines 2590-2626 (the
"evaluate" describe block); the leaf-trigger TS tests live alongside
the leaf-pass tests and are exercised transitively through the
integration tests.

### Test design — store stand-ins, not full SQLite

Trigger evaluation depends only on:

1. ``summary_store.get_context_token_count(conv_id) → int``
2. ``summary_store.get_context_items(conv_id) → list[ContextItemRecord]``
3. ``conversation_store.get_message_by_id(mid, include_suppressed=True)
   → MessageRecord | None``

We can satisfy these with lightweight in-memory stand-ins, avoiding the
test cost of a fully migrated SQLite DB. Integration coverage that uses
the real stores will land alongside Epic 04's leaf-pass tests
(04-02..04-08).

References:

* Source: ``lossless-claw/src/compaction.ts`` lines 408-459, 919-997.
* Spec: ``epics/04-compaction/04-01-compaction-evaluate.md``.
* Porting guide: ``docs/porting-guides/assembler-compaction.md``
  §"Trigger evaluation".
* ADR-017 (sync stores), ADR-024 (project layout), ADR-029 (Wave-N).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from lossless_hermes.compaction import (
    DEFAULT_LEAF_CHUNK_TOKENS,
    EMPTY_FRESH_TAIL_ORDINAL,
    CompactionConfig,
    CompactionDecision,
    CompactionEngine,
    LeafTriggerResult,
)


# ---------------------------------------------------------------------------
# Test fixtures — minimal in-memory store stand-ins
# ---------------------------------------------------------------------------


@dataclass
class _StubContextItem:
    """Minimal stand-in for :class:`ContextItemRecord`.

    Only the fields read by :class:`CompactionEngine` are present:
    ``ordinal``, ``item_type``, ``message_id``, ``summary_id``.
    """

    ordinal: int
    item_type: str
    message_id: int | None = None
    summary_id: str | None = None


@dataclass
class _StubMessage:
    """Minimal stand-in for :class:`MessageRecord`.

    Only ``content`` + ``token_count`` are inspected by compaction
    (specifically by :meth:`CompactionEngine._get_message_token_count`).
    """

    content: str
    token_count: int


class _StubSummaryStore:
    """In-memory :class:`_SummaryStoreLike` stand-in for unit tests."""

    def __init__(
        self,
        *,
        context_token_count: int = 0,
        context_items: list[_StubContextItem] | None = None,
    ) -> None:
        self._context_token_count = context_token_count
        self._context_items: list[_StubContextItem] = list(context_items or [])

    def get_context_token_count(self, conversation_id: int) -> int:
        return self._context_token_count

    def get_context_items(self, conversation_id: int) -> list[_StubContextItem]:
        # Return a copy so callers can't mutate our internal list.
        return list(self._context_items)


class _StubConversationStore:
    """In-memory :class:`_ConversationStoreLike` stand-in for unit tests."""

    def __init__(self, messages: dict[int, _StubMessage] | None = None) -> None:
        self._messages: dict[int, _StubMessage] = dict(messages or {})

    def get_message_by_id(
        self,
        message_id: int,
        *,
        include_suppressed: bool = False,
    ) -> _StubMessage | None:
        return self._messages.get(message_id)


# Convenience factories so tests stay declarative.


def _make_engine(
    *,
    context_token_count: int = 0,
    context_items: list[_StubContextItem] | None = None,
    messages: dict[int, _StubMessage] | None = None,
    config: CompactionConfig | None = None,
) -> CompactionEngine:
    """Construct a :class:`CompactionEngine` with stub stores and the given config."""
    summary_store = _StubSummaryStore(
        context_token_count=context_token_count,
        context_items=context_items,
    )
    conversation_store = _StubConversationStore(messages=messages)
    return CompactionEngine(
        conversation_store=conversation_store,
        summary_store=summary_store,
        config=config or CompactionConfig(),
    )


# ---------------------------------------------------------------------------
# evaluate() — context-level threshold trigger
# ---------------------------------------------------------------------------


class TestEvaluateUnderThreshold:
    """``current_tokens <= threshold`` → ``should_compact=False``, reason ``"none"``."""

    def test_well_under_threshold(self) -> None:
        """5k stored tokens vs 10k budget × 0.75 = 7500 threshold → no compaction."""
        engine = _make_engine(context_token_count=5_000)
        decision = engine.evaluate(conversation_id=1, token_budget=10_000)
        assert decision == CompactionDecision(
            should_compact=False,
            reason="none",
            current_tokens=5_000,
            threshold=7_500,
        )

    def test_zero_stored_zero_observed(self) -> None:
        """Zero token count returns the empty-decision shape, not an error."""
        engine = _make_engine(context_token_count=0)
        decision = engine.evaluate(conversation_id=1, token_budget=10_000)
        assert decision.should_compact is False
        assert decision.reason == "none"
        assert decision.current_tokens == 0
        assert decision.threshold == 7_500

    def test_exactly_at_threshold_does_not_fire(self) -> None:
        """``current_tokens == threshold`` → no compaction (strict ``>``).

        Verifies AC: ``should_compact=True`` iff ``current_tokens > threshold``
        (strict ``>``, NOT ``>=``). At equality the gate does NOT fire.
        TS source: compaction.ts:423.
        """
        # 7500 stored vs threshold of 7500 → ``7500 > 7500`` is False.
        engine = _make_engine(context_token_count=7_500)
        decision = engine.evaluate(conversation_id=1, token_budget=10_000)
        assert decision.should_compact is False
        assert decision.reason == "none"


class TestEvaluateOverThreshold:
    """``current_tokens > threshold`` → ``should_compact=True``, reason ``"threshold"``."""

    def test_well_over_threshold(self) -> None:
        """8k stored vs 7500 threshold → compaction fires."""
        engine = _make_engine(context_token_count=8_000)
        decision = engine.evaluate(conversation_id=1, token_budget=10_000)
        assert decision == CompactionDecision(
            should_compact=True,
            reason="threshold",
            current_tokens=8_000,
            threshold=7_500,
        )

    def test_one_over_threshold(self) -> None:
        """Just one token over → still fires (strict ``>`` boundary check)."""
        engine = _make_engine(context_token_count=7_501)
        decision = engine.evaluate(conversation_id=1, token_budget=10_000)
        assert decision.should_compact is True
        assert decision.reason == "threshold"
        assert decision.current_tokens == 7_501
        assert decision.threshold == 7_500


class TestEvaluateObservedTokenCount:
    """``observed_token_count`` participates in the ``max(stored, live)`` ordering."""

    def test_observed_wins_when_larger(self) -> None:
        """stored=5k, observed=8k → ``current_tokens=8k`` → over 7500 → fire.

        Mirrors TS test "evaluate uses observed live token count when it
        exceeds stored count" (lcm-integration.test.ts:2615-2626).
        """
        engine = _make_engine(context_token_count=5_000)
        decision = engine.evaluate(
            conversation_id=1,
            token_budget=10_000,
            observed_token_count=8_000,
        )
        assert decision.should_compact is True
        assert decision.current_tokens == 8_000
        assert decision.threshold == 7_500
        assert decision.reason == "threshold"

    def test_stored_wins_when_larger(self) -> None:
        """stored=8k, observed=5k → ``current_tokens=8k`` → fire."""
        engine = _make_engine(context_token_count=8_000)
        decision = engine.evaluate(
            conversation_id=1,
            token_budget=10_000,
            observed_token_count=5_000,
        )
        assert decision.should_compact is True
        assert decision.current_tokens == 8_000

    def test_observed_none_uses_stored_only(self) -> None:
        """``observed_token_count=None`` short-circuits the live path."""
        engine = _make_engine(context_token_count=6_000)
        decision = engine.evaluate(
            conversation_id=1,
            token_budget=10_000,
            observed_token_count=None,
        )
        assert decision.current_tokens == 6_000

    def test_observed_zero_ignored(self) -> None:
        """``observed_token_count=0`` is non-positive → ignored (TS line 417)."""
        engine = _make_engine(context_token_count=5_000)
        decision = engine.evaluate(
            conversation_id=1,
            token_budget=10_000,
            observed_token_count=0,
        )
        assert decision.current_tokens == 5_000

    def test_observed_negative_ignored(self) -> None:
        """A negative observed count is non-positive → ignored.

        TS uses ``observedTokenCount > 0`` (line 417); Python matches.
        Guards against bad caller input rather than crashing.
        """
        engine = _make_engine(context_token_count=5_000)
        decision = engine.evaluate(
            conversation_id=1,
            token_budget=10_000,
            observed_token_count=-100,
        )
        assert decision.current_tokens == 5_000


class TestEvaluateThresholdMath:
    """``threshold = floor(context_threshold * token_budget)`` boundary cases."""

    def test_custom_context_threshold(self) -> None:
        """``context_threshold=0.5`` × 10_000 → threshold=5000.

        Mirrors TS test "evaluate respects custom contextThreshold".
        """
        engine = _make_engine(
            context_token_count=5_001,
            config=CompactionConfig(context_threshold=0.5),
        )
        decision = engine.evaluate(conversation_id=1, token_budget=10_000)
        assert decision.threshold == 5_000
        assert decision.should_compact is True

    def test_threshold_floors_fractional_product(self) -> None:
        """``floor(0.75 * 1001) = 750``, not 750.75 — verify the floor.

        TS ``Math.floor`` on a positive float == Python ``int()``.
        """
        engine = _make_engine(context_token_count=750)  # exactly at floor
        decision = engine.evaluate(conversation_id=1, token_budget=1_001)
        # 0.75 * 1001 = 750.75; floor → 750.
        assert decision.threshold == 750
        # ``current_tokens == threshold`` → does NOT fire.
        assert decision.should_compact is False

    def test_zero_budget_threshold_zero(self) -> None:
        """``token_budget=0`` → ``threshold=0``; any positive current_tokens fires."""
        engine = _make_engine(context_token_count=1)
        decision = engine.evaluate(conversation_id=1, token_budget=0)
        assert decision.threshold == 0
        assert decision.current_tokens == 1
        assert decision.should_compact is True
        assert decision.reason == "threshold"

    def test_zero_budget_zero_tokens(self) -> None:
        """``token_budget=0`` + ``current_tokens=0`` → does NOT fire (``0 > 0`` is False)."""
        engine = _make_engine(context_token_count=0)
        decision = engine.evaluate(conversation_id=1, token_budget=0)
        assert decision.threshold == 0
        assert decision.current_tokens == 0
        assert decision.should_compact is False
        assert decision.reason == "none"

    def test_context_threshold_one(self) -> None:
        """``context_threshold=1.0`` → threshold equals budget."""
        engine = _make_engine(
            context_token_count=10_000,
            config=CompactionConfig(context_threshold=1.0),
        )
        decision = engine.evaluate(conversation_id=1, token_budget=10_000)
        assert decision.threshold == 10_000
        # ``10_000 > 10_000`` is False → no fire.
        assert decision.should_compact is False

    def test_context_threshold_zero(self) -> None:
        """``context_threshold=0.0`` → threshold=0; any positive count fires."""
        engine = _make_engine(
            context_token_count=1,
            config=CompactionConfig(context_threshold=0.0),
        )
        decision = engine.evaluate(conversation_id=1, token_budget=10_000)
        assert decision.threshold == 0
        assert decision.should_compact is True


class TestEvaluateIsSync:
    """:meth:`evaluate` must be sync (``def``, not ``async def``) per ADR-017."""

    def test_evaluate_returns_immediate_value(self) -> None:
        """The return value is a :class:`CompactionDecision`, not a coroutine."""
        import inspect

        engine = _make_engine(context_token_count=1)
        result = engine.evaluate(conversation_id=1, token_budget=100)
        assert isinstance(result, CompactionDecision)
        assert not inspect.iscoroutine(result)
        assert not inspect.isawaitable(result)

    def test_evaluate_method_is_not_async(self) -> None:
        """The unbound :meth:`evaluate` method itself is sync."""
        import inspect

        assert not inspect.iscoroutinefunction(CompactionEngine.evaluate)


# ---------------------------------------------------------------------------
# evaluate_leaf_trigger() — soft incremental trigger
# ---------------------------------------------------------------------------


def _build_message_context(
    n_messages: int,
    tokens_per_message: int,
    *,
    starting_ordinal: int = 0,
    starting_message_id: int = 1,
) -> tuple[list[_StubContextItem], dict[int, _StubMessage]]:
    """Build ``n`` raw-message context items + a matching messages dict.

    Each message contributes ``tokens_per_message`` tokens. ``ordinal``
    starts at ``starting_ordinal`` and ``message_id`` at
    ``starting_message_id`` — both ascend by 1 per message.

    Returns ``(context_items, messages)`` for direct use with
    :func:`_make_engine`.
    """
    context_items: list[_StubContextItem] = []
    messages: dict[int, _StubMessage] = {}
    for i in range(n_messages):
        mid = starting_message_id + i
        ord_ = starting_ordinal + i
        context_items.append(_StubContextItem(ordinal=ord_, item_type="message", message_id=mid))
        messages[mid] = _StubMessage(content=f"message {i}", token_count=tokens_per_message)
    return context_items, messages


class TestEvaluateLeafTriggerFires:
    """``raw_tokens_outside_tail >= threshold`` → ``should_compact=True``."""

    def test_default_leaf_chunk_tokens_at_boundary(self) -> None:
        """``raw_outside_tail == 20_000`` → fires (``>=``, NOT strict ``>``).

        Verifies AC: leaf trigger uses ``>=`` (NOT strict ``>``) — soft
        trigger fires AT the boundary. TS source: compaction.ts:455.
        """
        # 10 messages × 2000 tokens = 20_000 outside fresh tail.
        # Default fresh_tail_count is 8; we need >= 8 messages PLUS the
        # outside-the-tail ones. Build 18 messages, last 8 are tail.
        context_items, messages = _build_message_context(18, 2_000)
        engine = _make_engine(
            context_items=context_items,
            messages=messages,
            # Default config; fresh_tail_count=8, leaf_chunk_tokens=20_000.
        )
        result = engine.evaluate_leaf_trigger(conversation_id=1)
        # 10 messages outside the tail × 2000 tokens = 20_000. AT boundary.
        assert result.should_compact is True
        assert result.reason == "leaf-trigger"
        assert result.raw_tokens_outside_tail == 20_000
        assert result.threshold == DEFAULT_LEAF_CHUNK_TOKENS

    def test_over_default_threshold(self) -> None:
        """Well over the 20k threshold → fires."""
        # 18 messages × 1500 tokens, last 8 are tail (= 12000 protected),
        # first 10 outside (= 15000). Wait that's not over. Let me make it bigger.
        # 20 messages × 2500 tokens, last 8 are tail, first 12 outside = 30000.
        context_items, messages = _build_message_context(20, 2_500)
        engine = _make_engine(context_items=context_items, messages=messages)
        result = engine.evaluate_leaf_trigger(conversation_id=1)
        # 12 messages × 2500 = 30_000 outside tail.
        assert result.should_compact is True
        assert result.raw_tokens_outside_tail == 30_000
        assert result.threshold == DEFAULT_LEAF_CHUNK_TOKENS


class TestEvaluateLeafTriggerDoesNotFire:
    """``raw_tokens_outside_tail < threshold`` → ``should_compact=False``."""

    def test_below_default_threshold(self) -> None:
        """Just under 20_000 → does not fire."""
        # 18 messages × 1000 tokens, last 8 are tail, first 10 outside = 10000.
        context_items, messages = _build_message_context(18, 1_000)
        engine = _make_engine(context_items=context_items, messages=messages)
        result = engine.evaluate_leaf_trigger(conversation_id=1)
        assert result.should_compact is False
        assert result.reason == "below-leaf-trigger"
        assert result.raw_tokens_outside_tail == 10_000
        assert result.threshold == DEFAULT_LEAF_CHUNK_TOKENS

    def test_just_below_threshold(self) -> None:
        """``raw_outside_tail == threshold - 1`` → no fire (strict less than)."""
        # We need exactly 19_999 outside tail. Use 9 messages outside × ~2222 each
        # + 8 in tail. But we can't get exactly 19999 cleanly. Instead: build
        # 8 (tail) + 1 outside × 19999 tokens.
        outside = _StubContextItem(ordinal=0, item_type="message", message_id=1)
        msg_outside = _StubMessage(content="outside", token_count=19_999)
        tail_items = [
            _StubContextItem(ordinal=i + 1, item_type="message", message_id=i + 2) for i in range(8)
        ]
        tail_msgs = {i + 2: _StubMessage(content=f"tail-{i}", token_count=100) for i in range(8)}
        context_items = [outside, *tail_items]
        messages = {1: msg_outside, **tail_msgs}
        engine = _make_engine(context_items=context_items, messages=messages)
        result = engine.evaluate_leaf_trigger(conversation_id=1)
        assert result.raw_tokens_outside_tail == 19_999
        assert result.should_compact is False
        assert result.reason == "below-leaf-trigger"

    def test_empty_context(self) -> None:
        """No context items at all → 0 raw tokens outside tail → no fire."""
        engine = _make_engine(context_items=[])
        result = engine.evaluate_leaf_trigger(conversation_id=1)
        assert result.raw_tokens_outside_tail == 0
        assert result.should_compact is False
        assert result.reason == "below-leaf-trigger"
        assert result.threshold == DEFAULT_LEAF_CHUNK_TOKENS

    def test_only_fresh_tail_no_outside(self) -> None:
        """All messages fit in the fresh tail → 0 outside → no fire."""
        # 8 messages × 5000 each — all 8 fit in fresh_tail_count=8 → 0 outside.
        context_items, messages = _build_message_context(8, 5_000)
        engine = _make_engine(context_items=context_items, messages=messages)
        result = engine.evaluate_leaf_trigger(conversation_id=1)
        assert result.raw_tokens_outside_tail == 0
        assert result.should_compact is False


class TestEvaluateLeafTriggerOverride:
    """``leaf_chunk_tokens_override`` parameter takes precedence over config."""

    def test_override_lowers_threshold_fires_earlier(self) -> None:
        """Pass ``override=10_000`` → fires at 10k outside tail.

        Mirrors TS test "evaluate_leaf_trigger override" (per spec).
        """
        context_items, messages = _build_message_context(18, 1_000)
        engine = _make_engine(context_items=context_items, messages=messages)
        # Without override: raw_outside_tail=10_000 vs threshold=20_000 → no fire.
        result = engine.evaluate_leaf_trigger(
            conversation_id=1,
            leaf_chunk_tokens_override=10_000,
        )
        assert result.should_compact is True
        assert result.reason == "leaf-trigger"
        assert result.raw_tokens_outside_tail == 10_000
        assert result.threshold == 10_000

    def test_override_raises_threshold_blocks_fire(self) -> None:
        """Pass ``override=50_000`` → does not fire at 30k outside tail."""
        context_items, messages = _build_message_context(20, 2_500)
        engine = _make_engine(context_items=context_items, messages=messages)
        # Without override would fire (30k > 20k); override blocks it.
        result = engine.evaluate_leaf_trigger(
            conversation_id=1,
            leaf_chunk_tokens_override=50_000,
        )
        assert result.should_compact is False
        assert result.raw_tokens_outside_tail == 30_000
        assert result.threshold == 50_000

    def test_override_zero_falls_through_to_config(self) -> None:
        """``override=0`` is non-positive → falls through to config.

        Mirrors TS ``resolveLeafChunkTokens`` (compaction.ts:873-879)
        which only accepts ``override > 0``.
        """
        context_items, messages = _build_message_context(18, 2_000)
        engine = _make_engine(context_items=context_items, messages=messages)
        result = engine.evaluate_leaf_trigger(
            conversation_id=1,
            leaf_chunk_tokens_override=0,
        )
        # Falls through to default 20_000; 10 messages × 2000 = 20_000. Fires.
        assert result.threshold == DEFAULT_LEAF_CHUNK_TOKENS
        assert result.should_compact is True

    def test_override_negative_falls_through_to_config(self) -> None:
        """Negative override → falls through to config."""
        context_items, messages = _build_message_context(18, 2_000)
        engine = _make_engine(context_items=context_items, messages=messages)
        result = engine.evaluate_leaf_trigger(
            conversation_id=1,
            leaf_chunk_tokens_override=-100,
        )
        assert result.threshold == DEFAULT_LEAF_CHUNK_TOKENS

    def test_config_leaf_chunk_tokens_used(self) -> None:
        """When override is ``None``, ``config.leaf_chunk_tokens`` is used."""
        context_items, messages = _build_message_context(18, 1_000)
        engine = _make_engine(
            context_items=context_items,
            messages=messages,
            config=CompactionConfig(leaf_chunk_tokens=5_000),
        )
        result = engine.evaluate_leaf_trigger(conversation_id=1)
        # 10 × 1000 = 10000 outside tail; threshold from config = 5000.
        assert result.threshold == 5_000
        assert result.raw_tokens_outside_tail == 10_000
        assert result.should_compact is True

    def test_default_when_config_none(self) -> None:
        """When ``config.leaf_chunk_tokens=None``, fall back to default 20_000."""
        context_items, messages = _build_message_context(18, 2_000)
        engine = _make_engine(
            context_items=context_items,
            messages=messages,
            config=CompactionConfig(leaf_chunk_tokens=None),
        )
        result = engine.evaluate_leaf_trigger(conversation_id=1)
        assert result.threshold == DEFAULT_LEAF_CHUNK_TOKENS


# ---------------------------------------------------------------------------
# evaluate_leaf_trigger() — fresh tail boundary semantics
# ---------------------------------------------------------------------------


class TestEvaluateLeafTriggerFreshTailBoundary:
    """Messages inside the fresh tail contribute 0 to the sum."""

    def test_messages_in_fresh_tail_excluded(self) -> None:
        """Verifies AC: ``ordinal < fresh_tail_ordinal`` is the strict bound.

        Build 10 messages: first 2 outside tail, last 8 inside
        (fresh_tail_count=8). The 8 tail messages contribute 0 to the
        sum even though they're raw messages.
        """
        context_items, messages = _build_message_context(10, 5_000)
        engine = _make_engine(
            context_items=context_items,
            messages=messages,
            # fresh_tail_count=8 → 8 newest protected, oldest 2 outside.
        )
        result = engine.evaluate_leaf_trigger(conversation_id=1)
        # 2 outside × 5000 = 10_000 (NOT 50_000 = 10 × 5000).
        assert result.raw_tokens_outside_tail == 10_000

    def test_short_context_all_in_fresh_tail(self) -> None:
        """When all messages fit in the tail, raw_tokens_outside_tail=0."""
        # Only 3 messages; fresh_tail_count=8 ≥ 3 → all protected.
        context_items, messages = _build_message_context(3, 10_000)
        engine = _make_engine(context_items=context_items, messages=messages)
        result = engine.evaluate_leaf_trigger(conversation_id=1)
        assert result.raw_tokens_outside_tail == 0

    def test_fresh_tail_count_one(self) -> None:
        """``fresh_tail_count=1`` → only newest message protected."""
        context_items, messages = _build_message_context(5, 3_000)
        engine = _make_engine(
            context_items=context_items,
            messages=messages,
            config=CompactionConfig(fresh_tail_count=1),
        )
        result = engine.evaluate_leaf_trigger(conversation_id=1)
        # 4 outside × 3000 = 12000.
        assert result.raw_tokens_outside_tail == 12_000

    def test_summary_items_skipped_from_sum(self) -> None:
        """Summary-type items contribute 0 (only raw messages count)."""
        # Mix: 5 messages outside, 1 summary, 8 messages in tail.
        # Wait — summary at ordinal 5 is between outside and tail. The
        # fresh-tail walk filters to raw-message-only items, so the
        # summary doesn't affect the boundary calculation either. The
        # 8 newest raw messages are the tail (ordinals 6..13); the
        # boundary is the ordinal of the oldest of those (= 6). Items
        # with ordinal < 6 are: 5 raw messages (ordinals 0..4) + 1
        # summary at ordinal 5. The 5 raw messages count toward the
        # sum; the summary is skipped.
        outside_items = [
            _StubContextItem(ordinal=i, item_type="message", message_id=i + 1) for i in range(5)
        ]
        summary_item = _StubContextItem(
            ordinal=5,
            item_type="summary",
            summary_id="sum_abc",
        )
        tail_items = [
            _StubContextItem(ordinal=6 + i, item_type="message", message_id=10 + i)
            for i in range(8)
        ]
        outside_msgs = {
            i + 1: _StubMessage(content=f"out-{i}", token_count=1_000) for i in range(5)
        }
        tail_msgs = {10 + i: _StubMessage(content=f"tail-{i}", token_count=500) for i in range(8)}
        context_items = [*outside_items, summary_item, *tail_items]
        messages = {**outside_msgs, **tail_msgs}
        engine = _make_engine(context_items=context_items, messages=messages)
        result = engine.evaluate_leaf_trigger(conversation_id=1)
        # 5 raw outside × 1000 = 5000; summary skipped; tail excluded.
        assert result.raw_tokens_outside_tail == 5_000

    def test_fresh_tail_count_zero_disables_protection(self) -> None:
        """``fresh_tail_count=0`` → :data:`EMPTY_FRESH_TAIL_ORDINAL`.

        With no tail protection, the boundary is at infinity → ALL
        items with ``ordinal < infinity`` (i.e. all of them) count
        toward the sum.
        """
        context_items, messages = _build_message_context(5, 4_000)
        engine = _make_engine(
            context_items=context_items,
            messages=messages,
            config=CompactionConfig(fresh_tail_count=0),
        )
        result = engine.evaluate_leaf_trigger(conversation_id=1)
        # All 5 × 4000 = 20_000 count (no tail protection).
        assert result.raw_tokens_outside_tail == 20_000
        assert result.should_compact is True


class TestEvaluateLeafTriggerFreshTailMaxTokens:
    """``fresh_tail_max_tokens`` caps the protected tail by token sum."""

    def test_max_tokens_caps_protected_count(self) -> None:
        """When tail tokens would exceed cap, walk stops early.

        With ``fresh_tail_max_tokens=10_000`` and per-message tokens
        of 4000: newest is always kept (TS line 948-952 — the
        ``protectedCount > 0`` gate). After the newest (4k), adding
        the 2nd-newest would push protected to 8k (still under cap).
        Adding the 3rd-newest would push to 12k (over cap) → stop.
        So 2 messages protected, the rest outside.
        """
        # 6 messages: 2 will be protected (tail), 4 outside.
        context_items, messages = _build_message_context(6, 4_000)
        engine = _make_engine(
            context_items=context_items,
            messages=messages,
            config=CompactionConfig(
                fresh_tail_count=8,
                fresh_tail_max_tokens=10_000,
            ),
        )
        result = engine.evaluate_leaf_trigger(conversation_id=1)
        # Walk newest→oldest: msg5 (4k, kept, protected=4k), msg4 (4k,
        # would push to 8k ≤ 10k, kept, protected=8k), msg3 (4k, would
        # push to 12k > 10k, STOP). Tail = {msg4, msg5} → boundary
        # ordinal = msg4.ordinal = 4. Outside = ordinals 0..3 (4
        # messages × 4000 = 16_000).
        assert result.raw_tokens_outside_tail == 16_000

    def test_newest_always_kept_even_over_cap(self) -> None:
        """A single newest message that exceeds the cap is still protected.

        TS lines 948-952: the ``protectedCount > 0`` gate means the
        token cap only kicks in AFTER the first iteration. The newest
        message is always in the tail.
        """
        # Single huge message (50k) with a 10k cap.
        context_items, messages = _build_message_context(2, 50_000)
        engine = _make_engine(
            context_items=context_items,
            messages=messages,
            config=CompactionConfig(
                fresh_tail_count=8,
                fresh_tail_max_tokens=10_000,
            ),
        )
        result = engine.evaluate_leaf_trigger(conversation_id=1)
        # Newest (msg1, 50k) is kept regardless of cap. 2nd-newest
        # (msg0) would push to 100k > cap → not kept. So tail=[msg1],
        # outside=[msg0] → 50_000 raw outside.
        assert result.raw_tokens_outside_tail == 50_000


# ---------------------------------------------------------------------------
# evaluate_leaf_trigger() — token-count fallback semantics
# ---------------------------------------------------------------------------


class TestEvaluateLeafTriggerTokenFallback:
    """Token resolution falls back to :func:`estimate_tokens` on missing/zero ``token_count``."""

    def test_zero_token_count_falls_back_to_estimate(self) -> None:
        """A message with ``token_count=0`` falls back to estimate_tokens(content).

        Mirrors TS ``getMessageTokenCount`` (compaction.ts:965-978):
        when ``message.tokenCount`` is non-positive or non-finite, fall
        back to :func:`estimate_tokens`.
        """
        # Single outside message with token_count=0 but real content.
        # Use 200 ASCII chars → ~50 tokens via estimator (4 chars/token).
        ascii_content = "a" * 200
        outside = _StubContextItem(ordinal=0, item_type="message", message_id=1)
        msg = _StubMessage(content=ascii_content, token_count=0)
        # Add 8 tail messages so the outside message is actually outside.
        tail_items = [
            _StubContextItem(ordinal=i + 1, item_type="message", message_id=i + 2) for i in range(8)
        ]
        tail_msgs = {i + 2: _StubMessage(content=f"tail-{i}", token_count=100) for i in range(8)}
        context_items = [outside, *tail_items]
        messages = {1: msg, **tail_msgs}
        engine = _make_engine(context_items=context_items, messages=messages)
        result = engine.evaluate_leaf_trigger(conversation_id=1)
        # estimate_tokens("a" * 200) = ceil(200 * 0.25) = 50.
        assert result.raw_tokens_outside_tail == 50

    def test_missing_message_id_falls_back_to_zero(self) -> None:
        """A context_items row whose message_id is missing from the store → 0 tokens.

        Mirrors TS ``getMessageTokenCount`` line 968 — ``if (!message)
        return 0``. Guards against stale context_items pointing at
        deleted messages.
        """
        # Outside context_item points at message_id=999 which isn't in
        # the messages dict.
        outside = _StubContextItem(ordinal=0, item_type="message", message_id=999)
        tail_items = [
            _StubContextItem(ordinal=i + 1, item_type="message", message_id=i + 2) for i in range(8)
        ]
        tail_msgs = {i + 2: _StubMessage(content=f"tail-{i}", token_count=100) for i in range(8)}
        context_items = [outside, *tail_items]
        # 999 is NOT in messages dict.
        engine = _make_engine(context_items=context_items, messages=tail_msgs)
        result = engine.evaluate_leaf_trigger(conversation_id=1)
        # The orphaned row contributes 0; tail contributes 0 (in tail).
        assert result.raw_tokens_outside_tail == 0


class TestEvaluateLeafTriggerIsSync:
    """:meth:`evaluate_leaf_trigger` must be sync per ADR-017."""

    def test_returns_immediate_value(self) -> None:
        import inspect

        engine = _make_engine(context_items=[])
        result = engine.evaluate_leaf_trigger(conversation_id=1)
        assert isinstance(result, LeafTriggerResult)
        assert not inspect.iscoroutine(result)
        assert not inspect.isawaitable(result)

    def test_method_is_not_async(self) -> None:
        import inspect

        assert not inspect.iscoroutinefunction(CompactionEngine.evaluate_leaf_trigger)


# ---------------------------------------------------------------------------
# Dataclass shape — :class:`CompactionDecision` + :class:`LeafTriggerResult`
# ---------------------------------------------------------------------------


class TestCompactionDecisionShape:
    """:class:`CompactionDecision` matches the TS shape exactly."""

    def test_has_required_fields(self) -> None:
        """``should_compact``, ``reason``, ``current_tokens``, ``threshold``."""
        d = CompactionDecision(
            should_compact=True,
            reason="threshold",
            current_tokens=100,
            threshold=50,
        )
        assert d.should_compact is True
        assert d.reason == "threshold"
        assert d.current_tokens == 100
        assert d.threshold == 50

    def test_is_frozen(self) -> None:
        """Decisions are immutable — guards against accidental mutation downstream."""
        d = CompactionDecision(
            should_compact=False,
            reason="none",
            current_tokens=0,
            threshold=0,
        )
        with pytest.raises((AttributeError, Exception)):
            d.should_compact = True  # type: ignore[misc]

    def test_reason_accepts_threshold_none_manual(self) -> None:
        """``reason`` is :data:`CompactionReason` — three valid values.

        ``"manual"`` is reserved for operator-triggered compaction
        (08-04 ``/lcm compact`` command). ``evaluate()`` itself only
        returns ``"threshold"`` or ``"none"``, but the dataclass
        accepts ``"manual"`` for the eventual operator path.
        """
        # Just construct each — type system enforces the literal at
        # static-analysis time; runtime allows the strings.
        CompactionDecision(should_compact=True, reason="threshold", current_tokens=1, threshold=0)
        CompactionDecision(should_compact=False, reason="none", current_tokens=0, threshold=10)
        CompactionDecision(should_compact=True, reason="manual", current_tokens=0, threshold=0)


class TestLeafTriggerResultShape:
    """:class:`LeafTriggerResult` matches the documented Python shape."""

    def test_has_required_fields(self) -> None:
        """``should_compact``, ``reason``, ``raw_tokens_outside_tail``, ``threshold``."""
        r = LeafTriggerResult(
            should_compact=True,
            reason="leaf-trigger",
            raw_tokens_outside_tail=20_000,
            threshold=20_000,
        )
        assert r.should_compact is True
        assert r.reason == "leaf-trigger"
        assert r.raw_tokens_outside_tail == 20_000
        assert r.threshold == 20_000

    def test_is_frozen(self) -> None:
        """Results are immutable."""
        r = LeafTriggerResult(
            should_compact=False,
            reason="below-leaf-trigger",
            raw_tokens_outside_tail=0,
            threshold=20_000,
        )
        with pytest.raises((AttributeError, Exception)):
            r.should_compact = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# EMPTY_FRESH_TAIL_ORDINAL sentinel
# ---------------------------------------------------------------------------


class TestEmptyFreshTailOrdinal:
    """Sentinel comparator usable in ``ordinal < boundary`` checks."""

    def test_is_large_positive_int(self) -> None:
        """The sentinel must compare greater than any reasonable ordinal."""
        assert EMPTY_FRESH_TAIL_ORDINAL > 10**9
        # Sentinel is sys.maxsize on the target platform; this confirms
        # any plausible ordinal is strictly less than it.

    def test_works_as_boundary(self) -> None:
        """A small ordinal compares ``< EMPTY_FRESH_TAIL_ORDINAL``."""
        small_ordinals = [-1, 0, 1, 100, 10_000, 10**6]
        for o in small_ordinals:
            assert o < EMPTY_FRESH_TAIL_ORDINAL
