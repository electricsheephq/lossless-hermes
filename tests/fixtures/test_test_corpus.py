"""Tests for the synthetic test-corpus fixture (issue 09-08).

Validates ``tests/fixtures/test_corpus.py`` — the Python port of
``lossless-claw/test/fixtures/v41-test-corpus.ts``:

* :func:`build_test_corpus` seeds the expected row counts.
* The corpus is deterministic — two builds produce byte-identical rows.
* Seeded ``summary_id`` values match
  :data:`tests.fixtures.eva_baseline_v2.CORPUS_SUMMARY_IDS` exactly,
  bidirectionally (the +52.5pp benchmark's ground-truth resolves
  against this corpus, so a drift in either file must fail fast).
* Suppressed leaves are excluded from the FTS index + carry a
  ``suppressed_at`` (parity with production FTS triggers).
* Parent/child links + entity mentions resolve.
* The ``test_corpus`` pytest fixture (``tests/conftest.py``) seeds the
  yielded connection.

These tests run in regular CI — no API keys, no live calls.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from tests.fixtures.eva_baseline_v2 import CORPUS_SUMMARY_IDS
from tests.fixtures.test_corpus import (
    BASE_DATE,
    FIXTURE_CONDENSED,
    FIXTURE_CONVERSATIONS,
    FIXTURE_ENTITIES,
    FIXTURE_LEAVES,
    build_test_corpus,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fresh_conn() -> sqlite3.Connection:
    """A bare in-memory connection — :func:`build_test_corpus` migrates it."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _table_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


# ---------------------------------------------------------------------------
# Row counts
# ---------------------------------------------------------------------------


class TestRowCounts:
    def test_metadata_counts_match_fixture_arrays(self) -> None:
        """The returned metadata counts match the fixture-array lengths."""
        conn = _fresh_conn()
        meta = build_test_corpus(conn)
        assert meta["leaf_count"] == len(FIXTURE_LEAVES)
        assert meta["condensed_count"] == len(FIXTURE_CONDENSED)
        assert meta["entity_count"] == len(FIXTURE_ENTITIES)
        assert meta["base_date"] == BASE_DATE

    def test_conversations_seeded(self) -> None:
        conn = _fresh_conn()
        build_test_corpus(conn)
        assert _table_count(conn, "conversations") == len(FIXTURE_CONVERSATIONS)

    def test_summaries_seeded_leaf_plus_condensed(self) -> None:
        """``summaries`` carries every leaf + every condensed row."""
        conn = _fresh_conn()
        meta = build_test_corpus(conn)
        assert _table_count(conn, "summaries") == (meta["leaf_count"] + meta["condensed_count"])

    def test_one_message_per_leaf(self) -> None:
        """The TS loop inserts exactly 1 user message per leaf."""
        conn = _fresh_conn()
        meta = build_test_corpus(conn)
        assert _table_count(conn, "messages") == meta["leaf_count"]
        roles = {r[0] for r in conn.execute("SELECT DISTINCT role FROM messages").fetchall()}
        assert roles == {"user"}

    def test_entity_mentions_seeded(self) -> None:
        """Total mentions == sum of every entity's ``mentioned_in`` length."""
        conn = _fresh_conn()
        build_test_corpus(conn)
        expected = sum(len(e.mentioned_in) for e in FIXTURE_ENTITIES)
        assert _table_count(conn, "lcm_entity_mentions") == expected

    def test_summary_parents_seeded(self) -> None:
        """Total parent links == sum of every condensed row's child count."""
        conn = _fresh_conn()
        build_test_corpus(conn)
        expected = sum(len(c.child_ids) for c in FIXTURE_CONDENSED)
        assert _table_count(conn, "summary_parents") == expected


# ---------------------------------------------------------------------------
# Ground-truth parity with eva-baseline-v2
# ---------------------------------------------------------------------------


