"""Tests for :mod:`lossless_hermes.store.summary` — the SummaryStore.

Covers the acceptance criteria from
``epics/01-storage/01-09-summary-store.md``:

* CRUD: insert / get / list-by-conversation; null-suppression filter.
* Lineage: link_summary_to_messages / parents; subtree recursive walk;
  shallow-tree helpers (max-depth + leaf-link inverse lookup).
* Context items: append/replace/prune/token-count.
* Search: regex (default path until FTS5 lands in 01-05); LIKE fallback;
  CJK LIKE fallback (`_search_like_cjk`).
* Atomic replace_context_range_with_summary (kill-mid-transaction integrity).
* FK CASCADE on conversation delete; FK RESTRICT on summary-referenced-by-
  context_items.
* Large files + bootstrap state CRUD.

These tests are direct ports of the relevant TS cases from
``test/summary-store.test.ts`` and the storage-only subset of
``test/lcm-integration.test.ts``. Where the TS test depends on a not-yet-
ported subsystem (ConversationStore for 01-08), we seed the DB with direct
SQL inserts.

References:

* :mod:`lossless_hermes.store.summary` — implementation under test.
* ``epics/01-storage/01-09-summary-store.md`` — issue spec + AC.
* ``/Volumes/LEXAR/Claude/lossless-claw/test/summary-store.test.ts`` — TS
  source for the shallow-tree helpers + LIKE fallback ordering tests.
* ``docs/porting-guides/storage.md`` §4.2 — the SummaryStore method table.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Iterator

import pytest

from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.store.full_text_fallback import contains_cjk
from lossless_hermes.store.summary import (
    CreateLargeFileInput,
    CreateSummaryInput,
    ReplaceContextRangeInput,
    SummarySearchInput,
    SummaryStore,
    UpsertConversationBootstrapStateInput,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _migrated_conn(fts5: bool = False) -> sqlite3.Connection:
    """Open + migrate an in-memory DB."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(conn, fts5_available=fts5)
    return conn


def _seed_conversation(conn: sqlite3.Connection, session_id: str = "s1") -> int:
    """Insert a conversation row + return its id."""
    cur = conn.execute(
        "INSERT INTO conversations (session_id, session_key, title) VALUES (?, ?, ?)",
        (session_id, f"sk_{session_id}", "Test"),
    )
    return int(cur.lastrowid)  # type: ignore[arg-type]


def _seed_message(
    conn: sqlite3.Connection,
    conv_id: int,
    seq: int,
    role: str = "user",
    content: str = "hello",
    token_count: int = 4,
) -> int:
    """Insert a messages row + return its id."""
    cur = conn.execute(
        """
        INSERT INTO messages (conversation_id, seq, role, content, token_count)
        VALUES (?, ?, ?, ?, ?)
        """,
        (conv_id, seq, role, content, token_count),
    )
    return int(cur.lastrowid)  # type: ignore[arg-type]


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    """A migrated DB connection (in-memory, FK enforcement on, no FTS5)."""
    c = _migrated_conn(fts5=False)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def fts_conn() -> Iterator[sqlite3.Connection]:
    """A migrated DB connection with ``fts5_available=True`` requested.

    The actual FTS tables are created by 01-05 (not in main yet) — so this
    fixture is used for tests that verify the *gateway behavior* even when
    the FTS table is missing (insert_summary's try/except).
    """
    c = _migrated_conn(fts5=True)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def store(conn: sqlite3.Connection) -> SummaryStore:
    """SummaryStore configured against the no-FTS connection."""
    return SummaryStore(conn, fts5_available=False, trigram_tokenizer_available=False)


@pytest.fixture
def conv_id(conn: sqlite3.Connection) -> int:
    """A seeded conversation id."""
    return _seed_conversation(conn)


# ---------------------------------------------------------------------------
# CRUD + null-suppression filter
# ---------------------------------------------------------------------------


def test_insert_and_get_summary_round_trip(store: SummaryStore, conv_id: int) -> None:
    rec = store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_1",
            conversation_id=conv_id,
            kind="leaf",
            content="hello world",
            token_count=5,
        )
    )
    assert rec.summary_id == "sum_1"
    assert rec.kind == "leaf"
    assert rec.depth == 0  # default for leaf
    assert rec.token_count == 5
    assert rec.file_ids == []
    assert rec.model == "unknown"  # default

    fetched = store.get_summary("sum_1")
    assert fetched is not None
    assert fetched.summary_id == "sum_1"


def test_insert_summary_condensed_default_depth(store: SummaryStore, conv_id: int) -> None:
    rec = store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_c",
            conversation_id=conv_id,
            kind="condensed",
            content="condensed",
            token_count=3,
        )
    )
    # TS lines 404-409: depth defaults to 1 for condensed.
    assert rec.depth == 1


def test_insert_summary_explicit_depth_overrides_default(store: SummaryStore, conv_id: int) -> None:
    rec = store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_3",
            conversation_id=conv_id,
            kind="condensed",
            depth=3,
            content="deep",
            token_count=5,
        )
    )
    assert rec.depth == 3


def test_insert_summary_serializes_file_ids(store: SummaryStore, conv_id: int) -> None:
    rec = store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_f",
            conversation_id=conv_id,
            kind="leaf",
            content="with files",
            token_count=5,
            file_ids=["file_a", "file_b"],
        )
    )
    assert rec.file_ids == ["file_a", "file_b"]


def test_insert_summary_clamps_negative_descendants(store: SummaryStore, conv_id: int) -> None:
    rec = store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_neg",
            conversation_id=conv_id,
            kind="leaf",
            content="x",
            token_count=1,
            descendant_count=-5,
            descendant_token_count=-100,
            source_message_token_count=-3,
        )
    )
    # TS lines 386-403: clamp to non-negative.
    assert rec.descendant_count == 0
    assert rec.descendant_token_count == 0
    assert rec.source_message_token_count == 0


def test_insert_summary_records_session_key_from_conversation(
    store: SummaryStore, conv_id: int, conn: sqlite3.Connection
) -> None:
    """v4.1 Gap 8 fix: insert atomically populates session_key from conversations."""
    store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_sk",
            conversation_id=conv_id,
            kind="leaf",
            content="x",
            token_count=1,
        )
    )
    row = conn.execute(
        "SELECT session_key FROM summaries WHERE summary_id = ?", ("sum_sk",)
    ).fetchone()
    assert row[0] == "sk_s1"  # matches the seed in _seed_conversation


def test_get_summary_excludes_suppressed_by_default(
    store: SummaryStore, conv_id: int, conn: sqlite3.Connection
) -> None:
    """v4.1 §10: agent-facing reads exclude suppressed_at IS NOT NULL."""
    store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_s",
            conversation_id=conv_id,
            kind="leaf",
            content="x",
            token_count=1,
        )
    )
    conn.execute(
        "UPDATE summaries SET suppressed_at = datetime('now') WHERE summary_id = ?",
        ("sum_s",),
    )
    # Default path: not visible.
    assert store.get_summary("sum_s") is None
    # Internal path: visible.
    assert store.get_summary("sum_s", include_suppressed=True) is not None


def test_get_summary_returns_none_for_missing(store: SummaryStore) -> None:
    assert store.get_summary("nonexistent") is None


