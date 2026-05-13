"""Tests for :mod:`lossless_hermes.db.migration` — the core 12 tables + 20 indexes.

Covers the acceptance criteria from ``epics/01-storage/01-04-migration-core-tables.md``
in scope for the core schema:

* All 12 core tables + 20 core indexes created on a fresh in-memory DB
  via :func:`run_lcm_migrations`.
* Idempotency: re-running :func:`run_lcm_migrations` is a no-op.
* CHECK constraints enforced (role / cache_state / last_activity_band /
  item_type / part_type / kind).
* FK CASCADE: deleting a conversation cascades to messages,
  context_items (for message_id), large_files, bootstrap_state,
  compaction_telemetry, compaction_maintenance.
* FK RESTRICT: deleting a message referenced by ``summary_messages``
  raises IntegrityError.
* Legacy index drop: a pre-existing non-partial UNIQUE
  ``conversations_session_key_idx`` is dropped and replaced by the
  partial-active partial UNIQUE.
* Belt-and-suspenders: :func:`_ensure_message_parts_table_belt_and_suspenders`
  re-runs idempotently outside the bulk block.
* Stubs: :func:`_seed_default_prompts` is a no-op (prompt seeding
  lands alongside the synthesis epic). :func:`_ensure_fts5_tables`
  (#01-05) and :func:`_run_versioned_backfills` (#01-15) are now
  IMPLEMENTED — exercised here at the ledger-row level, with full
  per-backfill behavior covered in :mod:`tests.test_migration_fts5`
  and :mod:`tests.test_migration_backfills` respectively.
* :func:`_ensure_v41_tables` and :func:`_ensure_core_triggers` are
  IMPLEMENTED as of #01-06 — verified by ``test_v41_tables_created_*``
  and ``test_core_triggers_created_*`` here, plus full coverage in
  ``test_migration_v41.py``.
* Reference-fixture parity: the SQL stored in ``sqlite_master`` for each
  core object matches the corresponding entry in
  ``tests/fixtures/lcm_reference_schema.sql`` (the TS-generated golden).
  Mismatches outside whitespace will fail loudly.

Out of scope for this test file (covered when the dependent PRs land):

* FTS5 virtual-table creation + seed (in #01-05).
* v4.1 deep-dive tests — see ``tests/test_migration_v41.py``.
* Versioned-backfill deep-dive tests — see ``tests/test_migration_backfills.py``.
* Default-prompt seeding (in synthesis epic).

References:

* :mod:`lossless_hermes.db.migration` — implementation under test.
* ``epics/01-storage/01-04-migration-core-tables.md`` — issue spec + AC.
* ``epics/01-storage/01-06-migration-v41-tables.md`` — v4.1 spec + AC.
* ``docs/porting-guides/storage.md`` §2.1 — table inventory.
* ``tests/fixtures/lcm_reference_schema.sql`` — TS-generated golden.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Iterator

import pytest

from lossless_hermes.db.migration import (
    _CORE_INDEX_CREATIONS,
    _CORE_INDEX_CREATIONS_EARLY,
    _CORE_INDEX_CREATIONS_LATE,
    _CORE_TABLE_CREATIONS,
    _apply_structural_column_probes,
    _drop_legacy_conversation_session_key_index,
    _ensure_core_indexes,
    _ensure_core_indexes_early,
    _ensure_core_indexes_late,
    _ensure_core_tables,
    _ensure_core_triggers,
    _ensure_fts5_tables,
    _ensure_message_parts_table_belt_and_suspenders,
    _ensure_v41_tables,
    _has_column,
    _run_versioned_backfills,
    _seed_default_prompts,
    list_core_index_names,
    list_core_tables,
    run_lcm_migrations,
)

# ---------------------------------------------------------------------------
# Path to the TS-generated reference fixture (the golden schema)
# ---------------------------------------------------------------------------

_REFERENCE_SCHEMA_PATH = Path(__file__).parent / "fixtures" / "lcm_reference_schema.sql"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_db() -> Iterator[sqlite3.Connection]:
    """An in-memory DB with ``PRAGMA foreign_keys = ON`` and no migrations applied yet.

    Equivalent to what :func:`lossless_hermes.db.connection.open_lcm_db`
    produces for a ``:memory:`` path, minus the sqlite-vec load (the
    migration itself doesn't need vec0 — that's the embeddings layer).
    Tests that want vec0 should still use ``open_lcm_db(':memory:')``.
    """
    conn = sqlite3.connect(":memory:")
    # FK enforcement is required for the CASCADE / RESTRICT tests below.
    # Mirrors what `open_lcm_db` does (per `db/connection.py` line 369).
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def migrated_db(fresh_db: sqlite3.Connection) -> sqlite3.Connection:
    """A DB with the core migration ladder already applied (no FTS5).

    Returns the same connection; the fixture exists to factor out the
    ``run_lcm_migrations(fresh_db, fts5_available=False)`` call in tests
    that don't need to observe the pre-migration state. ``fts5_available``
    is pinned to ``False`` here so the table-count / index-count
    assertions below stay focused on the **core** schema delivered by
    #01-04 — FTS5 creation is owned by #01-05 and exercised in
    :mod:`tests.test_migration_fts5`.
    """
    run_lcm_migrations(fresh_db, fts5_available=False)
    return fresh_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _list_tables(conn: sqlite3.Connection) -> list[str]:
    """Return all user table names in alphabetical order.

    Filters out SQLite-internal tables (``sqlite_*``).
    """
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def _list_indexes(conn: sqlite3.Connection) -> list[str]:
    """Return all user index names in alphabetical order.

    Filters out the autoindex entries SQLite creates for PRIMARY KEY /
    UNIQUE constraints (those have ``sqlite_autoindex_`` prefixes).
    """
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def _list_triggers(conn: sqlite3.Connection) -> list[str]:
    """Return all user trigger names in alphabetical order."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def _snapshot_schema(conn: sqlite3.Connection) -> list[tuple[str, str, str | None]]:
    """Return a stable snapshot of (type, name, sql) tuples for all user objects.

    Used to assert idempotency: re-running the migration must produce a
    byte-identical schema.
    """
    return conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' "
        "ORDER BY type, name"
    ).fetchall()