class TestGroundTruthParity:
    """The benchmark's ground-truth (eva-baseline-v2) resolves here."""

    def test_seeded_ids_match_corpus_summary_ids_bidirectionally(self) -> None:
        """Every seeded ``summary_id`` is in ``CORPUS_SUMMARY_IDS`` and v.v.

        This is the load-bearing parity check: ``eva_baseline_v2.py``'s
        ``expected_summary_ids`` draw from ``CORPUS_SUMMARY_IDS``; if this
        corpus seeds a different ID set, the benchmark grades against
        ground-truth that doesn't exist. The check is bidirectional so a
        rename in *either* file is caught.
        """
        conn = _fresh_conn()
        meta = build_test_corpus(conn)
        seeded = set(meta["leaf_summary_ids"]) | set(meta["condensed_summary_ids"])
        assert seeded == set(CORPUS_SUMMARY_IDS), (
            f"corpus / eva-baseline-v2 drift — "
            f"in CORPUS_SUMMARY_IDS only: {sorted(set(CORPUS_SUMMARY_IDS) - seeded)}; "
            f"seeded only: {sorted(seeded - set(CORPUS_SUMMARY_IDS))}"
        )

    def test_seeded_ids_in_db_match_corpus_summary_ids(self) -> None:
        """The actual ``summaries`` rows (not just metadata) match."""
        conn = _fresh_conn()
        build_test_corpus(conn)
        rows = {r[0] for r in conn.execute("SELECT summary_id FROM summaries").fetchall()}
        assert rows == set(CORPUS_SUMMARY_IDS)


# ---------------------------------------------------------------------------
# Suppression
# ---------------------------------------------------------------------------


class TestSuppression:
    def test_suppressed_leaves_carry_suppressed_at(self) -> None:
        conn = _fresh_conn()
        meta = build_test_corpus(conn)
        n_suppressed = int(
            conn.execute(
                "SELECT COUNT(*) FROM summaries WHERE suppressed_at IS NOT NULL"
            ).fetchone()[0]
        )
        assert n_suppressed == meta["suppressed_count"]
        assert n_suppressed == sum(1 for leaf in FIXTURE_LEAVES if leaf.suppressed)

    def test_suppressed_leaves_excluded_from_fts(self) -> None:
        """Suppressed leaves get no ``summaries_fts`` row (FTS-trigger parity)."""
        conn = _fresh_conn()
        meta = build_test_corpus(conn)
        assert _table_count(conn, "summaries_fts") == (
            meta["leaf_count"] - meta["suppressed_count"] + meta["condensed_count"]
        )
        # The specific suppressed leaves are absent from the FTS index.
        for leaf in FIXTURE_LEAVES:
            if leaf.suppressed:
                hit = conn.execute(
                    "SELECT 1 FROM summaries_fts WHERE summary_id = ?",
                    (leaf.summary_id,),
                ).fetchone()
                assert hit is None, f"{leaf.summary_id} should not be FTS-indexed"

    def test_suppressed_message_rows_carry_suppressed_at(self) -> None:
        """A suppressed leaf's backing message is also suppressed."""
        conn = _fresh_conn()
        build_test_corpus(conn)
        n = int(
            conn.execute(
                "SELECT COUNT(*) FROM messages WHERE suppressed_at IS NOT NULL"
            ).fetchone()[0]
        )
        assert n == sum(1 for leaf in FIXTURE_LEAVES if leaf.suppressed)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_two_builds_produce_identical_summary_rows(self) -> None:
        """Re-running the builder yields byte-identical summary content.

        The TS source guarantees determinism via a fixed ``BASE_DATE``;
        this verifies the Python port keeps it for every column the
        benchmark reads (``created_at`` / ``latest_at`` are BASE_DATE-
        relative; the entity table's ``datetime('now')`` columns are
        deliberately excluded — see the :func:`build_test_corpus` note).
        """
        cols = (
            "summary_id, conversation_id, session_key, kind, depth, "
            "content, token_count, created_at, latest_at"
        )
        conn_a = _fresh_conn()
        build_test_corpus(conn_a)
        rows_a = conn_a.execute(f"SELECT {cols} FROM summaries ORDER BY summary_id").fetchall()

        conn_b = _fresh_conn()
        build_test_corpus(conn_b)
        rows_b = conn_b.execute(f"SELECT {cols} FROM summaries ORDER BY summary_id").fetchall()

        assert rows_a == rows_b

    def test_two_builds_produce_identical_message_rows(self) -> None:
        conn_a = _fresh_conn()
        build_test_corpus(conn_a)
        rows_a = conn_a.execute(
            "SELECT conversation_id, seq, role, content, token_count, "
            "created_at, identity_hash FROM messages ORDER BY message_id"
        ).fetchall()
        conn_b = _fresh_conn()
        build_test_corpus(conn_b)
        rows_b = conn_b.execute(
            "SELECT conversation_id, seq, role, content, token_count, "
            "created_at, identity_hash FROM messages ORDER BY message_id"
        ).fetchall()
        assert rows_a == rows_b