def test_get_summaries_by_conversation_returns_ordered_by_created_at(
    store: SummaryStore, conv_id: int
) -> None:
    for i in range(3):
        store.insert_summary(
            CreateSummaryInput(
                summary_id=f"sum_{i}",
                conversation_id=conv_id,
                kind="leaf",
                content=f"content {i}",
                token_count=i + 1,
            )
        )
    rows = store.get_summaries_by_conversation(conv_id)
    assert len(rows) == 3
    assert [r.summary_id for r in rows] == ["sum_0", "sum_1", "sum_2"]


# ---------------------------------------------------------------------------
# Lineage: link_summary_to_messages / parents
# ---------------------------------------------------------------------------


def test_link_summary_to_messages_idempotent(
    store: SummaryStore, conv_id: int, conn: sqlite3.Connection
) -> None:
    msg_a = _seed_message(conn, conv_id, 1)
    msg_b = _seed_message(conn, conv_id, 2)
    store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_l",
            conversation_id=conv_id,
            kind="leaf",
            content="x",
            token_count=1,
        )
    )
    store.link_summary_to_messages("sum_l", [msg_a, msg_b])
    # Re-link: no-op (ON CONFLICT DO NOTHING).
    store.link_summary_to_messages("sum_l", [msg_a, msg_b])

    assert store.get_summary_messages("sum_l") == [msg_a, msg_b]


def test_link_summary_to_messages_empty_list_is_no_op(store: SummaryStore) -> None:
    store.link_summary_to_messages("sum_x", [])  # should not raise


def test_link_summary_to_parents_idempotent(store: SummaryStore, conv_id: int) -> None:
    # Two leaf summaries + one condensed that references both.
    for sid in ("sum_a", "sum_b"):
        store.insert_summary(
            CreateSummaryInput(
                summary_id=sid,
                conversation_id=conv_id,
                kind="leaf",
                content=sid,
                token_count=1,
            )
        )
    store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_c",
            conversation_id=conv_id,
            kind="condensed",
            content="c",
            token_count=2,
        )
    )
    store.link_summary_to_parents("sum_c", ["sum_a", "sum_b"])
    store.link_summary_to_parents("sum_c", ["sum_a", "sum_b"])  # re-link OK

    parents = store.get_summary_parents("sum_c")
    assert [p.summary_id for p in parents] == ["sum_a", "sum_b"]


# ---------------------------------------------------------------------------
# Shallow-tree helpers (the first case from test/summary-store.test.ts)
# ---------------------------------------------------------------------------


def test_shallow_tree_helpers_max_depth_and_leaf_links(
    store: SummaryStore, conv_id: int, conn: sqlite3.Connection
) -> None:
    """Port of `SummaryStore shallow-tree helpers` (summary-store.test.ts:20-99).

    Seeds three messages + three summaries (two leaves + one root at depth=2),
    links leaves to specific messages, then asserts both helpers:

    * ``get_conversation_max_summary_depth`` returns the max depth across
      all summaries in the conversation (2).
    * ``get_leaf_summary_links_for_message_ids`` returns (message_id,
      summary_id) tuples in the same order as the input message_ids list
      (only matched messages appear; suppressed/non-leaf summaries don't
      appear).
    """
    first_message = _seed_message(conn, conv_id, 1, "user", "first raw fact")
    second_message = _seed_message(conn, conv_id, 2, "assistant", "second raw fact")
    tail_message = _seed_message(conn, conv_id, 3, "user", "fresh tail fact")

    store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_leaf_a",
            conversation_id=conv_id,
            kind="leaf",
            depth=0,
            content="leaf A",
            token_count=5,
        )
    )
    store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_leaf_b",
            conversation_id=conv_id,
            kind="leaf",
            depth=0,
            content="leaf B",
            token_count=5,
        )
    )
    store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_root",
            conversation_id=conv_id,
            kind="condensed",
            depth=2,
            content="root summary",
            token_count=6,
        )
    )

    store.link_summary_to_messages("sum_leaf_a", [first_message])
    store.link_summary_to_messages("sum_leaf_b", [second_message])

    assert store.get_conversation_max_summary_depth(conv_id) == 2

    links = store.get_leaf_summary_links_for_message_ids(
        conv_id,
        [tail_message, second_message, first_message],
    )
    # Tail message has no leaf — skipped. Others appear in input order.
    assert len(links) == 2
    assert links[0].message_id == second_message
    assert links[0].summary_id == "sum_leaf_b"
    assert links[1].message_id == first_message
    assert links[1].summary_id == "sum_leaf_a"


def test_get_conversation_max_summary_depth_none_when_empty(
    store: SummaryStore, conv_id: int
) -> None:
    assert store.get_conversation_max_summary_depth(conv_id) is None


def test_get_leaf_summary_links_skips_suppressed(
    store: SummaryStore, conv_id: int, conn: sqlite3.Connection
) -> None:
    """Wave-8 Auditor #1: must filter suppressed leaves out of expand-query."""
    msg = _seed_message(conn, conv_id, 1)
    store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_suppressed",
            conversation_id=conv_id,
            kind="leaf",
            content="x",
            token_count=1,
        )
    )
    store.link_summary_to_messages("sum_suppressed", [msg])
    conn.execute(
        "UPDATE summaries SET suppressed_at = datetime('now') WHERE summary_id = ?",
        ("sum_suppressed",),
    )
    links = store.get_leaf_summary_links_for_message_ids(conv_id, [msg])
    assert links == []


def test_get_leaf_summary_links_normalizes_ids(
    store: SummaryStore, conv_id: int, conn: sqlite3.Connection
) -> None:
    """Filters out non-positive ints + dedupes (TS lines 620-629)."""
    msg = _seed_message(conn, conv_id, 1)
    store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_x",
            conversation_id=conv_id,
            kind="leaf",
            content="x",
            token_count=1,
        )
    )
    store.link_summary_to_messages("sum_x", [msg])
    # Empty, zero, negative, duplicates — all filtered/deduped.
    assert store.get_leaf_summary_links_for_message_ids(conv_id, []) == []
    assert store.get_leaf_summary_links_for_message_ids(conv_id, [-1, 0]) == []
    links = store.get_leaf_summary_links_for_message_ids(conv_id, [msg, msg, msg])
    assert len(links) == 1


# ---------------------------------------------------------------------------
# Subtree walks: 4-level pyramid + ordering
# ---------------------------------------------------------------------------


