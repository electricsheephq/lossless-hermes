"""Tests for :mod:`lossless_hermes.operator.purge` (issue 08-04).

Ports the TS test list from
``lossless-claw/test/operator-purge.test.ts`` plus the new tests
mandated by ``epics/08-cli-ops/08-04-purge-soft-suppression.md``
"Acceptance criteria":

* ``test_cascade_full_six_steps`` — runs ``run_purge`` against a seeded
  fixture (20 leaves, 5 condensed, 10 messages, 3 caches) and asserts
  each of the six cascade steps fired correctly.
* ``test_message_shared_with_unsuppressed_leaf_not_purged`` — step 5's
  ``NOT EXISTS`` invariant (Wave-7 P0-2 regression).
* ``test_allow_main_session_required`` — purging ``agent:main:thread:foo``
  without the flag returns an error (note: TS source gates only the
  literal ``agent:main:main`` session — see test docstring for the
  port's spec-fidelity decision).

The fixture path follows the TS test 1:1 — in-memory SQLite, full
migration ladder applied, seed conversations + summaries + (where
relevant) messages / context_items / summary_messages / summary_parents /
synthesis_cache rows.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone

import pytest

from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.operator.purge import (
    PurgeCriteria,
    PurgeError,
    PurgeOptions,
    preview_purge_affected,
    run_purge,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    """In-memory SQLite with the full migration ladder applied.

    Mirrors the TS ``setupDb`` helper at ``operator-purge.test.ts:6-12``.
    Also creates two conversations:

    * conv 1: session_key ``"sk1"`` — the default seeded session for
      most tests.
    * conv 2: session_key ``"agent:main:main"`` — used for the
      allow-main-session safeguard test.

    Both autocommit-mode (FK enforcement on, no outer transaction).
    """
    conn = sqlite3.connect(":memory:", isolation_level=None)  # autocommit
    conn.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(conn, fts5_available=False, seed_default_prompts=False)
    conn.execute("INSERT INTO conversations (session_id, session_key) VALUES ('s1', 'sk1')")
    conn.execute(
        "INSERT INTO conversations (session_id, session_key) VALUES ('s2', 'agent:main:main')"
    )
    try:
        yield conn
    finally:
        conn.close()


def _insert_leaf(
    db: sqlite3.Connection,
    summary_id: str,
    conversation_id: int = 1,
    content: str = "x",
    token_count: int = 100,
) -> None:
    """Insert a leaf summary linked to the named conversation's session_key.

    Helper port of TS ``insertLeaf`` at ``operator-purge.test.ts:14-25``.
    """
    db.execute(
        """
        INSERT INTO summaries
          (summary_id, conversation_id, kind, content, token_count, session_key)
          VALUES (?, ?, 'leaf', ?, ?,
                  (SELECT session_key FROM conversations
                     WHERE conversation_id = ?))
        """,
        (summary_id, conversation_id, content, token_count, conversation_id),
    )


def _insert_condensed(
    db: sqlite3.Connection,
    summary_id: str,
    conversation_id: int,
    parent_leaf_ids: list[str],
) -> None:
    """Insert a condensed summary with summary_parents rows for each parent.

    Helper port of TS ``insertCondensed`` at ``operator-purge.test.ts:27-38``.
    """
    db.execute(
        """
        INSERT INTO summaries
          (summary_id, conversation_id, kind, content, token_count, session_key)
          VALUES (?, ?, 'condensed', 'cond', 1,
                  (SELECT session_key FROM conversations
                     WHERE conversation_id = ?))
        """,
        (summary_id, conversation_id, conversation_id),
    )
    for i, parent_id in enumerate(parent_leaf_ids):
        db.execute(
            """
            INSERT INTO summary_parents (summary_id, parent_summary_id, ordinal)
              VALUES (?, ?, ?)
            """,
            (summary_id, parent_id, i),
        )


# ---------------------------------------------------------------------------
# Input validation — ports ``operator-purge — input validation`` describe
# ---------------------------------------------------------------------------


def test_missing_reason_raises_missing_reason(db: sqlite3.Connection) -> None:
    """Empty reason → ``PurgeError(missing_reason)``.

    Ports TS ``operator-purge.test.ts:41-50``.
    """
    with pytest.raises(PurgeError) as exc_info:
        run_purge(
            db,
            PurgeOptions(
                reason="",
                criteria=PurgeCriteria(summary_ids=["leaf_a"]),
            ),
        )
    assert exc_info.value.kind == "missing_reason"


def test_whitespace_only_reason_raises_missing_reason(db: sqlite3.Connection) -> None:
    """Reason that's only whitespace → ``PurgeError(missing_reason)``.

    The TS check is ``opts.reason.trim().length === 0`` (purge.ts:124).
    Python uses :py:meth:`str.strip` for parity — this test pins the
    whitespace-only branch.
    """
    with pytest.raises(PurgeError) as exc_info:
        run_purge(
            db,
            PurgeOptions(
                reason="   \t\n",
                criteria=PurgeCriteria(summary_ids=["leaf_a"]),
            ),
        )
    assert exc_info.value.kind == "missing_reason"


def test_no_criteria_raises_no_criteria(db: sqlite3.Connection) -> None:
    """Empty criteria → ``PurgeError(no_criteria)``.

    Ports TS ``operator-purge.test.ts:52-56``.
    """
    with pytest.raises(PurgeError) as exc_info:
        run_purge(db, PurgeOptions(reason="test"))
    assert exc_info.value.kind == "no_criteria"
    assert "at least one criterion" in str(exc_info.value)


def test_main_session_refused_without_flag(db: sqlite3.Connection) -> None:
    """``agent:main:main`` without ``allow_main_session`` → ``PurgeError``.

    Ports TS ``operator-purge.test.ts:58-67``.
    """
    with pytest.raises(PurgeError) as exc_info:
        run_purge(
            db,
            PurgeOptions(
                reason="trying to delete main",
                criteria=PurgeCriteria(session_key="agent:main:main"),
            ),
        )
    assert exc_info.value.kind == "main_session_blocked"
    assert "agent:main:main" in str(exc_info.value)


def test_main_session_allowed_with_flag(db: sqlite3.Connection) -> None:
    """``agent:main:main`` + ``allow_main_session=True`` → succeeds.

    Ports TS ``operator-purge.test.ts:69-79``. Seeds a leaf on the
    ``agent:main:main`` session (conv 2) and verifies the purge returns
    it as affected.
    """
    _insert_leaf(db, "leaf_main", conversation_id=2)
    result = run_purge(
        db,
        PurgeOptions(
            reason="explicit main session purge",
            criteria=PurgeCriteria(session_key="agent:main:main"),
            allow_main_session=True,
        ),
    )
    assert "leaf_main" in result.affected_leaf_ids


def test_allow_main_session_required(db: sqlite3.Connection) -> None:
    """Per issue 08-04 AC: ``--allow-main-session`` is required to purge
    a session key matching ``agent:main:main``.

    Note: the issue spec language says "``agent:main:thread:*``" but the
    TS source guards only the exact literal ``agent:main:main`` (the
    constant in purge.ts:136). This test pins TS-source behavior; if the
    glob-matching spec is later upgraded, this test will need to expand.
    """
    with pytest.raises(PurgeError) as exc_info:
        run_purge(
            db,
            PurgeOptions(
                reason="block this",
                criteria=PurgeCriteria(session_key="agent:main:main"),
            ),
        )
    assert exc_info.value.kind == "main_session_blocked"


# ---------------------------------------------------------------------------
# Soft mode — happy paths
# ---------------------------------------------------------------------------


def test_sets_suppressed_at_and_reason_on_matched_leaves(
    db: sqlite3.Connection,
) -> None:
    """Step 1 verification: ``suppressed_at`` + ``suppress_reason`` set.

    Ports TS ``operator-purge.test.ts:83-104``.
    """
    _insert_leaf(db, "leaf_a")
    _insert_leaf(db, "leaf_b")
    _insert_leaf(db, "leaf_other_session", conversation_id=2)

    result = run_purge(
        db,
        PurgeOptions(reason="test reason", criteria=PurgeCriteria(session_key="sk1")),
    )
    assert result.mode == "soft"
    assert sorted(result.affected_leaf_ids) == ["leaf_a", "leaf_b"]

    rows = db.execute(
        """
        SELECT summary_id, suppressed_at, suppress_reason
          FROM summaries
          WHERE summary_id IN ('leaf_a', 'leaf_b', 'leaf_other_session')
          ORDER BY summary_id
        """
    ).fetchall()
    # rows[0] = leaf_a, rows[1] = leaf_b, rows[2] = leaf_other_session
    assert rows[0][1] is not None  # leaf_a.suppressed_at
    assert rows[0][2] == "test reason"
    assert rows[1][1] is not None
    assert rows[1][2] == "test reason"
    # The other-session leaf must NOT be touched
    assert rows[2][1] is None
    assert rows[2][2] is None


def test_flags_condensed_with_contains_suppressed_leaves(
    db: sqlite3.Connection,
) -> None:
    """Step 2 verification: condensed.contains_suppressed_leaves flipped.

    Ports TS ``operator-purge.test.ts:106-125``.
    """
    _insert_leaf(db, "leaf_a")
    _insert_leaf(db, "leaf_b")
    _insert_leaf(db, "leaf_unrelated")
    _insert_condensed(db, "cond_x", conversation_id=1, parent_leaf_ids=["leaf_a", "leaf_b"])
    _insert_condensed(db, "cond_y", conversation_id=1, parent_leaf_ids=["leaf_unrelated"])

    run_purge(db, PurgeOptions(reason="test", criteria=PurgeCriteria(summary_ids=["leaf_a"])))

    rows = db.execute(
        """
        SELECT summary_id, contains_suppressed_leaves
          FROM summaries
          WHERE kind = 'condensed'
          ORDER BY summary_id
        """
    ).fetchall()
    # cond_x has leaf_a as parent → flagged
    assert rows[0] == ("cond_x", 1)
    # cond_y has only leaf_unrelated → not flagged
    assert rows[1] == ("cond_y", 0)


# ---------------------------------------------------------------------------
# Criteria flexibility
# ---------------------------------------------------------------------------


def test_range_purge_session_key_plus_min_token_count(
    db: sqlite3.Connection,
) -> None:
    """Range purge: only leaves with token_count >= threshold are touched.

    Ports TS ``operator-purge.test.ts:133-146``.
    """
    _insert_leaf(db, "leaf_small", token_count=50)
    _insert_leaf(db, "leaf_big", token_count=5000)
    _insert_leaf(db, "leaf_huge", token_count=30000)

    result = run_purge(
        db,
        PurgeOptions(
            reason="purge big leaves",
            criteria=PurgeCriteria(session_key="sk1", min_token_count=1000),
        ),
    )
    assert sorted(result.affected_leaf_ids) == ["leaf_big", "leaf_huge"]


def test_range_purge_with_since(db: sqlite3.Connection) -> None:
    """Range purge: ``since`` excludes older rows.

    Ports TS ``operator-purge.test.ts:148-162``.
    """
    _insert_leaf(db, "leaf_old")
    _insert_leaf(db, "leaf_new")
    db.execute("UPDATE summaries SET created_at = '2026-01-01' WHERE summary_id = 'leaf_old'")
    db.execute("UPDATE summaries SET created_at = '2026-05-01' WHERE summary_id = 'leaf_new'")

    result = run_purge(
        db,
        PurgeOptions(
            reason="purge recent",
            criteria=PurgeCriteria(
                session_key="sk1", since=datetime(2026, 3, 1, tzinfo=timezone.utc)
            ),
        ),
    )
    assert result.affected_leaf_ids == ["leaf_new"]


def test_explicit_summary_ids_filter_invalid(db: sqlite3.Connection) -> None:
    """Explicit summary_ids: invalid / already-suppressed IDs filtered out.

    Ports TS ``operator-purge.test.ts:164-177``. Mistakes (typos,
    condensed IDs, already-suppressed IDs) silently exclude — no
    half-execution.
    """
    _insert_leaf(db, "leaf_a")
    _insert_leaf(db, "leaf_already_suppressed")
    db.execute(
        "UPDATE summaries SET suppressed_at = '2026-01-01' "
        "WHERE summary_id = 'leaf_already_suppressed'"
    )

    result = run_purge(
        db,
        PurgeOptions(
            reason="test",
            criteria=PurgeCriteria(
                summary_ids=["leaf_a", "leaf_already_suppressed", "leaf_does_not_exist"]
            ),
        ),
    )
    assert result.affected_leaf_ids == ["leaf_a"]


def test_empty_match_returns_empty_result(db: sqlite3.Connection) -> None:
    """No leaves matching → empty result (not an error).

    Ports TS ``operator-purge.test.ts:181-189``.
    """
    result = run_purge(
        db,
        PurgeOptions(reason="nothing to purge", criteria=PurgeCriteria(session_key="sk1")),
    )
    assert result.affected_leaf_ids == []
    assert result.mode == "soft"


# ---------------------------------------------------------------------------
# Atomic transaction
# ---------------------------------------------------------------------------


def test_soft_purge_atomic_step1_and_step2_set_together(
    db: sqlite3.Connection,
) -> None:
    """Atomicity proxy: step 1 (suppress) + step 2 (flag condensed) both
    visible after run_purge — confirms BEGIN IMMEDIATE wraps both.

    Ports TS ``operator-purge.test.ts:193-211``. A real rollback scenario
    is hard to inject without monkeypatching SQL; this test pins the
    "both-or-neither" contract by checking both writes landed together.
    """
    _insert_leaf(db, "leaf_a")
    _insert_condensed(db, "cond_x", conversation_id=1, parent_leaf_ids=["leaf_a"])

    run_purge(db, PurgeOptions(reason="test", criteria=PurgeCriteria(summary_ids=["leaf_a"])))

    leaf_suppressed_at = db.execute(
        "SELECT suppressed_at FROM summaries WHERE summary_id = 'leaf_a'"
    ).fetchone()[0]
    cond_flag = db.execute(
        "SELECT contains_suppressed_leaves FROM summaries WHERE summary_id = 'cond_x'"
    ).fetchone()[0]
    assert leaf_suppressed_at is not None
    assert cond_flag == 1


# ---------------------------------------------------------------------------
# preview_purge_affected — Wave-2 BUG-2/BUG-3 regression coverage
# ---------------------------------------------------------------------------


def test_preview_matches_apply_count_for_range_purge(db: sqlite3.Connection) -> None:
    """Preview count == apply count for range purge.

    Ports TS ``operator-purge.test.ts:218-232``. Wave-2 BUG-2 regression.
    """
    _insert_leaf(db, "leaf_a")
    _insert_leaf(db, "leaf_b")
    _insert_leaf(db, "leaf_c")
    _insert_leaf(db, "leaf_already")
    db.execute(
        "UPDATE summaries SET suppressed_at = datetime('now') WHERE summary_id = 'leaf_already'"
    )

    criteria = PurgeCriteria(session_key="sk1")
    preview = preview_purge_affected(db, criteria)
    result = run_purge(db, PurgeOptions(reason="regression-test", criteria=criteria))
    assert preview == len(result.affected_leaf_ids)
    assert preview == 3


def test_preview_summary_ids_filters_non_leaf_and_suppressed(
    db: sqlite3.Connection,
) -> None:
    """Preview filters out condensed + already-suppressed + non-existent IDs.

    Ports TS ``operator-purge.test.ts:235-255``. Wave-2 BUG-3 regression.
    """
    _insert_leaf(db, "leaf_real")
    _insert_condensed(db, "cond_x", conversation_id=1, parent_leaf_ids=["leaf_real"])
    db.execute(
        """
        INSERT INTO summaries
          (summary_id, conversation_id, kind, content, token_count,
           session_key, suppressed_at)
          VALUES ('leaf_supp', 1, 'leaf', 'x', 1, 'sk1', datetime('now'))
        """
    )

    criteria = PurgeCriteria(summary_ids=["leaf_real", "cond_x", "leaf_supp", "ghost_id"])
    preview = preview_purge_affected(db, criteria)
    result = run_purge(db, PurgeOptions(reason="regression", criteria=criteria))
    assert preview == 1
    assert result.affected_leaf_ids == ["leaf_real"]


def test_preview_since_filter_matches_apply(db: sqlite3.Connection) -> None:
    """Preview reflects since-filter exactly like apply.

    Ports TS ``operator-purge.test.ts:257-274``.
    """
    _insert_leaf(db, "leaf_old")
    _insert_leaf(db, "leaf_new")
    db.execute(
        "UPDATE summaries SET created_at = '2026-01-01 00:00:00' WHERE summary_id = 'leaf_old'"
    )
    db.execute(
        "UPDATE summaries SET created_at = '2026-05-01 00:00:00' WHERE summary_id = 'leaf_new'"
    )

    criteria = PurgeCriteria(session_key="sk1", since=datetime(2026, 4, 1, tzinfo=timezone.utc))
    preview = preview_purge_affected(db, criteria)
    result = run_purge(db, PurgeOptions(reason="regression", criteria=criteria))
    assert preview == len(result.affected_leaf_ids)
    assert result.affected_leaf_ids == ["leaf_new"]


def test_preview_zero_when_no_match(db: sqlite3.Connection) -> None:
    """Preview returns 0 when no leaves match (clean negative).

    Ports TS ``operator-purge.test.ts:276-287``.
    """
    _insert_leaf(db, "leaf_a")
    preview = preview_purge_affected(db, PurgeCriteria(session_key="non-existent-session"))
    assert preview == 0


# ---------------------------------------------------------------------------
# Wave-7 P0-2 regression — shared message gating
# ---------------------------------------------------------------------------


def _insert_message(db: sqlite3.Connection, message_id: int, conv_id: int = 1) -> None:
    """Insert a message row with the given numeric ID."""
    db.execute(
        """
        INSERT INTO messages
          (message_id, conversation_id, seq, role, content, token_count)
          VALUES (?, ?, ?, 'user', 'shared msg', 5)
        """,
        (message_id, conv_id, message_id),  # use message_id as seq for uniqueness
    )


def test_message_shared_with_unsuppressed_leaf_not_purged(
    db: sqlite3.Connection,
) -> None:
    """Wave-7 P0-2: shared message stays un-suppressed when only ONE of
    its referencing leaves is purged.

    Ports TS ``operator-purge.test.ts:292-317``. Without the
    ``NOT EXISTS`` gate, this scenario silently suppresses the message
    and orphans leaf_b's content.
    """
    _insert_leaf(db, "leaf_a")
    _insert_leaf(db, "leaf_b")
    _insert_message(db, message_id=1)
    db.execute(
        "INSERT INTO summary_messages (summary_id, message_id, ordinal) VALUES ('leaf_a', 1, 0)"
    )
    db.execute(
        "INSERT INTO summary_messages (summary_id, message_id, ordinal) VALUES ('leaf_b', 1, 0)"
    )

    run_purge(db, PurgeOptions(reason="test", criteria=PurgeCriteria(summary_ids=["leaf_a"])))

    msg_suppressed_at = db.execute(
        "SELECT suppressed_at FROM messages WHERE message_id = 1"
    ).fetchone()[0]
    # The message must NOT be suppressed — leaf_b still references it.
    assert msg_suppressed_at is None

    # Sanity: leaf_a IS suppressed.
    leaf_a_suppressed_at = db.execute(
        "SELECT suppressed_at FROM summaries WHERE summary_id = 'leaf_a'"
    ).fetchone()[0]
    assert leaf_a_suppressed_at is not None


def test_message_shared_all_referencing_leaves_purged_is_suppressed(
    db: sqlite3.Connection,
) -> None:
    """Wave-7 P0-2: shared message IS suppressed when ALL referencing
    leaves are purged in the same call.

    Ports TS ``operator-purge.test.ts:319-335``.
    """
    _insert_leaf(db, "leaf_a")
    _insert_leaf(db, "leaf_b")
    _insert_message(db, message_id=1)
    db.execute(
        "INSERT INTO summary_messages (summary_id, message_id, ordinal) VALUES ('leaf_a', 1, 0)"
    )
    db.execute(
        "INSERT INTO summary_messages (summary_id, message_id, ordinal) VALUES ('leaf_b', 1, 0)"
    )

    run_purge(
        db,
        PurgeOptions(reason="test", criteria=PurgeCriteria(summary_ids=["leaf_a", "leaf_b"])),
    )

    msg_suppressed_at = db.execute(
        "SELECT suppressed_at FROM messages WHERE message_id = 1"
    ).fetchone()[0]
    assert msg_suppressed_at is not None


# ---------------------------------------------------------------------------
# Cascade — full 6-step fixture test (issue 08-04 AC)
# ---------------------------------------------------------------------------


def test_cascade_full_six_steps(db: sqlite3.Connection) -> None:
    """Issue 08-04 AC: run ``run_purge`` against a seeded fixture and
    assert each of the six cascade steps fired correctly.

    Fixture shape:

    * 20 leaves on sk1 (10 to be purged, 10 to remain)
    * 5 condensed: 2 with at-least-one-purged-parent (must be flagged),
      2 with only un-purged parents (must NOT be flagged), 1 with mixed
      (must be flagged).
    * 10 messages, 6 linked via summary_messages to purged leaves
      (3 exclusively, 3 shared with un-purged leaves).
    * 3 synthesis caches, 2 referencing purged leaves (must be deleted),
      1 referencing only un-purged leaves (must remain).
    * Context items: 1 summary-type pointing at a purged leaf + 1
      message-type pointing at a purged-only message + 1 sentinel
      message-type pointing at an unrelated message (must remain).

    Then asserts:

    1. All 10 targeted leaves have ``suppressed_at`` set.
    2. All 3 condensed with a purged parent have
       ``contains_suppressed_leaves=1``; the other 2 stay at 0.
    3. The summary-type context_item is deleted; sentinel preserved.
    4. The message-type context_item for the purged-only message is
       deleted; sentinel preserved.
    5. The 3 exclusively-purged messages have ``suppressed_at`` set;
       the 3 shared-with-un-purged messages do NOT.
    6. The 2 caches referencing purged leaves are deleted; the
       sentinel cache remains. Cache leaf refs cascade-deleted by FK.
    """
    # --- Seed leaves ---
    purged_ids = [f"leaf_p_{i}" for i in range(10)]
    kept_ids = [f"leaf_k_{i}" for i in range(10)]
    for sid in purged_ids:
        _insert_leaf(db, sid)
    for sid in kept_ids:
        _insert_leaf(db, sid)

    # --- Seed condensed: cond_only_purged (parents in purged), cond_mixed
    # (one parent purged, one kept), cond_only_kept x2, cond_other_purged.
    _insert_condensed(db, "cond_only_purged_a", 1, ["leaf_p_0", "leaf_p_1"])
    _insert_condensed(db, "cond_only_purged_b", 1, ["leaf_p_2"])
    _insert_condensed(db, "cond_mixed", 1, ["leaf_p_3", "leaf_k_0"])
    _insert_condensed(db, "cond_only_kept_a", 1, ["leaf_k_1", "leaf_k_2"])
    _insert_condensed(db, "cond_only_kept_b", 1, ["leaf_k_3"])

    # --- Seed messages: msgs 1-3 exclusive to purged, msgs 4-6 shared,
    # msgs 7-10 unrelated.
    for mid in range(1, 11):
        _insert_message(db, message_id=mid)
    # Exclusive: msg 1 → leaf_p_0; msg 2 → leaf_p_1; msg 3 → leaf_p_2
    for mid, sid in [(1, "leaf_p_0"), (2, "leaf_p_1"), (3, "leaf_p_2")]:
        db.execute(
            "INSERT INTO summary_messages (summary_id, message_id, ordinal) VALUES (?, ?, 0)",
            (sid, mid),
        )
    # Shared: msg 4 → both leaf_p_3 and leaf_k_0
    # msg 5 → leaf_p_4 and leaf_k_1
    # msg 6 → leaf_p_5 and leaf_k_2
    for mid, sid_p, sid_k in [
        (4, "leaf_p_3", "leaf_k_0"),
        (5, "leaf_p_4", "leaf_k_1"),
        (6, "leaf_p_5", "leaf_k_2"),
    ]:
        db.execute(
            "INSERT INTO summary_messages (summary_id, message_id, ordinal) VALUES (?, ?, 0)",
            (sid_p, mid),
        )
        db.execute(
            "INSERT INTO summary_messages (summary_id, message_id, ordinal) VALUES (?, ?, 1)",
            (sid_k, mid),
        )

    # --- Seed context_items ---
    # Summary item pointing at leaf_p_0
    db.execute(
        """
        INSERT INTO context_items
          (conversation_id, ordinal, item_type, summary_id)
          VALUES (1, 0, 'summary', 'leaf_p_0')
        """
    )
    # Message item pointing at the exclusive-purged msg 1
    db.execute(
        """
        INSERT INTO context_items
          (conversation_id, ordinal, item_type, message_id)
          VALUES (1, 1, 'message', 1)
        """
    )
    # Sentinel: summary item pointing at leaf_k_0 (should NOT be deleted)
    db.execute(
        """
        INSERT INTO context_items
          (conversation_id, ordinal, item_type, summary_id)
          VALUES (1, 2, 'summary', 'leaf_k_0')
        """
    )
    # Sentinel: message item pointing at unrelated msg 10
    db.execute(
        """
        INSERT INTO context_items
          (conversation_id, ordinal, item_type, message_id)
          VALUES (1, 3, 'message', 10)
        """
    )

    # --- Seed synthesis caches ---
    # We need an active prompt_id in lcm_prompt_registry first.
    db.execute(
        """
        INSERT INTO lcm_prompt_registry
          (prompt_id, memory_type, tier_label, pass_kind, version, template)
          VALUES ('prompt-test', 'episodic-leaf', 'weekly', 'single', 1, 'x')
        """
    )
    # cache_a refs leaf_p_0 → must be deleted
    # cache_b refs leaf_p_5 → must be deleted
    # cache_c refs leaf_k_0 only → must remain
    for cache_id, leaf_refs in [
        ("cache_a", ["leaf_p_0"]),
        ("cache_b", ["leaf_p_5"]),
        ("cache_c", ["leaf_k_0"]),
    ]:
        # leaf_fingerprint must be unique per (session_key, range_start,
        # range_end, leaf_fingerprint, grep_filter, tier_label, prompt_id)
        # because of the lookup-unique index. We use the cache_id itself
        # as the fingerprint to keep each row distinct.
        db.execute(
            """
            INSERT INTO lcm_synthesis_cache (
              cache_id, session_key, range_start, range_end, leaf_fingerprint,
              content, entity_index, model_used, prompt_id, tier_label,
              source_leaf_ids, built_at, source_token_count, output_token_count,
              actual_range_covered, leaf_count_synthesized
            )
            VALUES (
              ?, 'sk1', '2026-01-01', '2026-12-31', ?,
              'c', '{}', 'claude', 'prompt-test', 'weekly',
              ?, datetime('now'), 1, 1, '2026-01-01/2026-12-31', ?
            )
            """,
            (cache_id, f"fp-{cache_id}", ",".join(leaf_refs), len(leaf_refs)),
        )
        for leaf_id in leaf_refs:
            db.execute(
                "INSERT INTO lcm_cache_leaf_refs (cache_id, leaf_summary_id) VALUES (?, ?)",
                (cache_id, leaf_id),
            )

    # --- Run purge ---
    result = run_purge(
        db,
        PurgeOptions(
            reason="full-six-steps fixture",
            criteria=PurgeCriteria(summary_ids=purged_ids),
        ),
    )
    assert sorted(result.affected_leaf_ids) == sorted(purged_ids)

    # --- Step 1 assertion: all 10 targeted leaves are suppressed --------
    suppressed_count = db.execute(
        "SELECT COUNT(*) FROM summaries WHERE summary_id IN ({}) AND suppressed_at IS NOT NULL".format(
            ",".join(["?"] * len(purged_ids))
        ),
        tuple(purged_ids),
    ).fetchone()[0]
    assert suppressed_count == 10
    # Kept leaves: none should be suppressed.
    kept_suppressed_count = db.execute(
        "SELECT COUNT(*) FROM summaries WHERE summary_id IN ({}) AND suppressed_at IS NOT NULL".format(
            ",".join(["?"] * len(kept_ids))
        ),
        tuple(kept_ids),
    ).fetchone()[0]
    assert kept_suppressed_count == 0

    # --- Step 2 assertion: 3 condensed flagged, 2 not -------------------
    flagged_rows = db.execute(
        "SELECT summary_id FROM summaries "
        "WHERE kind = 'condensed' AND contains_suppressed_leaves = 1 "
        "ORDER BY summary_id"
    ).fetchall()
    flagged_ids = {r[0] for r in flagged_rows}
    assert flagged_ids == {"cond_only_purged_a", "cond_only_purged_b", "cond_mixed"}

    # --- Step 3 assertion: summary-type context_item for purged leaf is gone
    summary_item_count = db.execute(
        "SELECT COUNT(*) FROM context_items WHERE item_type = 'summary' AND summary_id = 'leaf_p_0'"
    ).fetchone()[0]
    assert summary_item_count == 0
    # Sentinel summary item for leaf_k_0 must remain.
    sentinel_summary_count = db.execute(
        "SELECT COUNT(*) FROM context_items WHERE item_type = 'summary' AND summary_id = 'leaf_k_0'"
    ).fetchone()[0]
    assert sentinel_summary_count == 1

    # --- Step 4 assertion: message-type context_item for purged msg is gone
    purged_msg_item_count = db.execute(
        "SELECT COUNT(*) FROM context_items WHERE item_type = 'message' AND message_id = 1"
    ).fetchone()[0]
    assert purged_msg_item_count == 0
    # Sentinel unrelated msg 10 must remain.
    sentinel_msg_count = db.execute(
        "SELECT COUNT(*) FROM context_items WHERE item_type = 'message' AND message_id = 10"
    ).fetchone()[0]
    assert sentinel_msg_count == 1

    # --- Step 5 assertion: exclusive messages suppressed, shared messages NOT
    exclusive_msgs_suppressed = db.execute(
        "SELECT COUNT(*) FROM messages WHERE message_id IN (1, 2, 3) AND suppressed_at IS NOT NULL"
    ).fetchone()[0]
    assert exclusive_msgs_suppressed == 3
    shared_msgs_suppressed = db.execute(
        "SELECT COUNT(*) FROM messages WHERE message_id IN (4, 5, 6) AND suppressed_at IS NOT NULL"
    ).fetchone()[0]
    assert shared_msgs_suppressed == 0

    # --- Step 6 assertion: cache_a, cache_b deleted; cache_c remains ----
    remaining_caches = {
        r[0]
        for r in db.execute("SELECT cache_id FROM lcm_synthesis_cache ORDER BY cache_id").fetchall()
    }
    assert remaining_caches == {"cache_c"}


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


def test_result_mode_always_soft(db: sqlite3.Connection) -> None:
    """``PurgeResult.mode`` is always ``"soft"`` (no immediate path).

    Per doctor-ops.md §"Prune cascade" line 270: the hard-delete drainer
    was removed in the first-principles pass; soft mode is the only mode.
    """
    _insert_leaf(db, "leaf_a")
    result = run_purge(db, PurgeOptions(reason="r", criteria=PurgeCriteria(summary_ids=["leaf_a"])))
    assert result.mode == "soft"


def test_purge_session_id_format(db: sqlite3.Connection) -> None:
    """``purge_session_id`` follows ``purge_<ms-epoch>_<6-hex>``.

    The TS format is ``purge_${Date.now()}_${randomSuffix()}`` (purge.ts:143).
    Python uses ``int(time.time() * 1000)`` for the ms-epoch + 6 hex chars.
    """
    _insert_leaf(db, "leaf_a")
    result = run_purge(db, PurgeOptions(reason="r", criteria=PurgeCriteria(summary_ids=["leaf_a"])))
    parts = result.purge_session_id.split("_")
    assert parts[0] == "purge"
    assert parts[1].isdigit()
    assert len(parts[2]) == 6
    assert all(c in "0123456789abcdef" for c in parts[2])
