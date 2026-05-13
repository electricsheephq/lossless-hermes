"""Tests for :func:`lossless_hermes.assembler.resolve_fresh_tail_ordinal`.

Ports the implicit fresh-tail invariants exercised by
``lossless-claw/test/lcm-integration.test.ts`` (LCM commit ``1f07fbd``)
to standalone unit tests. There is no dedicated ``fresh-tail.test.ts`` in
the TS suite — fresh-tail behavior is covered transitively through
integration tests at lines 682, 738-745, 842-908, 924-942, 1028-1450,
1510, 1567, 1628, 1688, 1761, 1838, 3084-4000+. The cases below pull the
boundary invariants into focused fixtures and add the spec-mandated
edge cases (empty input, all-summaries, single item, mixed,
``fresh_tail_count = 0``).

### Invariants verified

* **Newest is always protected**, even if alone over ``fresh_tail_max_tokens``
  (TS lines 1018-1024, ``protectedCount > 0`` gate).
* **Default ``fresh_tail_count = 8``** matches TS ``AssembleContextInput``
  default (line 128).
* **``fresh_tail_max_tokens`` is optional** — ``None`` collapses to
  count-only gating.
* **Only raw messages count** — summaries are skipped in the walk, but
  the splitter (downstream, 03-06) still includes them in the fresh-tail
  slice when their ordinal ``>= boundary``.
* **Empty input → :data:`EMPTY_FRESH_TAIL_ORDINAL`**.
* **All-summary input → :data:`EMPTY_FRESH_TAIL_ORDINAL`**.
* **``fresh_tail_count <= 0`` → :data:`EMPTY_FRESH_TAIL_ORDINAL`** (mirrors
  TS line 988; **contradicts** the issue-spec AC that claims newest is
  preserved; TS is canonical).
* **Boundary is the ordinal of the OLDEST kept item**, not "first ordinal
  past the tail" — the splitter at TS 1156 uses ``>= boundary``.

### Reference

* Source: ``lossless-claw/src/assembler.ts`` 983-1032.
* Spec: ``epics/03-ingest-assembly/03-05-fresh-tail-resolver.md``.
* Porting guide: ``docs/porting-guides/assembler-compaction.md``.
"""

from __future__ import annotations

import sys

import pytest