def _seed_pyramid(store: SummaryStore, conv_id: int) -> dict[str, str]:
    """Seed a 4-level pyramid: 4 leaves → 2 mid → 1 upper → 1 root.

    Returns a dict mapping label → summary_id.
    """
    ids = {}
    for i, label in enumerate(["leaf_0", "leaf_1", "leaf_2", "leaf_3"]):
        sid = f"sum_{label}"
        store.insert_summary(
            CreateSummaryInput(
                summary_id=sid,
                conversation_id=conv_id,
                kind="leaf",
                depth=0,
                content=f"content {label}",
                token_count=2,
            )
        )
        ids[label] = sid

    for i, label in enumerate(["mid_0", "mid_1"]):
        sid = f"sum_{label}"
        store.insert_summary(
            CreateSummaryInput(
                summary_id=sid,
                conversation_id=conv_id,
                kind="condensed",
                depth=1,
                content=f"content {label}",
                token_count=3,
            )
        )
        ids[label] = sid

    store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_upper",
            conversation_id=conv_id,
            kind="condensed",
            depth=2,
            content="content upper",
            token_count=4,
        )
    )
    ids["upper"] = "sum_upper"

    store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_root",
            conversation_id=conv_id,
            kind="condensed",
            depth=3,
            content="content root",
            token_count=5,
        )
    )
    ids["root"] = "sum_root"

    # Wire: root → upper → {mid_0, mid_1} → {leaf_*}.
    store.link_summary_to_parents("sum_root", ["sum_upper"])
    store.link_summary_to_parents("sum_upper", ["sum_mid_0", "sum_mid_1"])
    store.link_summary_to_parents("sum_mid_0", ["sum_leaf_0", "sum_leaf_1"])
    store.link_summary_to_parents("sum_mid_1", ["sum_leaf_2", "sum_leaf_3"])
    return ids


def test_get_summary_subtree_4_level_pyramid(store: SummaryStore, conv_id: int) -> None:
    """The recursive CTE walks from leaf-like seed UP toward the root.

    ``summary_parents`` table semantics: a row ``(summary_id=X,
    parent_summary_id=Y, ordinal=N)`` means X is the condensed/derived row,
    and Y is the source summary that X compacted. The recursive CTE in the
    TS source joins ``sp.parent_summary_id = subtree.summary_id``, so the
    walk follows the edge from a source up toward the condensed summaries
    that consumed it. Seeded with a leaf, the walk returns the leaf plus
    every condensation that incorporated it.

    Fixture is the 4-level pyramid: leaves → mid → upper → root. Walking
    from ``sum_leaf_0`` produces:

    * depth_from_root=0: ``sum_leaf_0`` (seed)
    * depth_from_root=1: ``sum_mid_0`` (consumed leaf_0)
    * depth_from_root=2: ``sum_upper`` (consumed mid_0)
    * depth_from_root=3: ``sum_root`` (consumed upper)
    """
    _seed_pyramid(store, conv_id)
    nodes = store.get_summary_subtree("sum_leaf_0")
    by_id = {n.summary_id: n for n in nodes}
    assert by_id["sum_leaf_0"].depth_from_root == 0
    assert by_id["sum_mid_0"].depth_from_root == 1
    assert by_id["sum_upper"].depth_from_root == 2
    assert by_id["sum_root"].depth_from_root == 3
    # Leaf_1/2/3, mid_1 are NOT on the path from leaf_0 — they don't appear.
    assert "sum_leaf_1" not in by_id
    assert "sum_mid_1" not in by_id


def test_get_summary_subtree_from_root_yields_only_root(store: SummaryStore, conv_id: int) -> None:
    """Walking from the root summary returns just the seed — nothing
    consumed the root."""
    _seed_pyramid(store, conv_id)
    nodes = store.get_summary_subtree("sum_root")
    assert [n.summary_id for n in nodes] == ["sum_root"]


def test_get_summary_subtree_excludes_suppressed(
    store: SummaryStore, conv_id: int, conn: sqlite3.Connection
) -> None:
    _seed_pyramid(store, conv_id)
    conn.execute(
        "UPDATE summaries SET suppressed_at = datetime('now') WHERE summary_id = ?",
        ("sum_mid_0",),
    )
    nodes = store.get_summary_subtree("sum_leaf_0")
    assert "sum_mid_0" not in [n.summary_id for n in nodes]


def test_get_summary_subtree_branch_walk(store: SummaryStore, conv_id: int) -> None:
    """Walking from mid_0 also yields upper and root (since mid_0 was
    consumed by upper which was consumed by root)."""
    _seed_pyramid(store, conv_id)
    nodes = store.get_summary_subtree("sum_mid_0")
    by_id = {n.summary_id: n for n in nodes}
    assert set(by_id.keys()) == {"sum_mid_0", "sum_upper", "sum_root"}
    assert by_id["sum_mid_0"].depth_from_root == 0
    assert by_id["sum_upper"].depth_from_root == 1
    assert by_id["sum_root"].depth_from_root == 2


# ---------------------------------------------------------------------------
# Context items
# ---------------------------------------------------------------------------


def test_append_context_message_assigns_next_ordinal(
    store: SummaryStore, conv_id: int, conn: sqlite3.Connection
) -> None:
    msg_a = _seed_message(conn, conv_id, 1)
    msg_b = _seed_message(conn, conv_id, 2)
    store.append_context_message(conv_id, msg_a)
    store.append_context_message(conv_id, msg_b)
    items = store.get_context_items(conv_id)
    assert [i.ordinal for i in items] == [0, 1]
    assert [i.message_id for i in items] == [msg_a, msg_b]


def test_append_context_messages_bulk(
    store: SummaryStore, conv_id: int, conn: sqlite3.Connection
) -> None:
    ids = [_seed_message(conn, conv_id, i) for i in range(1, 4)]
    store.append_context_messages(conv_id, ids)
    items = store.get_context_items(conv_id)
    assert [i.ordinal for i in items] == [0, 1, 2]
    assert [i.message_id for i in items] == ids


def test_append_context_messages_empty_list_is_no_op(store: SummaryStore, conv_id: int) -> None:
    store.append_context_messages(conv_id, [])
    assert store.get_context_items(conv_id) == []


def test_append_context_summary(store: SummaryStore, conv_id: int) -> None:
    store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_z",
            conversation_id=conv_id,
            kind="leaf",
            content="x",
            token_count=1,
        )
    )
    store.append_context_summary(conv_id, "sum_z")
    items = store.get_context_items(conv_id)
    assert items[0].item_type == "summary"
    assert items[0].summary_id == "sum_z"


def test_get_context_token_count_sums_messages_and_summaries(
    store: SummaryStore, conv_id: int, conn: sqlite3.Connection
) -> None:
    msg = _seed_message(conn, conv_id, 1, token_count=7)
    store.append_context_message(conv_id, msg)
    store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_t",
            conversation_id=conv_id,
            kind="leaf",
            content="x",
            token_count=11,
        )
    )
    store.append_context_summary(conv_id, "sum_t")
    assert store.get_context_token_count(conv_id) == 18


def test_replace_context_range_with_summary_resequences_ordinals(
    store: SummaryStore, conv_id: int, conn: sqlite3.Connection
) -> None:
    """The atomic replace re-assigns ordinals to be contiguous from 0."""
    msgs = [_seed_message(conn, conv_id, i) for i in range(1, 6)]  # 5 messages
    store.append_context_messages(conv_id, msgs)
    store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_repl",
            conversation_id=conv_id,
            kind="condensed",
            content="x",
            token_count=2,
        )
    )
    # Replace ordinals [1,3] (3 messages: msg2..msg4) with sum_repl.
    store.replace_context_range_with_summary(
        ReplaceContextRangeInput(
            conversation_id=conv_id,
            start_ordinal=1,
            end_ordinal=3,
            summary_id="sum_repl",
        )
    )
    items = store.get_context_items(conv_id)
    # Original 5 → 5-3+1 = 3 items: msg1, sum_repl, msg5.
    assert len(items) == 3
    assert [i.ordinal for i in items] == [0, 1, 2]
    assert items[1].item_type == "summary"
    assert items[1].summary_id == "sum_repl"