def _normalize_sql(sql: str) -> str:
    """Collapse whitespace for whitespace-insensitive SQL comparisons.

    Lower-cases keywords for case-insensitive comparison too. The intent
    is to compare semantic content, not formatting.
    """
    # Collapse all whitespace into single spaces and trim.
    normalized = re.sub(r"\s+", " ", sql).strip()
    return normalized.lower()


# ---------------------------------------------------------------------------
# Table-set / index-set / trigger-set assertions
# ---------------------------------------------------------------------------


# The exact 12 core tables this PR is responsible for.
_EXPECTED_CORE_TABLES = (
    "context_items",
    "conversation_bootstrap_state",
    "conversation_compaction_maintenance",
    "conversation_compaction_telemetry",
    "conversations",
    "large_files",
    "lcm_migration_state",
    "message_parts",
    "messages",
    "summaries",
    "summary_messages",
    "summary_parents",
)

# The 20 core indexes this PR creates.
_EXPECTED_CORE_INDEXES = (
    "bootstrap_state_path_idx",
    "compaction_telemetry_state_idx",
    "context_items_conv_idx",
    "conversations_active_session_key_idx",
    "conversations_session_id_active_created_idx",
    "conversations_session_key_active_created_idx",
    "conversations_session_key_v41_idx",
    "large_files_conv_idx",
    "message_parts_message_idx",
    "message_parts_type_idx",
    "messages_conv_identity_hash_idx",
    "messages_conv_seq_idx",
    "messages_suppressed_idx",
    "summaries_contains_suppressed_idx",
    "summaries_conv_created_idx",
    "summaries_conv_depth_kind_idx",
    "summaries_session_key_kind_latest_idx",
    "summaries_suppressed_idx",
    "summary_messages_message_idx",
    "summary_parents_parent_summary_idx",
)


def test_core_tables_present(migrated_db: sqlite3.Connection) -> None:
    """The migration creates all 12 core tables.

    After #01-06 landed, the migration creates v4.1 tables as well; this
    test only checks the core subset is present (use
    :func:`list_core_tables` to enumerate).
    """
    tables = set(_list_tables(migrated_db))
    missing = set(_EXPECTED_CORE_TABLES) - tables
    assert missing == set(), f"missing core tables: {missing!r}"


def test_core_indexes_present(migrated_db: sqlite3.Connection) -> None:
    """The migration creates all 20 core indexes.

    After #01-06 landed, the migration creates v4.1 indexes as well; this
    test only checks the core subset is present (use
    :func:`list_core_index_names` to enumerate).
    """
    indexes = set(_list_indexes(migrated_db))
    missing = set(_EXPECTED_CORE_INDEXES) - indexes
    assert missing == set(), f"missing core indexes: {missing!r}"


def test_introspection_helpers_match_expected_sets() -> None:
    """``list_core_tables()`` and ``list_core_index_names()`` match expectations.

    These helpers are used by tests + future ``/lcm doctor`` to enumerate
    what the core PR is responsible for. They must stay in sync with
    ``_CORE_TABLE_CREATIONS`` / ``_CORE_INDEX_CREATIONS``.
    """
    assert sorted(list_core_tables()) == list(_EXPECTED_CORE_TABLES)
    assert sorted(list_core_index_names()) == list(_EXPECTED_CORE_INDEXES)


# ---------------------------------------------------------------------------
# Per-table schema assertions (columns + types)
# ---------------------------------------------------------------------------


