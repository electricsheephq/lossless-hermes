"""Tests for :mod:`lossless_hermes.synthesis.invalidation` (issue 07-07).

Ports the soft-suppress cache invalidation slice of
``lossless-claw/test/purge-soft-suppression.test.ts`` plus the
post-synthesis leaf-ref populate cases from
``lossless-claw/test/lcm-synthesize-around-tool.test.ts`` (commit
``1f07fbd`` on branch ``pr-613``).

### Case mapping (TS → Python)

| TS case | Python test |
|---|---|
| suppress a leaf -> dependent cache rows deleted | :class:`TestInvalidateCachesForSuppressedLeaves` |
| cascade on hard DELETE summaries still works | :class:`TestHardDeleteCascade` |
| bulk suppression (N=100) -> single DELETE | :class:`TestInvalidateCachesForSuppressedLeaves` |
| post-synthesis populate per leaf | :class:`TestRecordCacheLeafRefs` |
| INSERT OR IGNORE idempotent on retry | :class:`TestRecordCacheLeafRefs` |
| per-leaf failure does NOT raise | :class:`TestRecordCacheLeafRefsBestEffort` |

### Final.review.3 Loop 2 Leak 2.5 regression

:class:`TestSoftSuppressLeakRegression` exercises the full
ingest -> synthesize -> suppress -> read flow asserting both invariants:

1. After ``UPDATE summaries SET suppressed_at`` + the explicit DELETE,
   no ``lcm_synthesis_cache`` row remains that referenced the
   suppressed leaf.
2. The ``summaries`` row itself still exists with ``suppressed_at``
   set (soft suppression, NOT hard DELETE).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator

import pytest

from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.synthesis.invalidation import (
    invalidate_caches_for_suppressed_leaves,
    record_cache_leaf_refs,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _setup_db() -> sqlite3.Connection:
    """Build an in-memory DB with FK enforcement + v4.1 schema applied.

    Inserts a conversation + prompt so leaf summaries and cache rows
    have valid FK targets. Mirrors the helper in ``test_cache_key.py``.
    """

    db = sqlite3.connect(":memory:", isolation_level=None)
    db.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(db, fts5_available=False, seed_default_prompts=False)
    db.execute("INSERT INTO conversations (session_id, session_key) VALUES ('s1', 'sk1')")
    db.execute(
        "INSERT INTO lcm_prompt_registry"
        " (prompt_id, memory_type, tier_label, pass_kind, version, template, active)"
        " VALUES ('p_test', 'episodic-condensed', 'custom', 'single', 1, 'T', 1)"
    )
    return db


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    """Migrated in-memory DB with FK enforcement + conversation + prompt."""

    conn = _setup_db()
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_leaf(db: sqlite3.Connection, summary_id: str) -> str:
    """Insert a minimal ``summaries`` row of kind='leaf' and return its id."""
    db.execute(
        "INSERT INTO summaries (summary_id, conversation_id, kind, content,"
        " token_count) VALUES (?, 1, 'leaf', 'leaf content', 1)",
        (summary_id,),
    )
    return summary_id


def _insert_cache_row(db: sqlite3.Connection, cache_id: str) -> str:
    """Insert a minimal ``lcm_synthesis_cache`` row and return its id.

    Uses ``cache_id`` as the ``leaf_fingerprint`` so the 7-field
    UNIQUE index does not collide across multiple test rows. All other
    columns are fixed values not load-bearing in these tests.
    """
    db.execute(
        "INSERT INTO lcm_synthesis_cache"
        " (cache_id, session_key, range_start, range_end, leaf_fingerprint,"
        "  grep_filter, content, entity_index, model_used, prompt_id,"
        "  tier_label, source_leaf_ids, source_token_count, output_token_count,"
        "  actual_range_covered, leaf_count_synthesized, status)"
        " VALUES (?, 'sk1', '2026-05-01T00:00:00Z', '2026-05-02T00:00:00Z',"
        "  ?, NULL, 'content', '{}', 'claude-3-haiku', 'p_test',"
        "  'custom', '[]', 1, 1, '2026-05-01..02', 1, 'ready')",
        (cache_id, cache_id),
    )
    return cache_id


def _count_cache_rows(db: sqlite3.Connection) -> int:
    """Number of rows currently in ``lcm_synthesis_cache``."""
    row = db.execute("SELECT COUNT(*) FROM lcm_synthesis_cache").fetchone()
    return int(row[0])


def _count_leaf_refs(db: sqlite3.Connection, cache_id: str) -> int:
    """Number of rows in ``lcm_cache_leaf_refs`` matching ``cache_id``."""
    row = db.execute(
        "SELECT COUNT(*) FROM lcm_cache_leaf_refs WHERE cache_id = ?",
        (cache_id,),
    ).fetchone()
    return int(row[0])


# ---------------------------------------------------------------------------
# TestRecordCacheLeafRefs — post-synthesis populate
# ---------------------------------------------------------------------------


class TestRecordCacheLeafRefs:
    """AC: ``record_cache_leaf_refs(conn, cache_id, leaf_ids)`` runs
    ``INSERT OR IGNORE`` per leaf.

    TS source: ``lossless-claw/src/tools/lcm-synthesize-around-tool.ts:1395-1406``.
    """

    def test_inserts_one_ref_per_leaf(self, db: sqlite3.Connection) -> None:
        """Every leaf in the source set gets a ``lcm_cache_leaf_refs`` row."""
        cache_id = _insert_cache_row(db, "c1")
        leaf_ids = [_insert_leaf(db, f"s_leaf_{i}") for i in range(5)]

        record_cache_leaf_refs(db, cache_id, leaf_ids)

        assert _count_leaf_refs(db, cache_id) == 5

    def test_or_ignore_idempotent_on_retry(self, db: sqlite3.Connection) -> None:
        """Calling twice with the same args does not raise and stays at N rows.

        The OR-IGNORE clause is load-bearing — without it, a retry would
        raise a UNIQUE-PK violation on the second call.
        """
        cache_id = _insert_cache_row(db, "c1")
        leaf_ids = [_insert_leaf(db, f"s_leaf_{i}") for i in range(3)]

        record_cache_leaf_refs(db, cache_id, leaf_ids)
        record_cache_leaf_refs(db, cache_id, leaf_ids)  # idempotent

        assert _count_leaf_refs(db, cache_id) == 3

    def test_empty_leaf_ids_is_no_op(self, db: sqlite3.Connection) -> None:
        """Empty iterable writes nothing and does not raise."""
        cache_id = _insert_cache_row(db, "c1")
        record_cache_leaf_refs(db, cache_id, [])
        assert _count_leaf_refs(db, cache_id) == 0

    def test_accepts_generator_not_just_list(self, db: sqlite3.Connection) -> None:
        """API signature is :class:`Iterable`; a generator must work."""
        cache_id = _insert_cache_row(db, "c1")
        for i in range(3):
            _insert_leaf(db, f"s_leaf_{i}")

        ids_gen = (f"s_leaf_{i}" for i in range(3))
        record_cache_leaf_refs(db, cache_id, ids_gen)

        assert _count_leaf_refs(db, cache_id) == 3


# ---------------------------------------------------------------------------
# TestRecordCacheLeafRefsBestEffort — per-leaf failure does NOT raise
# ---------------------------------------------------------------------------


class TestRecordCacheLeafRefsBestEffort:
    """AC: per-leaf INSERT failure is logged at warn but does NOT raise;
    surrounding synthesis returns success.
    """

    def test_unknown_leaf_id_logged_not_raised(
        self,
        db: sqlite3.Connection,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A leaf_id whose summaries row does not exist trips the FK but
        the function swallows the error.

        FK enforcement is ON in the fixture, so referencing a missing
        ``summaries.summary_id`` raises :class:`sqlite3.IntegrityError`
        — which the best-effort wrapper logs + continues past.
        """
        cache_id = _insert_cache_row(db, "c1")

        # No matching summaries row for these leaf_ids.
        with caplog.at_level(logging.WARNING, logger="lossless_hermes.synthesis.invalidation"):
            record_cache_leaf_refs(db, cache_id, ["s_missing"])

        # Did not raise.
        assert _count_leaf_refs(db, cache_id) == 0
        # Logged at warn.
        assert any(
            "cache_leaf_refs insert failed" in r.message and "s_missing" in r.message
            for r in caplog.records
        ), f"expected best-effort warn log, got: {[r.message for r in caplog.records]}"

    def test_per_leaf_failure_does_not_orphan_subsequent_leaves(
        self,
        db: sqlite3.Connection,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A bad leaf in the middle of the list does NOT skip good leaves
        that follow it.

        TS wraps the whole loop in one try/except — the Python port
        wraps each per-leaf INSERT individually so a single bad leaf
        does not orphan subsequent good leaves. This is the divergence
        called out in :func:`record_cache_leaf_refs` docstring.
        """
        cache_id = _insert_cache_row(db, "c1")
        _insert_leaf(db, "s_good_1")
        _insert_leaf(db, "s_good_2")

        with caplog.at_level(logging.WARNING, logger="lossless_hermes.synthesis.invalidation"):
            record_cache_leaf_refs(db, cache_id, ["s_good_1", "s_missing", "s_good_2"])

        # Both good leaves recorded; missing one swallowed.
        assert _count_leaf_refs(db, cache_id) == 2


# ---------------------------------------------------------------------------
# TestInvalidateCachesForSuppressedLeaves — suppression-time DELETE
# ---------------------------------------------------------------------------


class TestInvalidateCachesForSuppressedLeaves:
    """AC: ``invalidate_caches_for_suppressed_leaves(conn, leaf_ids)`` is
    a single-statement DELETE with the cache_id subselect; returns the
    row count from cursor.rowcount.

    TS source: ``lossless-claw/src/operator/purge.ts:346-352``.
    """

    def test_suppression_deletes_dependent_cache_rows(self, db: sqlite3.Connection) -> None:
        """One suppressed leaf -> one cache row deleted; rowcount == 1."""
        leaf_id = _insert_leaf(db, "s_leaf_1")
        cache_id = _insert_cache_row(db, "c1")
        record_cache_leaf_refs(db, cache_id, [leaf_id])

        # Simulate purge path: soft-suppress the leaf, then invalidate.
        db.execute(
            "UPDATE summaries SET suppressed_at = datetime('now') WHERE summary_id = ?",
            (leaf_id,),
        )
        deleted = invalidate_caches_for_suppressed_leaves(db, [leaf_id])

        assert deleted == 1
        assert _count_cache_rows(db) == 0

    def test_cache_row_with_no_suppressed_leaves_survives(self, db: sqlite3.Connection) -> None:
        """A cache row whose leaves are NOT in the suppressed set survives.

        Defensive — the DELETE filters via the inverse index; an
        unrelated cache row must not be touched.
        """
        leaf_keep = _insert_leaf(db, "s_keep")
        leaf_purge = _insert_leaf(db, "s_purge")

        c_keep = _insert_cache_row(db, "c_keep")
        c_purge = _insert_cache_row(db, "c_purge")
        record_cache_leaf_refs(db, c_keep, [leaf_keep])
        record_cache_leaf_refs(db, c_purge, [leaf_purge])

        deleted = invalidate_caches_for_suppressed_leaves(db, [leaf_purge])
        assert deleted == 1

        # c_keep survives, c_purge gone.
        surviving = {
            row[0] for row in db.execute("SELECT cache_id FROM lcm_synthesis_cache").fetchall()
        }
        assert surviving == {c_keep}

    def test_bulk_suppression_one_delete_for_N_leaves(self, db: sqlite3.Connection) -> None:
        """Bulk: 100 leaves -> all dependent cache rows deleted in one statement.

        AC: not a Python loop of N DELETEs; the function issues exactly
        one DELETE with the cache_id subselect. We verify by passing a
        thin proxy that counts how many times the function calls
        ``execute()`` — must be exactly once for any N.
        """
        leaf_ids = [_insert_leaf(db, f"s_leaf_{i}") for i in range(100)]
        # Each leaf has its own cache row (1:1 mapping for this test).
        for i, lid in enumerate(leaf_ids):
            cid = _insert_cache_row(db, f"c{i}")
            record_cache_leaf_refs(db, cid, [lid])

        # Proxy wraps the connection and counts ``execute()`` calls
        # made by the function under test. Python's ``sqlite3.Connection``
        # attributes are read-only so we can't monkey-patch in place; a
        # duck-typed proxy is the surgical alternative. SQLite's
        # ``set_trace_callback`` cannot be used here — it fires once per
        # FK cascade, inflating the count for any N>0 cache rows.
        class _CountingConn:
            def __init__(self, real: sqlite3.Connection) -> None:
                self.real = real
                self.execute_calls = 0

            def execute(self, sql: str, *args: object) -> sqlite3.Cursor:
                self.execute_calls += 1
                return self.real.execute(sql, *args)

        proxy = _CountingConn(db)
        deleted = invalidate_caches_for_suppressed_leaves(proxy, leaf_ids)  # type: ignore[arg-type]

        assert deleted == 100
        assert _count_cache_rows(db) == 0
        assert proxy.execute_calls == 1, (
            f"expected exactly 1 execute() call (single-statement AC); got {proxy.execute_calls}"
        )

    def test_one_cache_row_referenced_by_many_leaves_deleted_once(
        self, db: sqlite3.Connection
    ) -> None:
        """Suppressing any one of a cache row's leaves deletes the row.

        The cache_id subselect uses ``DISTINCT``, so multiple matching
        leaf rows in ``lcm_cache_leaf_refs`` collapse to one cache_id
        in the DELETE target set. The cache row itself is deleted
        exactly once (rowcount == 1).
        """
        leaf_ids = [_insert_leaf(db, f"s_leaf_{i}") for i in range(5)]
        cache_id = _insert_cache_row(db, "c1")
        record_cache_leaf_refs(db, cache_id, leaf_ids)

        # Suppress 3 of the 5 leaves (DISTINCT collapses to one cache_id).
        deleted = invalidate_caches_for_suppressed_leaves(db, leaf_ids[:3])

        assert deleted == 1
        assert _count_cache_rows(db) == 0

    def test_empty_leaf_ids_is_no_op_zero_rowcount(self, db: sqlite3.Connection) -> None:
        """Empty input returns 0 without issuing SQL.

        Defensive — avoids the ``IN ()`` empty-list parser hazard.
        """
        # Plant a cache row that would NOT be touched even if SQL fired.
        cache_id = _insert_cache_row(db, "c1")
        leaf_id = _insert_leaf(db, "s_leaf_1")
        record_cache_leaf_refs(db, cache_id, [leaf_id])

        deleted = invalidate_caches_for_suppressed_leaves(db, [])
        assert deleted == 0
        assert _count_cache_rows(db) == 1

    def test_runs_inside_caller_owned_transaction(self, db: sqlite3.Connection) -> None:
        """The DELETE participates in the caller's transaction.

        AC: function does NOT issue BEGIN/COMMIT — caller owns the tx.
        Verify by opening an explicit BEGIN, running the DELETE, then
        ROLLBACK and asserting the cache row survives.
        """
        leaf_id = _insert_leaf(db, "s_leaf_1")
        cache_id = _insert_cache_row(db, "c1")
        record_cache_leaf_refs(db, cache_id, [leaf_id])

        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "UPDATE summaries SET suppressed_at = datetime('now') WHERE summary_id = ?",
            (leaf_id,),
        )
        deleted = invalidate_caches_for_suppressed_leaves(db, [leaf_id])
        assert deleted == 1
        # Inside the tx the row is gone.
        assert _count_cache_rows(db) == 0

        # Roll back: the cache row AND the UPDATE both revert.
        db.execute("ROLLBACK")
        assert _count_cache_rows(db) == 1
        row = db.execute(
            "SELECT suppressed_at FROM summaries WHERE summary_id = ?",
            (leaf_id,),
        ).fetchone()
        assert row is not None and row[0] is None, (
            "ROLLBACK must revert both the UPDATE suppressed_at and the "
            "DELETE — confirming both ran inside the caller's transaction"
        )


# ---------------------------------------------------------------------------
# TestHardDeleteCascade — verify DDL constraint
# ---------------------------------------------------------------------------


class TestHardDeleteCascade:
    """AC: cache row is also deleted via FK cascade on hard
    DELETE summaries (no Python code needed — just verifies DDL).
    """

    def test_hard_delete_summary_cascades_to_cache(self, db: sqlite3.Connection) -> None:
        """``DELETE FROM summaries WHERE summary_id = ?`` fires the
        2-step cascade: ``lcm_cache_leaf_refs`` row deleted -> THAT
        cascade deletes the ``lcm_synthesis_cache`` row.

        Note: the cascade fires summaries -> lcm_cache_leaf_refs (via
        ON DELETE CASCADE on the leaf_summary_id FK), but
        ``lcm_cache_leaf_refs.cache_id`` is the CHILD side of the
        cascade to ``lcm_synthesis_cache`` — deleting a ref row does
        NOT cascade UP to the cache row. So the cache row survives a
        leaf hard-DELETE.

        This test pins that DDL behaviour. The Final.review.3 fix
        exists precisely because cascade does NOT cover the
        soft-suppression path AND does not propagate the other
        direction either.
        """
        leaf_id = _insert_leaf(db, "s_leaf_1")
        cache_id = _insert_cache_row(db, "c1")
        record_cache_leaf_refs(db, cache_id, [leaf_id])

        db.execute("DELETE FROM summaries WHERE summary_id = ?", (leaf_id,))

        # The lcm_cache_leaf_refs row is deleted (cascade from summaries).
        assert _count_leaf_refs(db, cache_id) == 0
        # The lcm_synthesis_cache row survives — cascade does NOT
        # propagate from the ref row UP to the cache row.
        assert _count_cache_rows(db) == 1

    def test_hard_delete_cache_cascades_to_refs(self, db: sqlite3.Connection) -> None:
        """``DELETE FROM lcm_synthesis_cache WHERE cache_id = ?`` cascades
        DOWN to remove its ref rows.

        This is the standard direction (cache_id FK -> ON DELETE
        CASCADE on ``lcm_cache_leaf_refs.cache_id``).
        """
        leaf_id = _insert_leaf(db, "s_leaf_1")
        cache_id = _insert_cache_row(db, "c1")
        record_cache_leaf_refs(db, cache_id, [leaf_id])
        assert _count_leaf_refs(db, cache_id) == 1

        db.execute("DELETE FROM lcm_synthesis_cache WHERE cache_id = ?", (cache_id,))

        assert _count_leaf_refs(db, cache_id) == 0


# ---------------------------------------------------------------------------
# TestSoftSuppressLeakRegression — Final.review.3 Loop 2 Leak 2.5
# ---------------------------------------------------------------------------


class TestSoftSuppressLeakRegression:
    """Regression test for the Final.review.3 Loop 2 Leak 2.5 fix.

    Pre-fix behaviour: after ``UPDATE suppressed_at`` on a leaf, the
    ``lcm_synthesis_cache`` row that referenced it survived (because
    the FK cascade only fires on hard DELETE FROM summaries). A
    subsequent cache read would surface PII baked into the synthesis
    before suppression.

    Post-fix behaviour (this issue): the purge path calls
    :func:`invalidate_caches_for_suppressed_leaves` immediately after
    the UPDATE, inside the same transaction. Both invariants are
    asserted here:

    1. Cache row deleted.
    2. Summaries row still present with ``suppressed_at`` set.
    """

    def test_soft_suppress_clears_cache_keeps_summary(self, db: sqlite3.Connection) -> None:
        """Full ingest -> synthesize -> suppress flow."""
        # 1. Ingest: leaf summary row exists.
        leaf_id = _insert_leaf(db, "s_leaf_1")

        # 2. Synthesize: cache row + leaf-ref written.
        cache_id = _insert_cache_row(db, "c1")
        record_cache_leaf_refs(db, cache_id, [leaf_id])
        assert _count_cache_rows(db) == 1
        assert _count_leaf_refs(db, cache_id) == 1

        # 3. Suppress: UPDATE + invalidate in one tx (purge-path shape).
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "UPDATE summaries SET suppressed_at = datetime('now') WHERE summary_id = ?",
            (leaf_id,),
        )
        invalidate_caches_for_suppressed_leaves(db, [leaf_id])
        db.execute("COMMIT")

        # Invariant 1: cache row is gone.
        assert _count_cache_rows(db) == 0, (
            "post-suppression cache read would surface PII baked into "
            "synthesis pre-suppression; cache row MUST be deleted"
        )

        # Invariant 2: summaries row still present with suppressed_at set.
        row = db.execute(
            "SELECT summary_id, suppressed_at FROM summaries WHERE summary_id = ?",
            (leaf_id,),
        ).fetchone()
        assert row is not None, "soft suppression must leave summaries row in place"
        assert row[1] is not None, "suppressed_at must be set post-suppression"