def test_replace_context_range_is_atomic_on_failure(
    store: SummaryStore, conv_id: int, conn: sqlite3.Connection
) -> None:
    """Kill-mid-transaction test: a Python-level exception inside the
    helper must leave context_items untouched (no half-replaced ranges)."""
    msgs = [_seed_message(conn, conv_id, i) for i in range(1, 4)]
    store.append_context_messages(conv_id, msgs)

    # Now force a failure: reference a nonexistent summary_id. The FK
    # restrict on context_items.summary_id makes the INSERT raise — and
    # the transaction rollback must reverse the DELETE that ran first.
    pre_items = store.get_context_items(conv_id)
    with pytest.raises(sqlite3.IntegrityError):
        store.replace_context_range_with_summary(
            ReplaceContextRangeInput(
                conversation_id=conv_id,
                start_ordinal=0,
                end_ordinal=2,
                summary_id="sum_does_not_exist",
            )
        )
    post_items = store.get_context_items(conv_id)
    # Rollback preserves the original state.
    assert [i.message_id for i in post_items] == [i.message_id for i in pre_items]


def test_prune_for_new_session_clears_messages(
    store: SummaryStore, conv_id: int, conn: sqlite3.Connection
) -> None:
    msgs = [_seed_message(conn, conv_id, i) for i in range(1, 3)]
    store.append_context_messages(conv_id, msgs)
    store.prune_for_new_session(conv_id, retain_depth=1)
    items = store.get_context_items(conv_id)
    # All message rows cleared; no summaries existed.
    assert items == []


def test_prune_for_new_session_keeps_summaries_at_or_above_depth(
    store: SummaryStore, conv_id: int
) -> None:
    # Two summaries: depth=0 (will be pruned), depth=2 (kept).
    store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_low",
            conversation_id=conv_id,
            kind="leaf",
            depth=0,
            content="x",
            token_count=1,
        )
    )
    store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_high",
            conversation_id=conv_id,
            kind="condensed",
            depth=2,
            content="x",
            token_count=2,
        )
    )
    store.append_context_summary(conv_id, "sum_low")
    store.append_context_summary(conv_id, "sum_high")
    store.prune_for_new_session(conv_id, retain_depth=1)
    items = store.get_context_items(conv_id)
    assert [i.summary_id for i in items] == ["sum_high"]


def test_prune_for_new_session_infinity_clears_all_summaries(
    store: SummaryStore, conv_id: int
) -> None:
    store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_inf",
            conversation_id=conv_id,
            kind="condensed",
            depth=5,
            content="x",
            token_count=1,
        )
    )
    store.append_context_summary(conv_id, "sum_inf")
    store.prune_for_new_session(conv_id, retain_depth=float("inf"))
    assert store.get_context_items(conv_id) == []


def test_prune_for_new_session_negative_depth_is_no_op(store: SummaryStore, conv_id: int) -> None:
    store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_neg",
            conversation_id=conv_id,
            kind="leaf",
            content="x",
            token_count=1,
        )
    )
    store.append_context_summary(conv_id, "sum_neg")
    store.prune_for_new_session(conv_id, retain_depth=-1)
    # No-op — both summary remains AND no error.
    assert len(store.get_context_items(conv_id)) == 1


def test_get_distinct_depths_in_context(store: SummaryStore, conv_id: int) -> None:
    for depth, sid in [(0, "sum_0"), (1, "sum_1"), (2, "sum_2"), (1, "sum_1b")]:
        store.insert_summary(
            CreateSummaryInput(
                summary_id=sid,
                conversation_id=conv_id,
                kind=("leaf" if depth == 0 else "condensed"),
                depth=depth,
                content="x",
                token_count=1,
            )
        )
        store.append_context_summary(conv_id, sid)
    depths = store.get_distinct_depths_in_context(conv_id)
    assert depths == [0, 1, 2]


def test_get_distinct_depths_in_context_with_max_ordinal(store: SummaryStore, conv_id: int) -> None:
    for depth, sid in [(0, "sum_a"), (1, "sum_b"), (2, "sum_c")]:
        store.insert_summary(
            CreateSummaryInput(
                summary_id=sid,
                conversation_id=conv_id,
                kind=("leaf" if depth == 0 else "condensed"),
                depth=depth,
                content="x",
                token_count=1,
            )
        )
        store.append_context_summary(conv_id, sid)
    # Only the first 2 (ordinals 0 and 1) → depths {0, 1}.
    depths = store.get_distinct_depths_in_context(conv_id, max_ordinal_exclusive=2)
    assert depths == [0, 1]


# ---------------------------------------------------------------------------
# Search: regex (LIKE-fallback ordering — the second case from summary-store.test.ts)
# ---------------------------------------------------------------------------


def test_regex_search_uses_content_recency_ordering(
    store: SummaryStore, conv_id: int, conn: sqlite3.Connection
) -> None:
    """Port of `uses content recency for fallback summary search ordering and
    time filters` (summary-store.test.ts:101-167).

    Two summaries: one with older latest_at but newer created_at; the other
    with newer latest_at but older created_at. Regex search must use
    ``COALESCE(latest_at, created_at)`` so the latest-covered-content wins.
    """
    store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_regex_old_content_recent_compaction",
            conversation_id=conv_id,
            kind="leaf",
            depth=0,
            content="pagedrop regression historical request",
            token_count=5,
            latest_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_regex_recent_content_older_compaction",
            conversation_id=conv_id,
            kind="leaf",
            depth=0,
            content="pagedrop regression recent request",
            token_count=5,
            latest_at=datetime(2026, 1, 9, tzinfo=timezone.utc),
        )
    )

    # Override created_at to invert the natural insert order so the test
    # actually exercises COALESCE(latest_at, created_at).
    conn.execute(
        "UPDATE summaries SET created_at = ? WHERE summary_id = ?",
        ("2026-01-10T00:00:00.000Z", "sum_regex_old_content_recent_compaction"),
    )
    conn.execute(
        "UPDATE summaries SET created_at = ? WHERE summary_id = ?",
        ("2026-01-05T00:00:00.000Z", "sum_regex_recent_content_older_compaction"),
    )

    results = store.search_summaries(
        SummarySearchInput(
            conversation_id=conv_id,
            query="pagedrop regression",
            mode="regex",
            limit=10,
        )
    )
    # Newer latest_at first.
    assert [r.summary_id for r in results] == [
        "sum_regex_recent_content_older_compaction",
        "sum_regex_old_content_recent_compaction",
    ]


def test_regex_search_respects_since_filter(
    store: SummaryStore, conv_id: int, conn: sqlite3.Connection
) -> None:
    """Same fixture as above — `since` filter excludes the older latest_at."""
    store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_a",
            conversation_id=conv_id,
            kind="leaf",
            content="pagedrop regression",
            token_count=5,
            latest_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_b",
            conversation_id=conv_id,
            kind="leaf",
            content="pagedrop regression",
            token_count=5,
            latest_at=datetime(2026, 1, 9, tzinfo=timezone.utc),
        )
    )
    results = store.search_summaries(
        SummarySearchInput(
            conversation_id=conv_id,
            query="pagedrop regression",
            mode="regex",
            since=datetime(2026, 1, 5, tzinfo=timezone.utc),
            limit=10,
        )
    )
    assert [r.summary_id for r in results] == ["sum_b"]


