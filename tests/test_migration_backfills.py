"""Tests for the versioned-backfill section of :mod:`lossless_hermes.db.migration`.

Covers the acceptance criteria from ``epics/01-storage/01-15-versioned-backfills.md``:

* :data:`VERSIONED_BACKFILL_STEPS` declares the 3 backfills at
  ``algorithm_version=1``.
* :func:`_has_completed_versioned_backfill` returns False on a fresh DB
  and True after one :func:`_mark_versioned_backfill_complete` call.
* **Idempotency**: running :func:`run_lcm_migrations` twice on a fresh
  DB produces the same 3 ledger rows; the second pass does not re-invoke
  the backfill bodies (verified by a monkey-patched callable counter).
* **Algorithm-version bump**: manually setting one ledger row to
  ``algorithm_version=0`` re-runs the matching backfill and upserts the
  row to version 1.
* **Pre-existing data**: depths + metadata are computed correctly on a
  4-level pyramid summary fixture; tool-call extraction follows the
  documented key-precedence chains.
* **Identity-hash rehash**: a NULL identity_hash on a legacy row gets
  populated with the spike-003 byte-identical SHA-256 digest.
* **Fork-side ``lcm_rollups`` no-op**: when the table is absent (upstream
  default), the helper is a no-op; when present with a session_key
  column, it gets populated.

References:

* :mod:`lossless_hermes.db.migration` — implementation under test.
* ``epics/01-storage/01-15-versioned-backfills.md`` — issue spec.
* ``docs/spike-results/003-identity-hash.md`` — the byte-identical
  SHA-256 fixture.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Iterator

import pytest

from lossless_hermes.db.migration import (
    VERSIONED_BACKFILL_STEPS,
    _backfill_conversation_session_keys,
    _backfill_fork_rollups_session_keys,
    _backfill_message_identity_hashes,
    _backfill_summary_depths,
    _backfill_summary_metadata,
    _backfill_summary_session_keys,
    _backfill_tool_call_columns,
    _has_completed_versioned_backfill,
    _mark_versioned_backfill_complete,
    _run_versioned_backfills,
    run_lcm_migrations,
)
from lossless_hermes.store.message_identity import build_message_identity_hash


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_db() -> Iterator[sqlite3.Connection]:
    """An in-memory DB with FK enforcement, no migrations applied."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def migrated_db(fresh_db: sqlite3.Connection) -> sqlite3.Connection:
    """A DB after a full migration pass (no FTS5)."""
    run_lcm_migrations(fresh_db, fts5_available=False)
    return fresh_db


# ---------------------------------------------------------------------------
# AC: ledger constant + helper invariants
# ---------------------------------------------------------------------------


def test_versioned_backfill_steps_constant() -> None:
    """:data:`VERSIONED_BACKFILL_STEPS` declares the 3 steps at version 1."""
    assert VERSIONED_BACKFILL_STEPS == {
        "backfillSummaryDepths": 1,
        "backfillSummaryMetadata": 1,
        "backfillToolCallColumns": 1,
    }


def test_has_completed_versioned_backfill_false_on_fresh_db(
    fresh_db: sqlite3.Connection,
) -> None:
    """On a freshly-created ledger table, no completion rows exist."""
    fresh_db.execute(
        "CREATE TABLE lcm_migration_state ("
        "  step_name TEXT NOT NULL, "
        "  algorithm_version INTEGER NOT NULL, "
        "  completed_at TEXT NOT NULL DEFAULT (datetime('now')), "
        "  PRIMARY KEY (step_name, algorithm_version)"
        ")"
    )
    assert _has_completed_versioned_backfill(fresh_db, "backfillSummaryDepths", 1) is False


def test_mark_versioned_backfill_complete_upserts(
    fresh_db: sqlite3.Connection,
) -> None:
    """Marking completion inserts a row; re-marking refreshes ``completed_at``."""
    fresh_db.execute(
        "CREATE TABLE lcm_migration_state ("
        "  step_name TEXT NOT NULL, "
        "  algorithm_version INTEGER NOT NULL, "
        "  completed_at TEXT NOT NULL DEFAULT (datetime('now')), "
        "  PRIMARY KEY (step_name, algorithm_version)"
        ")"
    )
    _mark_versioned_backfill_complete(fresh_db, "backfillSummaryDepths", 1)
    assert _has_completed_versioned_backfill(fresh_db, "backfillSummaryDepths", 1) is True
    rows = fresh_db.execute(
        "SELECT step_name, algorithm_version FROM lcm_migration_state"
    ).fetchall()
    assert rows == [("backfillSummaryDepths", 1)]

    # Re-marking is harmless (PK conflict → DO UPDATE).
    _mark_versioned_backfill_complete(fresh_db, "backfillSummaryDepths", 1)
    rows = fresh_db.execute(
        "SELECT step_name, algorithm_version FROM lcm_migration_state"
    ).fetchall()
    assert rows == [("backfillSummaryDepths", 1)]


