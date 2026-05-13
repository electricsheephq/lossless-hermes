"""Tests for the condensation pass (issue 04-03).

Covers the acceptance criteria from
``epics/04-compaction/04-03-compaction-condensation.md``:

* :meth:`CompactionEngine._resolve_fanout_for_depth` dispatches correctly
  on the four ``(hard_trigger, depth)`` cases.
* :meth:`CompactionEngine._select_shallowest_condensation_candidate`
  prefers the shallowest depth with a qualifying chunk.
* :meth:`CompactionEngine._select_oldest_chunk_at_depth` terminates the
  walk on depth mismatch + token cap.
* :meth:`CompactionEngine._condensed_pass` writes the new summary with
  ``kind="condensed"`` + ``depth=target_depth+1`` and links the DAG
  edge summary→parent_summaries (NOT messary→messages).
* Descendant counts accumulate from children: each child contributes
  its own subtree count + 1, plus its token count + descendant tokens.
* Prior-summary context is resolved ONLY at depth 0; deeper passes get
  ``previous_summary=None`` to mirror the D2/D3+ template's lack of a
  ``<previous_context>`` block.
* Hard-trigger sweeps relax the fanout floor via
  :meth:`_resolve_condensed_min_fanout_hard`.

### Test design — protocol-based store stand-ins, not full SQLite

Like the existing :file:`test_compaction_evaluate.py` and
:file:`test_compaction_anti_thrashing.py`, these tests use small
in-memory stubs that implement just the load-bearing subset of the
:class:`_SummaryStoreLike` Protocol the condensation methods inspect.
This keeps each test under 50 lines without giving up coverage of the
real algorithm — the only difference between a stub and the production
:class:`~lossless_hermes.store.summary.SummaryStore` is *where* the
rows live (RAM vs SQLite); the lookup / transaction surface is
identical.

References:

* TS source: ``lossless-claw/src/compaction.ts`` (LCM commit ``1f07fbd``
  on branch ``pr-613``) lines 1141-1325, 1614-1751.
* Test source: ``lossless-claw/test/compaction-maintenance-store.test.ts``
  (condensation cases).
* Spec: ``epics/04-compaction/04-03-compaction-condensation.md``.
* Porting guide: ``docs/porting-guides/assembler-compaction.md``
  §"Condensation algorithm".
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

import pytest

from lossless_hermes.compaction import (
    CompactionConfig,
    CompactionEngine,
    CondensedChunkSelection,
    CondensedPhaseCandidate,
    LeafPassOutcome,
    LeafPassResult,
)


# ---------------------------------------------------------------------------
# Test fixtures — store stand-ins
# ---------------------------------------------------------------------------


@dataclass
class _StubContextItem:
    """Minimal stand-in for :class:`ContextItemRecord`."""

    ordinal: int
    item_type: str  # "message" | "summary"
    message_id: int | None = None
    summary_id: str | None = None


@dataclass
class _StubMessage:
    """Minimal stand-in for :class:`MessageRecord`."""

    content: str
    token_count: int


@dataclass
class _StubSummary:
    """Minimal stand-in for :class:`SummaryRecord` consumed by 04-03 paths."""

    summary_id: str
    depth: int
    content: str
    token_count: int
    earliest_at: datetime | None = None
    latest_at: datetime | None = None
    created_at: datetime = field(
        default_factory=lambda: datetime(2026, 5, 14, 12, 0, 0, tzinfo=timezone.utc)
    )
    descendant_count: int = 0
    descendant_token_count: int = 0
    source_message_token_count: int = 0
    file_ids: list[str] = field(default_factory=list)


@dataclass
class _CapturedInsert:
    """Record of an ``insert_summary`` call for assertions."""

    summary_id: str
    kind: str
    depth: int
    content: str
    token_count: int
    file_ids: list[str]
    earliest_at: datetime | None
    latest_at: datetime | None
    descendant_count: int
    descendant_token_count: int
    source_message_token_count: int
    model: str | None


@dataclass
class _CapturedLinkParents:
    summary_id: str
    parent_summary_ids: list[str]


@dataclass
class _CapturedReplace:
    conversation_id: int
    start_ordinal: int
    end_ordinal: int
    summary_id: str


class _StubSummaryStore:
    """In-memory :class:`_SummaryStoreLike` stand-in for 04-03 tests.

    Carries the context items + a summaries dict; captures
    :meth:`insert_summary`, :meth:`link_summary_to_parents`, and
    :meth:`replace_context_range_with_summary` calls into list
    attributes for assertions.
    """

    def __init__(
        self,
        *,
        context_items: list[_StubContextItem] | None = None,
        summaries: dict[str, _StubSummary] | None = None,
        distinct_depths: list[int] | None = None,
        context_token_count: int = 0,
    ) -> None:
        self.context_items: list[_StubContextItem] = list(context_items or [])
        self.summaries: dict[str, _StubSummary] = dict(summaries or {})
        self.distinct_depths: list[int] = list(distinct_depths or [])
        self.context_token_count = context_token_count
        self.inserts: list[_CapturedInsert] = []
        self.link_parents_calls: list[_CapturedLinkParents] = []
        self.link_messages_calls: list[Any] = []  # tracked to assert "never called"
        self.replaces: list[_CapturedReplace] = []

    # ── Methods consumed by 04-01..04-03 ──────────────────────────────────

    def get_context_token_count(self, conversation_id: int) -> int:
        return self.context_token_count

    def get_context_items(self, conversation_id: int) -> list[_StubContextItem]:
        return list(self.context_items)

    def get_summary(
        self,
        summary_id: str,
        *,
        include_suppressed: bool = False,
    ) -> _StubSummary | None:
        return self.summaries.get(summary_id)

    def get_distinct_depths_in_context(
        self,
        conversation_id: int,
        *,
        max_ordinal_exclusive: int | None = None,
    ) -> list[int]:
        # Tests pre-seed the depth list; the production store derives
        # it from the depths of summary items below ``max_ordinal_
        # exclusive`` but the stub returns the seeded list unchanged.
        return list(self.distinct_depths)

    def link_summary_to_parents(
        self,
        summary_id: str,
        parent_summary_ids: list[str],
    ) -> None:
        self.link_parents_calls.append(
            _CapturedLinkParents(
                summary_id=summary_id,
                parent_summary_ids=list(parent_summary_ids),
            )
        )

    def link_summary_to_messages(self, summary_id: str, message_ids: list[int]) -> None:
        # Tracked so we can assert the condensation pass NEVER calls
        # this (the DAG edge is summary→parent_summaries, not
        # summary→messages).
        self.link_messages_calls.append((summary_id, list(message_ids)))

    def insert_summary(self, input_: Any) -> Any:
        # Capture the load-bearing fields off the
        # :class:`CreateSummaryInput` dataclass. Real production
        # store returns a :class:`SummaryRecord`; the stub doesn't
        # need to because callers don't read the return value.
        self.inserts.append(
            _CapturedInsert(
                summary_id=input_.summary_id,
                kind=input_.kind,
                depth=input_.depth if input_.depth is not None else -1,
                content=input_.content,
                token_count=input_.token_count,
                file_ids=list(input_.file_ids or []),
                earliest_at=input_.earliest_at,
                latest_at=input_.latest_at,
                descendant_count=input_.descendant_count or 0,
                descendant_token_count=input_.descendant_token_count or 0,
                source_message_token_count=input_.source_message_token_count or 0,
                model=input_.model,
            )
        )
        # Mirror the production insert by registering the row so
        # subsequent ``get_summary`` calls see it.
        self.summaries[input_.summary_id] = _StubSummary(
            summary_id=input_.summary_id,
            depth=input_.depth if input_.depth is not None else 0,
            content=input_.content,
            token_count=input_.token_count,
            earliest_at=input_.earliest_at,
            latest_at=input_.latest_at,
            descendant_count=input_.descendant_count or 0,
            descendant_token_count=input_.descendant_token_count or 0,
            source_message_token_count=input_.source_message_token_count or 0,
            file_ids=list(input_.file_ids or []),
        )
        return self.summaries[input_.summary_id]

    def replace_context_range_with_summary(self, input_: Any) -> None:
        self.replaces.append(
            _CapturedReplace(
                conversation_id=input_.conversation_id,
                start_ordinal=input_.start_ordinal,
                end_ordinal=input_.end_ordinal,
                summary_id=input_.summary_id,
            )
        )

    @contextmanager
    def with_transaction(self) -> Iterator[None]:
        # No-op transaction; tests don't simulate rollback.
        yield


class _StubConversationStore:
    """In-memory :class:`_ConversationStoreLike` stand-in."""

    def __init__(self, messages: dict[int, _StubMessage] | None = None) -> None:
        self._messages: dict[int, _StubMessage] = dict(messages or {})

    def get_message_by_id(
        self,
        message_id: int,
        *,
        include_suppressed: bool = False,
    ) -> _StubMessage | None:
        return self._messages.get(message_id)


def _make_engine(
    *,
    context_items: list[_StubContextItem] | None = None,
    summaries: dict[str, _StubSummary] | None = None,
    distinct_depths: list[int] | None = None,
    config: CompactionConfig | None = None,
) -> tuple[CompactionEngine, _StubSummaryStore]:
    """Construct a :class:`CompactionEngine` with default test fixtures.

    Returns the engine + the summary store so tests can inspect the
    captured insert / link / replace calls after running the method
    under test.
    """
    summary_store = _StubSummaryStore(
        context_items=context_items,
        summaries=summaries,
        distinct_depths=distinct_depths,
    )
    conversation_store = _StubConversationStore()
    cfg = config if config is not None else CompactionConfig()
    engine = CompactionEngine(
        conversation_store=conversation_store,
        summary_store=summary_store,
        config=cfg,
    )
    return engine, summary_store


def _capturing_summarize() -> tuple[Any, list[Any]]:
    """A summarize callback that records its calls and returns a fixed reply.

    Returns the callable + the call-record list so a test can:

    * assert the callback was called with ``options.is_condensed=True``,
      ``options.depth=target_depth+1``, etc., and
    * verify the returned content reaches
      :meth:`_condensed_pass`'s persistence step.
    """
    calls: list[Any] = []

    def summarize(text: str, aggressive: bool = False, options: Any = None) -> str:
        calls.append({"text": text, "aggressive": aggressive, "options": dict(options or {})})
        return "[CONDENSED SUMMARY]"

    return summarize, calls


# ---------------------------------------------------------------------------
# _resolve_fanout_for_depth — 4-case dispatch matrix
# ---------------------------------------------------------------------------


class TestResolveFanoutForDepth:
    """Cover the 4-case dispatch table for :meth:`_resolve_fanout_for_depth`.

    Mirrors TS ``resolveFanoutForDepth`` (compaction.ts lines 1173-1181).
    """

    def test_soft_depth_zero_uses_leaf_min_fanout(self) -> None:
        engine, _ = _make_engine(config=CompactionConfig(leaf_min_fanout=8))
        assert engine._resolve_fanout_for_depth(0, hard_trigger=False) == 8

    def test_soft_deeper_depth_uses_condensed_min_fanout(self) -> None:
        engine, _ = _make_engine(config=CompactionConfig(condensed_min_fanout=4))
        assert engine._resolve_fanout_for_depth(1, hard_trigger=False) == 4
        assert engine._resolve_fanout_for_depth(5, hard_trigger=False) == 4

    def test_hard_depth_zero_uses_condensed_min_fanout_hard(self) -> None:
        engine, _ = _make_engine(config=CompactionConfig(condensed_min_fanout_hard=2))
        assert engine._resolve_fanout_for_depth(0, hard_trigger=True) == 2

    def test_hard_deeper_depth_uses_condensed_min_fanout_hard(self) -> None:
        engine, _ = _make_engine(config=CompactionConfig(condensed_min_fanout_hard=2))
        # Hard trigger overrides depth dispatch for ALL depths.
        assert engine._resolve_fanout_for_depth(2, hard_trigger=True) == 2
        assert engine._resolve_fanout_for_depth(7, hard_trigger=True) == 2

    def test_non_positive_config_falls_back_to_defaults(self) -> None:
        # Negative / zero values collapse to TS defaults (8, 4, 2).
        engine, _ = _make_engine(
            config=CompactionConfig(
                leaf_min_fanout=0,
                condensed_min_fanout=-3,
                condensed_min_fanout_hard=0,
            )
        )
        assert engine._resolve_fanout_for_depth(0, hard_trigger=False) == 8
        assert engine._resolve_fanout_for_depth(2, hard_trigger=False) == 4
        assert engine._resolve_fanout_for_depth(0, hard_trigger=True) == 2


# ---------------------------------------------------------------------------
# _resolve_condensed_min_chunk_tokens — ratio floor
# ---------------------------------------------------------------------------


class TestResolveCondensedMinChunkTokens:
    """Cover :meth:`_resolve_condensed_min_chunk_tokens` (TS lines 1184-1188)."""

    def test_floor_is_max_of_target_and_ratio(self) -> None:
        # leaf_chunk_tokens=20_000 → ratio_floor = 2_000. Target=900 →
        # max(900, 2_000) = 2_000 (ratio wins).
        engine, _ = _make_engine(
            config=CompactionConfig(leaf_chunk_tokens=20_000, condensed_target_tokens=900)
        )
        assert engine._resolve_condensed_min_chunk_tokens() == 2_000

    def test_target_wins_when_higher_than_ratio(self) -> None:
        # leaf_chunk_tokens=5_000 → ratio_floor = 500. Target=900 →
        # max(900, 500) = 900 (target wins).
        engine, _ = _make_engine(
            config=CompactionConfig(leaf_chunk_tokens=5_000, condensed_target_tokens=900)
        )
        assert engine._resolve_condensed_min_chunk_tokens() == 900


# ---------------------------------------------------------------------------
# _select_oldest_chunk_at_depth — chunk-walker invariants
# ---------------------------------------------------------------------------


class TestSelectOldestChunkAtDepth:
    """Cover :meth:`_select_oldest_chunk_at_depth` termination conditions.

    Mirrors TS ``selectOldestChunkAtDepth`` (compaction.ts lines
    1230-1282).
    """

    def test_terminates_on_depth_mismatch(self) -> None:
        """A depth-1 summary mid-walk through depth=0 stops the chunk."""
        summaries = {
            "s0": _StubSummary("s0", depth=0, content="leaf 0", token_count=300),
            "s1": _StubSummary("s1", depth=0, content="leaf 1", token_count=300),
            "s_other": _StubSummary("s_other", depth=1, content="condensed", token_count=400),
            "s2": _StubSummary("s2", depth=0, content="leaf 2", token_count=300),
        }
        context_items = [
            _StubContextItem(ordinal=0, item_type="summary", summary_id="s0"),
            _StubContextItem(ordinal=1, item_type="summary", summary_id="s1"),
            _StubContextItem(ordinal=2, item_type="summary", summary_id="s_other"),
            _StubContextItem(ordinal=3, item_type="summary", summary_id="s2"),
        ]
        # fresh_tail_count=0 so the fresh tail sentinel doesn't truncate.
        engine, _ = _make_engine(
            context_items=context_items,
            summaries=summaries,
            config=CompactionConfig(fresh_tail_count=0),
        )
        sel = engine._select_oldest_chunk_at_depth(conversation_id=1, target_depth=0)
        # We expect [s0, s1] only — the depth-1 row at ordinal 2 stops
        # the walk so ``s2`` is NOT included.
        assert [item.summary_id for item in sel.items] == ["s0", "s1"]
        assert sel.summary_tokens == 600

    def test_terminates_on_non_summary_item(self) -> None:
        """A raw-message item mid-walk through summaries stops the chunk."""
        summaries = {
            "s0": _StubSummary("s0", depth=0, content="leaf 0", token_count=300),
            "s1": _StubSummary("s1", depth=0, content="leaf 1", token_count=300),
        }
        context_items = [
            _StubContextItem(ordinal=0, item_type="summary", summary_id="s0"),
            _StubContextItem(ordinal=1, item_type="message", message_id=42),
            _StubContextItem(ordinal=2, item_type="summary", summary_id="s1"),
        ]
        engine, _ = _make_engine(
            context_items=context_items,
            summaries=summaries,
            config=CompactionConfig(fresh_tail_count=0),
        )
        sel = engine._select_oldest_chunk_at_depth(conversation_id=1, target_depth=0)
        # [s0] only — message at ordinal 1 stops the started chunk.
        assert [item.summary_id for item in sel.items] == ["s0"]

    def test_respects_leaf_chunk_tokens_cap(self) -> None:
        """Adding the next summary stops the walk when it would exceed cap."""
        summaries = {
            "s0": _StubSummary("s0", depth=0, content="leaf", token_count=400),
            "s1": _StubSummary("s1", depth=0, content="leaf", token_count=400),
            "s2": _StubSummary("s2", depth=0, content="leaf", token_count=400),
        }
        context_items = [
            _StubContextItem(ordinal=0, item_type="summary", summary_id="s0"),
            _StubContextItem(ordinal=1, item_type="summary", summary_id="s1"),
            _StubContextItem(ordinal=2, item_type="summary", summary_id="s2"),
        ]
        # Cap = 900: 400 + 400 = 800 (fits), + 400 = 1200 (exceeds) → break.
        engine, _ = _make_engine(
            context_items=context_items,
            summaries=summaries,
            config=CompactionConfig(leaf_chunk_tokens=900, fresh_tail_count=0),
        )
        sel = engine._select_oldest_chunk_at_depth(conversation_id=1, target_depth=0)
        assert [item.summary_id for item in sel.items] == ["s0", "s1"]
        assert sel.summary_tokens == 800

    def test_skips_leading_non_summary_items(self) -> None:
        """Leading message rows are skipped (the chunk just starts later)."""
        summaries = {
            "s0": _StubSummary("s0", depth=0, content="leaf", token_count=300),
        }
        context_items = [
            _StubContextItem(ordinal=0, item_type="message", message_id=1),
            _StubContextItem(ordinal=1, item_type="message", message_id=2),
            _StubContextItem(ordinal=2, item_type="summary", summary_id="s0"),
        ]
        engine, _ = _make_engine(
            context_items=context_items,
            summaries=summaries,
            config=CompactionConfig(fresh_tail_count=0),
        )
        sel = engine._select_oldest_chunk_at_depth(conversation_id=1, target_depth=0)
        # The leading messages do NOT terminate the walk (chunk is
        # still empty); they're skipped.
        assert [item.summary_id for item in sel.items] == ["s0"]

    def test_skips_leading_wrong_depth_summaries(self) -> None:
        """Wrong-depth summaries before any same-depth row are skipped."""
        summaries = {
            "s_wrong": _StubSummary("s_wrong", depth=1, content="cond", token_count=300),
            "s0": _StubSummary("s0", depth=0, content="leaf", token_count=300),
        }
        context_items = [
            _StubContextItem(ordinal=0, item_type="summary", summary_id="s_wrong"),
            _StubContextItem(ordinal=1, item_type="summary", summary_id="s0"),
        ]
        engine, _ = _make_engine(
            context_items=context_items,
            summaries=summaries,
            config=CompactionConfig(fresh_tail_count=0),
        )
        sel = engine._select_oldest_chunk_at_depth(conversation_id=1, target_depth=0)
        # Leading depth-1 summary is skipped; depth-0 s0 starts the chunk.
        assert [item.summary_id for item in sel.items] == ["s0"]


# ---------------------------------------------------------------------------
# _select_shallowest_condensation_candidate — depth picker
# ---------------------------------------------------------------------------


class TestSelectShallowestCondensationCandidate:
    """Cover :meth:`_select_shallowest_condensation_candidate`.

    Mirrors TS ``selectShallowestCondensationCandidate`` (compaction.ts
    lines 1193-1222).
    """

    def test_picks_depth_zero_first_when_leaves_qualify(self) -> None:
        """Depth 0 wins when both depth 0 and depth 1 have valid chunks."""
        # 8 depth-0 leaves @ 300 tokens each = 2400 tokens (≥
        # min_chunk_tokens=2000 for leaf_chunk_tokens=20_000) and
        # passes the leaf_min_fanout=8 fanout.
        summaries: dict[str, _StubSummary] = {}
        context_items: list[_StubContextItem] = []
        for i in range(8):
            sid = f"s{i}"
            summaries[sid] = _StubSummary(sid, depth=0, content=f"leaf {i}", token_count=300)
            context_items.append(_StubContextItem(ordinal=i, item_type="summary", summary_id=sid))

        engine, _ = _make_engine(
            context_items=context_items,
            summaries=summaries,
            distinct_depths=[0, 1],
            config=CompactionConfig(
                leaf_min_fanout=8,
                condensed_min_fanout=4,
                fresh_tail_count=0,
            ),
        )
        candidate = engine._select_shallowest_condensation_candidate(
            conversation_id=1, hard_trigger=False
        )
        assert candidate is not None
        assert candidate.target_depth == 0
        assert len(candidate.chunk.items) == 8

    def test_picks_deeper_when_shallowest_chunk_below_fanout(self) -> None:
        """If depth 0 only has 3 leaves (fanout=8 fails), check depth 1."""
        summaries: dict[str, _StubSummary] = {}
        context_items: list[_StubContextItem] = []
        # 3 depth-0 leaves — below fanout 8.
        for i in range(3):
            sid = f"s_leaf_{i}"
            summaries[sid] = _StubSummary(sid, depth=0, content=f"leaf {i}", token_count=300)
            context_items.append(_StubContextItem(ordinal=i, item_type="summary", summary_id=sid))
        # The depth-0 walk would terminate at the first non-depth-0
        # row, so depth-1 candidates need their own block AFTER the
        # depth-0 block to be reachable when depth=1 is probed.
        # In production, the depth-1 walk starts from ordinal 0 again
        # (each depth is probed fresh) so the leading depth-0 rows
        # are simply skipped (the "leading non-same-depth rows are
        # skipped" branch). 4 depth-1 condensed @ 1000 tokens each.
        for i in range(4):
            sid = f"s_cond_{i}"
            summaries[sid] = _StubSummary(sid, depth=1, content=f"cond {i}", token_count=1000)
            context_items.append(
                _StubContextItem(ordinal=3 + i, item_type="summary", summary_id=sid)
            )

        engine, _ = _make_engine(
            context_items=context_items,
            summaries=summaries,
            distinct_depths=[0, 1],
            config=CompactionConfig(
                leaf_min_fanout=8,
                condensed_min_fanout=4,
                condensed_target_tokens=900,
                leaf_chunk_tokens=20_000,
                fresh_tail_count=0,
            ),
        )
        candidate = engine._select_shallowest_condensation_candidate(
            conversation_id=1, hard_trigger=False
        )
        # Depth 0 has only 3 leaves (below fanout 8) so it's skipped.
        # Depth 1 has 4 condensed @ 1000 tokens = 4000 tokens (well
        # above the 2000 min_chunk_tokens floor) and clears the
        # fanout-4 gate.
        assert candidate is not None
        assert candidate.target_depth == 1
        assert len(candidate.chunk.items) == 4

    def test_returns_none_when_no_depth_qualifies(self) -> None:
        """Below-fanout chunks at every depth return ``None``."""
        # Only 2 leaves — below fanout 8 and the hard fanout 2 isn't
        # set (soft trigger only).
        summaries = {
            "s0": _StubSummary("s0", depth=0, content="leaf", token_count=300),
            "s1": _StubSummary("s1", depth=0, content="leaf", token_count=300),
        }
        context_items = [
            _StubContextItem(ordinal=0, item_type="summary", summary_id="s0"),
            _StubContextItem(ordinal=1, item_type="summary", summary_id="s1"),
        ]
        engine, _ = _make_engine(
            context_items=context_items,
            summaries=summaries,
            distinct_depths=[0],
            config=CompactionConfig(leaf_min_fanout=8, fresh_tail_count=0),
        )
        assert (
            engine._select_shallowest_condensation_candidate(conversation_id=1, hard_trigger=False)
            is None
        )

    def test_hard_trigger_relaxes_fanout(self) -> None:
        """Hard trigger lets a 2-leaf chunk qualify when soft would skip."""
        # 2 leaves @ 1500 tokens each = 3000 tokens (above min_chunk_tokens
        # floor of 2000 for leaf_chunk_tokens=20_000). Soft fanout 8
        # fails; hard fanout 2 passes.
        summaries = {
            "s0": _StubSummary("s0", depth=0, content="leaf 0", token_count=1500),
            "s1": _StubSummary("s1", depth=0, content="leaf 1", token_count=1500),
        }
        context_items = [
            _StubContextItem(ordinal=0, item_type="summary", summary_id="s0"),
            _StubContextItem(ordinal=1, item_type="summary", summary_id="s1"),
        ]
        engine, _ = _make_engine(
            context_items=context_items,
            summaries=summaries,
            distinct_depths=[0],
            config=CompactionConfig(
                leaf_min_fanout=8,
                condensed_min_fanout_hard=2,
                condensed_target_tokens=900,
                leaf_chunk_tokens=20_000,
                fresh_tail_count=0,
            ),
        )
        # Soft trigger: below fanout → None.
        assert (
            engine._select_shallowest_condensation_candidate(conversation_id=1, hard_trigger=False)
            is None
        )
        # Hard trigger: fanout 2 passes, chunk qualifies.
        candidate = engine._select_shallowest_condensation_candidate(
            conversation_id=1, hard_trigger=True
        )
        assert candidate is not None
        assert candidate.target_depth == 0


# ---------------------------------------------------------------------------
# _condensed_pass — production body
# ---------------------------------------------------------------------------


class TestCondensedPass:
    """Cover :meth:`_condensed_pass` end-to-end.

    Mirrors TS ``condensedPass`` (compaction.ts lines 1614-1751).
    """

    def _build_qualifying_chunk(
        self,
        *,
        depth: int = 0,
        n: int = 8,
        token_count: int = 300,
        descendant_count_each: int = 0,
        descendant_token_count_each: int = 0,
        source_message_token_count_each: int = 0,
    ) -> tuple[list[_StubContextItem], dict[str, _StubSummary]]:
        """Build a depth-N chunk of N same-depth summary items + records."""
        summaries: dict[str, _StubSummary] = {}
        context_items: list[_StubContextItem] = []
        base_ts = datetime(2026, 5, 14, 12, 0, 0, tzinfo=timezone.utc)
        for i in range(n):
            sid = f"s{i}"
            summaries[sid] = _StubSummary(
                summary_id=sid,
                depth=depth,
                content=f"content {i}",
                token_count=token_count,
                earliest_at=base_ts,
                latest_at=base_ts,
                created_at=base_ts,
                descendant_count=descendant_count_each,
                descendant_token_count=descendant_token_count_each,
                source_message_token_count=source_message_token_count_each,
            )
            context_items.append(_StubContextItem(ordinal=i, item_type="summary", summary_id=sid))
        return context_items, summaries

    def test_writes_summary_with_kind_condensed_and_depth_plus_one(self) -> None:
        """Insert uses kind='condensed' and depth=target_depth+1."""
        context_items, summaries = self._build_qualifying_chunk(depth=0, n=8, token_count=300)
        engine, store = _make_engine(
            context_items=context_items,
            summaries=summaries,
            distinct_depths=[0],
            config=CompactionConfig(
                leaf_min_fanout=8,
                condensed_target_tokens=900,
                leaf_chunk_tokens=20_000,
                fresh_tail_count=0,
            ),
        )
        summarize, _ = _capturing_summarize()
        outcome = engine._run_condensed_pass(
            conversation_id=1,
            hard_trigger=False,
            summarize=summarize,
            summary_model="claude-opus-4-7",
        )
        assert isinstance(outcome, LeafPassOutcome)
        assert outcome.auth_failure is False
        result = outcome.summary
        assert result is not None
        assert isinstance(result, LeafPassResult)
        assert len(store.inserts) == 1
        insert = store.inserts[0]
        assert insert.kind == "condensed"
        assert insert.depth == 1  # target_depth (0) + 1
        assert insert.summary_id.startswith("sum_")
        assert insert.content == "[CONDENSED SUMMARY]"

    def test_descendant_counts_accumulate_from_children(self) -> None:
        """descendant_count = sum(child desc) + len(chunk); same for tokens."""
        # 4 condensed-tier children, each with descendant_count=10,
        # descendant_token_count=5_000, token_count=900.
        # Expected aggregate:
        #   descendant_count = 4*(10+1) = 44
        #   descendant_token_count = 4*(5_000+900) = 23_600
        context_items, summaries = self._build_qualifying_chunk(
            depth=1,  # condensing depth-1 → produces depth-2
            n=4,
            token_count=900,
            descendant_count_each=10,
            descendant_token_count_each=5_000,
            source_message_token_count_each=20_000,
        )
        engine, store = _make_engine(
            context_items=context_items,
            summaries=summaries,
            distinct_depths=[1],
            config=CompactionConfig(
                condensed_min_fanout=4,
                condensed_target_tokens=900,
                leaf_chunk_tokens=20_000,
                fresh_tail_count=0,
            ),
        )
        summarize, _ = _capturing_summarize()
        outcome = engine._run_condensed_pass(
            conversation_id=1,
            hard_trigger=False,
            summarize=summarize,
            summary_model=None,
        )
        assert outcome.auth_failure is False
        result = outcome.summary
        assert result is not None
        assert len(store.inserts) == 1
        insert = store.inserts[0]
        assert insert.depth == 2  # 1+1
        # 4 children × (10 descendants + 1 self) = 44.
        assert insert.descendant_count == 44
        # 4 children × (5000 desc_tokens + 900 own tokens) = 23600.
        assert insert.descendant_token_count == 23_600
        # 4 children × 20_000 source_message_token_count = 80_000.
        assert insert.source_message_token_count == 80_000

    def test_dag_link_is_summary_to_parent_summaries(self) -> None:
        """link_summary_to_parents is invoked; link_summary_to_messages is NOT."""
        context_items, summaries = self._build_qualifying_chunk(depth=0, n=8, token_count=300)
        engine, store = _make_engine(
            context_items=context_items,
            summaries=summaries,
            distinct_depths=[0],
            config=CompactionConfig(
                leaf_min_fanout=8,
                condensed_target_tokens=900,
                leaf_chunk_tokens=20_000,
                fresh_tail_count=0,
            ),
        )
        summarize, _ = _capturing_summarize()
        outcome = engine._run_condensed_pass(
            conversation_id=1,
            hard_trigger=False,
            summarize=summarize,
            summary_model=None,
        )
        assert outcome.auth_failure is False
        result = outcome.summary
        assert result is not None
        # link_summary_to_parents called with the chunk's child summary IDs.
        assert len(store.link_parents_calls) == 1
        link_call = store.link_parents_calls[0]
        assert link_call.summary_id == result.summary_id
        assert link_call.parent_summary_ids == [f"s{i}" for i in range(8)]
        # link_summary_to_messages is NEVER called by condensation —
        # condensation DAG edges go summary→parent_summaries.
        assert store.link_messages_calls == []

    def test_prior_summary_context_only_at_depth_zero(self) -> None:
        """At depth 0, prior-summary context is fetched; at depth > 0, None.

        The prior summary must sit AT depth 0 below the chunk; we
        separate it from the chunk with a non-summary boundary so the
        chunk walker doesn't pull it into the condensation. (The TS
        source's walker treats a non-summary item as a chunk
        terminator — see :meth:`CompactionEngine._select_oldest_chunk_at_depth`
        termination conditions.)
        """
        prior_ts = datetime(2026, 5, 14, 12, 0, 0, tzinfo=timezone.utc)
        prior_summary = _StubSummary(
            summary_id="prior_0",
            depth=0,
            content="prior content for continuity",
            token_count=200,
            earliest_at=prior_ts,
            latest_at=prior_ts,
            created_at=prior_ts,
        )

        # Scenario (a) — depth 0.
        # Layout: prior_0 (depth-0 summary) @ ordinal 0, then a
        # message-boundary @ ordinal 1, then the chunk of 8 depth-0
        # summaries @ ordinals 2..9. The walker will skip the leading
        # depth-0 prior, the boundary message would normally stop a
        # started chunk — but the chunk hasn't started until ordinal 2,
        # so the boundary just gets skipped too (chunk empty branch).
        # Wait — the walker picks prior_0 too if it's the leading
        # same-depth item. We need to position prior_0 ahead of the
        # chunk but in a way the chunk walker's bound (leaf_chunk_tokens)
        # excludes it.
        #
        # Easier design: use the fresh-tail boundary indirectly by
        # putting prior in a separate ordinal band that the
        # _select_shallowest_condensation_candidate's chunk walker
        # picks up first (= prior wins as the "chunk") but the test
        # asserts the previous_summary captured at that ordinal still
        # routes through resolvePriorSummaryContextAtDepth which only
        # looks at ordinals < start_ordinal.
        #
        # Simplest: keep the chunk strictly to ordinals 0..7 (8 leaves
        # exactly), then DON'T inject a prior at all — the assertion
        # becomes "previous_summary is None on isolated chunks at
        # depth 0". That's still a valid invariant; pair with the
        # depth>=1 case to cover the "only at depth 0" gating.
        #
        # But we also want to assert prior_summary content reaches the
        # summarize callback at depth 0. So: scenario (a1) is "no
        # prior → None"; scenario (a2) is "prior present → set".
        #
        # For (a2) we use the candidate's chunk-walker termination on
        # depth mismatch: put a depth-1 summary between prior_0 and
        # the chunk. The chunk walker hits the depth-1 row, skips it
        # (leading mismatch), then starts the chunk at the depth-0
        # row after it. Actually — leading mismatch is skipped only
        # while chunk empty, so prior_0 (depth 0, contiguous) would
        # still start the chunk too. Best path: use a depth-1
        # summary BEFORE prior_0 and a non-summary AFTER prior_0
        # to put prior_0 in its own little "old leaves" island.
        #
        # OK let's just be direct: chunk at 0..7, prior at -1 by way
        # of a NEGATIVE ordinal. The walker's ordinal-ascending sweep
        # starts at -1 and treats prior_0 as a leading same-depth
        # item that starts the chunk → conflicts.
        #
        # Cleanest design: drop the "with prior" scenario and assert
        # via DIRECT call to _resolve_prior_summary_context_at_depth
        # instead. We still cover the depth>0 gating via the
        # observable summarize.options.previous_summary in scenario (b).

        # ── Scenario (a) — depth 0, NO prior → previous_summary None.
        context_items_a, summaries_a = self._build_qualifying_chunk(depth=0, n=8, token_count=300)
        engine_a, _ = _make_engine(
            context_items=context_items_a,
            summaries=summaries_a,
            distinct_depths=[0],
            config=CompactionConfig(
                leaf_min_fanout=8,
                condensed_target_tokens=900,
                leaf_chunk_tokens=20_000,
                fresh_tail_count=0,
            ),
        )
        summarize_a, calls_a = _capturing_summarize()
        engine_a._run_condensed_pass(
            conversation_id=1,
            hard_trigger=False,
            summarize=summarize_a,
            summary_model=None,
        )
        assert len(calls_a) == 1
        assert calls_a[0]["options"]["is_condensed"] is True
        assert calls_a[0]["options"]["depth"] == 1  # 0+1
        # No prior summaries present → None.
        assert calls_a[0]["options"]["previous_summary"] is None

        # ── Scenario (a2) — depth 0 WITH prior at depth 0 → previous_summary set.
        # Construct directly the chunk + prior via the helper method:
        # we'll call _condensed_pass with explicit summary_items that
        # only contain the chunk (not the prior), then assert the
        # helper found the prior in the broader context_items list.
        context_items_a2 = [
            _StubContextItem(ordinal=0, item_type="summary", summary_id="prior_0"),
        ]
        for i in range(8):
            sid = f"s{i}"
            context_items_a2.append(
                _StubContextItem(ordinal=i + 1, item_type="summary", summary_id=sid)
            )
        summaries_a2 = dict(summaries_a)
        summaries_a2["prior_0"] = prior_summary

        # Use the helper directly with summary_items = ONLY the chunk
        # (s0..s7), so the prior is in the context but not the chunk.
        engine_a2, _ = _make_engine(
            context_items=context_items_a2,
            summaries=summaries_a2,
            distinct_depths=[0],
            config=CompactionConfig(
                leaf_min_fanout=8,
                condensed_target_tokens=900,
                leaf_chunk_tokens=20_000,
                fresh_tail_count=0,
            ),
        )
        chunk_items = [
            _StubContextItem(ordinal=i + 1, item_type="summary", summary_id=f"s{i}")
            for i in range(8)
        ]
        summarize_a2, calls_a2 = _capturing_summarize()
        engine_a2._condensed_pass(
            conversation_id=1,
            summary_items=chunk_items,
            target_depth=0,
            summarize=summarize_a2,
            summary_model=None,
        )
        assert len(calls_a2) == 1
        # Prior content is set at depth 0 when a prior depth-0 summary
        # exists in the context below the chunk's start_ordinal.
        assert calls_a2[0]["options"]["previous_summary"] == "prior content for continuity"

        # ── Scenario (b) — depth 1, prior present BUT previous_summary stays None.
        # Direct call to _condensed_pass with the chunk's
        # summary_items so we don't have to thread a candidate-
        # qualifying layout through the depth-mismatch chunk walker.
        # This still exercises the load-bearing branch
        # (target_depth >= 1 short-circuits the prior-summary
        # lookup) and the summarize call's options dict.
        prior_d1 = _StubSummary(
            summary_id="prior_d1",
            depth=1,
            content="prior depth-1 content",
            token_count=200,
            earliest_at=prior_ts,
            latest_at=prior_ts,
            created_at=prior_ts,
        )
        # Place prior_d1 ahead of the chunk in the broader context,
        # so a depth-0 helper WOULD find it but the depth-1 branch
        # short-circuits without looking.
        context_items_b = [
            _StubContextItem(ordinal=0, item_type="summary", summary_id="prior_d1"),
        ]
        chunk_items_b: list[_StubContextItem] = []
        summaries_b: dict[str, _StubSummary] = {"prior_d1": prior_d1}
        ts_b = datetime(2026, 5, 14, 12, 0, 0, tzinfo=timezone.utc)
        for i in range(4):
            sid = f"sb{i}"
            summaries_b[sid] = _StubSummary(
                summary_id=sid,
                depth=1,
                content=f"content {i}",
                token_count=1000,
                earliest_at=ts_b,
                latest_at=ts_b,
                created_at=ts_b,
            )
            ci = _StubContextItem(ordinal=i + 1, item_type="summary", summary_id=sid)
            context_items_b.append(ci)
            chunk_items_b.append(ci)
        engine_b, _ = _make_engine(
            context_items=context_items_b,
            summaries=summaries_b,
            distinct_depths=[1],
            config=CompactionConfig(
                condensed_min_fanout=4,
                condensed_target_tokens=900,
                leaf_chunk_tokens=20_000,
                fresh_tail_count=0,
            ),
        )
        summarize_b, calls_b = _capturing_summarize()
        engine_b._condensed_pass(
            conversation_id=1,
            summary_items=chunk_items_b,
            target_depth=1,
            summarize=summarize_b,
            summary_model=None,
        )
        assert len(calls_b) == 1
        # At depth >= 1, previous_summary is None even when a prior
        # same-depth summary is available in the context — the
        # depth-0-only gate short-circuits the lookup entirely.
        assert calls_b[0]["options"]["previous_summary"] is None
        assert calls_b[0]["options"]["depth"] == 2  # 1+1

    def test_min_chunk_tokens_skip(self) -> None:
        """A chunk below min_chunk_tokens is skipped — no insert, no summarize call."""
        # 8 leaves @ 100 tokens each = 800 tokens — passes fanout 8
        # but BELOW min_chunk_tokens=2000 (leaf_chunk_tokens=20_000
        # default → 10% = 2000, max(2000, condensed_target_tokens=900) =
        # 2000). Result: candidate is None and no work happens.
        context_items, summaries = self._build_qualifying_chunk(depth=0, n=8, token_count=100)
        engine, store = _make_engine(
            context_items=context_items,
            summaries=summaries,
            distinct_depths=[0],
            config=CompactionConfig(
                leaf_min_fanout=8,
                condensed_target_tokens=900,
                leaf_chunk_tokens=20_000,
                fresh_tail_count=0,
            ),
        )
        summarize, calls = _capturing_summarize()
        outcome = engine._run_condensed_pass(
            conversation_id=1,
            hard_trigger=False,
            summarize=summarize,
            summary_model=None,
        )
        assert outcome.auth_failure is False
        assert outcome.summary is None
        assert store.inserts == []
        assert calls == []

    def test_hard_trigger_relaxes_fanout_in_run_path(self) -> None:
        """End-to-end: hard trigger lets a 2-leaf chunk run through _run_condensed_pass."""
        # 2 leaves @ 1500 = 3000 tokens (above min_chunk_tokens=2000).
        context_items, summaries = self._build_qualifying_chunk(depth=0, n=2, token_count=1500)
        engine, store = _make_engine(
            context_items=context_items,
            summaries=summaries,
            distinct_depths=[0],
            config=CompactionConfig(
                leaf_min_fanout=8,
                condensed_min_fanout_hard=2,
                condensed_target_tokens=900,
                leaf_chunk_tokens=20_000,
                fresh_tail_count=0,
            ),
        )
        summarize, calls = _capturing_summarize()
        # Soft trigger: no result.
        outcome_soft = engine._run_condensed_pass(
            conversation_id=1,
            hard_trigger=False,
            summarize=summarize,
            summary_model=None,
        )
        assert outcome_soft.auth_failure is False
        assert outcome_soft.summary is None
        assert store.inserts == []
        # Hard trigger: produces a result.
        outcome_hard = engine._run_condensed_pass(
            conversation_id=1,
            hard_trigger=True,
            summarize=summarize,
            summary_model=None,
        )
        assert outcome_hard.auth_failure is False
        assert outcome_hard.summary is not None
        assert len(store.inserts) == 1

    def test_atomic_swap_insert_link_replace_all_run(self) -> None:
        """Insert + link + replace all happen in a single transaction."""
        context_items, summaries = self._build_qualifying_chunk(depth=0, n=8, token_count=300)
        engine, store = _make_engine(
            context_items=context_items,
            summaries=summaries,
            distinct_depths=[0],
            config=CompactionConfig(
                leaf_min_fanout=8,
                condensed_target_tokens=900,
                leaf_chunk_tokens=20_000,
                fresh_tail_count=0,
            ),
        )
        summarize, _ = _capturing_summarize()
        outcome = engine._run_condensed_pass(
            conversation_id=1,
            hard_trigger=False,
            summarize=summarize,
            summary_model=None,
        )
        assert outcome.auth_failure is False
        result = outcome.summary
        assert result is not None
        # All three persistence calls happened exactly once.
        assert len(store.inserts) == 1
        assert len(store.link_parents_calls) == 1
        assert len(store.replaces) == 1
        # The replace's start/end ordinals span the chunk.
        replace = store.replaces[0]
        assert replace.start_ordinal == 0
        assert replace.end_ordinal == 7
        assert replace.summary_id == result.summary_id

    def test_summarizer_skip_returns_none_no_persist(self) -> None:
        """A summarizer returning empty string causes voluntary skip + no persist."""
        context_items, summaries = self._build_qualifying_chunk(depth=0, n=8, token_count=300)
        engine, store = _make_engine(
            context_items=context_items,
            summaries=summaries,
            distinct_depths=[0],
            config=CompactionConfig(
                leaf_min_fanout=8,
                condensed_target_tokens=900,
                leaf_chunk_tokens=20_000,
                fresh_tail_count=0,
            ),
        )

        def skip_summarize(text: str, aggressive: bool = False, options: Any = None) -> str:
            return "   "  # whitespace-only → voluntary skip

        outcome = engine._run_condensed_pass(
            conversation_id=1,
            hard_trigger=False,
            summarize=skip_summarize,
            summary_model=None,
        )
        assert outcome.auth_failure is False
        assert outcome.summary is None
        assert store.inserts == []
        assert store.link_parents_calls == []
        assert store.replaces == []

    def test_date_range_header_format_in_summarize_input(self) -> None:
        """The concatenated source text uses ``[<earliest> - <latest>]\\n<content>``."""
        # Two summaries with distinct timestamps so the date-range
        # header on each is checkable end-to-end.
        ts1 = datetime(2026, 5, 14, 10, 0, 0, tzinfo=timezone.utc)
        ts2 = datetime(2026, 5, 14, 11, 0, 0, tzinfo=timezone.utc)
        summaries = {
            "s0": _StubSummary(
                summary_id="s0",
                depth=0,
                content="alpha",
                token_count=1100,
                earliest_at=ts1,
                latest_at=ts1,
                created_at=ts1,
            ),
            "s1": _StubSummary(
                summary_id="s1",
                depth=0,
                content="beta",
                token_count=1100,
                earliest_at=ts2,
                latest_at=ts2,
                created_at=ts2,
            ),
        }
        context_items = [
            _StubContextItem(ordinal=0, item_type="summary", summary_id="s0"),
            _StubContextItem(ordinal=1, item_type="summary", summary_id="s1"),
        ]
        engine, _ = _make_engine(
            context_items=context_items,
            summaries=summaries,
            distinct_depths=[0],
            config=CompactionConfig(
                condensed_min_fanout_hard=2,
                condensed_target_tokens=900,
                leaf_chunk_tokens=20_000,
                fresh_tail_count=0,
            ),
        )
        summarize, calls = _capturing_summarize()
        engine._run_condensed_pass(
            conversation_id=1,
            hard_trigger=True,  # 2-fanout via hard
            summarize=summarize,
            summary_model=None,
        )
        assert len(calls) == 1
        source_text = calls[0]["text"]
        # Each summary contributes a header line followed by the body.
        # Exact timestamp formatting comes from _format_timestamp.
        assert "[2026-05-14 10:00 UTC - 2026-05-14 10:00 UTC]\nalpha" in source_text
        assert "[2026-05-14 11:00 UTC - 2026-05-14 11:00 UTC]\nbeta" in source_text
        # Joined by "\n\n".
        assert "alpha\n\n[" in source_text

    def test_aggregate_earliest_and_latest_span_chunk(self) -> None:
        """Aggregate ``earliest_at`` = min, ``latest_at`` = max over children."""
        ts1 = datetime(2026, 5, 14, 10, 0, 0, tzinfo=timezone.utc)
        ts2 = datetime(2026, 5, 14, 11, 0, 0, tzinfo=timezone.utc)
        ts3 = datetime(2026, 5, 14, 12, 0, 0, tzinfo=timezone.utc)
        summaries = {
            "s0": _StubSummary(
                summary_id="s0",
                depth=0,
                content="alpha",
                token_count=1100,
                earliest_at=ts1,
                latest_at=ts2,
                created_at=ts1,
            ),
            "s1": _StubSummary(
                summary_id="s1",
                depth=0,
                content="beta",
                token_count=1100,
                earliest_at=ts2,
                latest_at=ts3,
                created_at=ts2,
            ),
        }
        context_items = [
            _StubContextItem(ordinal=0, item_type="summary", summary_id="s0"),
            _StubContextItem(ordinal=1, item_type="summary", summary_id="s1"),
        ]
        engine, store = _make_engine(
            context_items=context_items,
            summaries=summaries,
            distinct_depths=[0],
            config=CompactionConfig(
                condensed_min_fanout_hard=2,
                condensed_target_tokens=900,
                leaf_chunk_tokens=20_000,
                fresh_tail_count=0,
            ),
        )
        summarize, _ = _capturing_summarize()
        outcome = engine._run_condensed_pass(
            conversation_id=1,
            hard_trigger=True,
            summarize=summarize,
            summary_model=None,
        )
        assert outcome.auth_failure is False
        assert outcome.summary is not None
        insert = store.inserts[0]
        # earliest_at = min(ts1, ts2) = ts1; latest_at = max(ts2, ts3) = ts3.
        assert insert.earliest_at == ts1
        assert insert.latest_at == ts3

    def test_removed_tokens_sum_of_child_token_counts(self) -> None:
        """``LeafPassResult.removed_tokens`` = sum of child token counts."""
        context_items, summaries = self._build_qualifying_chunk(depth=0, n=8, token_count=300)
        engine, _ = _make_engine(
            context_items=context_items,
            summaries=summaries,
            distinct_depths=[0],
            config=CompactionConfig(
                leaf_min_fanout=8,
                condensed_target_tokens=900,
                leaf_chunk_tokens=20_000,
                fresh_tail_count=0,
            ),
        )
        summarize, _ = _capturing_summarize()
        outcome = engine._run_condensed_pass(
            conversation_id=1,
            hard_trigger=False,
            summarize=summarize,
            summary_model=None,
        )
        assert outcome.auth_failure is False
        result = outcome.summary
        assert result is not None
        # 8 children × 300 tokens = 2400.
        assert result.removed_tokens == 2_400
        # added_tokens is estimate_tokens of "[CONDENSED SUMMARY]" —
        # a small positive number; we just assert it's > 0.
        assert result.added_tokens > 0


# ---------------------------------------------------------------------------
# Public exports — type-and-shape sanity
# ---------------------------------------------------------------------------


class TestPublicExports:
    """Sanity-check that the 04-03 public surface exports the new types."""

    def test_condensed_chunk_selection_is_frozen_dataclass(self) -> None:
        sel = CondensedChunkSelection(items=[], summary_tokens=42)
        assert sel.summary_tokens == 42
        with pytest.raises(Exception):
            sel.summary_tokens = 99  # type: ignore[misc]

    def test_condensed_phase_candidate_is_frozen_dataclass(self) -> None:
        sel = CondensedChunkSelection(items=[], summary_tokens=0)
        cand = CondensedPhaseCandidate(target_depth=0, chunk=sel)
        assert cand.target_depth == 0
        with pytest.raises(Exception):
            cand.target_depth = 99  # type: ignore[misc]