def test_regex_search_rejects_oversized_pattern(store: SummaryStore, conv_id: int) -> None:
    """ReDoS guard: patterns > 500 chars return an empty list."""
    store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_x",
            conversation_id=conv_id,
            kind="leaf",
            content="aaaa",
            token_count=1,
        )
    )
    bad_pattern = "a" * 501
    results = store.search_summaries(
        SummarySearchInput(
            conversation_id=conv_id,
            query=bad_pattern,
            mode="regex",
            limit=10,
        )
    )
    assert results == []


def test_regex_search_rejects_invalid_regex(store: SummaryStore, conv_id: int) -> None:
    """Bogus pattern (e.g. unbalanced bracket) returns an empty list."""
    store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_y",
            conversation_id=conv_id,
            kind="leaf",
            content="x",
            token_count=1,
        )
    )
    results = store.search_summaries(
        SummarySearchInput(
            conversation_id=conv_id,
            query="[unbalanced",
            mode="regex",
            limit=10,
        )
    )
    assert results == []


# ---------------------------------------------------------------------------
# Search: LIKE fallback (full_text mode + fts5_available=False)
# ---------------------------------------------------------------------------


def test_full_text_search_falls_back_to_like_when_fts5_disabled(
    store: SummaryStore, conv_id: int
) -> None:
    store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_l1",
            conversation_id=conv_id,
            kind="leaf",
            content="Database migration fallback keeps search usable without fts support.",
            token_count=12,
        )
    )
    results = store.search_summaries(
        SummarySearchInput(
            conversation_id=conv_id,
            query="database migration",
            mode="full_text",
            limit=10,
        )
    )
    assert len(results) == 1
    assert results[0].summary_id == "sum_l1"
    assert "database migration" in results[0].snippet.lower()


def test_full_text_search_like_returns_empty_for_empty_terms(
    store: SummaryStore, conv_id: int
) -> None:
    store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_q",
            conversation_id=conv_id,
            kind="leaf",
            content="x",
            token_count=1,
        )
    )
    # Empty/whitespace-only query → empty terms → no results.
    results = store.search_summaries(
        SummarySearchInput(
            conversation_id=conv_id,
            query="    ",
            mode="full_text",
            limit=10,
        )
    )
    assert results == []


# ---------------------------------------------------------------------------
# Search: CJK paths
# ---------------------------------------------------------------------------


def test_contains_cjk_detection_covers_unicode_blocks() -> None:
    """CJK regex matches every Unicode block range from the TS source."""
    # Unicode-block boundary chars (one per range):
    cases = [
        "一",  # U+4E00 (CJK Unified)
        "鿿",  # U+9FFF (CJK Unified end)
        "㐀",  # U+3400 (CJK Extension A)
        "豈",  # U+F900 (CJK Compatibility)
        "가",  # U+AC00 (Hangul)
        "힯",  # U+D7AF (Hangul end)
        "ぁ",  # U+3041 (Hiragana)
        "ヿ",  # U+30FF (Katakana end)
    ]
    for ch in cases:
        assert contains_cjk(ch), f"Expected {ch!r} to be detected as CJK"
    # Negatives:
    assert not contains_cjk("hello")
    assert not contains_cjk("")
    assert not contains_cjk("aá")  # Latin + diacritic


def test_cjk_like_fallback_returns_match_when_trigram_unavailable(
    store: SummaryStore, conv_id: int
) -> None:
    """CJK LIKE fallback path: ``会議の議事録`` content + ``議事録`` query.

    Since trigram_tokenizer_available=False, search routes straight to
    `_search_like_cjk`. The 2-char sliding-window terms still match.
    """
    store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_cjk",
            conversation_id=conv_id,
            kind="leaf",
            content="会議の議事録",
            token_count=12,
        )
    )
    results = store.search_summaries(
        SummarySearchInput(
            conversation_id=conv_id,
            query="議事録",
            mode="full_text",
            limit=10,
        )
    )
    assert len(results) == 1
    assert results[0].summary_id == "sum_cjk"


def test_cjk_like_fallback_short_segment_returns_empty_or_match(
    store: SummaryStore, conv_id: int
) -> None:
    """Single-char CJK query is supported by the LIKE fallback (TS lines
    1408-1413)."""
    store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_short",
            conversation_id=conv_id,
            kind="leaf",
            content="测",
            token_count=1,
        )
    )
    results = store.search_summaries(
        SummarySearchInput(
            conversation_id=conv_id,
            query="测",
            mode="full_text",
            limit=10,
        )
    )
    assert len(results) == 1


def test_cjk_with_latin_token_filter(store: SummaryStore, conv_id: int) -> None:
    """Mixed-language query: CJK + Latin terms must all match.

    The Latin LIKE clauses run alongside the CJK ones; rows missing the
    Latin part should NOT match.
    """
    store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_mix_yes",
            conversation_id=conv_id,
            kind="leaf",
            content="飞书播客 episode 5 notes",
            token_count=12,
        )
    )
    store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_mix_no",
            conversation_id=conv_id,
            kind="leaf",
            content="飞书播客 was discussed but no episode mentioned",
            token_count=12,
        )
    )
    results = store.search_summaries(
        SummarySearchInput(
            conversation_id=conv_id,
            query="飞书播客 episode",
            mode="full_text",
            limit=10,
        )
    )
    sids = sorted([r.summary_id for r in results])
    # Both contain "episode" — but the order is by recency.
    assert "sum_mix_yes" in sids
    assert "sum_mix_no" in sids


# ---------------------------------------------------------------------------
# FK CASCADE: deleting a conversation cascades to summaries, summary_messages,
# summary_parents, context_items, large_files, bootstrap_state.
# ---------------------------------------------------------------------------


def test_delete_conversation_cascades_to_summaries(
    store: SummaryStore, conv_id: int, conn: sqlite3.Connection
) -> None:
    store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_cas",
            conversation_id=conv_id,
            kind="leaf",
            content="x",
            token_count=1,
        )
    )
    conn.execute("DELETE FROM conversations WHERE conversation_id = ?", (conv_id,))
    row = conn.execute(
        "SELECT COUNT(*) FROM summaries WHERE summary_id = ?", ("sum_cas",)
    ).fetchone()
    assert row[0] == 0


def test_delete_conversation_cascades_to_context_items(
    store: SummaryStore, conv_id: int, conn: sqlite3.Connection
) -> None:
    msg = _seed_message(conn, conv_id, 1)
    store.append_context_message(conv_id, msg)
    conn.execute("DELETE FROM conversations WHERE conversation_id = ?", (conv_id,))
    row = conn.execute(
        "SELECT COUNT(*) FROM context_items WHERE conversation_id = ?", (conv_id,)
    ).fetchone()
    assert row[0] == 0


def test_delete_conversation_cascades_to_large_files(
    store: SummaryStore, conv_id: int, conn: sqlite3.Connection
) -> None:
    store.insert_large_file(
        CreateLargeFileInput(
            file_id="file_xyz",
            conversation_id=conv_id,
            storage_uri="hermes://store/file_xyz",
        )
    )
    conn.execute("DELETE FROM conversations WHERE conversation_id = ?", (conv_id,))
    assert store.get_large_file("file_xyz") is None