# ---------------------------------------------------------------------------
# AC: idempotency — run twice, no duplicate ledger rows, no re-invocation
# ---------------------------------------------------------------------------


def test_run_migration_twice_records_each_step_once(
    fresh_db: sqlite3.Connection,
) -> None:
    """Running :func:`run_lcm_migrations` twice produces exactly 3 ledger rows."""
    run_lcm_migrations(fresh_db, fts5_available=False)
    run_lcm_migrations(fresh_db, fts5_available=False)
    rows = fresh_db.execute(
        "SELECT step_name, algorithm_version FROM lcm_migration_state ORDER BY step_name"
    ).fetchall()
    assert rows == [
        ("backfillSummaryDepths", 1),
        ("backfillSummaryMetadata", 1),
        ("backfillToolCallColumns", 1),
    ]


def test_second_pass_does_not_re_invoke_backfill_bodies(
    fresh_db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The skip-if-complete guard short-circuits before the backfill body runs.

    First pass: 1 call to each of the 3 backfills.
    Second pass: 0 additional calls (ledger row already at the latest version).
    """
    from lossless_hermes.db import migration as migration_module

    call_counts: dict[str, int] = {
        "_backfill_summary_depths": 0,
        "_backfill_summary_metadata": 0,
        "_backfill_tool_call_columns": 0,
    }

    real_depths = migration_module._backfill_summary_depths
    real_metadata = migration_module._backfill_summary_metadata
    real_toolcalls = migration_module._backfill_tool_call_columns

    def counted_depths(db: sqlite3.Connection) -> None:
        call_counts["_backfill_summary_depths"] += 1
        real_depths(db)

    def counted_metadata(db: sqlite3.Connection) -> None:
        call_counts["_backfill_summary_metadata"] += 1
        real_metadata(db)

    def counted_toolcalls(db: sqlite3.Connection) -> None:
        call_counts["_backfill_tool_call_columns"] += 1
        real_toolcalls(db)

    monkeypatch.setattr(migration_module, "_backfill_summary_depths", counted_depths)
    monkeypatch.setattr(migration_module, "_backfill_summary_metadata", counted_metadata)
    monkeypatch.setattr(migration_module, "_backfill_tool_call_columns", counted_toolcalls)

    run_lcm_migrations(fresh_db, fts5_available=False)
    assert call_counts == {
        "_backfill_summary_depths": 1,
        "_backfill_summary_metadata": 1,
        "_backfill_tool_call_columns": 1,
    }

    # Second pass: ledger entries already exist; bodies should NOT re-run.
    run_lcm_migrations(fresh_db, fts5_available=False)
    assert call_counts == {
        "_backfill_summary_depths": 1,
        "_backfill_summary_metadata": 1,
        "_backfill_tool_call_columns": 1,
    }


# ---------------------------------------------------------------------------
# AC: algorithm-version bump triggers a re-run
# ---------------------------------------------------------------------------


def test_algorithm_version_bump_re_runs_backfill(
    fresh_db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Downgrading a ledger row to version=0 makes the next pass re-run it."""
    from lossless_hermes.db import migration as migration_module

    run_lcm_migrations(fresh_db, fts5_available=False)

    # Manually rewind the ledger: set backfillSummaryDepths back to v0.
    fresh_db.execute(
        "UPDATE lcm_migration_state SET algorithm_version = 0 "
        "WHERE step_name = 'backfillSummaryDepths'"
    )
    # Commit the manual rewind so re-invoking run_lcm_migrations (which
    # does its own BEGIN EXCLUSIVE) doesn't trip over a nested-tx error.
    # The Python sqlite3 module starts an implicit transaction on the
    # first DML — see test_idempotency_second_run_no_op for the equivalent
    # no-DML-between-calls pattern.
    fresh_db.commit()

    real_depths = migration_module._backfill_summary_depths
    real_metadata = migration_module._backfill_summary_metadata
    real_toolcalls = migration_module._backfill_tool_call_columns
    call_counts: dict[str, int] = {
        "_backfill_summary_depths": 0,
        "_backfill_summary_metadata": 0,
        "_backfill_tool_call_columns": 0,
    }

    def counted_depths(db: sqlite3.Connection) -> None:
        call_counts["_backfill_summary_depths"] += 1
        real_depths(db)

    def counted_metadata(db: sqlite3.Connection) -> None:
        call_counts["_backfill_summary_metadata"] += 1
        real_metadata(db)

    def counted_toolcalls(db: sqlite3.Connection) -> None:
        call_counts["_backfill_tool_call_columns"] += 1
        real_toolcalls(db)

    monkeypatch.setattr(migration_module, "_backfill_summary_depths", counted_depths)
    monkeypatch.setattr(migration_module, "_backfill_summary_metadata", counted_metadata)
    monkeypatch.setattr(migration_module, "_backfill_tool_call_columns", counted_toolcalls)

    run_lcm_migrations(fresh_db, fts5_available=False)

    # Only the rewound step (depths) re-runs.
    assert call_counts == {
        "_backfill_summary_depths": 1,
        "_backfill_summary_metadata": 0,
        "_backfill_tool_call_columns": 0,
    }

    # The ledger now carries both a v0 (legacy) and v1 (current) row for
    # backfillSummaryDepths — algorithm versions are an append-only history.
    rows = fresh_db.execute(
        "SELECT step_name, algorithm_version FROM lcm_migration_state "
        "WHERE step_name = 'backfillSummaryDepths' ORDER BY algorithm_version"
    ).fetchall()
    assert rows == [
        ("backfillSummaryDepths", 0),
        ("backfillSummaryDepths", 1),
    ]


# ---------------------------------------------------------------------------
# AC: pre-existing data — 4-level pyramid backfill
# ---------------------------------------------------------------------------


def _seed_conversation(db: sqlite3.Connection, conversation_id: int = 1) -> None:
    db.execute(
        "INSERT INTO conversations (conversation_id, session_id) VALUES (?, 'sess-1')",
        (conversation_id,),
    )


def _seed_message(
    db: sqlite3.Connection,
    message_id: int,
    conversation_id: int,
    seq: int,
    role: str = "user",
    content: str = "hello",
    token_count: int = 4,
    created_at: str | None = None,
    identity_hash: str | None = None,
) -> None:
    if created_at is None:
        db.execute(
            "INSERT INTO messages (message_id, conversation_id, seq, role, content, token_count, identity_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (message_id, conversation_id, seq, role, content, token_count, identity_hash),
        )
    else:
        db.execute(
            "INSERT INTO messages (message_id, conversation_id, seq, role, content, token_count, identity_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message_id,
                conversation_id,
                seq,
                role,
                content,
                token_count,
                identity_hash,
                created_at,
            ),
        )