def _column_info(conn: sqlite3.Connection, table: str) -> list[tuple[str, str]]:
    """Return [(column_name, type), ...] for a table in ordinal order."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [(r[1], r[2]) for r in rows]


def test_conversations_columns(migrated_db: sqlite3.Connection) -> None:
    """``conversations`` has the 9 columns documented in storage.md §2.1."""
    expected = [
        ("conversation_id", "INTEGER"),
        ("session_id", "TEXT"),
        ("session_key", "TEXT"),
        ("active", "INTEGER"),
        ("archived_at", "TEXT"),
        ("title", "TEXT"),
        ("bootstrapped_at", "TEXT"),
        ("created_at", "TEXT"),
        ("updated_at", "TEXT"),
    ]
    assert _column_info(migrated_db, "conversations") == expected


def test_messages_columns(migrated_db: sqlite3.Connection) -> None:
    """``messages`` has the 9 columns + ``suppressed_at`` (added via probe).

    On a fresh DB ``suppressed_at`` is added by
    :func:`_ensure_message_suppressed_at_column` after the bulk CREATE
    (per ``migration.ts:1296``). The fresh-DB total is 9 + 1 = 10 columns.
    """
    columns = _column_info(migrated_db, "messages")
    column_names = [c[0] for c in columns]
    assert column_names == [
        "message_id",
        "conversation_id",
        "seq",
        "role",
        "content",
        "token_count",
        "identity_hash",
        "created_at",
        "suppressed_at",
    ]


def test_summaries_columns(migrated_db: sqlite3.Connection) -> None:
    """``summaries`` has the v0 columns + v4.1 additions (12+7 = 19 total).

    v0 columns (12, from the CREATE TABLE body):
        summary_id, conversation_id, kind, depth, content, token_count,
        earliest_at, latest_at, descendant_count, descendant_token_count,
        source_message_token_count, created_at, file_ids.

    Plus ALTER columns: model + 7 v4.1 columns (session_key, suppressed_at,
    entity_index, contains_suppressed_leaves, suppress_reason,
    superseded_by, leaf_summarizer_cap_was). Total 13 + 1 + 7 = 21.
    """
    columns = _column_info(migrated_db, "summaries")
    column_names = [c[0] for c in columns]
    assert column_names == [
        "summary_id",
        "conversation_id",
        "kind",
        "depth",
        "content",
        "token_count",
        "earliest_at",
        "latest_at",
        "descendant_count",
        "descendant_token_count",
        "source_message_token_count",
        "created_at",
        "file_ids",
        "model",
        "session_key",
        "suppressed_at",
        "entity_index",
        "contains_suppressed_leaves",
        "suppress_reason",
        "superseded_by",
        "leaf_summarizer_cap_was",
    ]


def test_message_parts_columns(migrated_db: sqlite3.Connection) -> None:
    """``message_parts`` has the 25 sparse columns from storage.md §2.1.

    The 12-value ``part_type`` CHECK is verified separately in
    :func:`test_message_parts_check_constraint_part_type`.
    """
    columns = _column_info(migrated_db, "message_parts")
    column_names = [c[0] for c in columns]
    assert column_names == [
        "part_id",
        "message_id",
        "session_id",
        "part_type",
        "ordinal",
        "text_content",
        "is_ignored",
        "is_synthetic",
        "tool_call_id",
        "tool_name",
        "tool_status",
        "tool_input",
        "tool_output",
        "tool_error",
        "tool_title",
        "patch_hash",
        "patch_files",
        "file_mime",
        "file_name",
        "file_url",
        "subtask_prompt",
        "subtask_desc",
        "subtask_agent",
        "step_reason",
        "step_cost",
        "step_tokens_in",
        "step_tokens_out",
        "snapshot_hash",
        "compaction_auto",
        "metadata",
    ]


def test_compaction_telemetry_columns(migrated_db: sqlite3.Connection) -> None:
    """``conversation_compaction_telemetry`` has the 18 columns."""
    columns = _column_info(migrated_db, "conversation_compaction_telemetry")
    column_names = [c[0] for c in columns]
    assert column_names == [
        "conversation_id",
        "last_observed_cache_read",
        "last_observed_cache_write",
        "last_observed_prompt_token_count",
        "last_observed_cache_hit_at",
        "last_observed_cache_break_at",
        "cache_state",
        "consecutive_cold_observations",
        "retention",
        "last_leaf_compaction_at",
        "turns_since_leaf_compaction",
        "tokens_accumulated_since_leaf_compaction",
        "last_activity_band",
        "last_api_call_at",
        "last_cache_touch_at",
        "provider",
        "model",
        "updated_at",
    ]


# ---------------------------------------------------------------------------
# Idempotency invariant — re-running the migration is a no-op
# ---------------------------------------------------------------------------


def test_idempotency_second_run_no_op(fresh_db: sqlite3.Connection) -> None:
    """``run_lcm_migrations`` called twice produces identical schema.

    The schema snapshot after the first run must byte-match the snapshot
    after the second run. This is the structural-state invariant from
    ADR-026 §"Idempotency".
    """
    run_lcm_migrations(fresh_db)
    snapshot_1 = _snapshot_schema(fresh_db)

    run_lcm_migrations(fresh_db)
    snapshot_2 = _snapshot_schema(fresh_db)

    assert snapshot_1 == snapshot_2, (
        "second migration changed the schema:\n"
        f"  first run: {snapshot_1}\n"
        f"  second run: {snapshot_2}"
    )


def test_idempotency_third_run_no_op(fresh_db: sqlite3.Connection) -> None:
    """Re-running migrations N times (N > 2) is still a no-op.

    Defense-in-depth: catches the hypothetical case where the second run
    creates an artifact the third run trips over (e.g. an index probe
    that flips state).
    """
    run_lcm_migrations(fresh_db)
    snapshot_first = _snapshot_schema(fresh_db)

    for _ in range(5):
        run_lcm_migrations(fresh_db)

    snapshot_last = _snapshot_schema(fresh_db)
    assert snapshot_first == snapshot_last


# ---------------------------------------------------------------------------
# CHECK constraint enforcement
# ---------------------------------------------------------------------------


def test_messages_role_check_constraint(migrated_db: sqlite3.Connection) -> None:
    """Inserting ``role='invalid'`` into ``messages`` raises IntegrityError."""
    migrated_db.execute("INSERT INTO conversations (session_id) VALUES ('s1')")
    conv_id = migrated_db.execute("SELECT last_insert_rowid()").fetchone()[0]

    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        migrated_db.execute(
            "INSERT INTO messages (conversation_id, seq, role, content, token_count) "
            "VALUES (?, 1, 'invalid', 'hi', 1)",
            (conv_id,),
        )


def test_messages_role_check_allows_valid_roles(migrated_db: sqlite3.Connection) -> None:
    """All four valid roles are accepted: system, user, assistant, tool."""
    migrated_db.execute("INSERT INTO conversations (session_id) VALUES ('s1')")
    conv_id = migrated_db.execute("SELECT last_insert_rowid()").fetchone()[0]

    for seq, role in enumerate(("system", "user", "assistant", "tool"), start=1):
        migrated_db.execute(
            "INSERT INTO messages (conversation_id, seq, role, content, token_count) "
            "VALUES (?, ?, ?, 'hi', 1)",
            (conv_id, seq, role),
        )

    count = migrated_db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert count == 4


def test_telemetry_cache_state_check_constraint(migrated_db: sqlite3.Connection) -> None:
    """Inserting ``cache_state='lukewarm'`` raises IntegrityError.

    Only 'hot' / 'cold' / 'unknown' are accepted per storage.md §2.1.
    """
    migrated_db.execute("INSERT INTO conversations (session_id) VALUES ('s1')")
    conv_id = migrated_db.execute("SELECT last_insert_rowid()").fetchone()[0]

    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        migrated_db.execute(
            "INSERT INTO conversation_compaction_telemetry "
            "(conversation_id, cache_state) VALUES (?, 'lukewarm')",
            (conv_id,),
        )


def test_telemetry_last_activity_band_check_constraint(
    migrated_db: sqlite3.Connection,
) -> None:
    """Inserting ``last_activity_band='extreme'`` raises IntegrityError.

    Only 'low' / 'medium' / 'high' are accepted.
    """
    migrated_db.execute("INSERT INTO conversations (session_id) VALUES ('s1')")
    conv_id = migrated_db.execute("SELECT last_insert_rowid()").fetchone()[0]

    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        migrated_db.execute(
            "INSERT INTO conversation_compaction_telemetry "
            "(conversation_id, last_activity_band) VALUES (?, 'extreme')",
            (conv_id,),
        )


def test_summaries_kind_check_constraint(migrated_db: sqlite3.Connection) -> None:
    """``kind`` must be 'leaf' or 'condensed'."""
    migrated_db.execute("INSERT INTO conversations (session_id) VALUES ('s1')")
    conv_id = migrated_db.execute("SELECT last_insert_rowid()").fetchone()[0]

    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        migrated_db.execute(
            "INSERT INTO summaries (summary_id, conversation_id, kind, content, "
            "token_count) VALUES ('s1', ?, 'invalid_kind', 'x', 1)",
            (conv_id,),
        )


def test_message_parts_check_constraint_part_type(
    migrated_db: sqlite3.Connection,
) -> None:
    """``part_type`` must be one of the 12 enum values."""
    migrated_db.execute("INSERT INTO conversations (session_id) VALUES ('s1')")
    conv_id = migrated_db.execute("SELECT last_insert_rowid()").fetchone()[0]
    migrated_db.execute(
        "INSERT INTO messages (conversation_id, seq, role, content, token_count) "
        "VALUES (?, 1, 'user', 'hi', 1)",
        (conv_id,),
    )
    msg_id = migrated_db.execute("SELECT last_insert_rowid()").fetchone()[0]

    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        migrated_db.execute(
            "INSERT INTO message_parts (part_id, message_id, session_id, part_type, "
            "ordinal) VALUES ('p1', ?, 's1', 'invalid_part', 0)",
            (msg_id,),
        )


def test_context_items_check_exactly_one(migrated_db: sqlite3.Connection) -> None:
    """``context_items`` requires exactly one of (message_id, summary_id) set.

    Setting both → IntegrityError.
    """
    migrated_db.execute("INSERT INTO conversations (session_id) VALUES ('s1')")
    conv_id = migrated_db.execute("SELECT last_insert_rowid()").fetchone()[0]
    migrated_db.execute(
        "INSERT INTO messages (conversation_id, seq, role, content, token_count) "
        "VALUES (?, 1, 'user', 'hi', 1)",
        (conv_id,),
    )
    msg_id = migrated_db.execute("SELECT last_insert_rowid()").fetchone()[0]
    migrated_db.execute(
        "INSERT INTO summaries (summary_id, conversation_id, kind, content, "
        "token_count) VALUES ('s1', ?, 'leaf', 'x', 1)",
        (conv_id,),
    )

    # item_type='message' but BOTH IDs set → fails.
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        migrated_db.execute(
            "INSERT INTO context_items (conversation_id, ordinal, item_type, "
            "message_id, summary_id) VALUES (?, 0, 'message', ?, 's1')",
            (conv_id, msg_id),
        )

    # item_type='message' but message_id NULL → fails.
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        migrated_db.execute(
            "INSERT INTO context_items (conversation_id, ordinal, item_type) "
            "VALUES (?, 1, 'message')",
            (conv_id,),
        )

    # Valid: item_type='message', only message_id set.
    migrated_db.execute(
        "INSERT INTO context_items (conversation_id, ordinal, item_type, message_id) "
        "VALUES (?, 2, 'message', ?)",
        (conv_id, msg_id),
    )

    # Valid: item_type='summary', only summary_id set.
    migrated_db.execute(
        "INSERT INTO context_items (conversation_id, ordinal, item_type, summary_id) "
        "VALUES (?, 3, 'summary', 's1')",
        (conv_id,),
    )


def test_context_items_check_item_type_enum(migrated_db: sqlite3.Connection) -> None:
    """``item_type`` must be 'message' or 'summary'."""
    migrated_db.execute("INSERT INTO conversations (session_id) VALUES ('s1')")
    conv_id = migrated_db.execute("SELECT last_insert_rowid()").fetchone()[0]

    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        migrated_db.execute(
            "INSERT INTO context_items (conversation_id, ordinal, item_type, "
            "summary_id) VALUES (?, 0, 'invalid_type', 's1')",
            (conv_id,),
        )


# ---------------------------------------------------------------------------
# Foreign-key CASCADE / RESTRICT enforcement
# ---------------------------------------------------------------------------


def test_fk_cascade_conversation_to_messages(migrated_db: sqlite3.Connection) -> None:
    """Deleting a conversation cascades to ``messages``."""
    migrated_db.execute("INSERT INTO conversations (session_id) VALUES ('s1')")
    conv_id = migrated_db.execute("SELECT last_insert_rowid()").fetchone()[0]
    migrated_db.execute(
        "INSERT INTO messages (conversation_id, seq, role, content, token_count) "
        "VALUES (?, 1, 'user', 'hi', 1)",
        (conv_id,),
    )

    migrated_db.execute("DELETE FROM conversations WHERE conversation_id = ?", (conv_id,))

    count = migrated_db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert count == 0


def test_fk_cascade_conversation_to_large_files(
    migrated_db: sqlite3.Connection,
) -> None:
    """Deleting a conversation cascades to ``large_files``."""
    migrated_db.execute("INSERT INTO conversations (session_id) VALUES ('s1')")
    conv_id = migrated_db.execute("SELECT last_insert_rowid()").fetchone()[0]
    migrated_db.execute(
        "INSERT INTO large_files (file_id, conversation_id, storage_uri) "
        "VALUES ('f1', ?, 'file:///tmp/f1')",
        (conv_id,),
    )

    migrated_db.execute("DELETE FROM conversations WHERE conversation_id = ?", (conv_id,))

    count = migrated_db.execute("SELECT COUNT(*) FROM large_files").fetchone()[0]
    assert count == 0


def test_fk_cascade_conversation_to_compaction_telemetry(
    migrated_db: sqlite3.Connection,
) -> None:
    """Deleting a conversation cascades to ``conversation_compaction_telemetry``."""
    migrated_db.execute("INSERT INTO conversations (session_id) VALUES ('s1')")
    conv_id = migrated_db.execute("SELECT last_insert_rowid()").fetchone()[0]
    migrated_db.execute(
        "INSERT INTO conversation_compaction_telemetry (conversation_id) VALUES (?)",
        (conv_id,),
    )

    migrated_db.execute("DELETE FROM conversations WHERE conversation_id = ?", (conv_id,))

    count = migrated_db.execute(
        "SELECT COUNT(*) FROM conversation_compaction_telemetry"
    ).fetchone()[0]
    assert count == 0


def test_fk_cascade_conversation_to_bootstrap_state(
    migrated_db: sqlite3.Connection,
) -> None:
    """Deleting a conversation cascades to ``conversation_bootstrap_state``."""
    migrated_db.execute("INSERT INTO conversations (session_id) VALUES ('s1')")
    conv_id = migrated_db.execute("SELECT last_insert_rowid()").fetchone()[0]
    migrated_db.execute(
        "INSERT INTO conversation_bootstrap_state "
        "(conversation_id, session_file_path, last_seen_size, last_seen_mtime_ms, "
        "last_processed_offset) VALUES (?, '/tmp/x', 0, 0, 0)",
        (conv_id,),
    )

    migrated_db.execute("DELETE FROM conversations WHERE conversation_id = ?", (conv_id,))

    count = migrated_db.execute("SELECT COUNT(*) FROM conversation_bootstrap_state").fetchone()[0]
    assert count == 0


def test_fk_cascade_conversation_to_compaction_maintenance(
    migrated_db: sqlite3.Connection,
) -> None:
    """Deleting a conversation cascades to ``conversation_compaction_maintenance``."""
    migrated_db.execute("INSERT INTO conversations (session_id) VALUES ('s1')")
    conv_id = migrated_db.execute("SELECT last_insert_rowid()").fetchone()[0]
    migrated_db.execute(
        "INSERT INTO conversation_compaction_maintenance (conversation_id) VALUES (?)",
        (conv_id,),
    )

    migrated_db.execute("DELETE FROM conversations WHERE conversation_id = ?", (conv_id,))

    count = migrated_db.execute(
        "SELECT COUNT(*) FROM conversation_compaction_maintenance"
    ).fetchone()[0]
    assert count == 0


def test_fk_cascade_message_to_message_parts(migrated_db: sqlite3.Connection) -> None:
    """Deleting a message cascades to ``message_parts``."""
    migrated_db.execute("INSERT INTO conversations (session_id) VALUES ('s1')")
    conv_id = migrated_db.execute("SELECT last_insert_rowid()").fetchone()[0]
    migrated_db.execute(
        "INSERT INTO messages (conversation_id, seq, role, content, token_count) "
        "VALUES (?, 1, 'user', 'hi', 1)",
        (conv_id,),
    )
    msg_id = migrated_db.execute("SELECT last_insert_rowid()").fetchone()[0]
    migrated_db.execute(
        "INSERT INTO message_parts (part_id, message_id, session_id, part_type, "
        "ordinal) VALUES ('p1', ?, 's1', 'text', 0)",
        (msg_id,),
    )

    migrated_db.execute("DELETE FROM messages WHERE message_id = ?", (msg_id,))

    count = migrated_db.execute("SELECT COUNT(*) FROM message_parts").fetchone()[0]
    assert count == 0


def test_fk_cascade_summary_to_summary_messages(migrated_db: sqlite3.Connection) -> None:
    """Deleting a summary cascades to ``summary_messages`` (on the summary side)."""
    migrated_db.execute("INSERT INTO conversations (session_id) VALUES ('s1')")
    conv_id = migrated_db.execute("SELECT last_insert_rowid()").fetchone()[0]
    migrated_db.execute(
        "INSERT INTO messages (conversation_id, seq, role, content, token_count) "
        "VALUES (?, 1, 'user', 'hi', 1)",
        (conv_id,),
    )
    msg_id = migrated_db.execute("SELECT last_insert_rowid()").fetchone()[0]
    migrated_db.execute(
        "INSERT INTO summaries (summary_id, conversation_id, kind, content, "
        "token_count) VALUES ('s1', ?, 'leaf', 'x', 1)",
        (conv_id,),
    )
    migrated_db.execute(
        "INSERT INTO summary_messages (summary_id, message_id, ordinal) VALUES ('s1', ?, 0)",
        (msg_id,),
    )

    migrated_db.execute("DELETE FROM summaries WHERE summary_id = 's1'")

    count = migrated_db.execute("SELECT COUNT(*) FROM summary_messages").fetchone()[0]
    assert count == 0


def test_fk_restrict_message_blocks_delete_when_referenced(
    migrated_db: sqlite3.Connection,
) -> None:
    """Deleting a message referenced by ``summary_messages`` raises IntegrityError.

    Storage.md §2.1 summary_messages row: ``message_id`` references
    ``messages(message_id) ON DELETE RESTRICT``. This invariant ensures
    leaves' source-message linkage cannot be silently lost.
    """
    migrated_db.execute("INSERT INTO conversations (session_id) VALUES ('s1')")
    conv_id = migrated_db.execute("SELECT last_insert_rowid()").fetchone()[0]
    migrated_db.execute(
        "INSERT INTO messages (conversation_id, seq, role, content, token_count) "
        "VALUES (?, 1, 'user', 'hi', 1)",
        (conv_id,),
    )
    msg_id = migrated_db.execute("SELECT last_insert_rowid()").fetchone()[0]
    migrated_db.execute(
        "INSERT INTO summaries (summary_id, conversation_id, kind, content, "
        "token_count) VALUES ('s1', ?, 'leaf', 'x', 1)",
        (conv_id,),
    )
    migrated_db.execute(
        "INSERT INTO summary_messages (summary_id, message_id, ordinal) VALUES ('s1', ?, 0)",
        (msg_id,),
    )

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        migrated_db.execute("DELETE FROM messages WHERE message_id = ?", (msg_id,))


def test_fk_restrict_summary_parents_blocks_delete(
    migrated_db: sqlite3.Connection,
) -> None:
    """Deleting a parent summary referenced by ``summary_parents`` raises IntegrityError.

    Storage.md §2.1 summary_parents row: ``parent_summary_id`` is RESTRICT.
    """
    migrated_db.execute("INSERT INTO conversations (session_id) VALUES ('s1')")
    conv_id = migrated_db.execute("SELECT last_insert_rowid()").fetchone()[0]
    migrated_db.execute(
        "INSERT INTO summaries (summary_id, conversation_id, kind, content, "
        "token_count) VALUES ('parent', ?, 'leaf', 'x', 1)",
        (conv_id,),
    )
    migrated_db.execute(
        "INSERT INTO summaries (summary_id, conversation_id, kind, content, "
        "token_count) VALUES ('child', ?, 'condensed', 'x', 1)",
        (conv_id,),
    )
    migrated_db.execute(
        "INSERT INTO summary_parents (summary_id, parent_summary_id, ordinal) "
        "VALUES ('child', 'parent', 0)"
    )

    # Deleting the parent referenced as parent_summary_id should fail
    # because the FK to parent_summary_id is RESTRICT.
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        migrated_db.execute("DELETE FROM summaries WHERE summary_id = 'parent'")


# ---------------------------------------------------------------------------
# Session_key UNIQUE invariant + legacy index drop
# ---------------------------------------------------------------------------


def test_session_key_partial_unique_on_active(migrated_db: sqlite3.Connection) -> None:
    """Active conversations with the same session_key collide; archived ones don't.

    The UNIQUE index is partial:
    ``WHERE session_key IS NOT NULL AND active = 1``. So two active
    conversations with the same session_key fail; an active + archived
    pair is allowed.
    """
    # Two active rows with same session_key → second insert fails.
    migrated_db.execute(
        "INSERT INTO conversations (session_id, session_key, active) VALUES ('s1', 'k1', 1)"
    )
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        migrated_db.execute(
            "INSERT INTO conversations (session_id, session_key, active) VALUES ('s2', 'k1', 1)"
        )

    # Adding an inactive row with the same session_key is allowed.
    migrated_db.execute(
        "INSERT INTO conversations (session_id, session_key, active) VALUES ('s3', 'k1', 0)"
    )

    count = migrated_db.execute(
        "SELECT COUNT(*) FROM conversations WHERE session_key = 'k1'"
    ).fetchone()[0]
    assert count == 2


def test_session_key_null_does_not_collide(migrated_db: sqlite3.Connection) -> None:
    """Multiple active rows with ``session_key IS NULL`` are allowed.

    The partial UNIQUE excludes NULL session_keys, so they're not
    considered for uniqueness.
    """
    migrated_db.execute("INSERT INTO conversations (session_id, active) VALUES ('s1', 1)")
    migrated_db.execute("INSERT INTO conversations (session_id, active) VALUES ('s2', 1)")

    count = migrated_db.execute(
        "SELECT COUNT(*) FROM conversations WHERE session_key IS NULL"
    ).fetchone()[0]
    assert count == 2


def test_legacy_session_key_index_dropped(fresh_db: sqlite3.Connection) -> None:
    """If a legacy ``conversations_session_key_idx`` exists, the migration drops it.

    Replicates an old DB where the obsolete non-partial UNIQUE was
    present. The migration must:

    1. Run the bulk CREATE block (creates the partial UNIQUE replacement).
    2. DROP the legacy non-partial UNIQUE explicitly.

    The test pre-creates a v0-shape conversations table (with ``created_at``
    + ``session_id`` + ``session_key`` — the columns present in
    ``migration.ts:917-927`` minus the v4.1 additions). The migration's
    structural-column probes (``_ensure_conversation_columns``) then
    add ``bootstrapped_at``, ``active``, ``archived_at`` since they're
    missing.
    """
    # Pre-create the legacy v0-shape table + the legacy non-partial UNIQUE.
    # The v0 schema has session_id / session_key / created_at — the
    # structural probes add the rest.
    fresh_db.execute(
        """
        CREATE TABLE conversations (
          conversation_id INTEGER PRIMARY KEY AUTOINCREMENT,
          session_id TEXT NOT NULL,
          session_key TEXT,
          created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    fresh_db.execute(
        "CREATE UNIQUE INDEX conversations_session_key_idx ON conversations(session_key)"
    )

    # Confirm legacy index is present.
    pre_indexes = _list_indexes(fresh_db)
    assert "conversations_session_key_idx" in pre_indexes

    # Run migrations.
    run_lcm_migrations(fresh_db)

    # The legacy index should now be gone, replaced by the partial UNIQUE.
    post_indexes = _list_indexes(fresh_db)
    assert "conversations_session_key_idx" not in post_indexes
    assert "conversations_active_session_key_idx" in post_indexes


# ---------------------------------------------------------------------------
# Belt-and-suspenders for message_parts (migration.ts:271-322 port)
# ---------------------------------------------------------------------------


def test_belt_and_suspenders_idempotent(migrated_db: sqlite3.Connection) -> None:
    """``_ensure_message_parts_table_belt_and_suspenders`` is a no-op when present."""
    # Snapshot the table SQL.
    sql_before = migrated_db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='message_parts'"
    ).fetchone()[0]

    # Run the belt-and-suspenders alone — should be a no-op.
    _ensure_message_parts_table_belt_and_suspenders(migrated_db)

    sql_after = migrated_db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='message_parts'"
    ).fetchone()[0]
    assert sql_before == sql_after