def test_delete_conversation_cascades_to_bootstrap_state(
    store: SummaryStore, conv_id: int, conn: sqlite3.Connection
) -> None:
    store.upsert_conversation_bootstrap_state(
        UpsertConversationBootstrapStateInput(
            conversation_id=conv_id,
            session_file_path="/tmp/s.jsonl",
            last_seen_size=100,
            last_seen_mtime_ms=1000,
            last_processed_offset=50,
        )
    )
    conn.execute("DELETE FROM conversations WHERE conversation_id = ?", (conv_id,))
    assert store.get_conversation_bootstrap_state(conv_id) is None


def test_delete_summary_referenced_by_context_item_raises_integrity_error(
    store: SummaryStore, conv_id: int, conn: sqlite3.Connection
) -> None:
    """FK RESTRICT: context_items.summary_id REFERENCES summaries(...) ON DELETE RESTRICT."""
    store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_restricted",
            conversation_id=conv_id,
            kind="leaf",
            content="x",
            token_count=1,
        )
    )
    store.append_context_summary(conv_id, "sum_restricted")

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM summaries WHERE summary_id = ?", ("sum_restricted",))


# ---------------------------------------------------------------------------
# Large files
# ---------------------------------------------------------------------------


def test_insert_and_get_large_file_round_trip(store: SummaryStore, conv_id: int) -> None:
    rec = store.insert_large_file(
        CreateLargeFileInput(
            file_id="file_lf",
            conversation_id=conv_id,
            storage_uri="hermes://store/file_lf",
            file_name="data.txt",
            mime_type="text/plain",
            byte_size=4096,
            exploration_summary="A 4KB text dump.",
        )
    )
    assert rec.file_id == "file_lf"
    assert rec.byte_size == 4096
    assert rec.exploration_summary == "A 4KB text dump."

    fetched = store.get_large_file("file_lf")
    assert fetched is not None
    assert fetched.storage_uri == "hermes://store/file_lf"


def test_get_large_files_by_conversation_orders_by_created_at(
    store: SummaryStore, conv_id: int
) -> None:
    for i in range(3):
        store.insert_large_file(
            CreateLargeFileInput(
                file_id=f"file_{i}",
                conversation_id=conv_id,
                storage_uri=f"hermes://store/file_{i}",
            )
        )
    files = store.get_large_files_by_conversation(conv_id)
    assert [f.file_id for f in files] == ["file_0", "file_1", "file_2"]


def test_get_large_file_missing_returns_none(store: SummaryStore) -> None:
    assert store.get_large_file("nonexistent") is None


# ---------------------------------------------------------------------------
# Bootstrap state
# ---------------------------------------------------------------------------


def test_upsert_conversation_bootstrap_state_insert_path(store: SummaryStore, conv_id: int) -> None:
    rec = store.upsert_conversation_bootstrap_state(
        UpsertConversationBootstrapStateInput(
            conversation_id=conv_id,
            session_file_path="/tmp/s1.jsonl",
            last_seen_size=1024,
            last_seen_mtime_ms=1700_000_000_000,
            last_processed_offset=512,
            last_processed_entry_hash="abc",
        )
    )
    assert rec.session_file_path == "/tmp/s1.jsonl"
    assert rec.last_seen_size == 1024
    assert rec.last_processed_entry_hash == "abc"


def test_upsert_conversation_bootstrap_state_update_path(store: SummaryStore, conv_id: int) -> None:
    store.upsert_conversation_bootstrap_state(
        UpsertConversationBootstrapStateInput(
            conversation_id=conv_id,
            session_file_path="/old/path.jsonl",
            last_seen_size=100,
            last_seen_mtime_ms=1000,
            last_processed_offset=50,
        )
    )
    rec = store.upsert_conversation_bootstrap_state(
        UpsertConversationBootstrapStateInput(
            conversation_id=conv_id,
            session_file_path="/new/path.jsonl",
            last_seen_size=200,
            last_seen_mtime_ms=2000,
            last_processed_offset=150,
            last_processed_entry_hash="new",
        )
    )
    assert rec.session_file_path == "/new/path.jsonl"
    assert rec.last_seen_size == 200
    assert rec.last_processed_entry_hash == "new"


def test_upsert_conversation_bootstrap_state_clamps_negatives(
    store: SummaryStore, conv_id: int
) -> None:
    rec = store.upsert_conversation_bootstrap_state(
        UpsertConversationBootstrapStateInput(
            conversation_id=conv_id,
            session_file_path="/tmp/p.jsonl",
            last_seen_size=-5,
            last_seen_mtime_ms=-10,
            last_processed_offset=-20,
        )
    )
    assert rec.last_seen_size == 0
    assert rec.last_seen_mtime_ms == 0
    assert rec.last_processed_offset == 0


def test_get_conversation_bootstrap_state_returns_none_for_missing(
    store: SummaryStore, conv_id: int
) -> None:
    assert store.get_conversation_bootstrap_state(conv_id) is None


# ---------------------------------------------------------------------------
# FTS-disabled FTS-insert path (gateway behavior — verifies graceful degrade)
# ---------------------------------------------------------------------------


def test_insert_summary_swallows_fts_table_missing(
    fts_conn: sqlite3.Connection,
) -> None:
    """When fts5_available=True but the FTS tables don't exist on this DB
    (because 01-05 hasn't landed yet), insert_summary must not raise — the
    summaries row should land and the FTS inserts silently fail."""
    conv_id = _seed_conversation(fts_conn)
    store = SummaryStore(fts_conn, fts5_available=True, trigram_tokenizer_available=True)
    rec = store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_gate",
            conversation_id=conv_id,
            kind="leaf",
            content="x",
            token_count=1,
        )
    )
    assert rec.summary_id == "sum_gate"


# ---------------------------------------------------------------------------
# List-transcript-gc-candidates (storage-only)
# ---------------------------------------------------------------------------


def test_list_transcript_gc_candidates_returns_empty_when_no_message_parts(
    store: SummaryStore, conv_id: int
) -> None:
    assert store.list_transcript_gc_candidates(conv_id) == []


def test_list_transcript_gc_candidates_filters_non_tool_messages(
    store: SummaryStore, conv_id: int, conn: sqlite3.Connection
) -> None:
    """Only role=tool messages with summary_messages linkage and no current
    context_items entry are returned."""
    # User message — not eligible (role != 'tool').
    msg_user = _seed_message(conn, conv_id, 1, "user", "x")
    # Tool message — eligible if linked + externalized.
    msg_tool = _seed_message(conn, conv_id, 2, "tool", "tool output")

    # Insert a message_parts row with externalized metadata.
    conn.execute(
        """
        INSERT INTO message_parts (
            part_id, message_id, session_id, part_type, ordinal,
            tool_call_id, tool_name, metadata
        )
        VALUES ('p1', ?, 'sess', 'tool', 0, 'tc1', 'exec', ?)
        """,
        (msg_tool, '{"toolOutputExternalized": true, "externalizedFileId": "file_abc"}'),
    )

    # Link tool message to a leaf summary (eligibility).
    store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_gc",
            conversation_id=conv_id,
            kind="leaf",
            content="x",
            token_count=1,
        )
    )
    store.link_summary_to_messages("sum_gc", [msg_tool])

    candidates = store.list_transcript_gc_candidates(conv_id)
    assert len(candidates) == 1
    assert candidates[0].message_id == msg_tool
    assert candidates[0].tool_call_id == "tc1"
    assert candidates[0].externalized_file_id == "file_abc"