def _seed_summary(
    db: sqlite3.Connection,
    summary_id: str,
    conversation_id: int,
    kind: str,
    token_count: int,
    *,
    depth: int = 0,
    created_at: str = "2024-01-01T00:00:00Z",
) -> None:
    db.execute(
        "INSERT INTO summaries (summary_id, conversation_id, kind, depth, content, token_count, created_at) "
        "VALUES (?, ?, ?, ?, '', ?, ?)",
        (summary_id, conversation_id, kind, depth, token_count, created_at),
    )


def _link_leaf(db: sqlite3.Connection, summary_id: str, message_id: int, ordinal: int) -> None:
    db.execute(
        "INSERT INTO summary_messages (summary_id, message_id, ordinal) VALUES (?, ?, ?)",
        (summary_id, message_id, ordinal),
    )


def _link_parent(
    db: sqlite3.Connection,
    summary_id: str,
    parent_summary_id: str,
    ordinal: int | None = None,
) -> None:
    if ordinal is None:
        existing = db.execute(
            "SELECT COUNT(*) FROM summary_parents WHERE summary_id = ?",
            (summary_id,),
        ).fetchone()
        ordinal = int(existing[0])
    db.execute(
        "INSERT INTO summary_parents (summary_id, parent_summary_id, ordinal) VALUES (?, ?, ?)",
        (summary_id, parent_summary_id, ordinal),
    )