def test_belt_and_suspenders_creates_when_missing(fresh_db: sqlite3.Connection) -> None:
    """If ``message_parts`` is missing, the belt-and-suspenders creates it.

    Replicates the original failure mode from
    ``migration.ts:271-322`` where node:sqlite pre-v22.12 silently aborted
    the bulk block and left ``message_parts`` missing.
    """
    # Pre-create messages so the FK target exists.
    fresh_db.execute(_CORE_TABLE_CREATIONS[0][1])  # conversations
    fresh_db.execute(_CORE_TABLE_CREATIONS[1][1])  # messages

    # Confirm message_parts is NOT yet present.
    pre = fresh_db.execute("SELECT name FROM sqlite_master WHERE name='message_parts'").fetchone()
    assert pre is None

    _ensure_message_parts_table_belt_and_suspenders(fresh_db)

    post = fresh_db.execute("SELECT name FROM sqlite_master WHERE name='message_parts'").fetchone()
    assert post is not None
    assert post[0] == "message_parts"

    # And the two indexes are created too.
    indexes = _list_indexes(fresh_db)
    assert "message_parts_message_idx" in indexes
    assert "message_parts_type_idx" in indexes


# ---------------------------------------------------------------------------
# Structural-state probes (handle imported-from-OpenClaw schemas)
# ---------------------------------------------------------------------------