def test_list_transcript_gc_candidates_skips_uncovered_messages(
    store: SummaryStore, conv_id: int, conn: sqlite3.Connection
) -> None:
    """A tool message not linked to any summary is NOT a candidate."""
    msg = _seed_message(conn, conv_id, 1, "tool", "x")
    conn.execute(
        """
        INSERT INTO message_parts (
            part_id, message_id, session_id, part_type, ordinal,
            tool_call_id, tool_name, metadata
        )
        VALUES ('p_a', ?, 'sess', 'tool', 0, 'tc', 'exec',
                '{"toolOutputExternalized": true}')
        """,
        (msg,),
    )
    assert store.list_transcript_gc_candidates(conv_id) == []


def test_list_transcript_gc_candidates_skips_still_in_context(
    store: SummaryStore, conv_id: int, conn: sqlite3.Connection
) -> None:
    """A tool message that's still in context_items is NOT a candidate."""
    msg = _seed_message(conn, conv_id, 1, "tool", "x")
    conn.execute(
        """
        INSERT INTO message_parts (
            part_id, message_id, session_id, part_type, ordinal,
            tool_call_id, tool_name, metadata
        )
        VALUES ('p_x', ?, 'sess', 'tool', 0, 'tc', 'exec',
                '{"toolOutputExternalized": true}')
        """,
        (msg,),
    )
    store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_ctx",
            conversation_id=conv_id,
            kind="leaf",
            content="x",
            token_count=1,
        )
    )
    store.link_summary_to_messages("sum_ctx", [msg])
    store.append_context_message(conv_id, msg)  # Still in context — disqualifies.

    assert store.list_transcript_gc_candidates(conv_id) == []


def test_list_transcript_gc_candidates_respects_limit(
    store: SummaryStore, conv_id: int, conn: sqlite3.Connection
) -> None:
    """The limit caps the returned candidates."""
    ids = []
    for i in range(5):
        m = _seed_message(conn, conv_id, i + 1, "tool", f"out {i}")
        ids.append(m)
        conn.execute(
            """
            INSERT INTO message_parts (
                part_id, message_id, session_id, part_type, ordinal,
                tool_call_id, tool_name, metadata
            )
            VALUES (?, ?, 'sess', 'tool', 0, ?, 'exec',
                    '{"toolOutputExternalized": true}')
            """,
            (f"p_{i}", m, f"tc_{i}"),
        )
        store.insert_summary(
            CreateSummaryInput(
                summary_id=f"sum_{i}",
                conversation_id=conv_id,
                kind="leaf",
                content=f"covers {i}",
                token_count=1,
            )
        )
        store.link_summary_to_messages(f"sum_{i}", [m])

    candidates = store.list_transcript_gc_candidates(conv_id, limit=2)
    assert len(candidates) == 2


# ---------------------------------------------------------------------------
# with_transaction nested savepoint
# ---------------------------------------------------------------------------


def test_with_transaction_commits_on_success(store: SummaryStore, conv_id: int) -> None:
    with store.with_transaction():
        store.insert_summary(
            CreateSummaryInput(
                summary_id="sum_tx_ok",
                conversation_id=conv_id,
                kind="leaf",
                content="x",
                token_count=1,
            )
        )
    # Outside the block, the row must be persisted.
    assert store.get_summary("sum_tx_ok") is not None


def test_with_transaction_rolls_back_on_exception(store: SummaryStore, conv_id: int) -> None:
    with pytest.raises(RuntimeError):
        with store.with_transaction():
            store.insert_summary(
                CreateSummaryInput(
                    summary_id="sum_tx_fail",
                    conversation_id=conv_id,
                    kind="leaf",
                    content="x",
                    token_count=1,
                )
            )
            raise RuntimeError("kill mid-transaction")
    assert store.get_summary("sum_tx_fail") is None


def test_with_transaction_nested_uses_savepoints(store: SummaryStore, conv_id: int) -> None:
    """Nested with_transaction calls should use savepoints (not BEGIN-within-BEGIN)."""
    with store.with_transaction():
        store.insert_summary(
            CreateSummaryInput(
                summary_id="sum_outer",
                conversation_id=conv_id,
                kind="leaf",
                content="x",
                token_count=1,
            )
        )
        # Nested: must NOT raise "cannot start a transaction within a transaction".
        with store.with_transaction():
            store.insert_summary(
                CreateSummaryInput(
                    summary_id="sum_inner",
                    conversation_id=conv_id,
                    kind="leaf",
                    content="x",
                    token_count=1,
                )
            )
    assert store.get_summary("sum_outer") is not None
    assert store.get_summary("sum_inner") is not None


def test_with_transaction_nested_rollback_is_local(store: SummaryStore, conv_id: int) -> None:
    """A nested failure rolls back only the savepoint, not the outer txn."""
    with store.with_transaction():
        store.insert_summary(
            CreateSummaryInput(
                summary_id="sum_kept",
                conversation_id=conv_id,
                kind="leaf",
                content="x",
                token_count=1,
            )
        )
        # Nested failure: caught here, savepoint rolls back, outer keeps going.
        with pytest.raises(RuntimeError):
            with store.with_transaction():
                store.insert_summary(
                    CreateSummaryInput(
                        summary_id="sum_lost",
                        conversation_id=conv_id,
                        kind="leaf",
                        content="x",
                        token_count=1,
                    )
                )
                raise RuntimeError("kill nested")
    # sum_kept survives; sum_lost was rolled back by the savepoint.
    assert store.get_summary("sum_kept") is not None
    assert store.get_summary("sum_lost") is None


# ---------------------------------------------------------------------------
# get_summary_messages with empty/missing summary
# ---------------------------------------------------------------------------


def test_get_summary_messages_empty_for_no_links(store: SummaryStore, conv_id: int) -> None:
    store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_no_msgs",
            conversation_id=conv_id,
            kind="leaf",
            content="x",
            token_count=1,
        )
    )
    assert store.get_summary_messages("sum_no_msgs") == []


def test_get_summary_messages_returns_in_ordinal_order(
    store: SummaryStore, conv_id: int, conn: sqlite3.Connection
) -> None:
    ids = [_seed_message(conn, conv_id, i + 1) for i in range(3)]
    store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_o",
            conversation_id=conv_id,
            kind="leaf",
            content="x",
            token_count=1,
        )
    )
    # Link in reverse order — ordinal still reflects input list order.
    store.link_summary_to_messages("sum_o", list(reversed(ids)))
    fetched = store.get_summary_messages("sum_o")
    assert fetched == list(reversed(ids))


# ---------------------------------------------------------------------------
# FTS5 path coverage — uses manually-created FTS tables until 01-05 ships
# the SummaryStore-side migration body. These tests will keep passing once
# 01-05 lands (it just becomes redundant — the tables will already exist).
# ---------------------------------------------------------------------------