def test_backfill_summary_depths_pyramid(migrated_db: sqlite3.Connection) -> None:
    """A 4-level pyramid resolves to depths 0/1/2/3 as expected."""
    _seed_conversation(migrated_db)
    # Leaves L1, L2 (depth 0).
    _seed_summary(migrated_db, "L1", 1, "leaf", token_count=10)
    _seed_summary(migrated_db, "L2", 1, "leaf", token_count=10)
    # Condensed-1 from L1 + L2 (depth 1).
    _seed_summary(migrated_db, "C1", 1, "condensed", token_count=20)
    _link_parent(migrated_db, "C1", "L1")
    _link_parent(migrated_db, "C1", "L2")
    # Condensed-2 from C1 (depth 2).
    _seed_summary(migrated_db, "C2", 1, "condensed", token_count=15)
    _link_parent(migrated_db, "C2", "C1")
    # Condensed-3 from C2 (depth 3).
    _seed_summary(migrated_db, "C3", 1, "condensed", token_count=12)
    _link_parent(migrated_db, "C3", "C2")

    # Reset depths to a wrong value to prove the backfill writes them.
    migrated_db.execute("UPDATE summaries SET depth = -1")
    _backfill_summary_depths(migrated_db)

    rows = migrated_db.execute(
        "SELECT summary_id, depth FROM summaries ORDER BY summary_id"
    ).fetchall()
    assert rows == [
        ("C1", 1),
        ("C2", 2),
        ("C3", 3),
        ("L1", 0),
        ("L2", 0),
    ]


def test_backfill_summary_depths_orphan_condensed(
    migrated_db: sqlite3.Connection,
) -> None:
    """A condensed summary with no parents gets depth=1 (TS line 532)."""
    _seed_conversation(migrated_db)
    _seed_summary(migrated_db, "C0", 1, "condensed", token_count=10)
    _backfill_summary_depths(migrated_db)
    row = migrated_db.execute("SELECT depth FROM summaries WHERE summary_id = 'C0'").fetchone()
    assert row == (1,)


def test_backfill_summary_depths_cycle_guard(
    migrated_db: sqlite3.Connection,
) -> None:
    """A malformed cycle falls back to depth=1 (TS lines 561-566)."""
    _seed_conversation(migrated_db)
    _seed_summary(migrated_db, "X", 1, "condensed", token_count=10)
    _seed_summary(migrated_db, "Y", 1, "condensed", token_count=10)
    # Mutual parent edges → cycle. No leaves to ground the recursion.
    _link_parent(migrated_db, "X", "Y")
    _link_parent(migrated_db, "Y", "X")
    _backfill_summary_depths(migrated_db)
    rows = migrated_db.execute(
        "SELECT summary_id, depth FROM summaries ORDER BY summary_id"
    ).fetchall()
    assert rows == [("X", 1), ("Y", 1)]


def test_backfill_summary_depths_no_op_on_empty_summaries(
    migrated_db: sqlite3.Connection,
) -> None:
    """On a DB with zero summaries, depth backfill is a no-op (no errors)."""
    _backfill_summary_depths(migrated_db)
    count = migrated_db.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
    assert count == 0


def test_backfill_summary_depths_idempotent(
    migrated_db: sqlite3.Connection,
) -> None:
    """Re-running the backfill on already-correct data produces the same depths."""
    _seed_conversation(migrated_db)
    _seed_summary(migrated_db, "L1", 1, "leaf", token_count=10)
    _seed_summary(migrated_db, "C1", 1, "condensed", token_count=20)
    _link_parent(migrated_db, "C1", "L1")

    _backfill_summary_depths(migrated_db)
    rows_after_first = migrated_db.execute(
        "SELECT summary_id, depth FROM summaries ORDER BY summary_id"
    ).fetchall()
    _backfill_summary_depths(migrated_db)
    rows_after_second = migrated_db.execute(
        "SELECT summary_id, depth FROM summaries ORDER BY summary_id"
    ).fetchall()
    assert rows_after_first == rows_after_second == [("C1", 1), ("L1", 0)]


# ---------------------------------------------------------------------------
# AC: summary metadata aggregation
# ---------------------------------------------------------------------------