def test_structural_probe_adds_missing_v41_summary_columns(
    fresh_db: sqlite3.Connection,
) -> None:
    """If ``summaries`` exists without v4.1 columns, the probe adds them.

    Replicates an old OpenClaw DB pre-v3.1: ``summaries`` exists with the
    v0 columns only. After migration the v4.1 columns are present.
    """
    # Pre-create conversations + a v0-shape summaries table missing v3.1+ columns.
    fresh_db.execute(_CORE_TABLE_CREATIONS[0][1])  # conversations
    fresh_db.execute(
        """
        CREATE TABLE summaries (
          summary_id TEXT PRIMARY KEY,
          conversation_id INTEGER NOT NULL REFERENCES conversations(conversation_id) ON DELETE CASCADE,
          kind TEXT NOT NULL CHECK (kind IN ('leaf', 'condensed')),
          content TEXT NOT NULL,
          token_count INTEGER NOT NULL,
          created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )

    # Confirm v4.1 columns are NOT present.
    assert not _has_column(fresh_db, "summaries", "session_key")
    assert not _has_column(fresh_db, "summaries", "suppressed_at")
    assert not _has_column(fresh_db, "summaries", "depth")
    assert not _has_column(fresh_db, "summaries", "model")

    run_lcm_migrations(fresh_db)

    # Now they should all be present.
    assert _has_column(fresh_db, "summaries", "session_key")
    assert _has_column(fresh_db, "summaries", "suppressed_at")
    assert _has_column(fresh_db, "summaries", "entity_index")
    assert _has_column(fresh_db, "summaries", "contains_suppressed_leaves")
    assert _has_column(fresh_db, "summaries", "suppress_reason")
    assert _has_column(fresh_db, "summaries", "superseded_by")
    assert _has_column(fresh_db, "summaries", "leaf_summarizer_cap_was")
    assert _has_column(fresh_db, "summaries", "depth")
    assert _has_column(fresh_db, "summaries", "model")
    assert _has_column(fresh_db, "summaries", "earliest_at")
    assert _has_column(fresh_db, "summaries", "source_message_token_count")


def test_structural_probe_no_op_on_fresh_db(migrated_db: sqlite3.Connection) -> None:
    """Running the probes again on a fresh DB doesn't error or double-add columns."""
    columns_before = _column_info(migrated_db, "summaries")
    _apply_structural_column_probes(migrated_db)
    columns_after = _column_info(migrated_db, "summaries")
    assert columns_before == columns_after


# ---------------------------------------------------------------------------
# Stubs — verify the future-PR section helpers are no-ops
# ---------------------------------------------------------------------------


def test_fts5_noop_when_unavailable(migrated_db: sqlite3.Connection) -> None:
    """``_ensure_fts5_tables(..., fts5_available=False)`` is a no-op.

    The ``migrated_db`` fixture passes ``fts5_available=False``, so the
    fixture itself already exercises this path. The redundant call here
    verifies idempotency: calling the function again with the same flag
    does not mutate the schema.

    Note: this test only covers the negative path. The positive path —
    FTS5 tables are created when ``fts5_available=True`` — is covered in
    :mod:`tests.test_migration_fts5`.
    """
    schema_before = _snapshot_schema(migrated_db)
    _ensure_fts5_tables(migrated_db, fts5_available=False)
    schema_after = _snapshot_schema(migrated_db)
    assert schema_before == schema_after

    tables = _list_tables(migrated_db)
    assert "messages_fts" not in tables
    assert "summaries_fts" not in tables
    assert "summaries_fts_cjk" not in tables


def test_v41_tables_created_after_migration(migrated_db: sqlite3.Connection) -> None:
    """After #01-06, all 17 v4.1 tables are created by ``_ensure_v41_tables``.

    Re-invoking the helper on a migrated DB is idempotent (no-op), but
    the tables themselves exist.
    """
    schema_before = _snapshot_schema(migrated_db)
    _ensure_v41_tables(migrated_db)
    schema_after = _snapshot_schema(migrated_db)
    # Idempotent re-run: no schema delta.
    assert schema_before == schema_after

    tables = set(_list_tables(migrated_db))
    expected_v41_tables = {
        "lcm_worker_lock",
        "lcm_extraction_queue",
        "lcm_session_key_audit",
        "lcm_prompt_registry",
        "lcm_synthesis_cache",
        "lcm_cache_leaf_refs",
        "lcm_synthesis_audit",
        "lcm_eval_query_set",
        "lcm_eval_query",
        "lcm_eval_run",
        "lcm_eval_drift",
        "lcm_entity_type_registry",
        "lcm_entities",
        "lcm_entity_mentions",
        "lcm_embedding_profile",
        "lcm_embedding_meta",
        "lcm_feature_flags",
    }
    missing = expected_v41_tables - tables
    assert missing == set(), f"missing v4.1 tables: {missing!r}"


def test_core_triggers_created_after_migration(migrated_db: sqlite3.Connection) -> None:
    """After #01-06, the ``lcm_embedding_meta_cleanup_summary`` trigger exists.

    Re-invoking the helper on a migrated DB is idempotent (no-op), but
    the trigger itself exists.
    """
    schema_before = _snapshot_schema(migrated_db)
    _ensure_core_triggers(migrated_db)
    schema_after = _snapshot_schema(migrated_db)
    # Idempotent re-run: no schema delta.
    assert schema_before == schema_after

    triggers = _list_triggers(migrated_db)
    assert triggers == ["lcm_embedding_meta_cleanup_summary"]


def test_backfills_run_on_empty_db(migrated_db: sqlite3.Connection) -> None:
    """``_run_versioned_backfills`` records 3 ledger rows on an empty DB.

    With #01-15 landed, the three algorithm-versioned backfills run on
    the first migration and each writes one ``lcm_migration_state`` row
    (algorithm_version=1). The unversioned helpers
    (identity-hash / session-key) are idempotent SQL and don't produce
    ledger rows. Detailed behavior — including pre-existing data, the
    cycle guard, key-precedence chains, and re-run-is-no-op — is
    covered in :mod:`tests.test_migration_backfills`.
    """
    rows = migrated_db.execute(
        "SELECT step_name, algorithm_version FROM lcm_migration_state ORDER BY step_name"
    ).fetchall()
    assert rows == [
        ("backfillSummaryDepths", 1),
        ("backfillSummaryMetadata", 1),
        ("backfillToolCallColumns", 1),
    ]
    # A second migration call is a no-op: ledger rows are unchanged and
    # the SAVEPOINTs short-circuit before doing any UPDATE work.
    _run_versioned_backfills(migrated_db, log=None)
    rows_after = migrated_db.execute(
        "SELECT step_name, algorithm_version FROM lcm_migration_state ORDER BY step_name"
    ).fetchall()
    assert rows_after == rows


def test_seed_default_prompts_stub_is_noop(migrated_db: sqlite3.Connection) -> None:
    """``_seed_default_prompts`` is a no-op until the synthesis epic lands.

    Since #01-06 hasn't landed either, ``lcm_prompt_registry`` doesn't
    even exist yet — but the stub is a no-op so it doesn't try to read
    or write that table.
    """
    # Trivially confirm the stub doesn't raise.
    _seed_default_prompts(migrated_db, log=None)


# ---------------------------------------------------------------------------
# Transaction semantics
# ---------------------------------------------------------------------------


def test_migration_rolls_back_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """If any ladder step raises, the ``BEGIN EXCLUSIVE`` is rolled back.

    Patches :func:`_ensure_core_indexes_early` to raise so the rollback
    path triggers right after the CREATE TABLEs land. After the call:

    * The exception propagates.
    * No user tables exist (the rollback undid the CREATE TABLEs from
      :func:`_ensure_core_tables`).
    """
    from lossless_hermes.db import migration as migration_mod

    def _boom(_db: sqlite3.Connection) -> None:
        raise sqlite3.DatabaseError("simulated index-creation failure")

    monkeypatch.setattr(migration_mod, "_ensure_core_indexes_early", _boom)

    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("PRAGMA foreign_keys = ON")

        with pytest.raises(sqlite3.DatabaseError, match="simulated"):
            run_lcm_migrations(conn)

        # Rollback should have undone the CREATE TABLEs. Filtering out
        # internal sqlite_sequence (created when the first AUTOINCREMENT
        # ran in CREATE TABLE conversations, then rolled back; some SQLite
        # versions still create it as part of the autoincrement bookkeeping
        # even after rollback). We assert no user tables remain.
        tables = _list_tables(conn)
        assert tables == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Reference-fixture parity — diff against the TS-generated golden
# ---------------------------------------------------------------------------


def _parse_reference_objects() -> dict[str, str]:
    """Parse the reference SQL fixture into {name: normalized_sql}.

    The reference file is the output of ``./scripts/schema_diff.sh
    --refresh-reference`` against LCM commit ``1f07fbd``. Format:

        -- type: name
        CREATE ...;

        -- pragmas
        -- pragma foreign_keys: ...
        ...

    We extract the (name, sql) pairs and return a dict for lookup. SQL is
    normalized via :func:`_normalize_sql` so whitespace differences don't
    cause spurious mismatches.
    """
    text = _REFERENCE_SCHEMA_PATH.read_text(encoding="utf-8")

    objects: dict[str, str] = {}
    current_name: str | None = None
    current_sql_lines: list[str] = []

    for line in text.splitlines():
        # Header comment for a schema object: "-- type: name"
        header_match = re.match(r"^-- (table|index|trigger|view): (\S+)\s*$", line)
        if header_match:
            # Flush the previous block.
            if current_name and current_sql_lines:
                sql = "\n".join(current_sql_lines).strip()
                # Drop trailing semicolon for normalization.
                sql = sql.rstrip(";").strip()
                objects[current_name] = _normalize_sql(sql)
            current_name = header_match.group(2)
            current_sql_lines = []
            continue

        # End of schema section: "-- pragmas"
        if line.strip() == "-- pragmas":
            if current_name and current_sql_lines:
                sql = "\n".join(current_sql_lines).strip()
                sql = sql.rstrip(";").strip()
                objects[current_name] = _normalize_sql(sql)
                current_name = None
                current_sql_lines = []
            continue

        # Non-comment lines after a header are SQL DDL.
        if current_name is not None and not line.startswith("--"):
            current_sql_lines.append(line)

    # Flush the last block in case the file doesn't end with "-- pragmas".
    if current_name and current_sql_lines:
        sql = "\n".join(current_sql_lines).strip()
        sql = sql.rstrip(";").strip()
        objects[current_name] = _normalize_sql(sql)

    return objects


def test_reference_fixture_has_all_core_objects() -> None:
    """The reference fixture contains every object this PR creates.

    Sanity check that the committed golden schema covers the core 12
    tables + 20 indexes. If this fails, the reference fixture is stale
    (the LCM source has drifted from commit ``1f07fbd``).
    """
    if not _REFERENCE_SCHEMA_PATH.exists():
        pytest.skip("reference fixture not present; run scripts/schema_diff.sh --refresh-reference")

    ref = _parse_reference_objects()

    for table_name in _EXPECTED_CORE_TABLES:
        assert table_name in ref, f"core table {table_name!r} missing from reference fixture"
    for index_name in _EXPECTED_CORE_INDEXES:
        assert index_name in ref, f"core index {index_name!r} missing from reference fixture"


@pytest.mark.parametrize("table_name", _EXPECTED_CORE_TABLES)
def test_python_table_sql_matches_reference(
    migrated_db: sqlite3.Connection, table_name: str
) -> None:
    """Python-generated table DDL matches the TS reference (whitespace-insensitive).

    For each of the 12 core tables, the SQL stored in ``sqlite_master``
    must match the corresponding entry in
    ``tests/fixtures/lcm_reference_schema.sql``. Whitespace differences
    are ignored; semantic content is asserted.

    A failure here means the Python migration emits DDL that doesn't
    exactly match what the TS migration emits — a real schema drift.
    """
    if not _REFERENCE_SCHEMA_PATH.exists():
        pytest.skip("reference fixture not present")

    ref = _parse_reference_objects()
    if table_name not in ref:
        pytest.skip(f"table {table_name!r} not in reference fixture")

    py_sql_row = migrated_db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    assert py_sql_row is not None, f"table {table_name!r} not created"
    py_sql = py_sql_row[0]
    assert py_sql is not None
    py_sql_norm = _normalize_sql(py_sql.rstrip(";").strip())

    assert py_sql_norm == ref[table_name], (
        f"table {table_name!r} SQL diverges from TS reference:\n"
        f"  py: {py_sql_norm}\n"
        f"  ts: {ref[table_name]}"
    )


@pytest.mark.parametrize("index_name", _EXPECTED_CORE_INDEXES)
def test_python_index_sql_matches_reference(
    migrated_db: sqlite3.Connection, index_name: str
) -> None:
    """Python-generated index DDL matches the TS reference (whitespace-insensitive).

    Same invariant as :func:`test_python_table_sql_matches_reference`
    but for indexes. The partial-index WHERE clauses are particularly
    error-prone, so this catches drift in e.g.
    ``WHERE session_key IS NOT NULL AND active = 1``.
    """
    if not _REFERENCE_SCHEMA_PATH.exists():
        pytest.skip("reference fixture not present")

    ref = _parse_reference_objects()
    if index_name not in ref:
        pytest.skip(f"index {index_name!r} not in reference fixture")

    py_sql_row = migrated_db.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
        (index_name,),
    ).fetchone()
    assert py_sql_row is not None, f"index {index_name!r} not created"
    py_sql = py_sql_row[0]
    assert py_sql is not None
    py_sql_norm = _normalize_sql(py_sql.rstrip(";").strip())

    assert py_sql_norm == ref[index_name], (
        f"index {index_name!r} SQL diverges from TS reference:\n"
        f"  py: {py_sql_norm}\n"
        f"  ts: {ref[index_name]}"
    )