from lossless_hermes.assembler import (
    EMPTY_FRESH_TAIL_ORDINAL,
    ResolvedItem,
    resolve_fresh_tail_ordinal,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_msg(ordinal: int, tokens: int = 10) -> ResolvedItem:
    """Build a minimal raw-message :class:`ResolvedItem`.

    Only the fields actually inspected by
    :func:`resolve_fresh_tail_ordinal` are populated:
    ``ordinal``, ``tokens``, ``is_message``. The rest get safe
    defaults so the dataclass instantiates without error.
    """
    return ResolvedItem(
        ordinal=ordinal,
        message={"role": "user", "content": f"msg-{ordinal}"},
        tokens=tokens,
        is_message=True,
        text=f"msg-{ordinal}",
        message_id=ordinal,
    )


def _make_summary(ordinal: int, tokens: int = 50) -> ResolvedItem:
    """Build a minimal summary-type :class:`ResolvedItem`."""
    return ResolvedItem(
        ordinal=ordinal,
        message={"role": "user", "content": f"<summary>{ordinal}</summary>"},
        tokens=tokens,
        is_message=False,
        text=f"summary {ordinal}",
    )


# ---------------------------------------------------------------------------
# Edge cases — empty / single / all-one-kind
# ---------------------------------------------------------------------------


class TestEmptyInput:
    """Empty ``resolved`` list — sentinel return, no out-of-range issues."""

    def test_empty_list_with_default_count(self) -> None:
        """No items at all → :data:`EMPTY_FRESH_TAIL_ORDINAL`."""
        assert resolve_fresh_tail_ordinal([], 8, None) == EMPTY_FRESH_TAIL_ORDINAL

    def test_empty_list_with_zero_count(self) -> None:
        """Empty + ``count=0`` — both short-circuits → sentinel."""
        assert resolve_fresh_tail_ordinal([], 0, None) == EMPTY_FRESH_TAIL_ORDINAL

    def test_empty_list_with_token_cap(self) -> None:
        """Cap is moot when there are no items."""
        assert resolve_fresh_tail_ordinal([], 8, 1000) == EMPTY_FRESH_TAIL_ORDINAL

    def test_empty_list_is_int_returnable(self) -> None:
        """The sentinel is an ``int`` (not ``float`` / ``inf``).

        SQLite ordinals are ``INTEGER`` and downstream callers (e.g.
        ``SummaryStore.get_distinct_depths_in_context``) bind via
        ``int(boundary)``. The sentinel must therefore be int-typed
        without precision loss.
        """
        result = resolve_fresh_tail_ordinal([], 8, None)
        assert isinstance(result, int)
        # Must coerce through ``int(...)`` without error or precision loss.
        assert int(result) == result


class TestSingleItem:
    """A list of length one."""

    def test_single_message_count_8(self) -> None:
        """One raw message + ``count=8`` → boundary = that message's ordinal."""
        items = [_make_msg(ordinal=42, tokens=100)]
        assert resolve_fresh_tail_ordinal(items, 8, None) == 42

    def test_single_message_count_1(self) -> None:
        """One raw message + ``count=1`` → boundary = its ordinal."""
        items = [_make_msg(ordinal=42, tokens=100)]
        assert resolve_fresh_tail_ordinal(items, 1, None) == 42

    def test_single_summary(self) -> None:
        """One summary, no raw message → sentinel (nothing to protect)."""
        items = [_make_summary(ordinal=42)]
        assert resolve_fresh_tail_ordinal(items, 8, None) == EMPTY_FRESH_TAIL_ORDINAL

    def test_single_message_over_token_cap(self) -> None:
        """Newest is always kept, even if alone over the token cap.

        TS lines 1018-1024: ``protectedCount > 0`` means the cap check
        is skipped on the first iteration.
        """
        items = [_make_msg(ordinal=42, tokens=10_000)]
        assert resolve_fresh_tail_ordinal(items, 8, fresh_tail_max_tokens=100) == 42

    def test_single_message_count_zero(self) -> None:
        """``count = 0`` short-circuits before considering the item.

        Matches TS line 988 verbatim. The issue-spec AC bullet that
        says "fresh_tail_count = 0 still keeps the newest" is **wrong**;
        ``test/lcm-integration.test.ts:1510`` and four other test cases
        depend on this disabled behavior.
        """
        items = [_make_msg(ordinal=42, tokens=10)]
        assert resolve_fresh_tail_ordinal(items, 0, None) == EMPTY_FRESH_TAIL_ORDINAL


class TestAllMessages:
    """Sequences with only raw messages, no summaries."""

    def test_all_messages_count_fits(self) -> None:
        """5 messages, count=8 → every message is protected; boundary = oldest ordinal."""
        items = [_make_msg(ordinal=i, tokens=10) for i in range(5)]
        # All 5 fit within count=8, so the boundary is the oldest (smallest) ordinal.
        assert resolve_fresh_tail_ordinal(items, 8, None) == 0

    def test_all_messages_count_lt_size(self) -> None:
        """10 messages, count=3 → keep newest 3; boundary = ordinal of the 3rd-newest."""
        items = [_make_msg(ordinal=i, tokens=10) for i in range(10)]
        # Newest 3 are ordinals 7, 8, 9. Boundary = 7.
        assert resolve_fresh_tail_ordinal(items, 3, None) == 7

    def test_all_messages_count_eq_size(self) -> None:
        """Exact match → boundary = oldest ordinal."""
        items = [_make_msg(ordinal=i, tokens=10) for i in range(5)]
        assert resolve_fresh_tail_ordinal(items, 5, None) == 0

    def test_all_messages_count_one(self) -> None:
        """``count=1`` keeps only the newest."""
        items = [_make_msg(ordinal=i, tokens=10) for i in range(5)]
        # Newest is ordinal 4.
        assert resolve_fresh_tail_ordinal(items, 1, None) == 4

    def test_all_messages_count_larger_than_size(self) -> None:
        """``count > size`` keeps every message; boundary = oldest ordinal."""
        items = [_make_msg(ordinal=i, tokens=10) for i in range(3)]
        assert resolve_fresh_tail_ordinal(items, 100, None) == 0

    def test_all_messages_non_contiguous_ordinals(self) -> None:
        """Ordinals don't need to be 0..N-1 — they're DB-assigned.

        Real ``context_items.ordinal`` values are monotonically
        increasing per-conversation, but with gaps (e.g. after
        replace_context_range deletes a span). The function depends on
        order (newest-last) not on ordinal contiguity.
        """
        items = [
            _make_msg(ordinal=10, tokens=10),
            _make_msg(ordinal=15, tokens=10),
            _make_msg(ordinal=23, tokens=10),
            _make_msg(ordinal=100, tokens=10),
        ]
        # Newest 2 are 23 and 100; boundary = 23.
        assert resolve_fresh_tail_ordinal(items, 2, None) == 23


class TestAllSummaries:
    """Sequences with only summary items — no raw messages to protect."""

    def test_all_summaries_returns_sentinel(self) -> None:
        """Every item is a summary → sentinel.

        This is the canonical "old conversation, just-compacted" state:
        all messages have been collapsed into summaries and there's
        nothing to protect from eviction.
        """
        items = [_make_summary(ordinal=i, tokens=50) for i in range(5)]
        assert resolve_fresh_tail_ordinal(items, 8, None) == EMPTY_FRESH_TAIL_ORDINAL

    def test_all_summaries_with_token_cap(self) -> None:
        """Cap is moot when no raw messages exist."""
        items = [_make_summary(ordinal=i, tokens=50) for i in range(5)]
        assert (
            resolve_fresh_tail_ordinal(items, 8, fresh_tail_max_tokens=1000)
            == EMPTY_FRESH_TAIL_ORDINAL
        )

    def test_single_summary_at_high_ordinal(self) -> None:
        """A single summary at a high ordinal → still sentinel."""
        items = [_make_summary(ordinal=999, tokens=50)]
        assert resolve_fresh_tail_ordinal(items, 8, None) == EMPTY_FRESH_TAIL_ORDINAL


# ---------------------------------------------------------------------------
# Mixed messages + summaries
# ---------------------------------------------------------------------------


class TestMixedMessagesAndSummaries:
    """Summaries interleaved with raw messages — only raw messages count.

    The boundary is set by walking raw messages newest-to-oldest. The
    splitter (downstream in 03-06) uses ``ordinal >= boundary``, which
    *will* include summaries between kept messages in the fresh-tail
    slice — the spec calls this out explicitly.
    """

    def test_summary_between_messages(self) -> None:
        """Summary between two raw messages doesn't change the boundary.

        Layout (ordinals): msg(0), summary(1), msg(2), summary(3), msg(4).
        With ``count=2``, the newest 2 raw messages are at ordinals 4
        and 2. The boundary should be 2 — and the downstream splitter
        will pick up the summary at ordinal 3 because 3 >= 2.
        """
        items = [
            _make_msg(ordinal=0, tokens=10),
            _make_summary(ordinal=1, tokens=50),
            _make_msg(ordinal=2, tokens=10),
            _make_summary(ordinal=3, tokens=50),
            _make_msg(ordinal=4, tokens=10),
        ]
        assert resolve_fresh_tail_ordinal(items, 2, None) == 2

    def test_summary_at_newest_position(self) -> None:
        """A summary at the newest ordinal does NOT count as fresh-tail seed.

        Layout: msg(0), msg(1), msg(2), summary(3).
        With ``count=1``, the newest raw message is at ordinal 2;
        boundary = 2. The summary at ordinal 3 is included in the
        fresh tail by the splitter (3 >= 2), but does NOT consume a
        protection slot.
        """
        items = [
            _make_msg(ordinal=0, tokens=10),
            _make_msg(ordinal=1, tokens=10),
            _make_msg(ordinal=2, tokens=10),
            _make_summary(ordinal=3, tokens=50),
        ]
        assert resolve_fresh_tail_ordinal(items, 1, None) == 2

    def test_summary_block_at_start(self) -> None:
        """Multiple summaries at the start (compacted history) + messages at end.

        Layout: summary(0), summary(1), summary(2), msg(3), msg(4), msg(5).
        With ``count=2``, boundary = ordinal of 2nd-newest msg = 4.
        """
        items = [
            _make_summary(ordinal=0, tokens=50),
            _make_summary(ordinal=1, tokens=50),
            _make_summary(ordinal=2, tokens=50),
            _make_msg(ordinal=3, tokens=10),
            _make_msg(ordinal=4, tokens=10),
            _make_msg(ordinal=5, tokens=10),
        ]
        assert resolve_fresh_tail_ordinal(items, 2, None) == 4

    def test_summary_block_at_end_only(self) -> None:
        """Messages at start, summaries at end (unusual but valid).

        Layout: msg(0), msg(1), msg(2), summary(3), summary(4).
        With ``count=2``, boundary = ordinal of 2nd-newest msg = 1.
        Summaries at 3 and 4 are included in fresh tail by splitter.
        """
        items = [
            _make_msg(ordinal=0, tokens=10),
            _make_msg(ordinal=1, tokens=10),
            _make_msg(ordinal=2, tokens=10),
            _make_summary(ordinal=3, tokens=50),
            _make_summary(ordinal=4, tokens=50),
        ]
        assert resolve_fresh_tail_ordinal(items, 2, None) == 1


# ---------------------------------------------------------------------------
# Token-cap behavior
# ---------------------------------------------------------------------------


class TestTokenCap:
    """``fresh_tail_max_tokens`` interactions with the count budget."""

    def test_cap_stops_walk_before_count(self) -> None:
        """Cap can stop the walk before ``count`` is reached.

        4 messages, each 100 tokens. count=4, cap=250.
        Walk: protect newest (100, idx=3) → protectedTokens=100. Next:
        100+100=200 <= 250, keep (idx=2). Next: 200+100=300 > 250, stop.
        Boundary = ordinal of msg at idx 2 = 2.
        """
        items = [_make_msg(ordinal=i, tokens=100) for i in range(4)]
        assert resolve_fresh_tail_ordinal(items, 4, fresh_tail_max_tokens=250) == 2

    def test_cap_protects_newest_even_when_alone_exceeds(self) -> None:
        """The newest is always protected, even if alone over the cap.

        Reflects TS line 1018-1024 (``protectedCount > 0`` gate).
        4 messages: ordinals 0, 1, 2, 3. Newest (ordinal=3) is 1000
        tokens. cap=100. Boundary = 3 — the newest is kept; nothing
        else fits.
        """
        items = [
            _make_msg(ordinal=0, tokens=50),
            _make_msg(ordinal=1, tokens=50),
            _make_msg(ordinal=2, tokens=50),
            _make_msg(ordinal=3, tokens=1000),
        ]
        assert resolve_fresh_tail_ordinal(items, 8, fresh_tail_max_tokens=100) == 3

    def test_cap_zero_keeps_newest_only(self) -> None:
        """``cap=0`` still keeps the newest message.

        First iteration skips the cap check (``protectedCount > 0``).
        Second iteration: ``0 + 10 > 0`` → break. Only newest kept.
        """
        items = [_make_msg(ordinal=i, tokens=10) for i in range(3)]
        # Newest is ordinal 2; boundary = 2.
        assert resolve_fresh_tail_ordinal(items, 8, fresh_tail_max_tokens=0) == 2

    def test_cap_negative_collapses_to_none(self) -> None:
        """A negative cap is ignored (TS line 1000: ``>= 0`` guard).

        With cap=-1, behavior should match ``cap=None`` — only the
        count gates.
        """
        items = [_make_msg(ordinal=i, tokens=10) for i in range(5)]
        # count=3 → boundary = ordinal of 3rd-newest = 2.
        assert resolve_fresh_tail_ordinal(items, 3, fresh_tail_max_tokens=-1) == 2

    def test_cap_exact_match(self) -> None:
        """``protectedTokens + item.tokens == cap`` is kept (``>`` not ``>=``).

        TS line 1021: ``protectedTokens + item.tokens > tokenCap``.
        With protectedTokens=100 and item.tokens=50 and cap=150,
        150 > 150 is false → keep.
        """
        items = [_make_msg(ordinal=i, tokens=50) for i in range(5)]
        # Walk: ord=4 (+50, total 50), ord=3 (+50, total 100), ord=2 (+50, total 150 == cap, keep),
        # ord=1 (+50 → 200 > 150, break). Boundary = 2.
        assert resolve_fresh_tail_ordinal(items, 8, fresh_tail_max_tokens=150) == 2

    def test_cap_just_under(self) -> None:
        """Cap one token short of fitting an item — stop the walk."""
        items = [_make_msg(ordinal=i, tokens=50) for i in range(5)]
        # Walk: ord=4 (50), ord=3 (100, 100 > 99 → break). Boundary = 4.
        assert resolve_fresh_tail_ordinal(items, 8, fresh_tail_max_tokens=99) == 4

    def test_cap_with_summaries_between_messages(self) -> None:
        """Token cap counts only raw-message tokens, not summary tokens.

        Layout: msg(0)/10, summary(1)/200, msg(2)/10, msg(3)/10, msg(4)/10.
        cap=25, count=8. Walk raw messages newest-to-oldest:
        ord=4 (+10, total 10), ord=3 (+10, total 20), ord=2 (+10, total
        30 > 25 → break). Boundary = 3. Summaries don't consume budget.
        """
        items = [
            _make_msg(ordinal=0, tokens=10),
            _make_summary(ordinal=1, tokens=200),  # Big summary, ignored.
            _make_msg(ordinal=2, tokens=10),
            _make_msg(ordinal=3, tokens=10),
            _make_msg(ordinal=4, tokens=10),
        ]
        assert resolve_fresh_tail_ordinal(items, 8, fresh_tail_max_tokens=25) == 3


# ---------------------------------------------------------------------------
# Zero / negative counts (TS line 988)
# ---------------------------------------------------------------------------


class TestZeroCount:
    """``fresh_tail_count <= 0`` short-circuits.

    Mirrors TS line 988 verbatim. The issue-spec AC bullet
    "fresh_tail_count = 0 still keeps the newest" is **wrong** — TS is
    the source of truth and ``test/lcm-integration.test.ts`` uses
    ``freshTailCount: 0`` in tests at lines 1510, 1567, 1628, 1688,
    1761 specifically to disable the fresh-tail.
    """

    def test_count_zero_with_messages(self) -> None:
        items = [_make_msg(ordinal=i, tokens=10) for i in range(5)]
        assert resolve_fresh_tail_ordinal(items, 0, None) == EMPTY_FRESH_TAIL_ORDINAL

    def test_count_zero_with_summaries(self) -> None:
        items = [_make_summary(ordinal=i, tokens=50) for i in range(3)]
        assert resolve_fresh_tail_ordinal(items, 0, None) == EMPTY_FRESH_TAIL_ORDINAL

    def test_count_zero_with_token_cap(self) -> None:
        items = [_make_msg(ordinal=i, tokens=10) for i in range(5)]
        assert (
            resolve_fresh_tail_ordinal(items, 0, fresh_tail_max_tokens=1000)
            == EMPTY_FRESH_TAIL_ORDINAL
        )

    def test_negative_count(self) -> None:
        """Negative counts also short-circuit (TS ``<= 0`` covers this)."""
        items = [_make_msg(ordinal=i, tokens=10) for i in range(5)]
        assert resolve_fresh_tail_ordinal(items, -1, None) == EMPTY_FRESH_TAIL_ORDINAL

    def test_negative_count_large(self) -> None:
        items = [_make_msg(ordinal=i, tokens=10) for i in range(5)]
        assert resolve_fresh_tail_ordinal(items, -1_000_000, None) == EMPTY_FRESH_TAIL_ORDINAL


# ---------------------------------------------------------------------------
# Default ``fresh_tail_count = 8`` (TS AssembleContextInput default)
# ---------------------------------------------------------------------------


class TestDefaultFreshTailCount:
    """The default of 8 is set by the caller, not by this function.

    The TS source has no default at the function signature — ``assemble``
    line 1104 does ``const freshTailCount = input.freshTailCount ?? 8;``
    before passing it down. This test class asserts that explicitly
    passing 8 reproduces the spec'd defaults; the 03-08 integration
    test will validate that ``AssembleInput.fresh_tail_count: int = 8``
    matches.
    """

    def test_count_8_protects_eight(self) -> None:
        """8 messages + count=8 → boundary = oldest ordinal."""
        items = [_make_msg(ordinal=i, tokens=10) for i in range(8)]
        assert resolve_fresh_tail_ordinal(items, 8, None) == 0

    def test_count_8_with_more_messages(self) -> None:
        """20 messages + count=8 → boundary = ordinal of 8th-newest = 12."""
        items = [_make_msg(ordinal=i, tokens=10) for i in range(20)]
        # Protect ordinals 12..19; boundary = 12.
        assert resolve_fresh_tail_ordinal(items, 8, None) == 12

    def test_count_8_with_fewer_messages(self) -> None:
        """3 messages + count=8 → keep all; boundary = 0."""
        items = [_make_msg(ordinal=i, tokens=10) for i in range(3)]
        assert resolve_fresh_tail_ordinal(items, 8, None) == 0


# ---------------------------------------------------------------------------
# Boundary semantics — splitter integration sanity
# ---------------------------------------------------------------------------


class TestBoundarySemantics:
    """The returned ordinal is the OLDEST kept item, not "first past tail".

    Downstream uses:
      * ``item.ordinal >= boundary``  → fresh tail (kept).
      * ``item.ordinal <  boundary``  → evictable.

    These tests assert that the boundary matches the contract by
    simulating the splitter inline.
    """

    def test_boundary_is_oldest_kept_message(self) -> None:
        """Boundary equals the ordinal of the oldest protected item."""
        items = [_make_msg(ordinal=i, tokens=10) for i in range(10)]
        boundary = resolve_fresh_tail_ordinal(items, 3, None)
        # Simulate the splitter:
        fresh_tail = [item for item in items if item.ordinal >= boundary]
        evictable = [item for item in items if item.ordinal < boundary]
        assert len(fresh_tail) == 3
        assert len(evictable) == 7
        assert [it.ordinal for it in fresh_tail] == [7, 8, 9]
        assert [it.ordinal for it in evictable] == [0, 1, 2, 3, 4, 5, 6]

    def test_empty_sentinel_all_evictable(self) -> None:
        """When sentinel returned, everything is evictable.

        Splitter predicate: ``item.ordinal >= sys.maxsize`` is false
        for every real ordinal (SQLite INTEGER max is also
        ``2**63 - 1``, but real conversations will never approach
        that).
        """
        items = [_make_summary(ordinal=i, tokens=50) for i in range(5)]
        boundary = resolve_fresh_tail_ordinal(items, 8, None)
        # Simulate the splitter:
        fresh_tail = [item for item in items if item.ordinal >= boundary]
        evictable = [item for item in items if item.ordinal < boundary]
        assert len(fresh_tail) == 0
        assert len(evictable) == 5

    def test_boundary_with_summaries_between_kept(self) -> None:
        """Summaries between kept messages get classified as fresh tail.

        This is the splitter ``>= boundary`` invariant in action — the
        function returns 2 (ordinal of the oldest kept msg); the
        summary at ordinal 3 ends up in fresh tail because 3 >= 2.
        """
        items = [
            _make_msg(ordinal=0, tokens=10),  # evictable
            _make_msg(ordinal=1, tokens=10),  # evictable
            _make_msg(ordinal=2, tokens=10),  # fresh-tail seed (oldest kept)
            _make_summary(ordinal=3, tokens=50),  # fresh-tail by splitter
            _make_msg(ordinal=4, tokens=10),  # fresh-tail
        ]
        boundary = resolve_fresh_tail_ordinal(items, 2, None)
        assert boundary == 2
        fresh_tail = [item for item in items if item.ordinal >= boundary]
        assert [it.ordinal for it in fresh_tail] == [2, 3, 4]


# ---------------------------------------------------------------------------
# Fuzz / property tests
# ---------------------------------------------------------------------------


class TestFuzz:
    """Property-based assertions across random fixtures.

    No external hypothesis dep — we use deterministic ``random.Random``
    seeded per case for reproducibility.
    """

    @pytest.mark.parametrize("seed", list(range(20)))
    def test_boundary_le_newest_message_ordinal(self, seed: int) -> None:
        """The boundary is ``<=`` the ordinal of the newest raw message.

        This is the load-bearing splitter invariant: the newest message
        MUST be in the fresh tail (``item.ordinal >= boundary``), so
        ``boundary <= newest_msg.ordinal``.
        """
        import random

        rng = random.Random(seed)
        n = rng.randint(1, 30)
        # Random monotonically-increasing ordinals.
        ordinals = sorted(rng.sample(range(1, 1000), n))
        items: list[ResolvedItem] = []
        for ord_ in ordinals:
            if rng.random() < 0.3:
                items.append(_make_summary(ordinal=ord_, tokens=rng.randint(1, 100)))
            else:
                items.append(_make_msg(ordinal=ord_, tokens=rng.randint(1, 100)))

        raw_messages = [it for it in items if it.is_message]
        count = rng.randint(1, 16)
        cap = rng.choice([None, rng.randint(0, 500)])

        boundary = resolve_fresh_tail_ordinal(items, count, cap)
        if not raw_messages:
            assert boundary == EMPTY_FRESH_TAIL_ORDINAL
            return

        newest_msg = raw_messages[-1]
        # Boundary must be <= newest raw message's ordinal (else newest
        # would be evicted, violating the always-protect invariant).
        assert boundary <= newest_msg.ordinal, (
            f"boundary={boundary} > newest msg ordinal={newest_msg.ordinal}"
        )
        # And > 0 (or == EMPTY_FRESH_TAIL_ORDINAL if no raw messages,
        # already handled above).
        assert boundary >= 0

    @pytest.mark.parametrize("seed", list(range(20)))
    def test_boundary_matches_some_message_ordinal(self, seed: int) -> None:
        """The boundary always equals the ordinal of some kept raw message.

        When raw messages exist, ``resolve_fresh_tail_ordinal`` returns
        ``item.ordinal`` for the oldest protected message — never a
        synthetic value (unless the sentinel).
        """
        import random

        rng = random.Random(seed)
        n = rng.randint(2, 20)
        ordinals = sorted(rng.sample(range(1, 1000), n))
        items = [_make_msg(ordinal=o, tokens=rng.randint(1, 50)) for o in ordinals]

        count = rng.randint(1, 16)
        boundary = resolve_fresh_tail_ordinal(items, count, None)
        assert boundary in ordinals, f"boundary={boundary} not in any item.ordinal={ordinals}"

    @pytest.mark.parametrize("seed", list(range(20)))
    def test_protected_count_never_exceeds_request(self, seed: int) -> None:
        """Number of items with ``ordinal >= boundary`` (raw-only) is ``<= count``.

        This is the count-cap invariant: the function MUST NOT protect
        more raw messages than requested. (Summaries between protected
        messages are fine — they're added by the downstream splitter,
        not the count budget.)
        """
        import random

        rng = random.Random(seed + 1000)
        n = rng.randint(1, 20)
        ordinals = sorted(rng.sample(range(1, 1000), n))
        items = [_make_msg(ordinal=o, tokens=rng.randint(1, 50)) for o in ordinals]

        count = rng.randint(1, 16)
        boundary = resolve_fresh_tail_ordinal(items, count, None)
        protected_msgs = [it for it in items if it.is_message and it.ordinal >= boundary]
        assert len(protected_msgs) <= count


# ---------------------------------------------------------------------------
# Regression / canonical fixtures from TS integration tests
# ---------------------------------------------------------------------------


class TestRegressionFixtures:
    """Canonical scenarios pulled from ``test/lcm-integration.test.ts``.

    Each test mirrors a real scenario the TS suite already validates
    end-to-end, but isolated to just the fresh-tail step. Line refs
    point at the TS test.
    """

    def test_integration_682_count_4(self) -> None:
        """``lcm-integration.test.ts:682`` — ``freshTailCount: 4``.

        Stand-in: 6 raw messages + count=4 → boundary = ordinal of 4th
        newest (i.e. position N-4).
        """
        items = [_make_msg(ordinal=i, tokens=10) for i in range(6)]
        assert resolve_fresh_tail_ordinal(items, 4, None) == 2

    def test_integration_842_count_1(self) -> None:
        """``lcm-integration.test.ts:842`` — ``freshTailCount: 1``.

        With count=1, exactly the newest is protected.
        """
        items = [_make_msg(ordinal=i, tokens=10) for i in range(5)]
        assert resolve_fresh_tail_ordinal(items, 1, None) == 4

    def test_integration_924_count_4_with_cap(self) -> None:
        """``lcm-integration.test.ts:924`` — ``freshTailCount: 4, freshTailMaxTokens: 110``.

        4 messages, 30 tokens each. count=4, cap=110.
        Walk: ord=3 (30), ord=2 (60), ord=1 (90), ord=0 (90+30=120 > 110, break).
        Boundary = 1.
        """
        items = [_make_msg(ordinal=i, tokens=30) for i in range(4)]
        assert resolve_fresh_tail_ordinal(items, 4, fresh_tail_max_tokens=110) == 1

    def test_integration_941_count_2_cap_50(self) -> None:
        """``lcm-integration.test.ts:941`` — ``freshTailCount: 2, freshTailMaxTokens: 50``.

        4 messages, 30 tokens each. count=2, cap=50.
        Walk: ord=3 (30), ord=2 (30+30=60 > 50, break).
        Boundary = 3 (only newest kept).
        """
        items = [_make_msg(ordinal=i, tokens=30) for i in range(4)]
        assert resolve_fresh_tail_ordinal(items, 2, fresh_tail_max_tokens=50) == 3

    def test_integration_1510_count_0(self) -> None:
        """``lcm-integration.test.ts:1510`` — ``freshTailCount: 0``.

        The most important "count=0" regression test: every TS
        compaction config that uses ``freshTailCount: 0`` (and there
        are 5 such configs) relies on this disabled-fresh-tail
        behavior. The 03-05 issue-spec AC says "newest is preserved"
        which is **wrong**; this test guards the actual TS behavior.
        """
        items = [_make_msg(ordinal=i, tokens=10) for i in range(10)]
        assert resolve_fresh_tail_ordinal(items, 0, None) == EMPTY_FRESH_TAIL_ORDINAL

    def test_integration_1838_count_16(self) -> None:
        """``lcm-integration.test.ts:1838`` — ``freshTailCount: 16``.

        Larger window than the default — confirms no implicit ceiling.
        """
        items = [_make_msg(ordinal=i, tokens=10) for i in range(20)]
        # Keep newest 16 (ordinals 4..19); boundary = 4.
        assert resolve_fresh_tail_ordinal(items, 16, None) == 4


# ---------------------------------------------------------------------------
# Type / contract guards
# ---------------------------------------------------------------------------


class TestReturnType:
    """The return value must always be ``int``."""

    def test_sentinel_is_int(self) -> None:
        result = resolve_fresh_tail_ordinal([], 8, None)
        assert type(result) is int

    def test_normal_return_is_int(self) -> None:
        items = [_make_msg(ordinal=5, tokens=10)]
        result = resolve_fresh_tail_ordinal(items, 8, None)
        assert type(result) is int

    def test_sentinel_equals_sys_maxsize(self) -> None:
        """The sentinel is :data:`sys.maxsize` — documented for downstream.

        Future maintainers may need to know the sentinel's exact value
        when implementing the splitter (03-06). Pinning it here gives
        early warning if someone refactors the sentinel to a different
        type.
        """
        assert EMPTY_FRESH_TAIL_ORDINAL == sys.maxsize