def test_backfill_summary_metadata_leaf_from_messages(
    migrated_db: sqlite3.Connection,
) -> None:
    """Leaf metadata uses MIN/MAX of source messages' created_at + Σ(tokens)."""
    _seed_conversation(migrated_db)
    _seed_message(
        migrated_db,
        message_id=1,
        conversation_id=1,
        seq=0,
        token_count=10,
        created_at="2024-01-01T10:00:00Z",
    )
    _seed_message(
        migrated_db,
        message_id=2,
        conversation_id=1,
        seq=1,
        token_count=20,
        created_at="2024-01-02T10:00:00Z",
    )
    _seed_summary(
        migrated_db,
        "L1",
        1,
        "leaf",
        token_count=10,
        created_at="2024-01-01T00:00:00Z",
    )
    _link_leaf(migrated_db, "L1", 1, 0)
    _link_leaf(migrated_db, "L1", 2, 1)
    # Need depths to drive the topological walk; backfill them first.
    _backfill_summary_depths(migrated_db)

    _backfill_summary_metadata(migrated_db)
    row = migrated_db.execute(
        "SELECT earliest_at, latest_at, descendant_count, descendant_token_count, "
        "       source_message_token_count "
        "FROM summaries WHERE summary_id = 'L1'"
    ).fetchone()
    assert row is not None
    earliest_at, latest_at, dcount, dtokens, source_tokens = row
    assert earliest_at.startswith("2024-01-01T10:00:00")
    assert latest_at.startswith("2024-01-02T10:00:00")
    assert dcount == 0
    assert dtokens == 0
    assert source_tokens == 30  # 10 + 20


def test_backfill_summary_metadata_condensed_aggregates_parents(
    migrated_db: sqlite3.Connection,
) -> None:
    """Condensed metadata rolls up min/max + counts from the parent leaves."""
    _seed_conversation(migrated_db)
    _seed_message(
        migrated_db,
        message_id=1,
        conversation_id=1,
        seq=0,
        token_count=5,
        created_at="2024-01-01T10:00:00Z",
    )
    _seed_message(
        migrated_db,
        message_id=2,
        conversation_id=1,
        seq=1,
        token_count=7,
        created_at="2024-01-05T10:00:00Z",
    )
    _seed_summary(migrated_db, "L1", 1, "leaf", token_count=10)
    _seed_summary(migrated_db, "L2", 1, "leaf", token_count=20)
    _link_leaf(migrated_db, "L1", 1, 0)
    _link_leaf(migrated_db, "L2", 2, 0)
    _seed_summary(migrated_db, "C1", 1, "condensed", token_count=30)
    _link_parent(migrated_db, "C1", "L1")
    _link_parent(migrated_db, "C1", "L2")
    _backfill_summary_depths(migrated_db)

    _backfill_summary_metadata(migrated_db)

    c1 = migrated_db.execute(
        "SELECT earliest_at, latest_at, descendant_count, descendant_token_count, "
        "       source_message_token_count "
        "FROM summaries WHERE summary_id = 'C1'"
    ).fetchone()
    assert c1 is not None
    earliest_at, latest_at, dcount, dtokens, source_tokens = c1
    assert earliest_at.startswith("2024-01-01T10:00:00")
    assert latest_at.startswith("2024-01-05T10:00:00")
    # Two parents, each contributes (descendant_count + 1) = 1 each → 2.
    assert dcount == 2
    # Parent token_counts: 10 + 20 = 30. No grandchildren contribute.
    assert dtokens == 30
    # Source tokens roll up from leaf source_message_token_count: 5 + 7 = 12.
    assert source_tokens == 12


def test_backfill_summary_metadata_idempotent(
    migrated_db: sqlite3.Connection,
) -> None:
    """Re-running the metadata backfill produces the same row state."""
    _seed_conversation(migrated_db)
    _seed_message(
        migrated_db,
        message_id=1,
        conversation_id=1,
        seq=0,
        token_count=10,
        created_at="2024-01-01T10:00:00Z",
    )
    _seed_summary(migrated_db, "L1", 1, "leaf", token_count=10)
    _link_leaf(migrated_db, "L1", 1, 0)
    _backfill_summary_depths(migrated_db)

    _backfill_summary_metadata(migrated_db)
    first = migrated_db.execute(
        "SELECT earliest_at, latest_at, descendant_count, descendant_token_count, "
        "       source_message_token_count FROM summaries"
    ).fetchall()
    _backfill_summary_metadata(migrated_db)
    second = migrated_db.execute(
        "SELECT earliest_at, latest_at, descendant_count, descendant_token_count, "
        "       source_message_token_count FROM summaries"
    ).fetchall()
    assert first == second