# ---------------------------------------------------------------------------
# Referential integrity
# ---------------------------------------------------------------------------


class TestReferentialIntegrity:
    def test_parent_links_reference_real_summaries(self) -> None:
        """Every ``summary_parents`` row references seeded summaries."""
        conn = _fresh_conn()
        build_test_corpus(conn)
        orphans = conn.execute(
            """
            SELECT sp.summary_id, sp.parent_summary_id
            FROM summary_parents sp
            LEFT JOIN summaries c ON c.summary_id = sp.summary_id
            LEFT JOIN summaries p ON p.summary_id = sp.parent_summary_id
            WHERE c.summary_id IS NULL OR p.summary_id IS NULL
            """
        ).fetchall()
        assert orphans == []

    def test_entity_mentions_reference_real_summaries(self) -> None:
        conn = _fresh_conn()
        build_test_corpus(conn)
        orphans = conn.execute(
            """
            SELECT m.mention_id
            FROM lcm_entity_mentions m
            LEFT JOIN summaries s ON s.summary_id = m.summary_id
            WHERE s.summary_id IS NULL
            """
        ).fetchall()
        assert orphans == []

    def test_idempotent_conversations_insert(self) -> None:
        """Conversations use INSERT OR IGNORE — a second build is safe.

        ``build_test_corpus`` runs the migration ladder (idempotent) and
        re-inserts conversations with ``INSERT OR IGNORE``; the leaf /
        summary inserts would collide on PK, so a true second build on
        the *same* connection raises. This test only asserts the
        conversation arm is OR-IGNORE-guarded as the TS source is.
        """
        conn = _fresh_conn()
        build_test_corpus(conn)
        # Re-insert just the conversations — must not raise.
        for conv in FIXTURE_CONVERSATIONS:
            conn.execute(
                "INSERT OR IGNORE INTO conversations "
                "(conversation_id, session_id, session_key, active, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    conv.conversation_id,
                    conv.session_id,
                    conv.session_key,
                    conv.active,
                    conv.created_at,
                ),
            )
        assert _table_count(conn, "conversations") == len(FIXTURE_CONVERSATIONS)


# ---------------------------------------------------------------------------
# The pytest fixture wired in tests/conftest.py
# ---------------------------------------------------------------------------


class TestConftestFixture:
    def test_test_corpus_fixture_seeds_connection(
        self, test_corpus: dict[str, Any], db_in_memory: sqlite3.Connection
    ) -> None:
        """The ``test_corpus`` conftest fixture seeds ``db_in_memory``."""
        assert test_corpus["leaf_count"] == len(FIXTURE_LEAVES)
        # The same connection the fixture seeded carries the rows.
        assert _table_count(db_in_memory, "summaries") == len(CORPUS_SUMMARY_IDS)

    def test_test_corpus_fixture_returns_plain_dict(self, test_corpus: dict[str, Any]) -> None:
        """The fixture returns a plain dict (TypedDict round-trips as dict)."""
        assert isinstance(test_corpus, dict)
        assert set(test_corpus.keys()) >= {
            "base_date",
            "leaf_count",
            "condensed_count",
            "entity_count",
            "suppressed_count",
            "leaf_summary_ids",
            "condensed_summary_ids",
        }


# ---------------------------------------------------------------------------
# Migration transaction-state contract
# ---------------------------------------------------------------------------


def test_build_requires_autocommit_connection() -> None:
    """``build_test_corpus`` on a connection mid-transaction raises.

    :func:`build_test_corpus` calls ``run_lcm_migrations`` which opens
    ``BEGIN EXCLUSIVE`` — that raises inside an open transaction. This
    documents the contract (callers pass an autocommit / idle connection).
    """
    conn = sqlite3.connect(":memory:")  # deferred-mode
    conn.execute("CREATE TABLE _t (x)")
    conn.execute("INSERT INTO _t VALUES (1)")  # opens a deferred transaction
    assert conn.in_transaction
    with pytest.raises(sqlite3.OperationalError):
        build_test_corpus(conn)
