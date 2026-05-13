"""Tests for :mod:`lossless_hermes.prune` — the 6-step soft-suppression cascade.

Validates the full cascade end-to-end (per the issue spec acceptance
criteria): insert summary + messages, soft-prune the session, verify
``suppressed_at`` flips + ``contains_suppressed_leaves`` flag + cache
invalidation + non-shared-message gating.

Per the issue spec §"Validate":
* Test the 6-step prune cascade end-to-end (insert summary+messages,
  soft-prune session, verify suppressed_at flipped + cache invalidated).

Per the inline acceptance criteria (in spec §"acceptance criteria — prune"):
* ``parseDuration`` handles ``'30d'``, ``'12h'``, ``'7d3h'``, ``'30m'`` and
  raises ``ValueError`` on bad input — N/A here; this module ports the
  *soft-suppression* surface from purge.ts, not the hard-delete prune.ts
  which carries the duration parser. The duration parser ports separately
  when the data-retention prune.ts surface lands.

Each test exercises one or more of the 6 cascade steps so a regression
in any individual step shows up as a localised failure (not a global
"the prune is broken").
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest

from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.prune import (
    PruneCriteria,
    PruneError,
    preview_soft_prune_affected,
    soft_prune_session,
    soft_prune_summary_ids,
)


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def migrated_db() -> Iterator[sqlite3.Connection]:
    """In-memory SQLite with the core schema + foreign keys ON.

    Adds the two v4.1 cache tables manually since #01-06 hasn't landed.
    The cascade's step 6 is gated by ``_has_table``; we want the test
    suite to exercise step 6 explicitly, so we seed the tables here.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(conn)
    # Minimal v4.1 cache tables (full DDL lives in #01-06). We only need
    # the columns the cascade reads / writes — ``cache_id`` / ``leaf_summary_id``.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lcm_synthesis_cache (
          cache_id TEXT NOT NULL PRIMARY KEY,
          content TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lcm_cache_leaf_refs (
          cache_id TEXT NOT NULL REFERENCES lcm_synthesis_cache(cache_id) ON DELETE CASCADE,
          leaf_summary_id TEXT NOT NULL REFERENCES summaries(summary_id) ON DELETE CASCADE,
          PRIMARY KEY (cache_id, leaf_summary_id)
        )
        """
    )
    try:
        yield conn
    finally:
        conn.close()


def _insert_conversation(
    conn: sqlite3.Connection,
    *,
    session_id: str = "sess-1",
    session_key: str = "key-1",
) -> int:
    cur = conn.execute(
        "INSERT INTO conversations (session_id, session_key) VALUES (?, ?)",
        (session_id, session_key),
    )
    return int(cur.lastrowid or 0)


def _insert_message(
    conn: sqlite3.Connection,
    *,
    conversation_id: int,
    seq: int,
    content: str = "hello",
    role: str = "user",
) -> int:
    cur = conn.execute(
        "INSERT INTO messages "
        "(conversation_id, seq, role, content, token_count) "
        "VALUES (?, ?, ?, ?, ?)",
        (conversation_id, seq, role, content, 5),
    )
    return int(cur.lastrowid or 0)


def _insert_summary(
    conn: sqlite3.Connection,
    *,
    summary_id: str,
    conversation_id: int,
    kind: str = "leaf",
    depth: int = 0,
    content: str = "summary text",
    token_count: int = 10,
) -> None:
    conn.execute(
        "INSERT INTO summaries "
        "(summary_id, conversation_id, kind, depth, content, token_count) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (summary_id, conversation_id, kind, depth, content, token_count),
    )


def _link_summary_to_message(
    conn: sqlite3.Connection,
    *,
    summary_id: str,
    message_id: int,
    ordinal: int = 0,
) -> None:
    conn.execute(
        "INSERT INTO summary_messages (summary_id, message_id, ordinal) VALUES (?, ?, ?)",
        (summary_id, message_id, ordinal),
    )


def _link_condensed_to_leaf(
    conn: sqlite3.Connection,
    *,
    condensed_summary_id: str,
    parent_leaf_id: str,
    ordinal: int = 0,
) -> None:
    """Insert summary_parents (condensed -> leaf)."""
    conn.execute(
        "INSERT INTO summary_parents (summary_id, parent_summary_id, ordinal) VALUES (?, ?, ?)",
        (condensed_summary_id, parent_leaf_id, ordinal),
    )


def _insert_context_item_summary(
    conn: sqlite3.Connection,
    *,
    conversation_id: int,
    ordinal: int,
    summary_id: str,
) -> None:
    conn.execute(
        "INSERT INTO context_items "
        "(conversation_id, ordinal, item_type, summary_id) "
        "VALUES (?, ?, 'summary', ?)",
        (conversation_id, ordinal, summary_id),
    )


def _insert_context_item_message(
    conn: sqlite3.Connection,
    *,
    conversation_id: int,
    ordinal: int,
    message_id: int,
) -> None:
    conn.execute(
        "INSERT INTO context_items "
        "(conversation_id, ordinal, item_type, message_id) "
        "VALUES (?, ?, 'message', ?)",
        (conversation_id, ordinal, message_id),
    )


def _insert_cache_row(
    conn: sqlite3.Connection,
    *,
    cache_id: str,
    leaf_summary_id: str,
) -> None:
    conn.execute(
        "INSERT INTO lcm_synthesis_cache (cache_id, content) VALUES (?, ?)",
        (cache_id, "cached content"),
    )
    conn.execute(
        "INSERT INTO lcm_cache_leaf_refs (cache_id, leaf_summary_id) VALUES (?, ?)",
        (cache_id, leaf_summary_id),
    )


# ---------------------------------------------------------------------------
# Validation paths
# ---------------------------------------------------------------------------


def test_empty_reason_raises_prune_error(migrated_db: sqlite3.Connection) -> None:
    """A missing or whitespace-only reason is rejected."""
    with pytest.raises(PruneError) as exc_info:
        soft_prune_summary_ids(migrated_db, ["sum_X"], reason="")
    assert exc_info.value.kind == "missing_reason"

    with pytest.raises(PruneError) as exc_info:
        soft_prune_summary_ids(migrated_db, ["sum_X"], reason="   ")
    assert exc_info.value.kind == "missing_reason"


def test_no_criteria_rejected_in_preview(migrated_db: sqlite3.Connection) -> None:
    """An empty :class:`PruneCriteria` is rejected by preview."""
    with pytest.raises(PruneError) as exc_info:
        preview_soft_prune_affected(migrated_db, PruneCriteria())
    assert exc_info.value.kind == "no_criteria"


def test_empty_session_key_rejected(migrated_db: sqlite3.Connection) -> None:
    """``soft_prune_session`` rejects an empty session_key."""
    with pytest.raises(PruneError) as exc_info:
        soft_prune_session(migrated_db, "", reason="cleanup")
    assert exc_info.value.kind == "no_criteria"


def test_main_session_requires_allow_main_session(
    migrated_db: sqlite3.Connection,
) -> None:
    """``agent:main:main`` is rejected without explicit override."""
    with pytest.raises(PruneError) as exc_info:
        soft_prune_session(migrated_db, "agent:main:main", reason="cleanup")
    assert exc_info.value.kind == "main_session_blocked"


def test_main_session_allowed_with_override(
    migrated_db: sqlite3.Connection,
) -> None:
    """``allow_main_session=True`` lets the operator purge ``agent:main:main``."""
    _insert_conversation(migrated_db, session_id="main", session_key="agent:main:main")
    # Empty selector still succeeds because there are no matching leaves.
    result = soft_prune_session(
        migrated_db,
        "agent:main:main",
        reason="explicit operator action",
        allow_main_session=True,
    )
    assert result.affected_leaf_ids == ()


# ---------------------------------------------------------------------------
# 6-step cascade — end-to-end
# ---------------------------------------------------------------------------


def test_six_step_cascade_end_to_end(migrated_db: sqlite3.Connection) -> None:
    """Insert summary+messages, soft-prune session, verify all 6 cascade steps.

    Builds a realistic fixture:
    * 2 messages in conversation A.
    * 1 leaf summary (links to both messages).
    * 1 condensed summary (parents the leaf).
    * 2 context_items (one summary, one message).
    * 1 synthesis cache entry referencing the leaf.

    Then soft-prunes the session and asserts:
    * step 1: ``summaries.suppressed_at`` set on the leaf + ``suppress_reason``.
    * step 2: condensed.``contains_suppressed_leaves`` = 1.
    * step 3: context_items for the suppressed summary deleted.
    * step 4: context_items for the underlying messages deleted.
    * step 5: ``messages.suppressed_at`` set (no other leaves reference them).
    * step 6: synthesis_cache row deleted.
    """
    conv_id = _insert_conversation(migrated_db, session_id="sess-A", session_key="key-A")
    m1 = _insert_message(migrated_db, conversation_id=conv_id, seq=0, content="m1")
    m2 = _insert_message(migrated_db, conversation_id=conv_id, seq=1, content="m2")
    _insert_summary(migrated_db, summary_id="leaf_1", conversation_id=conv_id, kind="leaf")
    _insert_summary(migrated_db, summary_id="cond_1", conversation_id=conv_id, kind="condensed")
    _link_summary_to_message(migrated_db, summary_id="leaf_1", message_id=m1)
    _link_summary_to_message(migrated_db, summary_id="leaf_1", message_id=m2, ordinal=1)
    _link_condensed_to_leaf(migrated_db, condensed_summary_id="cond_1", parent_leaf_id="leaf_1")
    _insert_context_item_summary(
        migrated_db, conversation_id=conv_id, ordinal=0, summary_id="leaf_1"
    )
    _insert_context_item_message(migrated_db, conversation_id=conv_id, ordinal=1, message_id=m1)
    _insert_cache_row(migrated_db, cache_id="cache_X", leaf_summary_id="leaf_1")

    # ── soft-prune ──
    result = soft_prune_session(migrated_db, "key-A", reason="confidentiality")

    assert result.affected_leaf_ids == ("leaf_1",)
    assert result.mode == "soft"
    assert result.prune_session_id.startswith("prune_")
    assert result.counts == {
        "summaries_suppressed": 1,
        "condensed_flagged": 1,
        "context_items_summary_deleted": 1,
        "context_items_message_deleted": 1,
        "messages_suppressed": 2,
        "synthesis_cache_invalidated": 1,
    }

    # ── Step 1 verification: leaf is suppressed ──
    row = migrated_db.execute(
        "SELECT suppressed_at, suppress_reason FROM summaries WHERE summary_id = ?",
        ("leaf_1",),
    ).fetchone()
    assert row[0] is not None
    assert row[1] == "confidentiality"

    # ── Step 2 verification: condensed flagged ──
    row = migrated_db.execute(
        "SELECT contains_suppressed_leaves FROM summaries WHERE summary_id = ?",
        ("cond_1",),
    ).fetchone()
    assert row[0] == 1

    # ── Step 3 verification: context_items for suppressed summary gone ──
    row = migrated_db.execute(
        "SELECT COUNT(*) FROM context_items "
        "WHERE conversation_id = ? AND item_type = 'summary' AND summary_id = ?",
        (conv_id, "leaf_1"),
    ).fetchone()
    assert row[0] == 0

    # ── Step 4 verification: context_items for the underlying messages gone ──
    row = migrated_db.execute(
        "SELECT COUNT(*) FROM context_items WHERE conversation_id = ? AND item_type = 'message'",
        (conv_id,),
    ).fetchone()
    assert row[0] == 0

    # ── Step 5 verification: messages suppressed (no other referencing leaf) ──
    rows = migrated_db.execute(
        "SELECT message_id, suppressed_at FROM messages WHERE conversation_id = ? ORDER BY seq",
        (conv_id,),
    ).fetchall()
    assert len(rows) == 2
    for _msg_id, suppressed_at in rows:
        assert suppressed_at is not None, "message should be suppressed"

    # ── Step 6 verification: synthesis cache row invalidated ──
    row = migrated_db.execute(
        "SELECT COUNT(*) FROM lcm_synthesis_cache WHERE cache_id = ?",
        ("cache_X",),
    ).fetchone()
    assert row[0] == 0


def test_step_5_gate_preserves_shared_messages(
    migrated_db: sqlite3.Connection,
) -> None:
    """Step 5 NOT EXISTS gate (Wave-7 P0-2 fix): shared messages survive.

    When two leaves reference the same message and only one is purged,
    the message must NOT be suppressed — otherwise the non-purged leaf's
    assemble path breaks.
    """
    conv_id = _insert_conversation(migrated_db, session_id="sess-shared")
    m_shared = _insert_message(migrated_db, conversation_id=conv_id, seq=0, content="shared")
    _insert_summary(migrated_db, summary_id="leaf_purged", conversation_id=conv_id, kind="leaf")
    _insert_summary(migrated_db, summary_id="leaf_retained", conversation_id=conv_id, kind="leaf")
    _link_summary_to_message(migrated_db, summary_id="leaf_purged", message_id=m_shared)
    _link_summary_to_message(migrated_db, summary_id="leaf_retained", message_id=m_shared)

    result = soft_prune_summary_ids(migrated_db, ["leaf_purged"], reason="shared-msg-test")
    assert result.counts["messages_suppressed"] == 0, (
        "message shared with a non-purged leaf must NOT be suppressed "
        "(Wave-7 Auditor #14 P0-2 gate)"
    )

    # leaf_purged is suppressed; leaf_retained is intact; message is intact.
    row = migrated_db.execute(
        "SELECT suppressed_at FROM summaries WHERE summary_id = ?",
        ("leaf_purged",),
    ).fetchone()
    assert row[0] is not None
    row = migrated_db.execute(
        "SELECT suppressed_at FROM summaries WHERE summary_id = ?",
        ("leaf_retained",),
    ).fetchone()
    assert row[0] is None
    row = migrated_db.execute(
        "SELECT suppressed_at FROM messages WHERE message_id = ?",
        (m_shared,),
    ).fetchone()
    assert row[0] is None


def test_step_5_suppresses_message_when_all_referencing_leaves_purged(
    migrated_db: sqlite3.Connection,
) -> None:
    """When EVERY referencing leaf is in the purge set, the message IS suppressed.

    Inverse of the gate test: purging BOTH leaves that reference the
    shared message correctly suppresses the message.
    """
    conv_id = _insert_conversation(migrated_db, session_id="sess-both")
    m_shared = _insert_message(migrated_db, conversation_id=conv_id, seq=0, content="shared")
    _insert_summary(migrated_db, summary_id="leaf_A", conversation_id=conv_id, kind="leaf")
    _insert_summary(migrated_db, summary_id="leaf_B", conversation_id=conv_id, kind="leaf")
    _link_summary_to_message(migrated_db, summary_id="leaf_A", message_id=m_shared)
    _link_summary_to_message(migrated_db, summary_id="leaf_B", message_id=m_shared)

    result = soft_prune_summary_ids(migrated_db, ["leaf_A", "leaf_B"], reason="both-purged")
    assert result.counts["messages_suppressed"] == 1

    row = migrated_db.execute(
        "SELECT suppressed_at FROM messages WHERE message_id = ?", (m_shared,)
    ).fetchone()
    assert row[0] is not None


# ---------------------------------------------------------------------------
# Resolve paths — criteria filtering
# ---------------------------------------------------------------------------


def test_already_suppressed_leaves_excluded(
    migrated_db: sqlite3.Connection,
) -> None:
    """Already-suppressed leaves are silently dropped from the resolve set."""
    conv_id = _insert_conversation(migrated_db, session_id="sess-presup")
    _insert_summary(migrated_db, summary_id="leaf_X", conversation_id=conv_id)
    migrated_db.execute(
        "UPDATE summaries SET suppressed_at = datetime('now') WHERE summary_id = ?",
        ("leaf_X",),
    )

    result = soft_prune_summary_ids(migrated_db, ["leaf_X"], reason="should-be-noop")
    assert result.affected_leaf_ids == ()
    assert result.counts["summaries_suppressed"] == 0


def test_non_leaf_summaries_excluded(migrated_db: sqlite3.Connection) -> None:
    """Condensed summaries supplied explicitly are silently dropped."""
    conv_id = _insert_conversation(migrated_db)
    _insert_summary(migrated_db, summary_id="cond_X", conversation_id=conv_id, kind="condensed")

    result = soft_prune_summary_ids(migrated_db, ["cond_X"], reason="no-op")
    assert result.affected_leaf_ids == ()


def test_session_key_filter_scopes_resolve(
    migrated_db: sqlite3.Connection,
) -> None:
    """``soft_prune_session`` only affects leaves under ``session_key``."""
    conv_a = _insert_conversation(migrated_db, session_id="sess-A", session_key="key-A")
    conv_b = _insert_conversation(migrated_db, session_id="sess-B", session_key="key-B")
    _insert_summary(migrated_db, summary_id="leaf_a", conversation_id=conv_a)
    _insert_summary(migrated_db, summary_id="leaf_b", conversation_id=conv_b)

    result = soft_prune_session(migrated_db, "key-A", reason="scoped")
    assert result.affected_leaf_ids == ("leaf_a",)

    # leaf_b is unaffected.
    row = migrated_db.execute(
        "SELECT suppressed_at FROM summaries WHERE summary_id = ?", ("leaf_b",)
    ).fetchone()
    assert row[0] is None


def test_since_before_filters_apply(migrated_db: sqlite3.Connection) -> None:
    """``since`` / ``before`` filter on ``created_at``."""
    conv_id = _insert_conversation(migrated_db)
    now = datetime.now(timezone.utc)
    old_iso = (now - timedelta(days=30)).isoformat()
    recent_iso = (now - timedelta(days=1)).isoformat()
    migrated_db.execute(
        "INSERT INTO summaries (summary_id, conversation_id, kind, "
        "content, token_count, created_at) "
        "VALUES (?, ?, 'leaf', 'old', 5, ?)",
        ("leaf_old", conv_id, old_iso),
    )
    migrated_db.execute(
        "INSERT INTO summaries (summary_id, conversation_id, kind, "
        "content, token_count, created_at) "
        "VALUES (?, ?, 'leaf', 'recent', 5, ?)",
        ("leaf_recent", conv_id, recent_iso),
    )

    # Purge only the old leaf via before=now-15d.
    cutoff = now - timedelta(days=15)
    result = soft_prune_session(migrated_db, "key-1", reason="old-only", before=cutoff)
    assert result.affected_leaf_ids == ("leaf_old",)


def test_min_token_count_filter_applies(migrated_db: sqlite3.Connection) -> None:
    """``min_token_count`` filters by ``token_count >= min``."""
    conv_id = _insert_conversation(migrated_db)
    _insert_summary(
        migrated_db,
        summary_id="leaf_small",
        conversation_id=conv_id,
        token_count=5,
    )
    _insert_summary(
        migrated_db,
        summary_id="leaf_large",
        conversation_id=conv_id,
        token_count=100,
    )

    result = soft_prune_session(migrated_db, "key-1", reason="big-only", min_token_count=50)
    assert result.affected_leaf_ids == ("leaf_large",)


# ---------------------------------------------------------------------------
# Preview vs apply parity (Wave-2 Auditor #6 BUG-2/3)
# ---------------------------------------------------------------------------


def test_preview_matches_apply_count(migrated_db: sqlite3.Connection) -> None:
    """``preview_soft_prune_affected`` count == apply ``affected_leaf_ids`` length."""
    conv_id = _insert_conversation(migrated_db, session_key="key-shared")
    for i in range(3):
        _insert_summary(migrated_db, summary_id=f"leaf_{i}", conversation_id=conv_id)

    criteria = PruneCriteria(session_key="key-shared")
    preview_count = preview_soft_prune_affected(migrated_db, criteria)

    result = soft_prune_session(migrated_db, "key-shared", reason="parity")
    assert preview_count == len(result.affected_leaf_ids) == 3


def test_preview_is_read_only(migrated_db: sqlite3.Connection) -> None:
    """``preview`` does not modify any row.

    Snapshots ``suppressed_at`` before + after the preview; they must match.
    """
    conv_id = _insert_conversation(migrated_db, session_key="key-readonly")
    _insert_summary(migrated_db, summary_id="leaf_p", conversation_id=conv_id)

    before = migrated_db.execute(
        "SELECT suppressed_at FROM summaries WHERE summary_id = ?", ("leaf_p",)
    ).fetchone()

    preview_soft_prune_affected(migrated_db, PruneCriteria(session_key="key-readonly"))

    after = migrated_db.execute(
        "SELECT suppressed_at FROM summaries WHERE summary_id = ?", ("leaf_p",)
    ).fetchone()
    assert before == after


# ---------------------------------------------------------------------------
# Atomicity — Wave-8 fix
# ---------------------------------------------------------------------------


def test_atomic_resolve_and_cascade_in_one_transaction(
    migrated_db: sqlite3.Connection,
) -> None:
    """The 6-step cascade runs inside a single transaction.

    A failure mid-cascade should roll back the whole thing — exercised
    here by wrapping the connection in a proxy that raises on the
    step-3 DELETE.
    """
    conv_id = _insert_conversation(migrated_db)
    _insert_summary(migrated_db, summary_id="leaf_atomic", conversation_id=conv_id)

    class _FailingConnProxy:
        """Wraps a sqlite3.Connection and raises on the step-3 DELETE.

        Forwards every other call to the underlying connection so prune's
        cascade reaches the targeted statement intact.
        """

        def __init__(self, real: sqlite3.Connection) -> None:
            self._real = real

        def execute(self, sql: str, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            # Steps in order: BEGIN, resolve SELECT, step-1 UPDATE,
            # step-2 UPDATE, step-3 DELETE (target).
            if "DELETE FROM context_items" in sql and "summary_id IN" in sql:
                raise sqlite3.OperationalError("simulated mid-cascade failure")
            return self._real.execute(sql, *args, **kwargs)

        @property
        def in_transaction(self) -> bool:
            return self._real.in_transaction

        def commit(self) -> None:
            self._real.commit()

        def rollback(self) -> None:
            self._real.rollback()

    proxy = _FailingConnProxy(migrated_db)
    with pytest.raises(sqlite3.OperationalError, match="simulated mid-cascade"):
        soft_prune_summary_ids(
            proxy,  # type: ignore[arg-type]
            ["leaf_atomic"],
            reason="will-fail",
        )

    # After rollback, the leaf must be un-suppressed (the step-1 UPDATE
    # was rolled back).
    row = migrated_db.execute(
        "SELECT suppressed_at FROM summaries WHERE summary_id = ?",
        ("leaf_atomic",),
    ).fetchone()
    assert row[0] is None, (
        "step-1 UPDATE must roll back when a later step fails — "
        "Wave-8 atomic-resolve-and-cascade invariant"
    )


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_resolve_returns_empty_result(migrated_db: sqlite3.Connection) -> None:
    """Resolving to zero leaves returns an empty result without touching DB."""
    result = soft_prune_session(migrated_db, "no-such-key", reason="empty")
    assert result.affected_leaf_ids == ()
    assert result.counts["summaries_suppressed"] == 0


def test_cache_invalidation_skipped_when_tables_missing() -> None:
    """Step 6 is a no-op when v4.1 cache tables don't exist.

    Verifies the ``_has_table`` gate so this module is callable on a
    #01-04-only DB (cache tables land in #01-06).
    """
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(conn)
    # Deliberately DO NOT create lcm_synthesis_cache / lcm_cache_leaf_refs.

    conv_id = _insert_conversation(conn, session_id="sess-no-cache")
    _insert_summary(conn, summary_id="leaf_no_cache", conversation_id=conv_id)

    result = soft_prune_summary_ids(conn, ["leaf_no_cache"], reason="step-6-noop")
    assert result.counts["synthesis_cache_invalidated"] == 0
    # Cascade still completed — the leaf is suppressed.
    row = conn.execute(
        "SELECT suppressed_at FROM summaries WHERE summary_id = ?",
        ("leaf_no_cache",),
    ).fetchone()
    assert row[0] is not None
    conn.close()