# ---------------------------------------------------------------------------
# AC: tool-call column extraction — every key-precedence rung
# ---------------------------------------------------------------------------


def _seed_tool_part(
    db: sqlite3.Connection,
    part_id: str,
    message_id: int,
    ordinal: int,
    metadata: dict[str, object],
) -> None:
    db.execute(
        "INSERT INTO message_parts (part_id, message_id, session_id, part_type, ordinal, metadata) "
        "VALUES (?, ?, 'sess-1', 'tool', ?, ?)",
        (part_id, message_id, ordinal, json.dumps(metadata)),
    )


@pytest.mark.parametrize(
    "metadata,expected_call_id",
    [
        ({"toolCallId": "call_top"}, "call_top"),
        ({"raw": {"id": "call_raw_id"}}, "call_raw_id"),
        ({"raw": {"call_id": "call_abc"}}, "call_abc"),
        ({"raw": {"toolCallId": "call_camel"}}, "call_camel"),
        ({"raw": {"tool_call_id": "call_snake"}}, "call_snake"),
    ],
    ids=["toolCallId", "raw.id", "raw.call_id", "raw.toolCallId", "raw.tool_call_id"],
)
def test_backfill_tool_call_id_precedence(
    migrated_db: sqlite3.Connection,
    metadata: dict[str, object],
    expected_call_id: str,
) -> None:
    """Each rung of the ``COALESCE`` chain populates ``tool_call_id`` correctly."""
    _seed_conversation(migrated_db)
    _seed_message(migrated_db, message_id=1, conversation_id=1, seq=0)
    _seed_tool_part(migrated_db, "part-1", message_id=1, ordinal=0, metadata=metadata)

    _backfill_tool_call_columns(migrated_db)

    row = migrated_db.execute(
        "SELECT tool_call_id FROM message_parts WHERE part_id = 'part-1'"
    ).fetchone()
    assert row == (expected_call_id,)


def test_backfill_tool_name_and_input(migrated_db: sqlite3.Connection) -> None:
    """tool_name and tool_input get populated from the documented precedence."""
    _seed_conversation(migrated_db)
    _seed_message(migrated_db, message_id=1, conversation_id=1, seq=0)
    metadata = {
        "toolCallId": "call_a",
        "toolName": "Bash",
        "raw": {"input": "ls -la"},
    }
    _seed_tool_part(migrated_db, "part-1", message_id=1, ordinal=0, metadata=metadata)

    _backfill_tool_call_columns(migrated_db)

    row = migrated_db.execute(
        "SELECT tool_call_id, tool_name, tool_input FROM message_parts WHERE part_id = 'part-1'"
    ).fetchone()
    assert row == ("call_a", "Bash", "ls -la")


def test_backfill_tool_columns_does_not_overwrite_existing(
    migrated_db: sqlite3.Connection,
) -> None:
    """Rows with a pre-populated column are untouched (WHERE col IS NULL filter)."""
    _seed_conversation(migrated_db)
    _seed_message(migrated_db, message_id=1, conversation_id=1, seq=0)
    migrated_db.execute(
        "INSERT INTO message_parts (part_id, message_id, session_id, part_type, "
        "ordinal, tool_call_id, metadata) "
        "VALUES (?, ?, 'sess-1', 'tool', ?, 'pre-existing', ?)",
        ("part-1", 1, 0, json.dumps({"toolCallId": "ignored"})),
    )

    _backfill_tool_call_columns(migrated_db)

    row = migrated_db.execute(
        "SELECT tool_call_id FROM message_parts WHERE part_id = 'part-1'"
    ).fetchone()
    assert row == ("pre-existing",)


def test_backfill_tool_columns_idempotent(migrated_db: sqlite3.Connection) -> None:
    """Re-running tool-column backfill is a structural no-op."""
    _seed_conversation(migrated_db)
    _seed_message(migrated_db, message_id=1, conversation_id=1, seq=0)
    _seed_tool_part(
        migrated_db,
        "part-1",
        message_id=1,
        ordinal=0,
        metadata={"raw": {"call_id": "call_abc"}},
    )
    _backfill_tool_call_columns(migrated_db)
    first = migrated_db.execute("SELECT * FROM message_parts").fetchall()
    _backfill_tool_call_columns(migrated_db)
    second = migrated_db.execute("SELECT * FROM message_parts").fetchall()
    assert first == second


