"""LCM schema migration ladder.

Ports ``lossless-claw/src/db/migration.ts`` (commit ``1f07fbd``, ~2,037 LOC)
to Python. This module owns the **schema source of truth** for ``lcm.db``;
:func:`run_lcm_migrations` is the single sanctioned entry point per ADR-024
and ADR-026.

### What this module ships

Issues ``#01-04`` and ``#01-05`` (the core schema + FTS5):

* **12 always-on tables** (#01-04) — ``conversations``, ``messages``,
  ``message_parts``, ``summaries``, ``summary_messages``,
  ``summary_parents``, ``context_items``, ``large_files``,
  ``conversation_bootstrap_state``,
  ``conversation_compaction_telemetry``,
  ``conversation_compaction_maintenance``, ``lcm_migration_state``.
* **20 core indexes** (#01-04) — covering FK lookups, conversation/session
  scope filters, the partial UNIQUE on active session_key, the v4.1
  suppression / contains_suppressed / session_key_kind_latest indexes, and
  the v4.1 conversations_session_key_v41 partial index.
* **3 FTS5 virtual tables** (#01-05) — ``messages_fts`` (porter unicode61),
  ``summaries_fts`` (porter unicode61), ``summaries_fts_cjk`` (trigram,
  gated on the trigram-tokenizer feature probe). Population is handled by
  the application layer (#01-08 / #01-09); this module only creates the
  tables and seeds them from the existing ``messages`` / ``summaries``
  rows on first run.

### Section-stub pattern (ADR-027 analogue)

The orchestrator is split into seven section helpers so issues #01-05 / #01-06 /
#01-15 / synthesis can land in parallel without touching each other's regions:

* :func:`_ensure_core_tables` — body lives here (this PR).
* :func:`_ensure_core_indexes` — body lives here (this PR).
* :func:`_ensure_fts5_tables` — body landed in #01-05 (3 FTS5 virtual tables:
  ``messages_fts``, ``summaries_fts``, ``summaries_fts_cjk`` (gated on the
  trigram tokenizer being available)).
* :func:`_ensure_v41_tables` — **stub** in this PR; body lands in #01-06 (the 13
  v4.1 tables: ``lcm_worker_lock``, ``lcm_extraction_queue``,
  ``lcm_session_key_audit``, ``lcm_prompt_registry``, ``lcm_synthesis_cache``,
  ``lcm_cache_leaf_refs``, ``lcm_synthesis_audit``, 4 eval tables, 3 entity
  tables, 2 embedding registry tables, ``lcm_feature_flags`` + their indexes +
  the polymorphic-cleanup trigger).
* :func:`_ensure_core_triggers` — **stub** in this PR; body lands alongside
  #01-06 because the only core trigger (``lcm_embedding_meta_cleanup_summary``)
  references ``lcm_embedding_meta`` which is created in #01-06. SQLite accepts
  ``CREATE TRIGGER`` referencing a missing table at DDL parse time but fires on
  delete fail at runtime — so deferring the body keeps every interim state
  usable.
* :func:`_run_versioned_backfills` — **stub** in this PR; body lands in #01-15
  (``backfillSummaryDepths`` / ``backfillSummaryMetadata`` /
  ``backfillToolCallColumns``, each gated by ``lcm_migration_state``).
* :func:`_seed_default_prompts` — **stub** in this PR; body lands alongside the
  synthesis epic (depends on ``lcm_prompt_registry`` from #01-06).

### Idempotency invariant (ADR-026 §"Structural state")

Every ``CREATE TABLE`` uses ``IF NOT EXISTS``. Every ``CREATE INDEX`` uses
``IF NOT EXISTS``. Every ``ALTER TABLE ADD COLUMN`` is guarded by a
``PRAGMA table_info`` probe. Re-running :func:`run_lcm_migrations` against an
already-migrated DB is a no-op — verified by ``test_migration_core.py``
``test_idempotency_second_run_no_op``.

### Concurrent migration invariant

The entire ladder is wrapped in ``BEGIN EXCLUSIVE`` per
``docs/porting-guides/storage.md`` §10 and ADR-026 §Open Questions item 2. Two
processes calling :func:`run_lcm_migrations` against the same file-backed DB
simultaneously serialize through SQLite's write lock — the second blocks on
``busy_timeout`` (30 s; see ``db/connection.py``) and then sees the
already-applied schema, making its run a no-op.

### Wave-N provenance (ADR-029)

The TS source carries a handful of Wave-N comments inside the v4.1 section
(Wave-1/3/10 cache-CHECK widening, prompt-registry COALESCE UNIQUE). Those
comments belong to the #01-06 body and ship with that PR. The core scope in
THIS PR has **no Wave-N markers** in the TS source — the core schema landed
pre-Wave-1.

See:

* ADR-024 — project layout (this module's home).
* ADR-026 — schema versioning (structural + algorithm-version split).
* ADR-027 — engine splitting (section-helper pattern analogue).
* ADR-029 — Wave-N provenance.
* ``docs/porting-guides/storage.md`` §2.1 — full table/index inventory.
* ``tests/fixtures/lcm_reference_schema.sql`` — golden schema from LCM
  ``1f07fbd``; ``./scripts/schema_diff.sh --verify`` diffs against it.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Iterable, Protocol

from lossless_hermes.db.features import get_lcm_db_features

__all__ = [
    "MigrationLogger",
    "run_lcm_migrations",
]

_log = logging.getLogger("lossless_hermes.db.migration")


# ---------------------------------------------------------------------------
# MigrationLogger protocol (ports TS `MigrationLogger` shape)
# ---------------------------------------------------------------------------


class MigrationLogger(Protocol):
    """Optional logger sink for per-step migration progress.

    Mirrors TS ``MigrationLogger = { info?: (message: string) => void }``
    in ``migration.ts:7-9``. Implementers can supply a callable to receive
    one ``info`` line per step; production callers leave ``log=None`` and
    let the :mod:`logging` module handle it.

    The TS shape uses an optional ``info`` method. Python's :class:`Protocol`
    treats ``info`` as required; callers that want a no-op can pass
    ``logging.getLogger(...).info`` (a bound method matches the protocol)
    or instantiate :class:`logging.Logger` directly.
    """

    def info(self, message: str) -> None: ...  # pragma: no cover - protocol


# ---------------------------------------------------------------------------
# SQL constants — core tables (per storage.md §2.1 and migration.ts:917-1086)
# ---------------------------------------------------------------------------
#
# Layout notes (load-bearing):
# * Each CREATE TABLE is its own string constant so future schema audits can
#   ``grep -A30 "^_SQL_TABLE_<name> ="`` and read one table at a time.
# * Whitespace inside the string matches ``migration.ts`` byte-for-byte
#   (modulo Python r-string conventions); this minimizes schema-diff noise
#   when ``./scripts/schema_diff.sh --verify`` compares ``sqlite_master.sql``.
# * IF NOT EXISTS is mandatory per ADR-026 §"Structural state".


_SQL_TABLE_CONVERSATIONS = """
    CREATE TABLE IF NOT EXISTS conversations (
      conversation_id INTEGER PRIMARY KEY AUTOINCREMENT,
      session_id TEXT NOT NULL,
      session_key TEXT,
      active INTEGER NOT NULL DEFAULT 1,
      archived_at TEXT,
      title TEXT,
      bootstrapped_at TEXT,
      created_at TEXT NOT NULL DEFAULT (datetime('now')),
      updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
"""

_SQL_TABLE_MESSAGES = """
    CREATE TABLE IF NOT EXISTS messages (
      message_id INTEGER PRIMARY KEY AUTOINCREMENT,
      conversation_id INTEGER NOT NULL REFERENCES conversations(conversation_id) ON DELETE CASCADE,
      seq INTEGER NOT NULL,
      role TEXT NOT NULL CHECK (role IN ('system', 'user', 'assistant', 'tool')),
      content TEXT NOT NULL,
      token_count INTEGER NOT NULL,
      identity_hash TEXT,
      created_at TEXT NOT NULL DEFAULT (datetime('now')),
      UNIQUE (conversation_id, seq)
    )
"""

_SQL_TABLE_SUMMARIES = """
    CREATE TABLE IF NOT EXISTS summaries (
      summary_id TEXT PRIMARY KEY,
      conversation_id INTEGER NOT NULL REFERENCES conversations(conversation_id) ON DELETE CASCADE,
      kind TEXT NOT NULL CHECK (kind IN ('leaf', 'condensed')),
      depth INTEGER NOT NULL DEFAULT 0,
      content TEXT NOT NULL,
      token_count INTEGER NOT NULL,
      earliest_at TEXT,
      latest_at TEXT,
      descendant_count INTEGER NOT NULL DEFAULT 0,
      descendant_token_count INTEGER NOT NULL DEFAULT 0,
      source_message_token_count INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL DEFAULT (datetime('now')),
      file_ids TEXT NOT NULL DEFAULT '[]'
    )
"""

# Note: `message_parts` carries 25 sparse columns + 12-value CHECK on part_type.
# Inline comment-free for byte-equivalence to TS source string.
_SQL_TABLE_MESSAGE_PARTS = """
    CREATE TABLE IF NOT EXISTS message_parts (
      part_id TEXT PRIMARY KEY,
      message_id INTEGER NOT NULL REFERENCES messages(message_id) ON DELETE CASCADE,
      session_id TEXT NOT NULL,
      part_type TEXT NOT NULL CHECK (part_type IN (
        'text', 'reasoning', 'tool', 'patch', 'file',
        'subtask', 'compaction', 'step_start', 'step_finish',
        'snapshot', 'agent', 'retry'
      )),
      ordinal INTEGER NOT NULL,
      text_content TEXT,
      is_ignored INTEGER,
      is_synthetic INTEGER,
      tool_call_id TEXT,
      tool_name TEXT,
      tool_status TEXT,
      tool_input TEXT,
      tool_output TEXT,
      tool_error TEXT,
      tool_title TEXT,
      patch_hash TEXT,
      patch_files TEXT,
      file_mime TEXT,
      file_name TEXT,
      file_url TEXT,
      subtask_prompt TEXT,
      subtask_desc TEXT,
      subtask_agent TEXT,
      step_reason TEXT,
      step_cost REAL,
      step_tokens_in INTEGER,
      step_tokens_out INTEGER,
      snapshot_hash TEXT,
      compaction_auto INTEGER,
      metadata TEXT,
      UNIQUE (message_id, ordinal)
    )
"""

# summary_messages: ON DELETE RESTRICT on message_id (NOT cascade). Per
# storage.md §2.1 row "summary_messages" — restrict prevents accidental
# message deletion that would orphan the leaf's source-message linkage.
_SQL_TABLE_SUMMARY_MESSAGES = """
    CREATE TABLE IF NOT EXISTS summary_messages (
      summary_id TEXT NOT NULL REFERENCES summaries(summary_id) ON DELETE CASCADE,
      message_id INTEGER NOT NULL REFERENCES messages(message_id) ON DELETE RESTRICT,
      ordinal INTEGER NOT NULL,
      PRIMARY KEY (summary_id, message_id)
    )
"""

_SQL_TABLE_SUMMARY_PARENTS = """
    CREATE TABLE IF NOT EXISTS summary_parents (
      summary_id TEXT NOT NULL REFERENCES summaries(summary_id) ON DELETE CASCADE,
      parent_summary_id TEXT NOT NULL REFERENCES summaries(summary_id) ON DELETE RESTRICT,
      ordinal INTEGER NOT NULL,
      PRIMARY KEY (summary_id, parent_summary_id)
    )
"""

_SQL_TABLE_CONTEXT_ITEMS = """
    CREATE TABLE IF NOT EXISTS context_items (
      conversation_id INTEGER NOT NULL REFERENCES conversations(conversation_id) ON DELETE CASCADE,
      ordinal INTEGER NOT NULL,
      item_type TEXT NOT NULL CHECK (item_type IN ('message', 'summary')),
      message_id INTEGER REFERENCES messages(message_id) ON DELETE RESTRICT,
      summary_id TEXT REFERENCES summaries(summary_id) ON DELETE RESTRICT,
      created_at TEXT NOT NULL DEFAULT (datetime('now')),
      PRIMARY KEY (conversation_id, ordinal),
      CHECK (
        (item_type = 'message' AND message_id IS NOT NULL AND summary_id IS NULL) OR
        (item_type = 'summary' AND summary_id IS NOT NULL AND message_id IS NULL)
      )
    )
"""

_SQL_TABLE_LARGE_FILES = """
    CREATE TABLE IF NOT EXISTS large_files (
      file_id TEXT PRIMARY KEY,
      conversation_id INTEGER NOT NULL REFERENCES conversations(conversation_id) ON DELETE CASCADE,
      file_name TEXT,
      mime_type TEXT,
      byte_size INTEGER,
      storage_uri TEXT NOT NULL,
      exploration_summary TEXT,
      created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
"""

_SQL_TABLE_CONVERSATION_BOOTSTRAP_STATE = """
    CREATE TABLE IF NOT EXISTS conversation_bootstrap_state (
      conversation_id INTEGER PRIMARY KEY REFERENCES conversations(conversation_id) ON DELETE CASCADE,
      session_file_path TEXT NOT NULL,
      last_seen_size INTEGER NOT NULL,
      last_seen_mtime_ms INTEGER NOT NULL,
      last_processed_offset INTEGER NOT NULL,
      last_processed_entry_hash TEXT,
      updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
"""

_SQL_TABLE_CONVERSATION_COMPACTION_TELEMETRY = """
    CREATE TABLE IF NOT EXISTS conversation_compaction_telemetry (
      conversation_id INTEGER PRIMARY KEY REFERENCES conversations(conversation_id) ON DELETE CASCADE,
      last_observed_cache_read INTEGER,
      last_observed_cache_write INTEGER,
      last_observed_prompt_token_count INTEGER,
      last_observed_cache_hit_at TEXT,
      last_observed_cache_break_at TEXT,
      cache_state TEXT NOT NULL DEFAULT 'unknown'
        CHECK (cache_state IN ('hot', 'cold', 'unknown')),
      consecutive_cold_observations INTEGER NOT NULL DEFAULT 0,
      retention TEXT,
      last_leaf_compaction_at TEXT,
      turns_since_leaf_compaction INTEGER NOT NULL DEFAULT 0,
      tokens_accumulated_since_leaf_compaction INTEGER NOT NULL DEFAULT 0,
      last_activity_band TEXT NOT NULL DEFAULT 'low'
        CHECK (last_activity_band IN ('low', 'medium', 'high')),
      last_api_call_at TEXT,
      last_cache_touch_at TEXT,
      provider TEXT,
      model TEXT,
      updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
"""

_SQL_TABLE_CONVERSATION_COMPACTION_MAINTENANCE = """
    CREATE TABLE IF NOT EXISTS conversation_compaction_maintenance (
      conversation_id INTEGER PRIMARY KEY REFERENCES conversations(conversation_id) ON DELETE CASCADE,
      pending INTEGER NOT NULL DEFAULT 0,
      requested_at TEXT,
      reason TEXT,
      running INTEGER NOT NULL DEFAULT 0,
      last_started_at TEXT,
      last_finished_at TEXT,
      last_failure_summary TEXT,
      token_budget INTEGER,
      current_token_count INTEGER,
      updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
"""

# lcm_migration_state: the algorithm-version ledger (ADR-026 §"Algorithm-
# versioned state"). Created here so #01-15's backfill ladder can record
# completion immediately.
_SQL_TABLE_LCM_MIGRATION_STATE = """
    CREATE TABLE IF NOT EXISTS lcm_migration_state (
      step_name TEXT NOT NULL,
      algorithm_version INTEGER NOT NULL,
      completed_at TEXT NOT NULL DEFAULT (datetime('now')),
      PRIMARY KEY (step_name, algorithm_version)
    )
"""

# Tuple of (constant_name, sql) pairs in the order TS migration.ts creates them.
# Iteration order matters: messages must precede summary_messages (FK target);
# summaries must precede summary_parents; conversations must precede all
# tables that FK back to it; lcm_migration_state has no inbound FKs so it
# can be last.
_CORE_TABLE_CREATIONS: tuple[tuple[str, str], ...] = (
    ("conversations", _SQL_TABLE_CONVERSATIONS),
    ("messages", _SQL_TABLE_MESSAGES),
    ("summaries", _SQL_TABLE_SUMMARIES),
    ("message_parts", _SQL_TABLE_MESSAGE_PARTS),
    ("summary_messages", _SQL_TABLE_SUMMARY_MESSAGES),
    ("summary_parents", _SQL_TABLE_SUMMARY_PARENTS),
    ("context_items", _SQL_TABLE_CONTEXT_ITEMS),
    ("large_files", _SQL_TABLE_LARGE_FILES),
    ("conversation_bootstrap_state", _SQL_TABLE_CONVERSATION_BOOTSTRAP_STATE),
    ("conversation_compaction_telemetry", _SQL_TABLE_CONVERSATION_COMPACTION_TELEMETRY),
    ("conversation_compaction_maintenance", _SQL_TABLE_CONVERSATION_COMPACTION_MAINTENANCE),
    ("lcm_migration_state", _SQL_TABLE_LCM_MIGRATION_STATE),
)


# ---------------------------------------------------------------------------
# SQL constants — core indexes
# ---------------------------------------------------------------------------
#
# 20 indexes covering:
# * FK-target speedups (conv_seq, message_id, parent_summary_id, etc.).
# * Conversation/session scope (the three conversations_* indexes).
# * Partial UNIQUE on active session_key (the v3.1 cross-conv identity).
# * v4.1 partial indexes (suppressed_at, contains_suppressed_leaves, etc.).
#
# Indexes for FTS5 / v4.1 / synthesis / eval / entity / embedding tables go
# in #01-05 / #01-06 stub bodies.


# Phase-1 indexes: created early in the ladder (before the structural-column
# probes). These indexes reference columns that exist from the bulk CREATE
# TABLE block regardless of whether the DB is fresh or imported-from-OpenClaw.
_CORE_INDEX_CREATIONS_EARLY: tuple[str, ...] = (
    # Bulk-block indexes (migration.ts:1089-1103) — FK-target speedups plus
    # the always-on conversation/summary/message-part lookups.
    "CREATE INDEX IF NOT EXISTS messages_conv_seq_idx ON messages (conversation_id, seq)",
    "CREATE INDEX IF NOT EXISTS summaries_conv_created_idx ON summaries (conversation_id, created_at)",
    "CREATE INDEX IF NOT EXISTS summary_messages_message_idx ON summary_messages (message_id)",
    "CREATE INDEX IF NOT EXISTS summary_parents_parent_summary_idx ON summary_parents (parent_summary_id)",
    "CREATE INDEX IF NOT EXISTS message_parts_message_idx ON message_parts (message_id)",
    "CREATE INDEX IF NOT EXISTS message_parts_type_idx ON message_parts (part_type)",
    "CREATE INDEX IF NOT EXISTS context_items_conv_idx ON context_items (conversation_id, ordinal)",
    "CREATE INDEX IF NOT EXISTS large_files_conv_idx ON large_files (conversation_id, created_at)",
    """CREATE INDEX IF NOT EXISTS bootstrap_state_path_idx
      ON conversation_bootstrap_state (session_file_path, updated_at)""",
    """CREATE INDEX IF NOT EXISTS compaction_telemetry_state_idx
      ON conversation_compaction_telemetry (cache_state, updated_at)""",
)

# Phase-2 indexes: created AFTER the structural-column probes
# (`_apply_structural_column_probes`). These reference columns that may have
# been added by ALTERs on imported-from-OpenClaw DBs (e.g. `depth`,
# `session_key`, `suppressed_at`, `contains_suppressed_leaves`,
# `superseded_by`, `identity_hash`).
_CORE_INDEX_CREATIONS_LATE: tuple[str, ...] = (
    # conversations indexes (migration.ts:1131-1143) — the partial UNIQUE
    # replaces the obsolete global `conversations_session_key_idx`, which is
    # dropped explicitly in `_drop_legacy_conversation_session_key_index`.
    # `active` and `session_key` columns are added by
    # `_ensure_conversation_columns` if not present.
    """CREATE UNIQUE INDEX IF NOT EXISTS conversations_active_session_key_idx
      ON conversations (session_key)
      WHERE session_key IS NOT NULL AND active = 1""",
    """CREATE INDEX IF NOT EXISTS conversations_session_key_active_created_idx
      ON conversations (session_key, active, created_at)""",
    """CREATE INDEX IF NOT EXISTS conversations_session_id_active_created_idx
      ON conversations (session_id, active, created_at)""",
    # messages_conv_identity_hash_idx (migration.ts:1159-1163) — references
    # `identity_hash` column, added by `_ensure_message_identity_hash_column`
    # if not present.
    "CREATE INDEX IF NOT EXISTS messages_conv_identity_hash_idx ON messages (conversation_id, identity_hash)",
    # summaries_conv_depth_kind_idx (migration.ts:1170-1174) — references
    # `depth` column, added by `_ensure_summary_depth_column` if not present.
    "CREATE INDEX IF NOT EXISTS summaries_conv_depth_kind_idx ON summaries (conversation_id, depth, kind)",
    # v4.1 summary/message indexes (migration.ts:1986-2022) — reference v4.1
    # columns (session_key, suppressed_at, contains_suppressed_leaves,
    # superseded_by) added by `_ensure_summary_v41_columns` /
    # `_ensure_message_suppressed_at_column` if not present.
    """CREATE INDEX IF NOT EXISTS summaries_session_key_kind_latest_idx
      ON summaries (session_key, kind, latest_at DESC)
      WHERE session_key != ''""",
    """CREATE INDEX IF NOT EXISTS summaries_suppressed_idx
      ON summaries (suppressed_at)
      WHERE suppressed_at IS NOT NULL""",
    """CREATE INDEX IF NOT EXISTS summaries_contains_suppressed_idx
      ON summaries (contains_suppressed_leaves)
      WHERE contains_suppressed_leaves = 1 AND superseded_by IS NULL""",
    """CREATE INDEX IF NOT EXISTS messages_suppressed_idx
      ON messages (suppressed_at)
      WHERE suppressed_at IS NOT NULL""",
    """CREATE INDEX IF NOT EXISTS conversations_session_key_v41_idx
      ON conversations (session_key)
      WHERE session_key IS NOT NULL""",
)

# Combined index inventory — used by `list_core_index_names` and the
# `_iter_core_object_names` iterator. Iteration order matches the actual
# CREATE order in `run_lcm_migrations` so tests can assert deterministic
# state.
_CORE_INDEX_CREATIONS: tuple[str, ...] = (
    *_CORE_INDEX_CREATIONS_EARLY,
    *_CORE_INDEX_CREATIONS_LATE,
)


# ---------------------------------------------------------------------------
# Section helpers — bodies + stubs (per architectural hint)
# ---------------------------------------------------------------------------


def _ensure_core_tables(db: sqlite3.Connection) -> None:
    """Create the 12 always-on core tables.

    Ports the bulk-block ``db.exec()`` in ``migration.ts:916-1086`` (12 of
    the 25 tables in that block; the remaining 13 are v4.1 additions handled
    by :func:`_ensure_v41_tables` in #01-06).

    Idempotent via ``IF NOT EXISTS`` on every CREATE. Re-running on an
    already-migrated DB is a no-op.

    The TS source executes all 12 CREATEs inside one ``db.exec()`` string —
    Node's ``node:sqlite`` parses + runs as a multi-statement batch. Python's
    :meth:`sqlite3.Connection.executescript` would work the same way, but
    we use a per-table loop instead because:

    1. **Belt-and-suspenders for the message_parts CREATE.** TS's
       ``ensureMessagePartsTable`` (storage.md §2.1 last note) handles
       ``node:sqlite`` pre-v22.12 silently aborting the bulk block on
       constraint errors. Python's :meth:`executescript` raises on the
       first failure (no silent partial-success), so we don't *need* the
       belt-and-suspenders separate re-create. But running per-table makes
       the error message ("ConstraintError on table_name X") strictly
       more actionable than "executescript failed at byte 1842".
    2. **Schema-diff stability.** ``sqlite_master.sql`` stores each table's
       CREATE statement separately regardless of how it was originally
       executed; per-table loop matches that storage format and minimizes
       byte-noise in the diff.

    Args:
        db: An open :class:`sqlite3.Connection` already inside the
            ``BEGIN EXCLUSIVE`` from :func:`run_lcm_migrations`.
    """
    for table_name, sql in _CORE_TABLE_CREATIONS:
        try:
            db.execute(sql)
        except sqlite3.DatabaseError as exc:
            # Re-raise with the table name attached so failures during the
            # ladder identify which CREATE blew up. TS source's runMigrationStep
            # wraps each step with its name in the log line; this mirrors that
            # behavior for the bulk core-table block.
            raise sqlite3.DatabaseError(
                f"_ensure_core_tables: failed to create table {table_name!r}: {exc}"
            ) from exc


def _ensure_core_indexes_early(db: sqlite3.Connection) -> None:
    """Create the 10 phase-1 core indexes.

    Ports the bulk-block index creates in ``migration.ts:1089-1103``.
    These indexes reference columns that exist regardless of whether the
    DB is fresh or imported-from-OpenClaw (every column is in the v0
    schema). They're created **before** :func:`_apply_structural_column_probes`
    runs because they don't depend on any ALTER-added column.

    All indexes use ``IF NOT EXISTS`` so re-runs are no-ops.

    Args:
        db: An open :class:`sqlite3.Connection` already inside the
            ``BEGIN EXCLUSIVE`` from :func:`run_lcm_migrations`.
    """
    for sql in _CORE_INDEX_CREATIONS_EARLY:
        db.execute(sql)


def _ensure_core_indexes_late(db: sqlite3.Connection) -> None:
    """Create the 10 phase-2 core indexes that depend on ALTER-added columns.

    Ports:

    * ``migration.ts:1131-1143`` — the conversations index trio
      (``conversations_active_session_key_idx``,
      ``conversations_session_key_active_created_idx``,
      ``conversations_session_id_active_created_idx``).
    * ``migration.ts:1159-1163`` — ``messages_conv_identity_hash_idx``,
      referencing ``identity_hash`` (added by
      :func:`_ensure_message_identity_hash_column`).
    * ``migration.ts:1170-1174`` — ``summaries_conv_depth_kind_idx``,
      referencing ``depth`` (added by :func:`_ensure_summary_depth_column`).
    * ``migration.ts:1986-2022`` — the 5 v4.1 partial indexes on
      summaries.session_key / summaries.suppressed_at /
      summaries.contains_suppressed_leaves / messages.suppressed_at /
      conversations.session_key.

    Caller must invoke :func:`_apply_structural_column_probes` before
    this so any imported-DB schemas have the referenced columns. All
    indexes use ``IF NOT EXISTS``.

    Args:
        db: An open :class:`sqlite3.Connection` already inside the
            ``BEGIN EXCLUSIVE`` from :func:`run_lcm_migrations`.
    """
    for sql in _CORE_INDEX_CREATIONS_LATE:
        db.execute(sql)


def _ensure_core_indexes(db: sqlite3.Connection) -> None:
    """Backwards-compat alias: create all 20 core indexes in one call.

    Equivalent to running :func:`_ensure_core_indexes_early` then
    :func:`_ensure_core_indexes_late`. Useful for tests that want to
    assert "exactly the 20 core indexes exist" against a fresh DB
    without manually running both phases.

    Args:
        db: An open :class:`sqlite3.Connection` already inside the
            ``BEGIN EXCLUSIVE`` from :func:`run_lcm_migrations`.
    """
    _ensure_core_indexes_early(db)
    _ensure_core_indexes_late(db)


def _drop_legacy_conversation_session_key_index(db: sqlite3.Connection) -> None:
    """Drop the obsolete global UNIQUE ``conversations_session_key_idx``.

    Ports ``migration.ts:1144``:

    .. code-block:: typescript

        db.exec(`DROP INDEX IF EXISTS conversations_session_key_idx`);

    The legacy index was a non-partial UNIQUE on ``(session_key)`` — too
    strict because the v4.1 design re-uses the same session_key across
    archived/active conversations of the same session-family. The partial
    UNIQUE ``conversations_active_session_key_idx`` (created in
    :func:`_ensure_core_indexes`) replaces it, scoped to
    ``WHERE session_key IS NOT NULL AND active = 1``.

    ``DROP INDEX IF EXISTS`` is idempotent — a no-op on a fresh DB where
    the legacy index never existed.

    Args:
        db: An open :class:`sqlite3.Connection` already inside the
            ``BEGIN EXCLUSIVE`` from :func:`run_lcm_migrations`.
    """
    db.execute("DROP INDEX IF EXISTS conversations_session_key_idx")


def _apply_structural_column_probes(db: sqlite3.Connection) -> None:
    """Apply ``ALTER TABLE ADD COLUMN`` for forward-compat additive columns.

    Ports the four ``ensureXColumn`` calls inside ``runLcmMigrations``
    (``migration.ts:1107-1166``) for the core table set:

    * :func:`_ensure_conversation_columns` — bootstrapped_at, session_key,
      active, archived_at (the four columns added across v0 → v4.1).
    * :func:`_ensure_summary_depth_column` — ``depth`` column (added in
      v3 when summary depth became explicit).
    * :func:`_ensure_summary_metadata_columns` — earliest_at, latest_at,
      descendant_count, descendant_token_count, source_message_token_count.
    * :func:`_ensure_summary_model_column` — ``model`` column (v4.1 wired
      to the per-summary model attribution).
    * :func:`_ensure_summary_v41_columns` — the 7 v4.1 summary columns
      (session_key, suppressed_at, entity_index, contains_suppressed_leaves,
      suppress_reason, superseded_by, leaf_summarizer_cap_was).
    * :func:`_ensure_message_identity_hash_column` — identity_hash.
    * :func:`_ensure_message_suppressed_at_column` — suppressed_at.
    * :func:`_ensure_compaction_telemetry_columns` — 10 v4.1 telemetry columns
      that were not in the v0 schema.

    On a fresh DB these probes are no-ops (every column already exists from
    :func:`_ensure_core_tables`). On an imported OpenClaw DB (per
    ADR-025 step 3) these probes catch up the schema.

    Args:
        db: An open :class:`sqlite3.Connection` already inside the
            ``BEGIN EXCLUSIVE`` from :func:`run_lcm_migrations`.
    """
    _ensure_conversation_columns(db)
    _ensure_summary_depth_column(db)
    _ensure_summary_metadata_columns(db)
    _ensure_summary_model_column(db)
    _ensure_summary_v41_columns(db)
    _ensure_message_identity_hash_column(db)
    _ensure_message_suppressed_at_column(db)
    _ensure_compaction_telemetry_columns(db)


# ---- Structural column probes (ports the TS `ensure*Column` family) -------


def _has_column(db: sqlite3.Connection, table: str, column: str) -> bool:
    """Return ``True`` iff ``column`` already exists on ``table``.

    Mirrors the TS pattern ``db.prepare('PRAGMA table_info(X)').all().some(...)``
    from ``migration.ts:62-67`` (and dozens of other similar guards).

    Args:
        db: An open :class:`sqlite3.Connection`.
        table: Table name (already exists; caller has ensured this).
        column: Column name to probe.

    Returns:
        ``True`` if the column is present, ``False`` otherwise.
    """
    # PRAGMA table_info returns rows of (cid, name, type, notnull, dflt_value, pk).
    rows = db.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


def _ensure_conversation_columns(db: sqlite3.Connection) -> None:
    """Forward-compat ALTERs for ``conversations`` columns.

    Ports ``migration.ts:1107-1130`` — adds ``bootstrapped_at``,
    ``session_key``, ``active``, ``archived_at`` if absent, then
    ``UPDATE conversations SET active = 1 WHERE active IS NULL`` to
    fill in the column on legacy rows that pre-date the active flag.

    Each ALTER is independently idempotent via :func:`_has_column`.

    Args:
        db: Open :class:`sqlite3.Connection` inside the migration txn.
    """
    if not _has_column(db, "conversations", "bootstrapped_at"):
        db.execute("ALTER TABLE conversations ADD COLUMN bootstrapped_at TEXT")
    if not _has_column(db, "conversations", "session_key"):
        db.execute("ALTER TABLE conversations ADD COLUMN session_key TEXT")
    if not _has_column(db, "conversations", "active"):
        db.execute("ALTER TABLE conversations ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
    if not _has_column(db, "conversations", "archived_at"):
        db.execute("ALTER TABLE conversations ADD COLUMN archived_at TEXT")
    # Fill-in: legacy rows without an `active` value get the v4.1 default.
    db.execute("UPDATE conversations SET active = 1 WHERE active IS NULL")


def _ensure_summary_depth_column(db: sqlite3.Connection) -> None:
    """Add ``depth`` column to ``summaries`` if absent.

    Ports ``migration.ts:62-68``.
    """
    if not _has_column(db, "summaries", "depth"):
        db.execute("ALTER TABLE summaries ADD COLUMN depth INTEGER NOT NULL DEFAULT 0")


def _ensure_summary_metadata_columns(db: sqlite3.Connection) -> None:
    """Add the 5 metadata columns to ``summaries`` if absent.

    Ports ``migration.ts:70-95``. Columns: ``earliest_at``, ``latest_at``,
    ``descendant_count``, ``descendant_token_count``,
    ``source_message_token_count``. The TS source probes once and applies
    each independently; mirror that pattern.
    """
    if not _has_column(db, "summaries", "earliest_at"):
        db.execute("ALTER TABLE summaries ADD COLUMN earliest_at TEXT")
    if not _has_column(db, "summaries", "latest_at"):
        db.execute("ALTER TABLE summaries ADD COLUMN latest_at TEXT")
    if not _has_column(db, "summaries", "descendant_count"):
        db.execute("ALTER TABLE summaries ADD COLUMN descendant_count INTEGER NOT NULL DEFAULT 0")
    if not _has_column(db, "summaries", "descendant_token_count"):
        db.execute(
            "ALTER TABLE summaries ADD COLUMN descendant_token_count INTEGER NOT NULL DEFAULT 0"
        )
    if not _has_column(db, "summaries", "source_message_token_count"):
        db.execute(
            "ALTER TABLE summaries ADD COLUMN source_message_token_count INTEGER NOT NULL DEFAULT 0"
        )


def _ensure_summary_model_column(db: sqlite3.Connection) -> None:
    """Add ``model`` column to ``summaries`` if absent.

    Ports ``migration.ts:105-111``. Tracks the per-summary model attribution
    introduced in v4.1; default ``'unknown'`` for legacy rows pre-v4.1.
    """
    if not _has_column(db, "summaries", "model"):
        db.execute("ALTER TABLE summaries ADD COLUMN model TEXT NOT NULL DEFAULT 'unknown'")


def _ensure_summary_v41_columns(db: sqlite3.Connection) -> None:
    """Add the 7 v4.1 columns to ``summaries`` if absent.

    Ports ``migration.ts:131-161``. Columns:

    * ``session_key`` — v3.1 A1 cross-conv identity.
    * ``suppressed_at`` — v3.1 A3 lossless-forget cascade target.
    * ``entity_index`` — v3.1 §7.2 entity coref JSON sidecar.
    * ``contains_suppressed_leaves`` — v3.1 A3 idle-rebuild marker.
    * ``suppress_reason`` — v4.1.1 A2 lcm_describe surface.
    * ``superseded_by`` — v4.1.1 A2 forwarder FK (SET NULL).
    * ``leaf_summarizer_cap_was`` — v4.1 2,415-token-cap forensic marker.

    SQLite's ADD COLUMN constraints (per TS comment at ``migration.ts:122-130``):
    no PRIMARY KEY / UNIQUE, no CURRENT_TIMESTAMP defaults, NOT NULL columns
    have non-NULL defaults, REFERENCES columns have NULL default.
    """
    if not _has_column(db, "summaries", "session_key"):
        db.execute("ALTER TABLE summaries ADD COLUMN session_key TEXT NOT NULL DEFAULT ''")
    if not _has_column(db, "summaries", "suppressed_at"):
        db.execute("ALTER TABLE summaries ADD COLUMN suppressed_at TEXT")
    if not _has_column(db, "summaries", "entity_index"):
        db.execute("ALTER TABLE summaries ADD COLUMN entity_index TEXT")
    if not _has_column(db, "summaries", "contains_suppressed_leaves"):
        db.execute(
            "ALTER TABLE summaries ADD COLUMN contains_suppressed_leaves INTEGER NOT NULL DEFAULT 0"
        )
    if not _has_column(db, "summaries", "suppress_reason"):
        db.execute("ALTER TABLE summaries ADD COLUMN suppress_reason TEXT")
    if not _has_column(db, "summaries", "superseded_by"):
        # FK with SET NULL on parent delete. SQLite ADD COLUMN with
        # REFERENCES requires a NULL default.
        db.execute(
            "ALTER TABLE summaries ADD COLUMN superseded_by TEXT "
            "REFERENCES summaries(summary_id) ON DELETE SET NULL"
        )
    if not _has_column(db, "summaries", "leaf_summarizer_cap_was"):
        db.execute("ALTER TABLE summaries ADD COLUMN leaf_summarizer_cap_was INTEGER")


def _ensure_message_identity_hash_column(db: sqlite3.Connection) -> None:
    """Add ``identity_hash`` column to ``messages`` if absent.

    Ports ``migration.ts:324-330``. Used by the dedup ingest path; backfilled
    by ``backfillMessageIdentityHashes`` (out of scope for this PR — lands
    in #01-15 as part of versioned backfills).
    """
    if not _has_column(db, "messages", "identity_hash"):
        db.execute("ALTER TABLE messages ADD COLUMN identity_hash TEXT")


def _ensure_message_suppressed_at_column(db: sqlite3.Connection) -> None:
    """Add ``suppressed_at`` column to ``messages`` if absent.

    Ports ``migration.ts:168-174``. v3.1 A3 (extended in v4.1.1 A3):
    suppression cascade reaches raw messages via this column. All message-
    search read paths filter on it.
    """
    if not _has_column(db, "messages", "suppressed_at"):
        db.execute("ALTER TABLE messages ADD COLUMN suppressed_at TEXT")


def _ensure_compaction_telemetry_columns(db: sqlite3.Connection) -> None:
    """Add the 10 v4.1 telemetry columns to ``conversation_compaction_telemetry``.

    Ports ``migration.ts:198-255``. On a fresh DB these columns exist from
    :func:`_ensure_core_tables`; on an imported old DB these add them.

    Columns added:

    * ``consecutive_cold_observations``, ``last_leaf_compaction_at``,
      ``turns_since_leaf_compaction``, ``tokens_accumulated_since_leaf_compaction``.
    * ``last_activity_band`` (NOT NULL DEFAULT 'low' + CHECK constraint).
    * ``last_api_call_at``, ``last_cache_touch_at``, ``provider``, ``model``.
    * ``last_observed_prompt_token_count``.
    """
    table = "conversation_compaction_telemetry"
    if not _has_column(db, table, "consecutive_cold_observations"):
        db.execute(
            f"ALTER TABLE {table} ADD COLUMN "
            "consecutive_cold_observations INTEGER NOT NULL DEFAULT 0"
        )
    if not _has_column(db, table, "last_leaf_compaction_at"):
        db.execute(f"ALTER TABLE {table} ADD COLUMN last_leaf_compaction_at TEXT")
    if not _has_column(db, table, "turns_since_leaf_compaction"):
        db.execute(
            f"ALTER TABLE {table} ADD COLUMN turns_since_leaf_compaction INTEGER NOT NULL DEFAULT 0"
        )
    if not _has_column(db, table, "tokens_accumulated_since_leaf_compaction"):
        db.execute(
            f"ALTER TABLE {table} ADD COLUMN "
            "tokens_accumulated_since_leaf_compaction INTEGER NOT NULL DEFAULT 0"
        )
    if not _has_column(db, table, "last_activity_band"):
        db.execute(
            f"ALTER TABLE {table} ADD COLUMN "
            "last_activity_band TEXT NOT NULL DEFAULT 'low' "
            "CHECK (last_activity_band IN ('low', 'medium', 'high'))"
        )
    if not _has_column(db, table, "last_api_call_at"):
        db.execute(f"ALTER TABLE {table} ADD COLUMN last_api_call_at TEXT")
    if not _has_column(db, table, "last_cache_touch_at"):
        db.execute(f"ALTER TABLE {table} ADD COLUMN last_cache_touch_at TEXT")
    if not _has_column(db, table, "provider"):
        db.execute(f"ALTER TABLE {table} ADD COLUMN provider TEXT")
    if not _has_column(db, table, "model"):
        db.execute(f"ALTER TABLE {table} ADD COLUMN model TEXT")
    if not _has_column(db, table, "last_observed_prompt_token_count"):
        db.execute(f"ALTER TABLE {table} ADD COLUMN last_observed_prompt_token_count INTEGER")


def _ensure_message_parts_table_belt_and_suspenders(db: sqlite3.Connection) -> None:
    """Re-create ``message_parts`` if it doesn't exist after the bulk block.

    Ports ``migration.ts:271-322`` ``ensureMessagePartsTable``. The TS
    comment explains the rationale:

        `message_parts` is defined inside the large `db.exec()` block in
        `runLcmMigrations`. On some Node.js SQLite builds (particularly
        `node:sqlite` before v22.12) a syntax error or constraint-check
        mismatch anywhere in that block causes the exec to stop early,
        silently leaving tables that appear later in the string uncreated.

    Python's :meth:`sqlite3.Connection.execute` (one statement at a time)
    raises on failure, so we'd notice the partial-success bug. The per-table
    loop in :func:`_ensure_core_tables` mitigates the original failure mode
    further. But the spec requires this belt-and-suspenders for parity, and
    it's cheap (one ``sqlite_master`` query + a no-op if present).

    Idempotent: if ``message_parts`` already exists, this is a no-op.

    Args:
        db: Open :class:`sqlite3.Connection` inside the migration txn.
    """
    row = db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'message_parts'"
    ).fetchone()
    if row is not None:
        return
    # Re-create message_parts + its two non-partial indexes. The CREATE
    # SQL deliberately matches the bulk-block byte-for-byte so the
    # resulting `sqlite_master.sql` is identical.
    db.execute(_SQL_TABLE_MESSAGE_PARTS)
    db.execute("CREATE INDEX IF NOT EXISTS message_parts_message_idx ON message_parts (message_id)")
    db.execute("CREATE INDEX IF NOT EXISTS message_parts_type_idx ON message_parts (part_type)")


# ---------------------------------------------------------------------------
# SQL constants — FTS5 virtual tables (per storage.md §2.2 and migration.ts:1194-1262)
# ---------------------------------------------------------------------------
#
# Three standalone FTS5 virtual tables — none use content/content_rowid tracking
# (that's the "stale schema" pattern the recreate-detector guards against).
# Population is handled by the application layer (ConversationStore for
# messages_fts; SummaryStore for both summaries_fts variants) — there are
# **no SQL triggers** linking messages → messages_fts. The seed step below
# bulk-loads existing rows when the FTS table is first created (or recreated
# after a stale-schema purge); steady-state writes go through application
# inserts/deletes (see #01-08 / #01-09).
#
# String formatting matches TS migration.ts byte-for-byte (including the
# trailing-newline-and-indent inside each `CREATE VIRTUAL TABLE` body) so the
# `sqlite_master.sql` stored after CREATE diffs cleanly against the TS-
# generated reference in `tests/fixtures/lcm_reference_schema.sql`. The
# `--verify-subset` orchestrator normalizes whitespace via `re.sub(r"\s+", " ")`
# so minor indent drift is tolerated — but matching the TS layout reduces
# review noise.

_SQL_CREATE_MESSAGES_FTS = """
            CREATE VIRTUAL TABLE messages_fts USING fts5(
              content,
              tokenize='porter unicode61'
            )
          """

_SQL_SEED_MESSAGES_FTS = """
            INSERT INTO messages_fts(rowid, content)
            SELECT message_id, content FROM messages
          """

_SQL_CREATE_SUMMARIES_FTS = """
            CREATE VIRTUAL TABLE summaries_fts USING fts5(
              summary_id UNINDEXED,
              content,
              tokenize='porter unicode61'
            )
          """

_SQL_SEED_SUMMARIES_FTS = """
            INSERT INTO summaries_fts(summary_id, content)
            SELECT summary_id, content FROM summaries
          """

_SQL_CREATE_SUMMARIES_FTS_CJK = """
              CREATE VIRTUAL TABLE summaries_fts_cjk USING fts5(
                summary_id UNINDEXED,
                content,
                tokenize='trigram'
              )
            """

_SQL_SEED_SUMMARIES_FTS_CJK = """
              INSERT INTO summaries_fts_cjk(summary_id, content)
              SELECT summary_id, content FROM summaries
            """


@dataclass(frozen=True, slots=True)
class _FtsTableSpec:
    """Spec for one standalone FTS5 virtual table (ports TS ``FtsTableSpec``).

    Mirrors ``migration.ts:46-52``:

        type FtsTableSpec = {
          tableName: string;
          createSql: string;
          seedSql: string;
          expectedColumns: string[];
          staleSchemaPatterns?: string[];
        };

    Attributes:
        table_name: The virtual-table name (also used to derive the 5 shadow
            tables: ``<name>_data``, ``_idx``, ``_content``, ``_docsize``,
            ``_config``).
        create_sql: The ``CREATE VIRTUAL TABLE`` statement run when no
            existing-table-is-fine result.
        seed_sql: The bulk-load INSERT run immediately after a fresh create.
            Pulls existing rows from the parent table (``messages`` or
            ``summaries``).
        expected_columns: Column names that ``PRAGMA table_info`` must report
            on the existing FTS table; if any is missing, the table is
            considered stale and recreated.
        stale_schema_patterns: Substrings searched in the existing table's
            ``sqlite_master.sql``; any hit triggers a recreate. Used to
            detect legacy ``content_rowid`` setups that this PR replaces
            with default content tracking.
    """

    table_name: str
    create_sql: str
    seed_sql: str
    expected_columns: tuple[str, ...]
    stale_schema_patterns: tuple[str, ...] = field(default_factory=tuple)


_FTS_SPEC_MESSAGES_FTS = _FtsTableSpec(
    table_name="messages_fts",
    create_sql=_SQL_CREATE_MESSAGES_FTS,
    seed_sql=_SQL_SEED_MESSAGES_FTS,
    expected_columns=("content",),
    stale_schema_patterns=("content_rowid",),
)

_FTS_SPEC_SUMMARIES_FTS = _FtsTableSpec(
    table_name="summaries_fts",
    create_sql=_SQL_CREATE_SUMMARIES_FTS,
    seed_sql=_SQL_SEED_SUMMARIES_FTS,
    expected_columns=("summary_id", "content"),
    stale_schema_patterns=(
        "content_rowid='summary_id'",
        'content_rowid="summary_id"',
    ),
)

_FTS_SPEC_SUMMARIES_FTS_CJK = _FtsTableSpec(
    table_name="summaries_fts_cjk",
    create_sql=_SQL_CREATE_SUMMARIES_FTS_CJK,
    seed_sql=_SQL_SEED_SUMMARIES_FTS_CJK,
    expected_columns=("summary_id", "content"),
)


# SQL identifier whitelist (ports TS ``quoteSqlIdentifier`` regex check at
# migration.ts:839). Used to defensively bounds-check table names before
# embedding them in DROP statements. All call-sites in this module pass
# hard-coded constants so the check is purely belt-and-suspenders.
_VALID_SQL_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _quote_sql_identifier(identifier: str) -> str:
    """Return ``identifier`` double-quoted (TS ``quoteSqlIdentifier``).

    Validates the identifier matches ``[A-Za-z_][A-Za-z0-9_]*`` and escapes
    any embedded double-quotes by doubling them — same shape as TS source
    at ``migration.ts:838-843``.

    Args:
        identifier: A SQL identifier to quote.

    Returns:
        The identifier wrapped in double-quotes with any internal ``"``
        doubled.

    Raises:
        ValueError: If the identifier contains characters outside the
            allowed alphabet. All call-sites in this module pass module-
            level constants, so the raise is a defensive bug-catcher.
    """
    if not _VALID_SQL_IDENTIFIER.match(identifier):
        raise ValueError(f"Invalid SQL identifier: {identifier}")
    return '"' + identifier.replace('"', '""') + '"'


def _get_fts_shadow_table_names(table_name: str) -> tuple[str, ...]:
    """Return the 5 shadow-table names FTS5 creates alongside a virtual table.

    Ports ``getFtsShadowTableNames`` at ``migration.ts:828-836``. SQLite's
    FTS5 implementation materializes 5 backing tables for every virtual
    table: ``<name>_data``, ``<name>_idx``, ``<name>_content``,
    ``<name>_docsize``, ``<name>_config``. The stale-schema purge path
    drops all 5 before recreating the virtual table itself.

    Args:
        table_name: The user-visible FTS5 virtual table name.

    Returns:
        A 5-tuple of shadow table names in stable order (matches the
        TS source for review parity).
    """
    return (
        f"{table_name}_data",
        f"{table_name}_idx",
        f"{table_name}_content",
        f"{table_name}_docsize",
        f"{table_name}_config",
    )


def _get_existing_table_names(db: sqlite3.Connection, candidates: Iterable[str]) -> set[str]:
    """Return the subset of ``candidates`` that exist as tables in ``db``.

    Ports the helper used by ``shouldRecreateStandaloneFtsTable`` at
    ``migration.ts:805-826``. Issues a single parameterized query so the
    cost is O(1) round-trips regardless of candidate count.

    Args:
        db: Open :class:`sqlite3.Connection`.
        candidates: Table names to look for.

    Returns:
        A set containing exactly the names present in ``sqlite_master``
        with ``type = 'table'``.
    """
    names = tuple(candidates)
    if not names:
        return set()
    placeholders = ",".join("?" for _ in names)
    rows = db.execute(
        f"SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ({placeholders})",
        names,
    ).fetchall()
    return {row[0] for row in rows if isinstance(row[0], str) and row[0]}


def _should_recreate_standalone_fts_table(db: sqlite3.Connection, spec: _FtsTableSpec) -> bool:
    """Return True if ``spec.table_name`` is missing or stale.

    Ports ``shouldRecreateStandaloneFtsTable`` at ``migration.ts:845-876``.
    The function returns True if any of:

    * The FTS table doesn't exist (``sqlite_master`` lookup).
    * Any of the 5 shadow tables is missing (the virtual table is half-
      created — a previously-interrupted CREATE).
    * The existing ``sqlite_master.sql`` text contains any of
      ``spec.stale_schema_patterns`` (legacy ``content_rowid`` config).
    * ``PRAGMA table_info`` reports any of ``spec.expected_columns`` is
      missing on the existing table.

    Any other database error during the probe also returns True (treated
    as "we can't inspect, safer to recreate") — matches the TS source's
    bare ``catch { return true; }``.

    Args:
        db: Open :class:`sqlite3.Connection`.
        spec: Spec describing the expected virtual table shape.

    Returns:
        ``True`` if the caller should drop+recreate; ``False`` if the
        existing table is intact and current.
    """
    shadow_tables = _get_fts_shadow_table_names(spec.table_name)
    existing = _get_existing_table_names(db, (spec.table_name, *shadow_tables))
    if spec.table_name not in existing:
        return True
    if any(name not in existing for name in shadow_tables):
        return True

    try:
        row = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name = ?",
            (spec.table_name,),
        ).fetchone()
        sql_text = (row[0] if row and row[0] else "") or ""
        if any(pattern in sql_text for pattern in spec.stale_schema_patterns):
            return True

        # PRAGMA table_info on an FTS5 virtual table returns the user-
        # declared columns (e.g. ``summary_id``, ``content``). Hidden
        # columns like ``rank`` are NOT reported by this PRAGMA — only
        # the columns the CREATE VIRTUAL TABLE statement declared.
        column_rows = db.execute(
            f"PRAGMA table_info({_quote_sql_identifier(spec.table_name)})"
        ).fetchall()
        column_names = {row[1] for row in column_rows if isinstance(row[1], str) and row[1]}
        return any(col not in column_names for col in spec.expected_columns)
    except sqlite3.DatabaseError:
        return True


def _ensure_standalone_fts_table(db: sqlite3.Connection, spec: _FtsTableSpec) -> None:
    """Idempotently create + seed a standalone FTS5 virtual table.

    Ports ``ensureStandaloneFtsTable`` at ``migration.ts:878-889``. Wrapped
    around the stale-schema check so callers don't need to know whether
    the table needs purging — pass the spec and the helper does the right
    thing.

    On recreate:

    1. ``DROP TABLE IF EXISTS <name>`` (virtual table).
    2. ``DROP TABLE IF EXISTS <shadow>`` for each of the 5 shadow tables.
       The shadow drops are not strictly necessary after the virtual-table
       drop succeeds (SQLite cleans them up automatically), but they're
       belt-and-suspenders for half-created states the stale-schema check
       flagged.
    3. ``CREATE VIRTUAL TABLE …``.
    4. The seed INSERT (pulls existing rows from the parent table).

    On no-op (no recreate needed) the function returns immediately
    without touching the DB.

    Args:
        db: Open :class:`sqlite3.Connection`.
        spec: Spec describing the table to ensure.
    """
    if not _should_recreate_standalone_fts_table(db, spec):
        return

    quoted = _quote_sql_identifier(spec.table_name)
    db.execute(f"DROP TABLE IF EXISTS {quoted}")
    for shadow in _get_fts_shadow_table_names(spec.table_name):
        db.execute(f"DROP TABLE IF EXISTS {_quote_sql_identifier(shadow)}")
    db.execute(spec.create_sql)
    db.execute(spec.seed_sql)


def _ensure_fts5_tables(db: sqlite3.Connection, *, fts5_available: bool) -> None:
    """Create the 3 FTS5 virtual tables when FTS5 is available.

    Ports ``migration.ts:1182-1262``. Creates:

    * ``messages_fts`` — ``fts5(content, tokenize='porter unicode61')``.
      Standalone (default content tracking). Seeded from ``messages``.
    * ``summaries_fts`` — ``fts5(summary_id UNINDEXED, content,
      tokenize='porter unicode61')``. Standalone. Seeded from ``summaries``.
    * ``summaries_fts_cjk`` — ``fts5(summary_id UNINDEXED, content,
      tokenize='trigram')``. Standalone. Seeded from ``summaries``. Only
      created when the runtime trigram tokenizer probe succeeds (resolved
      from :func:`lossless_hermes.db.features.get_lcm_db_features`).

    No SQL triggers are created — population is handled by the
    application layer (ConversationStore on every message insert/delete;
    SummaryStore on every summary insert/delete). See spec
    ``epics/01-storage/01-05-migration-fts5-tables.md`` §"Out of scope".

    Idempotency: each table is wrapped through
    :func:`_ensure_standalone_fts_table`, which checks the existing
    schema and only recreates if missing, half-broken, or stale (legacy
    ``content_rowid`` config). Re-running this function on an already-
    migrated DB is a no-op.

    Trigram-skip path: when ``trigram_tokenizer_available`` is False, the
    function drops any pre-existing ``summaries_fts_cjk`` (best-effort —
    a stale virtual table should not block core migration) and skips
    creating a fresh one. This mirrors ``migration.ts:1186-1192``.

    Args:
        db: Open :class:`sqlite3.Connection` inside the migration txn.
        fts5_available: When ``False`` the function is a clean no-op
            (no FTS table inspection, no drops, no creates). Logs a
            DEBUG line and returns. Caller resolves this from
            :func:`lossless_hermes.db.features.get_lcm_db_features`
            (or passes ``False`` explicitly from tests that validate
            the LIKE-fallback path).
    """
    if not fts5_available:
        _log.debug(
            "FTS5 not available on this connection; skipping creation of "
            "messages_fts / summaries_fts / summaries_fts_cjk."
        )
        return

    # Detect trigram tokenizer availability at runtime (matches TS at
    # migration.ts:1185 — `detectedFeatures?.trigramTokenizerAvailable ?? false`).
    # The probe uses SAVEPOINT internally, which is safe inside the
    # BEGIN EXCLUSIVE transaction the orchestrator opens around this
    # function.
    features = get_lcm_db_features(db)
    trigram_available = features.fts5_trigram_available

    if not trigram_available:
        # Best-effort cleanup of any stale CJK table from a previous run
        # where trigram WAS available. A stale virtual table on its own
        # should not block migration — swallow errors.
        try:
            db.execute("DROP TABLE IF EXISTS summaries_fts_cjk")
        except sqlite3.DatabaseError:  # pragma: no cover - defensive
            pass

    _ensure_standalone_fts_table(db, _FTS_SPEC_MESSAGES_FTS)
    _ensure_standalone_fts_table(db, _FTS_SPEC_SUMMARIES_FTS)

    if trigram_available:
        _ensure_standalone_fts_table(db, _FTS_SPEC_SUMMARIES_FTS_CJK)


# ---- Stubs for future-PR sections ----------------------------------------


def _ensure_v41_tables(db: sqlite3.Connection) -> None:
    """Create the 13 v4.1 tables + their indexes + the polymorphic-cleanup
    trigger.

    **STUB**: body lands in #01-06. Currently a no-op.

    Tables out of scope for this PR (will land in #01-06):

    * ``lcm_worker_lock``, ``lcm_extraction_queue``,
      ``lcm_session_key_audit``, ``lcm_prompt_registry``,
      ``lcm_synthesis_cache``, ``lcm_cache_leaf_refs``,
      ``lcm_synthesis_audit``.
    * 4 eval tables: ``lcm_eval_query_set``, ``lcm_eval_query``,
      ``lcm_eval_run``, ``lcm_eval_drift``.
    * 3 entity tables: ``lcm_entity_type_registry``, ``lcm_entities``,
      ``lcm_entity_mentions``.
    * 2 embedding registry tables: ``lcm_embedding_profile``,
      ``lcm_embedding_meta``.
    * ``lcm_feature_flags``.

    Args:
        db: Open :class:`sqlite3.Connection` inside the migration txn.
    """
    # TODO(epic-01 issue 01-06): body lands in #01-06 (v4.1 tables + indexes).
    # When this lands, also add the lcm_embedding_meta_cleanup_summary
    # trigger inside _ensure_core_triggers (it references lcm_embedding_meta
    # which is created here).
    _ = db
    return


def _ensure_core_triggers(db: sqlite3.Connection) -> None:
    """Create the core triggers.

    **STUB**: body lands alongside #01-06.

    The only core trigger today is ``lcm_embedding_meta_cleanup_summary``
    (storage.md §2.8) — an AFTER DELETE ON summaries trigger that cleans
    polymorphic ``lcm_embedding_meta`` sidecar rows.

    **Why the body is deferred**: The trigger references ``lcm_embedding_meta``,
    which is created by :func:`_ensure_v41_tables` in #01-06. SQLite's
    ``CREATE TRIGGER`` accepts a reference to a missing table at DDL parse
    time, but the trigger fails at fire time. Creating the trigger now would
    break every ``DELETE FROM summaries`` until #01-06 lands (cascading FK
    deletes from ``conversations`` would also fail). Deferring keeps every
    interim state usable.

    When the trigger body is added (in #01-06), this function MUST run AFTER
    :func:`_ensure_v41_tables` so the table exists first. The order in
    :func:`run_lcm_migrations` already reflects that placement.

    Args:
        db: Open :class:`sqlite3.Connection` inside the migration txn.
    """
    # TODO(epic-01 issue 01-06): body lands in #01-06 (depends on
    # lcm_embedding_meta from _ensure_v41_tables). The full DDL:
    #
    #   CREATE TRIGGER IF NOT EXISTS lcm_embedding_meta_cleanup_summary
    #     AFTER DELETE ON summaries
    #     BEGIN
    #       DELETE FROM lcm_embedding_meta
    #         WHERE embedded_id = OLD.summary_id
    #           AND embedded_kind = 'summary';
    #     END
    #
    # Note: SQLite accepts CREATE TRIGGER with a missing referenced table,
    # but the trigger errors at fire time. Keep this stub a no-op until
    # _ensure_v41_tables ships its body.
    _ = db
    return


def _run_versioned_backfills(db: sqlite3.Connection, log: MigrationLogger | None) -> None:
    """Run algorithm-versioned data backfills.

    **STUB**: body lands in #01-15. Currently a no-op.

    Per ADR-026 §"Algorithm-versioned state", each step's completion is
    recorded in ``lcm_migration_state`` (created by :func:`_ensure_core_tables`
    in this PR). Re-runs are skipped via the
    ``hasCompletedVersionedBackfill`` check.

    Steps that will land in #01-15:

    * ``backfillSummaryDepths`` — compute depth from summary_parents edges
      (``migration.ts:476-577``).
    * ``backfillSummaryMetadata`` — earliest_at / latest_at /
      descendant_count / descendant_token_count / source_message_token_count
      (``migration.ts:579-749``).
    * ``backfillToolCallColumns`` — extract tool_call_id / tool_name /
      tool_input from metadata JSON (``migration.ts:757-811``).

    Also lands in #01-15:

    * ``backfillMessageIdentityHashes`` — chunked-batch SHA-256 population
      (``migration.ts:332-375``). NOT versioned (no algorithm version), runs
      every time but is no-op when all rows already have a hash.
    * ``backfillConversationSessionKeys`` — NULL → ``legacy:conv_<id>``
      with audit row (``migration.ts:1912-1935``).
    * ``backfillSummarySessionKeys`` — fill from conversations
      (``migration.ts:1937-1950``).
    * ``backfillForkRollupsSessionKeys`` — fork-side legacy table no-op
      (``migration.ts:1957-1981``).

    Args:
        db: Open :class:`sqlite3.Connection` inside the migration txn.
        log: Optional :class:`MigrationLogger` for per-step progress.
    """
    # TODO(epic-01 issue 01-15): body lands in #01-15 (versioned backfills).
    _ = (db, log)
    return


def _seed_default_prompts(db: sqlite3.Connection, log: MigrationLogger | None) -> None:
    """Seed the default synthesis prompts into ``lcm_prompt_registry``.

    **STUB**: body lands alongside the synthesis epic.

    Per ``migration.ts:1435-1442`` the TS source calls ``seedDefaultPrompts(db)``
    from ``src/synthesis/seed-default-prompts.ts``. The Python equivalent
    will live at ``src/lossless_hermes/synthesis/seed_default_prompts.py``.

    Idempotent: only seeds prompts where the
    ``(memory_type, tier_label, pass_kind)`` triple has no existing rows.
    Operator-registered prompts are never overwritten.

    Until the synthesis epic lands, this stub is a no-op. Callers that
    pass ``seed_default_prompts=True`` see no prompts seeded — and
    ``dispatchSynthesis`` would return ``missing_prompt`` errors. That's
    the intended ratchet: synthesis is non-functional until both #01-06
    (creates ``lcm_prompt_registry``) and the synthesis epic
    (``seed_default_prompts.py``) ship.

    Args:
        db: Open :class:`sqlite3.Connection` inside the migration txn.
        log: Optional :class:`MigrationLogger` for per-step progress.
    """
    # TODO(epic-01 synthesis): body lands alongside the synthesis epic.
    _ = (db, log)
    return


# ---------------------------------------------------------------------------
# Public API: run_lcm_migrations
# ---------------------------------------------------------------------------


def run_lcm_migrations(
    db: sqlite3.Connection,
    *,
    fts5_available: bool = True,
    seed_default_prompts: bool = True,
    log: MigrationLogger | None = None,
) -> None:
    """Apply the full LCM schema migration ladder.

    The single sanctioned entry point per ADR-024 and ADR-026. Idempotent:
    re-running on an already-migrated DB is a no-op.

    Section ordering (load-bearing):

    1. :func:`_ensure_core_tables` — 12 always-on tables (this PR).
    2. :func:`_ensure_core_indexes_early` — 10 phase-1 indexes that
       depend only on v0 schema columns (this PR).
    3. :func:`_apply_structural_column_probes` — additive ALTERs for
       imported-from-OpenClaw DBs (this PR). Adds v4.1 columns
       (``depth``, ``model``, ``session_key``, ``suppressed_at``, etc.)
       that the phase-2 indexes below depend on.
    4. :func:`_ensure_message_parts_table_belt_and_suspenders` — guards
       against partial-bulk-block failures (this PR).
    5. :func:`_ensure_core_indexes_late` — 10 phase-2 indexes (partial
       UNIQUE on session_key, v4.1 partials) that depend on the
       ALTER-added columns (this PR).
    6. :func:`_drop_legacy_conversation_session_key_index` — drops the
       obsolete non-partial UNIQUE replaced by the v3.1 partial UNIQUE
       (this PR). Runs after the partial UNIQUE is created.
    7. :func:`_ensure_fts5_tables` — 3 FTS5 virtual tables (#01-05).
    8. :func:`_ensure_v41_tables` — stub, body in #01-06 (creates
       ``lcm_embedding_meta`` which #9 depends on).
    9. :func:`_ensure_core_triggers` — stub, body in #01-06 (depends on
       ``lcm_embedding_meta``).
    10. :func:`_run_versioned_backfills` — stub, body in #01-15.
    11. :func:`_seed_default_prompts` — stub, body alongside synthesis epic.

    The whole ladder is wrapped in ``BEGIN EXCLUSIVE`` so two concurrent
    processes calling this function serialize through SQLite's write lock
    (the second sees the schema already applied and runs as a no-op). See
    ``docs/porting-guides/storage.md`` §10 and ADR-026 §Open Questions
    item 2.

    Note on transaction state: if ``db`` is already inside a transaction
    when this is called, ``BEGIN EXCLUSIVE`` will raise
    :class:`sqlite3.OperationalError` ("cannot start a transaction within
    a transaction"). Callers must invoke this on an autocommit-mode
    connection or commit/rollback any open transaction first.

    Args:
        db: An open :class:`sqlite3.Connection` opened via
            :func:`lossless_hermes.db.connection.open_lcm_db` (so PRAGMAs
            including ``foreign_keys = ON`` are already applied).
        fts5_available: When ``True`` (default), :func:`_ensure_fts5_tables`
            creates the 3 FTS5 virtual tables (``messages_fts``,
            ``summaries_fts``, ``summaries_fts_cjk`` (gated internally on
            trigram tokenizer availability)). Pass ``False`` to skip FTS5
            creation entirely (e.g. tests that deliberately exclude FTS5
            to validate the LIKE-fallback path). Caller can resolve the
            default from
            :func:`lossless_hermes.db.features.get_lcm_db_features`.
        seed_default_prompts: When ``True`` (default),
            :func:`_seed_default_prompts` will seed prompts once the
            synthesis epic ships. Pass ``False`` from tests that want an
            empty prompt registry (per ``migration.ts:899-905`` — tests
            register their own prompts at version 1 without UNIQUE
            collision).
        log: Optional :class:`MigrationLogger` for per-step progress.
            ``None`` (default) silences progress logging; production
            callers typically pass ``logging.getLogger(...).info``.

    Raises:
        sqlite3.DatabaseError: Any DDL failure during the ladder. The
            ``BEGIN EXCLUSIVE`` transaction is rolled back before the
            exception propagates, so the DB is left in its pre-call state.
        sqlite3.OperationalError: ``db`` was already inside a transaction
            when this function was called.

    Examples:
        Open + migrate an in-memory DB::

            >>> import sqlite3
            >>> from lossless_hermes.db.migration import run_lcm_migrations
            >>> conn = sqlite3.connect(':memory:')
            >>> conn.execute('PRAGMA foreign_keys = ON').fetchall()
            [(0,)]
            >>> run_lcm_migrations(conn)
            >>> conn.execute(
            ...     "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            ... ).fetchone()[0]
            12

        Idempotency::

            >>> run_lcm_migrations(conn)  # second run is a no-op
            >>> conn.execute(
            ...     "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            ... ).fetchone()[0]
            12
    """
    # BEGIN EXCLUSIVE serializes concurrent migration runs (ADR-026 §OQ.2).
    # Note: BEGIN EXCLUSIVE in SQLite is "begin a write transaction holding
    # an EXCLUSIVE lock"; once acquired, no other connection can read OR
    # write until COMMIT/ROLLBACK. This is stronger than BEGIN IMMEDIATE
    # (which holds a RESERVED lock and allows other readers) — the
    # migration ladder needs the exclusive lock because partway-applied
    # schema would confuse concurrent readers.
    db.execute("BEGIN EXCLUSIVE")
    try:
        # 1. Create the 12 core tables (CREATE TABLE IF NOT EXISTS).
        # Fresh DB: every table created. Imported-OpenClaw DB: existing
        # tables retained as-is; missing tables (e.g. compaction telemetry
        # from pre-v4.1) created. Per ADR-026 §"Structural state".
        _ensure_core_tables(db)

        # 2a. Phase-1 indexes (those that reference only v0 schema columns).
        # Safe to create regardless of imported-DB column state.
        _ensure_core_indexes_early(db)

        # 3. Forward-compat ALTERs for imported-from-OpenClaw DBs. On a
        # fresh DB these are no-ops (every column already exists from
        # `_ensure_core_tables`). On an imported old DB this adds the v4.1
        # columns (depth, model, session_key, suppressed_at, etc.) — which
        # the phase-2 indexes below depend on.
        _apply_structural_column_probes(db)

        # 4. Belt-and-suspenders for message_parts. Cheap; defensive.
        _ensure_message_parts_table_belt_and_suspenders(db)

        # 5. Phase-2 indexes (the partial UNIQUE on conversations.session_key
        # + the v4.1 partial indexes). Must come AFTER _apply_structural_
        # column_probes so the imported-DB columns exist.
        _ensure_core_indexes_late(db)

        # 6. Drop the legacy non-partial UNIQUE on conversations.session_key.
        # Runs AFTER `conversations_active_session_key_idx` is created so we
        # never have a window without any UNIQUE-on-session_key index. The
        # legacy DROP is a no-op on a fresh DB.
        _drop_legacy_conversation_session_key_index(db)

        # 7. FTS5 virtual tables (3 tables, gated on the runtime probe).
        # Skipping when fts5_available is False matches the TS contract —
        # `migration.ts:1184` reads
        # `options?.fts5Available ?? detectedFeatures?.fts5Available ?? false`.
        # Trigram availability for the CJK table is resolved internally via
        # `get_lcm_db_features(db)` — the savepoint-based probe is safe inside
        # the BEGIN EXCLUSIVE transaction.
        _ensure_fts5_tables(db, fts5_available=fts5_available)

        # 8. v4.1 tables (stub now; body in #01-06). Must precede the
        # trigger because the trigger references lcm_embedding_meta which
        # is created here.
        _ensure_v41_tables(db)

        # 9. Core triggers (stub now; body alongside #01-06). Placed AFTER
        # _ensure_v41_tables so the trigger's referenced table exists at
        # the time the trigger fires.
        _ensure_core_triggers(db)

        # 10. Versioned backfills (stub now; body in #01-15). Recorded in
        # lcm_migration_state (created in step 1).
        _run_versioned_backfills(db, log)

        # 11. Seed default synthesis prompts (stub now; body alongside the
        # synthesis epic). `seed_default_prompts=False` lets tests register
        # their own version-1 prompts without UNIQUE collision.
        if seed_default_prompts:
            _seed_default_prompts(db, log)

        db.execute("COMMIT")
    except BaseException:
        # Roll back on any failure — ladder is atomic. The `except` guard
        # uses ROLLBACK best-effort; if ROLLBACK itself fails we swallow
        # the error so the original migration failure propagates clearly.
        try:
            db.execute("ROLLBACK")
        except sqlite3.Error:  # pragma: no cover - defensive
            pass
        raise


# ---------------------------------------------------------------------------
# Introspection helpers (for tests + /lcm doctor in future)
# ---------------------------------------------------------------------------


def list_core_tables() -> tuple[str, ...]:
    """Return the names of the 12 always-on core tables in creation order.

    Useful for tests that need to assert the exact table set created by
    this PR's scope (separately from #01-05 / #01-06 additions).

    Returns:
        A tuple of table names in the order :func:`_ensure_core_tables`
        creates them.
    """
    return tuple(name for name, _sql in _CORE_TABLE_CREATIONS)


def list_core_index_names() -> tuple[str, ...]:
    """Return the names of the 20 always-on core indexes.

    Extracts the index name from each CREATE INDEX statement via simple
    string parsing — the canonical layout is:

        CREATE [UNIQUE ]INDEX IF NOT EXISTS <name> ON ...

    Returns:
        A tuple of index names in the order :func:`_ensure_core_indexes`
        creates them.
    """
    names: list[str] = []
    for sql in _CORE_INDEX_CREATIONS:
        # Parse the leading tokens to find the index name. Robust enough
        # for the controlled set of strings in this module — not a
        # general-purpose SQL parser.
        tokens = sql.split()
        # tokens: ['CREATE', 'UNIQUE'?, 'INDEX', 'IF', 'NOT', 'EXISTS', '<name>', ...]
        if "UNIQUE" in tokens[:2]:
            idx_name = tokens[6]
        else:
            idx_name = tokens[5]
        names.append(idx_name)
    return tuple(names)


def _iter_core_object_names() -> Iterable[str]:
    """Iterator over (tables ∪ indexes) names this PR creates.

    Used by tests for exact-set assertions. Doesn't include the trigger
    (deferred to #01-06 per :func:`_ensure_core_triggers`).
    """
    yield from list_core_tables()
    yield from list_core_index_names()