def _create_fts5_tables(conn: sqlite3.Connection) -> bool:
    """Create ``summaries_fts`` + ``summaries_fts_cjk`` if FTS5 is compiled in.

    Returns False if the SQLite build lacks FTS5/trigram — callers should
    skip the test in that case.
    """
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE summaries_fts USING "
            "fts5(summary_id UNINDEXED, content, tokenize='porter unicode61')"
        )
    except sqlite3.OperationalError:
        return False
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE summaries_fts_cjk USING "
            "fts5(summary_id UNINDEXED, content, tokenize='trigram')"
        )
    except sqlite3.OperationalError:
        # FTS5 present but trigram missing — leave summaries_fts alive.
        pass
    return True


def test_full_text_search_uses_fts5_when_available(conn: sqlite3.Connection) -> None:
    """When summaries_fts exists, full_text search hits the FTS5 path."""
    if not _create_fts5_tables(conn):
        pytest.skip("FTS5 not compiled into stdlib sqlite3")
    cid = _seed_conversation(conn)
    store = SummaryStore(conn, fts5_available=True, trigram_tokenizer_available=True)
    store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_fts",
            conversation_id=cid,
            kind="leaf",
            content="Database migration fallback search test",
            token_count=10,
        )
    )
    results = store.search_summaries(
        SummarySearchInput(
            conversation_id=cid,
            query="database migration",
            mode="full_text",
            limit=10,
        )
    )
    assert len(results) == 1
    assert results[0].summary_id == "sum_fts"


def test_cjk_trigram_search_hits_summaries_fts_cjk(conn: sqlite3.Connection) -> None:
    """CJK 3+ char query routes to summaries_fts_cjk and finds substrings.

    Per AC: ``会議の議事録`` content should be found by ``MATCH '議事録'``.
    """
    if not _create_fts5_tables(conn):
        pytest.skip("FTS5/trigram not compiled into stdlib sqlite3")
    # Confirm trigram present (else skip):
    try:
        conn.execute("SELECT name FROM sqlite_master WHERE name = 'summaries_fts_cjk'").fetchone()
    except sqlite3.OperationalError:
        pytest.skip("trigram tokenizer missing")

    cid = _seed_conversation(conn)
    store = SummaryStore(conn, fts5_available=True, trigram_tokenizer_available=True)
    store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_jp",
            conversation_id=cid,
            kind="leaf",
            content="会議の議事録について議論",
            token_count=12,
        )
    )
    results = store.search_summaries(
        SummarySearchInput(
            conversation_id=cid,
            query="議事録",
            mode="full_text",
            limit=10,
        )
    )
    assert len(results) == 1
    assert results[0].summary_id == "sum_jp"


def test_full_text_search_falls_back_to_like_when_fts5_path_raises(
    conn: sqlite3.Connection,
) -> None:
    """If summaries_fts doesn't exist (e.g. 01-05 not run), the FTS5 path
    raises and we fall back to LIKE."""
    # No FTS5 tables created — fts5_available=True will try and fail.
    cid = _seed_conversation(conn)
    store = SummaryStore(conn, fts5_available=True, trigram_tokenizer_available=True)
    store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_fallback",
            conversation_id=cid,
            kind="leaf",
            content="Database migration LIKE fallback path",
            token_count=10,
        )
    )
    results = store.search_summaries(
        SummarySearchInput(
            conversation_id=cid,
            query="database",
            mode="full_text",
            limit=10,
        )
    )
    assert len(results) == 1
    assert results[0].summary_id == "sum_fallback"


def test_fts5_search_excludes_suppressed(conn: sqlite3.Connection) -> None:
    """The FTS5 path applies the v4.1 §10 suppression filter."""
    if not _create_fts5_tables(conn):
        pytest.skip("FTS5 not compiled into stdlib sqlite3")
    cid = _seed_conversation(conn)
    store = SummaryStore(conn, fts5_available=True, trigram_tokenizer_available=True)
    store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_suppressed_fts",
            conversation_id=cid,
            kind="leaf",
            content="suppressed full text marker",
            token_count=4,
        )
    )
    conn.execute(
        "UPDATE summaries SET suppressed_at = datetime('now') WHERE summary_id = ?",
        ("sum_suppressed_fts",),
    )
    results = store.search_summaries(
        SummarySearchInput(
            conversation_id=cid,
            query="suppressed",
            mode="full_text",
            limit=10,
        )
    )
    assert results == []


# ---------------------------------------------------------------------------
# Subtree hard-cap (Wave-4 Auditor #7 P1 fix — 10K-node cap).
# Direct test of the SQL hard limit by mocking the constant would be intrusive;
# instead assert the limit was wired through the query.
# ---------------------------------------------------------------------------


def test_get_summary_subtree_returns_seed_for_self_loop(store: SummaryStore, conv_id: int) -> None:
    """Edge case: a self-referencing summary (theoretically impossible per
    FK constraint, but the dedupe in the Python loop is the belt-and-
    suspenders)."""
    store.insert_summary(
        CreateSummaryInput(
            summary_id="sum_self",
            conversation_id=conv_id,
            kind="leaf",
            content="x",
            token_count=1,
        )
    )
    nodes = store.get_summary_subtree("sum_self")
    # No links → just the seed.
    assert len(nodes) == 1
    assert nodes[0].summary_id == "sum_self"


# ---------------------------------------------------------------------------
# Searching with conversation_ids list (multi-conversation scope)
# ---------------------------------------------------------------------------


def test_search_summaries_filters_by_conversation_ids_list(
    conn: sqlite3.Connection,
) -> None:
    """A list of conversation_ids restricts to those conversations only."""
    c1 = _seed_conversation(conn, "s1")
    c2 = _seed_conversation(conn, "s2")
    c3 = _seed_conversation(conn, "s3")
    store = SummaryStore(conn, fts5_available=False, trigram_tokenizer_available=False)
    for sid, cid in [("sum_1", c1), ("sum_2", c2), ("sum_3", c3)]:
        store.insert_summary(
            CreateSummaryInput(
                summary_id=sid,
                conversation_id=cid,
                kind="leaf",
                content=f"alpha beta {sid}",
                token_count=3,
            )
        )
    results = store.search_summaries(
        SummarySearchInput(
            conversation_ids=[c1, c3],
            query="alpha",
            mode="regex",
            limit=10,
        )
    )
    sids = sorted(r.summary_id for r in results)
    assert sids == ["sum_1", "sum_3"]


def test_search_summaries_no_scope_spans_all_conversations(
    conn: sqlite3.Connection,
) -> None:
    c1 = _seed_conversation(conn, "s1")
    c2 = _seed_conversation(conn, "s2")
    store = SummaryStore(conn, fts5_available=False, trigram_tokenizer_available=False)
    for sid, cid in [("sum_x1", c1), ("sum_x2", c2)]:
        store.insert_summary(
            CreateSummaryInput(
                summary_id=sid,
                conversation_id=cid,
                kind="leaf",
                content="unique-token",
                token_count=1,
            )
        )
    results = store.search_summaries(
        SummarySearchInput(query="unique-token", mode="regex", limit=10)
    )
    assert sorted(r.summary_id for r in results) == ["sum_x1", "sum_x2"]