# ---------------------------------------------------------------------------
# AC: identity-hash rehash for legacy NULL/empty rows
# ---------------------------------------------------------------------------


def test_backfill_identity_hash_populates_null_rows(
    migrated_db: sqlite3.Connection,
) -> None:
    """A legacy NULL identity_hash gets the canonical SHA-256 digest.

    Spike-003 case #2 (``role='user'`` / ``content='hello'``) yields the
    digest ``87ce4613405ac8c20165d125a5c2219e8b38a9e030616dffd73a89faaf7293c8``.
    """
    _seed_conversation(migrated_db)
    _seed_message(
        migrated_db,
        message_id=1,
        conversation_id=1,
        seq=0,
        role="user",
        content="hello",
        identity_hash=None,
    )
    _backfill_message_identity_hashes(migrated_db)
    row = migrated_db.execute("SELECT identity_hash FROM messages WHERE message_id = 1").fetchone()
    assert row is not None
    assert row[0] == "87ce4613405ac8c20165d125a5c2219e8b38a9e030616dffd73a89faaf7293c8"
    # Cross-check against the Python helper directly.
    assert row[0] == build_message_identity_hash("user", "hello")


def test_backfill_identity_hash_skips_populated_rows(
    migrated_db: sqlite3.Connection,
) -> None:
    """Rows with a non-empty identity_hash are not re-hashed."""
    _seed_conversation(migrated_db)
    _seed_message(
        migrated_db,
        message_id=1,
        conversation_id=1,
        seq=0,
        role="user",
        content="something-else",
        identity_hash="pre-existing-hash",
    )
    _backfill_message_identity_hashes(migrated_db)
    row = migrated_db.execute("SELECT identity_hash FROM messages WHERE message_id = 1").fetchone()
    assert row == ("pre-existing-hash",)


def test_backfill_identity_hash_chunk_boundary(
    migrated_db: sqlite3.Connection,
) -> None:
    """More than one chunk's worth of rows (1,001) all get populated.

    Exercises the ``message_id > ?`` keyed-pagination loop.
    """
    _seed_conversation(migrated_db)
    # Seed 1,001 rows with NULL identity_hash so the loop iterates at least twice.
    rows = [(mid + 1, 1, mid, "user", f"msg-{mid}", 1, None) for mid in range(1_001)]
    migrated_db.executemany(
        "INSERT INTO messages (message_id, conversation_id, seq, role, content, "
        "token_count, identity_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    _backfill_message_identity_hashes(migrated_db)

    count_null = migrated_db.execute(
        "SELECT COUNT(*) FROM messages WHERE identity_hash IS NULL OR identity_hash = ''"
    ).fetchone()[0]
    assert count_null == 0
    # Spot-check the last seeded row.
    last = migrated_db.execute(
        "SELECT identity_hash FROM messages WHERE message_id = 1001"
    ).fetchone()
    assert last is not None
    assert last[0] == build_message_identity_hash("user", "msg-1000")


# ---------------------------------------------------------------------------
# AC: conversation/summary session-key backfill
# ---------------------------------------------------------------------------


def test_backfill_conversation_session_keys_populates_legacy_prefix(
    migrated_db: sqlite3.Connection,
) -> None:
    """NULL session_key on conversations gets the ``legacy:conv_<id>`` prefix."""
    # Seed a conversation directly (bypassing the default empty-string trigger).
    migrated_db.execute(
        "INSERT INTO conversations (conversation_id, session_id, session_key) "
        "VALUES (?, 'sess-1', NULL)",
        (42,),
    )
    _backfill_conversation_session_keys(migrated_db)
    row = migrated_db.execute(
        "SELECT session_key FROM conversations WHERE conversation_id = 42"
    ).fetchone()
    assert row == ("legacy:conv_42",)
    # Audit row exists with deterministic audit_id.
    audit = migrated_db.execute(
        "SELECT audit_id, conversation_id, original_session_key, new_session_key, "
        "       applied_by "
        "FROM lcm_session_key_audit WHERE conversation_id = 42"
    ).fetchone()
    assert audit == (
        "audit-backfill-conv-42",
        42,
        None,
        "legacy:conv_42",
        "migration",
    )
    # Re-running is idempotent: no duplicate audit row.
    _backfill_conversation_session_keys(migrated_db)
    count = migrated_db.execute(
        "SELECT COUNT(*) FROM lcm_session_key_audit WHERE conversation_id = 42"
    ).fetchone()[0]
    assert count == 1


def test_backfill_summary_session_keys_inherits_from_conversation(
    migrated_db: sqlite3.Connection,
) -> None:
    """Summaries with session_key='' inherit from the parent conversation."""
    migrated_db.execute(
        "INSERT INTO conversations (conversation_id, session_id, session_key) "
        "VALUES (?, 'sess-1', ?)",
        (1, "user:alice"),
    )
    _seed_summary(migrated_db, "S1", 1, "leaf", token_count=10)
    # The TS schema defaults summaries.session_key to ''; force that here.
    migrated_db.execute("UPDATE summaries SET session_key = '' WHERE summary_id = 'S1'")
    _backfill_summary_session_keys(migrated_db)
    row = migrated_db.execute(
        "SELECT session_key FROM summaries WHERE summary_id = 'S1'"
    ).fetchone()
    assert row == ("user:alice",)


# ---------------------------------------------------------------------------
# AC: fork-side lcm_rollups backfill — no-op when table absent, runs otherwise
# ---------------------------------------------------------------------------


def test_backfill_fork_rollups_no_op_when_table_absent(
    migrated_db: sqlite3.Connection,
) -> None:
    """On upstream installs (no ``lcm_rollups``), the helper is a no-op."""
    # Sanity: lcm_rollups truly is absent from the upstream schema.
    row = migrated_db.execute(
        "SELECT name FROM sqlite_master WHERE name = 'lcm_rollups'"
    ).fetchone()
    assert row is None
    # Helper does nothing, raises nothing.
    _backfill_fork_rollups_session_keys(migrated_db)


def test_backfill_fork_rollups_populates_session_keys(
    migrated_db: sqlite3.Connection,
) -> None:
    """When ``lcm_rollups`` exists with a session_key col, rows get populated."""
    migrated_db.execute(
        "INSERT INTO conversations (conversation_id, session_id, session_key) "
        "VALUES (?, 'sess-1', 'user:alice')",
        (1,),
    )
    migrated_db.execute(
        "CREATE TABLE lcm_rollups ("
        "  rollup_id TEXT PRIMARY KEY, "
        "  conversation_id INTEGER NOT NULL, "
        "  session_key TEXT NOT NULL DEFAULT ''"
        ")"
    )
    migrated_db.execute(
        "INSERT INTO lcm_rollups (rollup_id, conversation_id, session_key) VALUES ('r-1', 1, '')"
    )
    _backfill_fork_rollups_session_keys(migrated_db)
    row = migrated_db.execute(
        "SELECT session_key FROM lcm_rollups WHERE rollup_id = 'r-1'"
    ).fetchone()
    assert row == ("user:alice",)


def test_backfill_fork_rollups_skips_when_no_session_key_column(
    migrated_db: sqlite3.Connection,
) -> None:
    """Helper returns cleanly if ``lcm_rollups`` exists but lacks ``session_key``."""
    migrated_db.execute(
        "CREATE TABLE lcm_rollups (rollup_id TEXT PRIMARY KEY, conversation_id INTEGER)"
    )
    # Should not raise.
    _backfill_fork_rollups_session_keys(migrated_db)


# ---------------------------------------------------------------------------
# AC: the whole ladder integrated — covered by run_lcm_migrations
# ---------------------------------------------------------------------------


def test_run_versioned_backfills_records_ledger(
    fresh_db: sqlite3.Connection,
) -> None:
    """A single :func:`_run_versioned_backfills` call records all 3 ledger rows."""
    # Apply migrations once so the schema (incl. lcm_migration_state) exists.
    run_lcm_migrations(fresh_db, fts5_available=False)
    # Wipe ledger to simulate a fresh-state DB at the same schema version.
    fresh_db.execute("DELETE FROM lcm_migration_state")
    # Run the backfill section directly (we're outside a BEGIN EXCLUSIVE, but
    # SAVEPOINTs are valid outside a top-level txn in SQLite).
    _run_versioned_backfills(fresh_db, log=None)
    rows = fresh_db.execute(
        "SELECT step_name, algorithm_version FROM lcm_migration_state ORDER BY step_name"
    ).fetchall()
    assert rows == [
        ("backfillSummaryDepths", 1),
        ("backfillSummaryMetadata", 1),
        ("backfillToolCallColumns", 1),
    ]
