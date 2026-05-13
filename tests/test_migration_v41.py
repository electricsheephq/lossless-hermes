"""Tests for the v4.1 schema layer ported in ``epics/01-storage/01-06``.

Covers the acceptance criteria from ``epics/01-storage/01-06-migration-v41-tables.md``:

* All 17 v4.1 tables created on a fresh DB.
* All 24 v4.1 indexes created on a fresh DB.
* The ``lcm_embedding_meta_cleanup_summary`` trigger created and fires on
  ``DELETE FROM summaries``.
* CHECK constraints enforced:

  - ``lcm_extraction_queue.kind IN ('entity','procedure-recheck')``
  - ``lcm_synthesis_cache.tier_label`` widened set
  - ``lcm_eval_query.stratum IN ('fts-easy','fts-medium','paraphrastic')``
  - ``lcm_eval_run.trigger IN ('manual','prompt-update','model-update','ci','nightly')``
  - ``lcm_embedding_meta.embedded_kind IN ('summary','entity','theme')``
  - ``lcm_prompt_registry.memory_type``, ``pass_kind`` enums
  - ``lcm_synthesis_audit.status``, polymorphic NOT-NULL CHECK

* FK CASCADE verified:

  - Deleting a leaf summary cascades to ``lcm_extraction_queue``.
  - Deleting an entity cascades to ``lcm_entity_mentions``.
  - Deleting a conversation cascades to ``lcm_session_key_audit``.
  - Deleting a summary cascades to ``lcm_cache_leaf_refs`` (via summary_id).
  - Deleting a cache cascades to ``lcm_cache_leaf_refs`` and
    ``lcm_synthesis_audit`` (target_cache_id).

* Polymorphic FK behavior: deleting a summary fires the trigger
  ``lcm_embedding_meta_cleanup_summary`` which removes corresponding
  ``lcm_embedding_meta`` rows (since there is no FK on ``embedded_id``).
* UNIQUE ``(session_key, canonical_text COLLATE NOCASE)`` on ``lcm_entities``
  rejects case-variant duplicates.
* UNIQUE ``lcm_prompt_registry_uniq_lookup`` (null-safe COALESCE) closes the
  NULL-tier_label gap that the plain UNIQUE leaves open.
* UNIQUE ``lcm_synthesis_cache_lookup_uniq`` includes ``tier_label`` and
  ``prompt_id`` (LCM Wave-10 fix).
* Cache-recreate path: a DB with the old narrow CHECK on
  ``lcm_synthesis_cache.tier_label`` (missing ``'yearly'``) has the cache
  dropped and recreated, with orphaned audit rows pruned.
* Idempotency: ``run_lcm_migrations`` is a no-op on second run; calling
  ``_ensure_v41_tables`` and ``_ensure_core_triggers`` directly on a
  migrated DB is a no-op.
* Reference-fixture parity: every v4.1 object's SQL matches the
  corresponding entry in ``tests/fixtures/lcm_reference_schema.sql``.

References:

* :mod:`lossless_hermes.db.migration` — implementation under test.
* ``epics/01-storage/01-06-migration-v41-tables.md`` — issue spec + AC.
* ``docs/porting-guides/storage.md`` §2.3-§2.7 — table inventory.
* ``tests/fixtures/lcm_reference_schema.sql`` — TS-generated golden.
* ``docs/adr/029-wave-fix-provenance.md`` — Wave-N comments.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Iterator

import pytest

from lossless_hermes.db.migration import (
    _V41_INDEX_CREATIONS,
    _V41_TABLE_CREATIONS,
    _ensure_core_triggers,
    _ensure_v41_tables,
    list_v41_index_names,
    list_v41_tables,
    run_lcm_migrations,
)

_REFERENCE_SCHEMA_PATH = Path(__file__).parent / "fixtures" / "lcm_reference_schema.sql"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_db() -> Iterator[sqlite3.Connection]:
    """An in-memory DB with FK enforcement enabled, no migrations applied."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def migrated_db(fresh_db: sqlite3.Connection) -> sqlite3.Connection:
    """A DB with the full migration ladder applied (core + v4.1 + triggers)."""
    run_lcm_migrations(fresh_db)
    return fresh_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _list_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r[0] for r in rows}


def _list_indexes(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r[0] for r in rows}


def _list_triggers(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()
    return {r[0] for r in rows}


def _column_names(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [r[1] for r in rows]


def _normalize_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip().lower()


def _snapshot_schema(conn: sqlite3.Connection) -> list[tuple[str, str, str | None]]:
    return conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
        "ORDER BY type, name"
    ).fetchall()


# ---------------------------------------------------------------------------
# Expected table / index sets
# ---------------------------------------------------------------------------


_EXPECTED_V41_TABLES = (
    # Support layer
    "lcm_feature_flags",
    "lcm_worker_lock",
    "lcm_extraction_queue",
    "lcm_session_key_audit",
    # Synthesis layer
    "lcm_prompt_registry",
    "lcm_synthesis_cache",
    "lcm_cache_leaf_refs",
    "lcm_synthesis_audit",
    # Eval harness
    "lcm_eval_query_set",
    "lcm_eval_query",
    "lcm_eval_run",
    "lcm_eval_drift",
    # Entity layer
    "lcm_entity_type_registry",
    "lcm_entities",
    "lcm_entity_mentions",
    # Embedding registry
    "lcm_embedding_profile",
    "lcm_embedding_meta",
)

_EXPECTED_V41_INDEXES = (
    "lcm_cache_leaf_refs_by_leaf_idx",
    "lcm_embedding_meta_active_idx",
    "lcm_embedding_meta_by_kind_idx",
    "lcm_entities_canonical_uniq",
    "lcm_entities_lookup_idx",
    "lcm_entity_mentions_by_entity_idx",
    "lcm_entity_mentions_by_summary_idx",
    "lcm_eval_drift_recent_idx",
    "lcm_eval_query_must_not_regress_idx",
    "lcm_eval_query_set_stratum_idx",
    "lcm_eval_run_recent_idx",
    "lcm_extraction_queue_dead_letter_idx",
    "lcm_extraction_queue_pending_idx",
    "lcm_prompt_registry_active_idx",
    "lcm_prompt_registry_uniq_lookup",
    "lcm_session_key_audit_conv_idx",
    "lcm_synthesis_audit_completed_gc_idx",
    "lcm_synthesis_audit_session_idx",
    "lcm_synthesis_audit_started_gc_idx",
    "lcm_synthesis_audit_target_cache_idx",
    "lcm_synthesis_audit_target_summary_idx",
    "lcm_synthesis_cache_built_idx",
    "lcm_synthesis_cache_lookup_uniq",
    "lcm_synthesis_cache_status_building_idx",
)


# ---------------------------------------------------------------------------
# Table/index/trigger creation invariants
# ---------------------------------------------------------------------------


def test_v41_table_count(migrated_db: sqlite3.Connection) -> None:
    """All 17 v4.1 tables are created on a fresh DB."""
    tables = _list_tables(migrated_db)
    missing = set(_EXPECTED_V41_TABLES) - tables
    assert missing == set(), f"missing v4.1 tables: {sorted(missing)!r}"


def test_v41_index_count(migrated_db: sqlite3.Connection) -> None:
    """All 24 v4.1 indexes are created on a fresh DB."""
    indexes = _list_indexes(migrated_db)
    missing = set(_EXPECTED_V41_INDEXES) - indexes
    assert missing == set(), f"missing v4.1 indexes: {sorted(missing)!r}"


def test_v41_trigger_created(migrated_db: sqlite3.Connection) -> None:
    """The ``lcm_embedding_meta_cleanup_summary`` trigger is created."""
    triggers = _list_triggers(migrated_db)
    assert "lcm_embedding_meta_cleanup_summary" in triggers


def test_introspection_helpers_match_expected_sets() -> None:
    """``list_v41_tables()`` and ``list_v41_index_names()`` match expectations."""
    assert sorted(list_v41_tables()) == sorted(_EXPECTED_V41_TABLES)
    assert sorted(list_v41_index_names()) == sorted(_EXPECTED_V41_INDEXES)


def test_v41_constants_in_sync_with_table_creations() -> None:
    """``_V41_TABLE_CREATIONS`` matches the expected count + names."""
    assert len(_V41_TABLE_CREATIONS) == 17
    names = [name for name, _sql in _V41_TABLE_CREATIONS]
    assert sorted(names) == sorted(_EXPECTED_V41_TABLES)


def test_v41_constants_in_sync_with_index_creations() -> None:
    """``_V41_INDEX_CREATIONS`` matches the expected count."""
    assert len(_V41_INDEX_CREATIONS) == 24


# ---------------------------------------------------------------------------
# Per-table column assertions
# ---------------------------------------------------------------------------


def test_lcm_worker_lock_columns(migrated_db: sqlite3.Connection) -> None:
    assert _column_names(migrated_db, "lcm_worker_lock") == [
        "job_kind",
        "worker_id",
        "acquired_at",
        "expires_at",
        "last_heartbeat_at",
        "job_session_key",
        "job_metadata",
    ]


def test_lcm_feature_flags_columns(migrated_db: sqlite3.Connection) -> None:
    assert _column_names(migrated_db, "lcm_feature_flags") == [
        "flag",
        "value",
        "updated_at",
    ]


def test_lcm_extraction_queue_columns(migrated_db: sqlite3.Connection) -> None:
    assert _column_names(migrated_db, "lcm_extraction_queue") == [
        "queue_id",
        "leaf_id",
        "kind",
        "queued_at",
        "picked_at",
        "worker_id",
        "completed_at",
        "attempts",
        "last_error",
    ]


def test_lcm_session_key_audit_columns(migrated_db: sqlite3.Connection) -> None:
    assert _column_names(migrated_db, "lcm_session_key_audit") == [
        "audit_id",
        "conversation_id",
        "original_session_key",
        "new_session_key",
        "reason",
        "applied_at",
        "applied_by",
    ]


def test_lcm_prompt_registry_columns(migrated_db: sqlite3.Connection) -> None:
    assert _column_names(migrated_db, "lcm_prompt_registry") == [
        "prompt_id",
        "memory_type",
        "tier_label",
        "pass_kind",
        "version",
        "template",
        "model_recommendation",
        "created_at",
        "active",
        "bundle_version",
        "notes",
    ]


def test_lcm_synthesis_cache_columns(migrated_db: sqlite3.Connection) -> None:
    assert _column_names(migrated_db, "lcm_synthesis_cache") == [
        "cache_id",
        "session_key",
        "range_start",
        "range_end",
        "grep_filter",
        "leaf_fingerprint",
        "content",
        "entity_index",
        "model_used",
        "prompt_id",
        "tier_label",
        "source_leaf_ids",
        "source_condensed_ids",
        "built_at",
        "source_token_count",
        "output_token_count",
        "actual_range_covered",
        "leaf_count_synthesized",
        "status",
        "building_started_at",
        "failure_reason",
    ]


def test_lcm_cache_leaf_refs_columns(migrated_db: sqlite3.Connection) -> None:
    assert _column_names(migrated_db, "lcm_cache_leaf_refs") == [
        "cache_id",
        "leaf_summary_id",
    ]


def test_lcm_synthesis_audit_columns(migrated_db: sqlite3.Connection) -> None:
    assert _column_names(migrated_db, "lcm_synthesis_audit") == [
        "audit_id",
        "pass_session_id",
        "target_summary_id",
        "target_cache_id",
        "prompt_id",
        "pass_kind",
        "pass_input_truncated",
        "pass_output",
        "status",
        "model_used",
        "latency_ms",
        "cost_usd_cents",
        "last_error",
        "ran_at",
    ]


def test_lcm_eval_query_set_columns(migrated_db: sqlite3.Connection) -> None:
    assert _column_names(migrated_db, "lcm_eval_query_set") == [
        "query_set_id",
        "version",
        "description",
        "created_at",
    ]


def test_lcm_eval_query_columns(migrated_db: sqlite3.Connection) -> None:
    assert _column_names(migrated_db, "lcm_eval_query") == [
        "query_id",
        "query_set_id",
        "query_text",
        "stratum",
        "expected_topics",
        "expected_sources",
        "reference_summary",
        "must_not_regress",
        "rubric",
    ]


def test_lcm_eval_run_columns(migrated_db: sqlite3.Connection) -> None:
    assert _column_names(migrated_db, "lcm_eval_run") == [
        "run_id",
        "query_set_id",
        "prompt_bundle_version",
        "ran_at",
        "retrieval_recall_score",
        "synthesis_quality_score",
        "per_query_scores",
        "judge_models",
        "noise_floor_sd",
        "trigger",
    ]


def test_lcm_eval_drift_columns(migrated_db: sqlite3.Connection) -> None:
    assert _column_names(migrated_db, "lcm_eval_drift") == [
        "drift_id",
        "query_set_id",
        "cumulative_delta",
        "window_runs",
        "computed_at",
    ]


def test_lcm_entity_type_registry_columns(migrated_db: sqlite3.Connection) -> None:
    assert _column_names(migrated_db, "lcm_entity_type_registry") == [
        "type_name",
        "first_seen_at",
        "occurrence_count",
    ]


def test_lcm_entities_columns(migrated_db: sqlite3.Connection) -> None:
    assert _column_names(migrated_db, "lcm_entities") == [
        "entity_id",
        "session_key",
        "canonical_text",
        "entity_type",
        "first_seen_at",
        "last_seen_at",
        "first_seen_in_summary_id",
        "occurrence_count",
        "alternate_surfaces",
        "metadata",
    ]


def test_lcm_entity_mentions_columns(migrated_db: sqlite3.Connection) -> None:
    assert _column_names(migrated_db, "lcm_entity_mentions") == [
        "mention_id",
        "entity_id",
        "summary_id",
        "surface_form",
        "span_start",
        "span_end",
        "mentioned_at",
    ]


def test_lcm_embedding_profile_columns(migrated_db: sqlite3.Connection) -> None:
    assert _column_names(migrated_db, "lcm_embedding_profile") == [
        "model_name",
        "dim",
        "registered_at",
        "active",
        "archive_after",
    ]


def test_lcm_embedding_meta_columns(migrated_db: sqlite3.Connection) -> None:
    assert _column_names(migrated_db, "lcm_embedding_meta") == [
        "embedded_id",
        "embedded_kind",
        "embedding_model",
        "embedded_at",
        "source_token_count",
        "archived",
    ]


# ---------------------------------------------------------------------------
# CHECK constraint enforcement
# ---------------------------------------------------------------------------


def _insert_conversation(conn: sqlite3.Connection) -> int:
    conn.execute("INSERT INTO conversations (session_id) VALUES ('s1')")
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def _insert_summary(conn: sqlite3.Connection, summary_id: str, conv_id: int) -> None:
    conn.execute(
        "INSERT INTO summaries (summary_id, conversation_id, kind, content, token_count) "
        "VALUES (?, ?, 'leaf', 'x', 1)",
        (summary_id, conv_id),
    )


def _insert_embedding_profile(conn: sqlite3.Connection, model: str = "test-model") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO lcm_embedding_profile (model_name, dim, expires_at) "
        "VALUES (?, 128, NULL)"
        if False  # placeholder: actual schema has no expires_at
        else "INSERT OR IGNORE INTO lcm_embedding_profile (model_name, dim) VALUES (?, 128)",
        (model,),
    )


def _insert_prompt(
    conn: sqlite3.Connection, prompt_id: str = "p1", version: int | None = None
) -> None:
    """Insert a prompt row. Each call uses a fresh ``version`` so the
    ``UNIQUE(memory_type, tier_label, pass_kind, version)`` constraint
    doesn't collide across multiple inserts in the same test.

    Args:
        conn: Connection.
        prompt_id: Prompt PK.
        version: If None, derived from ``prompt_id`` (e.g. ``'p3'`` → 3).
    """
    if version is None:
        # Derive a version from any digits in the prompt_id (fallback to 1).
        digits = "".join(c for c in prompt_id if c.isdigit())
        version = int(digits) if digits else 1
    conn.execute(
        "INSERT INTO lcm_prompt_registry "
        "(prompt_id, memory_type, tier_label, pass_kind, version, template) "
        "VALUES (?, 'episodic-leaf', 'leaf', 'single', ?, 'template')",
        (prompt_id, version),
    )


def test_extraction_queue_kind_check_rejects_invalid(migrated_db: sqlite3.Connection) -> None:
    conv_id = _insert_conversation(migrated_db)
    _insert_summary(migrated_db, "leaf-1", conv_id)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        migrated_db.execute(
            "INSERT INTO lcm_extraction_queue (queue_id, leaf_id, kind) VALUES (?, ?, 'bad')",
            ("q1", "leaf-1"),
        )


def test_extraction_queue_kind_check_allows_valid(migrated_db: sqlite3.Connection) -> None:
    conv_id = _insert_conversation(migrated_db)
    _insert_summary(migrated_db, "leaf-1", conv_id)
    for kind in ("entity", "procedure-recheck"):
        migrated_db.execute(
            "INSERT INTO lcm_extraction_queue (queue_id, leaf_id, kind) VALUES (?, ?, ?)",
            (f"q-{kind}", "leaf-1", kind),
        )


def test_synthesis_cache_tier_label_check_rejects_invalid(
    migrated_db: sqlite3.Connection,
) -> None:
    _insert_prompt(migrated_db)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        migrated_db.execute(
            "INSERT INTO lcm_synthesis_cache "
            "(cache_id, session_key, range_start, range_end, leaf_fingerprint, "
            " model_used, prompt_id, tier_label, source_leaf_ids, "
            " source_token_count, output_token_count, actual_range_covered, "
            " leaf_count_synthesized) "
            "VALUES ('c1', 's1', '2026-01-01', '2026-01-31', 'fp1', 'm', 'p1', "
            "  'quarterly', '[]', 0, 0, '2026-01', 0)"
        )


def test_synthesis_cache_tier_label_check_allows_yearly(
    migrated_db: sqlite3.Connection,
) -> None:
    """'yearly' was added to the CHECK enum by the v4.13 widening."""
    _insert_prompt(migrated_db)
    migrated_db.execute(
        "INSERT INTO lcm_synthesis_cache "
        "(cache_id, session_key, range_start, range_end, leaf_fingerprint, "
        " model_used, prompt_id, tier_label, source_leaf_ids, "
        " source_token_count, output_token_count, actual_range_covered, "
        " leaf_count_synthesized) "
        "VALUES ('c-yearly', 's1', '2026-01-01', '2026-12-31', 'fp1', 'm', 'p1', "
        "  'yearly', '[]', 0, 0, '2026', 0)"
    )


def test_eval_query_stratum_check_rejects_invalid(migrated_db: sqlite3.Connection) -> None:
    migrated_db.execute("INSERT INTO lcm_eval_query_set (query_set_id, version) VALUES ('qs1', 1)")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        migrated_db.execute(
            "INSERT INTO lcm_eval_query "
            "(query_id, query_set_id, query_text, stratum, expected_topics, rubric) "
            "VALUES ('q1', 'qs1', 'find me x', 'hard', '[]', '{}')"
        )


def test_eval_run_trigger_check_rejects_invalid(migrated_db: sqlite3.Connection) -> None:
    migrated_db.execute("INSERT INTO lcm_eval_query_set (query_set_id, version) VALUES ('qs1', 1)")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        migrated_db.execute(
            "INSERT INTO lcm_eval_run "
            "(run_id, query_set_id, prompt_bundle_version, retrieval_recall_score, "
            " synthesis_quality_score, per_query_scores, judge_models, trigger) "
            "VALUES ('r1', 'qs1', 1, 0.5, 0.5, '[]', '[]', 'cron')"
        )


def test_embedding_meta_kind_check_rejects_invalid(migrated_db: sqlite3.Connection) -> None:
    migrated_db.execute("INSERT INTO lcm_embedding_profile (model_name, dim) VALUES ('m1', 128)")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        migrated_db.execute(
            "INSERT INTO lcm_embedding_meta "
            "(embedded_id, embedded_kind, embedding_model, source_token_count) "
            "VALUES ('x', 'other', 'm1', 5)"
        )


def test_prompt_registry_memory_type_check_rejects_invalid(
    migrated_db: sqlite3.Connection,
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        migrated_db.execute(
            "INSERT INTO lcm_prompt_registry "
            "(prompt_id, memory_type, tier_label, pass_kind, version, template) "
            "VALUES ('p1', 'invalid-type', 'leaf', 'single', 1, 't')"
        )


def test_prompt_registry_pass_kind_check_rejects_invalid(
    migrated_db: sqlite3.Connection,
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        migrated_db.execute(
            "INSERT INTO lcm_prompt_registry "
            "(prompt_id, memory_type, tier_label, pass_kind, version, template) "
            "VALUES ('p1', 'episodic-leaf', 'leaf', 'bad-kind', 1, 't')"
        )


def test_synthesis_audit_polymorphic_check_rejects_both_null(
    migrated_db: sqlite3.Connection,
) -> None:
    """CHECK requires at least one of target_summary_id or target_cache_id."""
    _insert_prompt(migrated_db)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        migrated_db.execute(
            "INSERT INTO lcm_synthesis_audit "
            "(audit_id, pass_session_id, prompt_id, pass_kind, "
            " pass_input_truncated, model_used) "
            "VALUES ('a1', 'sess1', 'p1', 'single', 'in', 'm')"
        )


def test_synthesis_audit_status_check_rejects_invalid(migrated_db: sqlite3.Connection) -> None:
    conv_id = _insert_conversation(migrated_db)
    _insert_summary(migrated_db, "s1", conv_id)
    _insert_prompt(migrated_db)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        migrated_db.execute(
            "INSERT INTO lcm_synthesis_audit "
            "(audit_id, pass_session_id, target_summary_id, prompt_id, pass_kind, "
            " pass_input_truncated, model_used, status) "
            "VALUES ('a1', 'sess1', 's1', 'p1', 'single', 'in', 'm', 'invalid')"
        )


# ---------------------------------------------------------------------------
# FK CASCADE / RESTRICT verification
# ---------------------------------------------------------------------------


def test_fk_cascade_summary_to_extraction_queue(migrated_db: sqlite3.Connection) -> None:
    """Deleting a leaf summary cascades to extraction_queue rows."""
    conv_id = _insert_conversation(migrated_db)
    _insert_summary(migrated_db, "leaf-1", conv_id)
    migrated_db.execute(
        "INSERT INTO lcm_extraction_queue (queue_id, leaf_id, kind) VALUES "
        "('q1', 'leaf-1', 'entity')"
    )
    assert migrated_db.execute("SELECT COUNT(*) FROM lcm_extraction_queue").fetchone()[0] == 1

    migrated_db.execute("DELETE FROM summaries WHERE summary_id = 'leaf-1'")
    assert migrated_db.execute("SELECT COUNT(*) FROM lcm_extraction_queue").fetchone()[0] == 0


def test_fk_cascade_entity_to_mentions(migrated_db: sqlite3.Connection) -> None:
    """Deleting an entity cascades to entity_mentions."""
    conv_id = _insert_conversation(migrated_db)
    _insert_summary(migrated_db, "s1", conv_id)
    migrated_db.execute(
        "INSERT INTO lcm_entities (entity_id, session_key, canonical_text, "
        " entity_type, first_seen_at, last_seen_at) "
        "VALUES ('e1', 'sk', 'Foo', 'person', '2026-01-01', '2026-01-01')"
    )
    migrated_db.execute(
        "INSERT INTO lcm_entity_mentions "
        "(mention_id, entity_id, summary_id, surface_form, mentioned_at) "
        "VALUES ('m1', 'e1', 's1', 'foo', '2026-01-01')"
    )
    assert migrated_db.execute("SELECT COUNT(*) FROM lcm_entity_mentions").fetchone()[0] == 1

    migrated_db.execute("DELETE FROM lcm_entities WHERE entity_id = 'e1'")
    assert migrated_db.execute("SELECT COUNT(*) FROM lcm_entity_mentions").fetchone()[0] == 0


def test_fk_cascade_conversation_to_session_key_audit(
    migrated_db: sqlite3.Connection,
) -> None:
    """Deleting a conversation cascades to session_key_audit rows."""
    conv_id = _insert_conversation(migrated_db)
    migrated_db.execute(
        "INSERT INTO lcm_session_key_audit "
        "(audit_id, conversation_id, original_session_key, new_session_key, reason) "
        "VALUES ('a1', ?, NULL, 'agent:main:main', 'rekey')",
        (conv_id,),
    )
    assert migrated_db.execute("SELECT COUNT(*) FROM lcm_session_key_audit").fetchone()[0] == 1

    migrated_db.execute("DELETE FROM conversations WHERE conversation_id = ?", (conv_id,))
    assert migrated_db.execute("SELECT COUNT(*) FROM lcm_session_key_audit").fetchone()[0] == 0


def test_fk_cascade_summary_to_cache_leaf_refs(migrated_db: sqlite3.Connection) -> None:
    """Deleting a summary cascades to cache_leaf_refs (leaf_summary_id side)."""
    conv_id = _insert_conversation(migrated_db)
    _insert_summary(migrated_db, "leaf-1", conv_id)
    _insert_prompt(migrated_db)
    migrated_db.execute(
        "INSERT INTO lcm_synthesis_cache "
        "(cache_id, session_key, range_start, range_end, leaf_fingerprint, "
        " model_used, prompt_id, tier_label, source_leaf_ids, "
        " source_token_count, output_token_count, actual_range_covered, "
        " leaf_count_synthesized) "
        "VALUES ('c1', 's1', '2026-01-01', '2026-01-02', 'fp', 'm', 'p1', "
        "  'custom', '[]', 0, 0, '2026-01', 0)"
    )
    migrated_db.execute(
        "INSERT INTO lcm_cache_leaf_refs (cache_id, leaf_summary_id) VALUES ('c1', 'leaf-1')"
    )
    assert migrated_db.execute("SELECT COUNT(*) FROM lcm_cache_leaf_refs").fetchone()[0] == 1

    migrated_db.execute("DELETE FROM summaries WHERE summary_id = 'leaf-1'")
    assert migrated_db.execute("SELECT COUNT(*) FROM lcm_cache_leaf_refs").fetchone()[0] == 0


def test_fk_cascade_cache_to_audit(migrated_db: sqlite3.Connection) -> None:
    """Deleting a cache cascades to synthesis_audit (target_cache_id) rows."""
    _insert_prompt(migrated_db)
    migrated_db.execute(
        "INSERT INTO lcm_synthesis_cache "
        "(cache_id, session_key, range_start, range_end, leaf_fingerprint, "
        " model_used, prompt_id, tier_label, source_leaf_ids, "
        " source_token_count, output_token_count, actual_range_covered, "
        " leaf_count_synthesized) "
        "VALUES ('c1', 's1', '2026-01-01', '2026-01-02', 'fp', 'm', 'p1', "
        "  'custom', '[]', 0, 0, '2026-01', 0)"
    )
    migrated_db.execute(
        "INSERT INTO lcm_synthesis_audit "
        "(audit_id, pass_session_id, target_cache_id, prompt_id, pass_kind, "
        " pass_input_truncated, model_used) "
        "VALUES ('a1', 'sess1', 'c1', 'p1', 'single', 'in', 'm')"
    )
    assert migrated_db.execute("SELECT COUNT(*) FROM lcm_synthesis_audit").fetchone()[0] == 1

    migrated_db.execute("DELETE FROM lcm_synthesis_cache WHERE cache_id = 'c1'")
    assert migrated_db.execute("SELECT COUNT(*) FROM lcm_synthesis_audit").fetchone()[0] == 0


def test_fk_restrict_prompt_with_dependent_cache(migrated_db: sqlite3.Connection) -> None:
    """Deleting a prompt referenced by a cache row raises IntegrityError."""
    _insert_prompt(migrated_db)
    migrated_db.execute(
        "INSERT INTO lcm_synthesis_cache "
        "(cache_id, session_key, range_start, range_end, leaf_fingerprint, "
        " model_used, prompt_id, tier_label, source_leaf_ids, "
        " source_token_count, output_token_count, actual_range_covered, "
        " leaf_count_synthesized) "
        "VALUES ('c1', 's1', '2026-01-01', '2026-01-02', 'fp', 'm', 'p1', "
        "  'custom', '[]', 0, 0, '2026-01', 0)"
    )
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        migrated_db.execute("DELETE FROM lcm_prompt_registry WHERE prompt_id = 'p1'")


def test_fk_restrict_embedding_profile_with_meta(migrated_db: sqlite3.Connection) -> None:
    """Deleting an embedding_profile with active meta rows raises IntegrityError."""
    migrated_db.execute("INSERT INTO lcm_embedding_profile (model_name, dim) VALUES ('m1', 128)")
    migrated_db.execute(
        "INSERT INTO lcm_embedding_meta "
        "(embedded_id, embedded_kind, embedding_model, source_token_count) "
        "VALUES ('x', 'entity', 'm1', 10)"
    )
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        migrated_db.execute("DELETE FROM lcm_embedding_profile WHERE model_name = 'm1'")


# ---------------------------------------------------------------------------
# Polymorphic FK trigger behavior
# ---------------------------------------------------------------------------


def test_embedding_meta_cleanup_trigger_fires_on_summary_delete(
    migrated_db: sqlite3.Connection,
) -> None:
    """Deleting a summary triggers cleanup of lcm_embedding_meta rows."""
    conv_id = _insert_conversation(migrated_db)
    _insert_summary(migrated_db, "s1", conv_id)
    migrated_db.execute("INSERT INTO lcm_embedding_profile (model_name, dim) VALUES ('m1', 128)")
    migrated_db.execute(
        "INSERT INTO lcm_embedding_meta "
        "(embedded_id, embedded_kind, embedding_model, source_token_count) "
        "VALUES ('s1', 'summary', 'm1', 10)"
    )
    assert migrated_db.execute("SELECT COUNT(*) FROM lcm_embedding_meta").fetchone()[0] == 1

    migrated_db.execute("DELETE FROM summaries WHERE summary_id = 's1'")
    # Trigger fires AFTER DELETE: corresponding meta row gone.
    assert migrated_db.execute("SELECT COUNT(*) FROM lcm_embedding_meta").fetchone()[0] == 0


def test_embedding_meta_cleanup_trigger_does_not_touch_entity_kind(
    migrated_db: sqlite3.Connection,
) -> None:
    """The trigger only deletes embedded_kind='summary' rows; entity rows survive."""
    conv_id = _insert_conversation(migrated_db)
    _insert_summary(migrated_db, "s1", conv_id)
    migrated_db.execute("INSERT INTO lcm_embedding_profile (model_name, dim) VALUES ('m1', 128)")
    # Insert one summary-kind meta + one entity-kind meta with the SAME embedded_id.
    # The entity meta should survive the trigger fire.
    migrated_db.execute(
        "INSERT INTO lcm_embedding_meta "
        "(embedded_id, embedded_kind, embedding_model, source_token_count) "
        "VALUES ('s1', 'summary', 'm1', 10)"
    )
    migrated_db.execute(
        "INSERT INTO lcm_embedding_meta "
        "(embedded_id, embedded_kind, embedding_model, source_token_count) "
        "VALUES ('s1', 'entity', 'm1', 10)"
    )
    assert migrated_db.execute("SELECT COUNT(*) FROM lcm_embedding_meta").fetchone()[0] == 2

    migrated_db.execute("DELETE FROM summaries WHERE summary_id = 's1'")
    # Trigger fires AFTER DELETE: summary-kind meta gone, entity-kind survives.
    rows = migrated_db.execute(
        "SELECT embedded_kind FROM lcm_embedding_meta WHERE embedded_id = 's1'"
    ).fetchall()
    assert [r[0] for r in rows] == ["entity"]


# ---------------------------------------------------------------------------
# UNIQUE constraint behavior
# ---------------------------------------------------------------------------


def test_lcm_entities_canonical_unique_collate_nocase(
    migrated_db: sqlite3.Connection,
) -> None:
    """UNIQUE (session_key, canonical_text COLLATE NOCASE) rejects case-variant dupes."""
    migrated_db.execute(
        "INSERT INTO lcm_entities (entity_id, session_key, canonical_text, "
        " entity_type, first_seen_at, last_seen_at) "
        "VALUES ('e1', 's1', 'Foo', 'person', '2026-01-01', '2026-01-01')"
    )
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        migrated_db.execute(
            "INSERT INTO lcm_entities (entity_id, session_key, canonical_text, "
            " entity_type, first_seen_at, last_seen_at) "
            "VALUES ('e2', 's1', 'FOO', 'person', '2026-01-01', '2026-01-01')"
        )


def test_lcm_entities_canonical_unique_allows_distinct_session_keys(
    migrated_db: sqlite3.Connection,
) -> None:
    """The UNIQUE constraint is scoped per session_key; different sks coexist."""
    migrated_db.execute(
        "INSERT INTO lcm_entities (entity_id, session_key, canonical_text, "
        " entity_type, first_seen_at, last_seen_at) "
        "VALUES ('e1', 's1', 'Foo', 'person', '2026-01-01', '2026-01-01')"
    )
    migrated_db.execute(
        "INSERT INTO lcm_entities (entity_id, session_key, canonical_text, "
        " entity_type, first_seen_at, last_seen_at) "
        "VALUES ('e2', 's2', 'Foo', 'person', '2026-01-01', '2026-01-01')"
    )


def test_prompt_registry_null_safe_unique_index_rejects_null_collision(
    migrated_db: sqlite3.Connection,
) -> None:
    """The COALESCE-based UNIQUE index closes the NULL-tier_label collision gap.

    SQLite's plain UNIQUE treats multiple NULLs as distinct, so two rows
    with ``tier_label = NULL`` and otherwise identical lookup keys both
    insert. The COALESCE-based UNIQUE index treats NULL as ``''`` for
    indexing — so the collision is caught.
    """
    migrated_db.execute(
        "INSERT INTO lcm_prompt_registry "
        "(prompt_id, memory_type, tier_label, pass_kind, version, template) "
        "VALUES ('p1', 'episodic-leaf', NULL, 'single', 1, 't1')"
    )
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        migrated_db.execute(
            "INSERT INTO lcm_prompt_registry "
            "(prompt_id, memory_type, tier_label, pass_kind, version, template) "
            "VALUES ('p2', 'episodic-leaf', NULL, 'single', 1, 't2')"
        )


def test_synthesis_cache_unique_lookup_includes_tier_and_prompt(
    migrated_db: sqlite3.Connection,
) -> None:
    """LCM Wave-10: UNIQUE includes tier_label + prompt_id so distinct (tier,
    prompt) pairs don't collide.
    """
    _insert_prompt(migrated_db, "p1")
    _insert_prompt(migrated_db, "p2")
    # Same range + leaves + filter + tier='custom' but DIFFERENT prompts: should coexist.
    migrated_db.execute(
        "INSERT INTO lcm_synthesis_cache "
        "(cache_id, session_key, range_start, range_end, leaf_fingerprint, "
        " model_used, prompt_id, tier_label, source_leaf_ids, "
        " source_token_count, output_token_count, actual_range_covered, "
        " leaf_count_synthesized) "
        "VALUES ('c1', 's1', '2026-01-01', '2026-01-02', 'fp', 'm', 'p1', "
        "  'custom', '[]', 0, 0, '2026-01', 0)"
    )
    migrated_db.execute(
        "INSERT INTO lcm_synthesis_cache "
        "(cache_id, session_key, range_start, range_end, leaf_fingerprint, "
        " model_used, prompt_id, tier_label, source_leaf_ids, "
        " source_token_count, output_token_count, actual_range_covered, "
        " leaf_count_synthesized) "
        "VALUES ('c2', 's1', '2026-01-01', '2026-01-02', 'fp', 'm', 'p2', "
        "  'custom', '[]', 0, 0, '2026-01', 0)"
    )
    # Same prompt + tier collide on UNIQUE.
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        migrated_db.execute(
            "INSERT INTO lcm_synthesis_cache "
            "(cache_id, session_key, range_start, range_end, leaf_fingerprint, "
            " model_used, prompt_id, tier_label, source_leaf_ids, "
            " source_token_count, output_token_count, actual_range_covered, "
            " leaf_count_synthesized) "
            "VALUES ('c3', 's1', '2026-01-01', '2026-01-02', 'fp', 'm', 'p1', "
            "  'custom', '[]', 0, 0, '2026-01', 0)"
        )


def test_synthesis_cache_unique_lookup_separates_tier_labels(
    migrated_db: sqlite3.Connection,
) -> None:
    """LCM Wave-10: UNIQUE keys (..., tier_label, prompt_id) — distinct tiers coexist."""
    _insert_prompt(migrated_db)
    # Same prompt, different tier_label values: should coexist.
    migrated_db.execute(
        "INSERT INTO lcm_synthesis_cache "
        "(cache_id, session_key, range_start, range_end, leaf_fingerprint, "
        " model_used, prompt_id, tier_label, source_leaf_ids, "
        " source_token_count, output_token_count, actual_range_covered, "
        " leaf_count_synthesized) "
        "VALUES ('c1', 's1', '2026-01-01', '2026-01-02', 'fp', 'm', 'p1', "
        "  'custom', '[]', 0, 0, '2026-01', 0)"
    )
    migrated_db.execute(
        "INSERT INTO lcm_synthesis_cache "
        "(cache_id, session_key, range_start, range_end, leaf_fingerprint, "
        " model_used, prompt_id, tier_label, source_leaf_ids, "
        " source_token_count, output_token_count, actual_range_covered, "
        " leaf_count_synthesized) "
        "VALUES ('c2', 's1', '2026-01-01', '2026-01-02', 'fp', 'm', 'p1', "
        "  'filtered', '[]', 0, 0, '2026-01', 0)"
    )
    assert migrated_db.execute("SELECT COUNT(*) FROM lcm_synthesis_cache").fetchone()[0] == 2


# ---------------------------------------------------------------------------
# Cache-recreate path (the old-narrow-CHECK ratchet)
# ---------------------------------------------------------------------------


def test_cache_recreate_path_drops_and_recreates_with_widened_check() -> None:
    """A pre-existing DB with the old narrow CHECK gets the cache table
    DROPed and recreated. Orphaned ``lcm_synthesis_audit`` rows referencing
    ``target_cache_id`` are deleted before the DROP.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")

    # Step 1: Hand-construct the old narrow CHECK schema on a fresh DB. We
    # build only the necessary tables: lcm_prompt_registry (FK target),
    # lcm_synthesis_cache (with OLD CHECK), and lcm_synthesis_audit
    # (FK to cache).
    conn.execute(
        """
        CREATE TABLE lcm_prompt_registry (
          prompt_id TEXT NOT NULL PRIMARY KEY,
          memory_type TEXT NOT NULL,
          tier_label TEXT,
          pass_kind TEXT NOT NULL,
          version INTEGER NOT NULL,
          template TEXT NOT NULL,
          model_recommendation TEXT,
          created_at TEXT NOT NULL DEFAULT (datetime('now')),
          active INTEGER NOT NULL DEFAULT 1,
          bundle_version INTEGER NOT NULL DEFAULT 1,
          notes TEXT,
          UNIQUE(memory_type, tier_label, pass_kind, version)
        )
        """
    )
    # OLD narrow CHECK: only 'year','custom','filtered'. Missing 'monthly' marker.
    conn.execute(
        """
        CREATE TABLE lcm_synthesis_cache (
          cache_id TEXT NOT NULL PRIMARY KEY,
          session_key TEXT NOT NULL,
          range_start TEXT NOT NULL,
          range_end TEXT NOT NULL,
          grep_filter TEXT,
          leaf_fingerprint TEXT NOT NULL,
          content TEXT,
          entity_index TEXT NOT NULL DEFAULT '{}',
          model_used TEXT NOT NULL,
          prompt_id TEXT NOT NULL REFERENCES lcm_prompt_registry(prompt_id) ON DELETE RESTRICT,
          tier_label TEXT NOT NULL CHECK (tier_label IN ('year', 'custom', 'filtered')),
          source_leaf_ids TEXT NOT NULL,
          source_condensed_ids TEXT,
          built_at TEXT NOT NULL DEFAULT (datetime('now')),
          source_token_count INTEGER NOT NULL,
          output_token_count INTEGER NOT NULL,
          actual_range_covered TEXT NOT NULL,
          leaf_count_synthesized INTEGER NOT NULL,
          status TEXT NOT NULL DEFAULT 'ready',
          building_started_at TEXT,
          failure_reason TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE lcm_synthesis_audit (
          audit_id TEXT NOT NULL PRIMARY KEY,
          pass_session_id TEXT NOT NULL,
          target_summary_id TEXT,
          target_cache_id TEXT REFERENCES lcm_synthesis_cache(cache_id) ON DELETE CASCADE,
          prompt_id TEXT NOT NULL REFERENCES lcm_prompt_registry(prompt_id) ON DELETE RESTRICT,
          pass_kind TEXT NOT NULL,
          pass_input_truncated TEXT NOT NULL,
          pass_output TEXT,
          status TEXT NOT NULL DEFAULT 'started',
          model_used TEXT NOT NULL,
          latency_ms INTEGER,
          cost_usd_cents INTEGER,
          last_error TEXT,
          ran_at TEXT NOT NULL DEFAULT (datetime('now')),
          CHECK (target_summary_id IS NOT NULL OR target_cache_id IS NOT NULL)
        )
        """
    )
    # Seed prompt + cache row + audit row referencing the cache.
    conn.execute(
        "INSERT INTO lcm_prompt_registry "
        "(prompt_id, memory_type, tier_label, pass_kind, version, template) "
        "VALUES ('p1', 'episodic-leaf', 'leaf', 'single', 1, 't')"
    )
    conn.execute(
        "INSERT INTO lcm_synthesis_cache "
        "(cache_id, session_key, range_start, range_end, leaf_fingerprint, "
        " model_used, prompt_id, tier_label, source_leaf_ids, "
        " source_token_count, output_token_count, actual_range_covered, "
        " leaf_count_synthesized) "
        "VALUES ('c1', 's1', '2026-01-01', '2026-01-02', 'fp', 'm', 'p1', "
        "  'custom', '[]', 0, 0, '2026-01', 0)"
    )
    conn.execute(
        "INSERT INTO lcm_synthesis_audit "
        "(audit_id, pass_session_id, target_cache_id, prompt_id, pass_kind, "
        " pass_input_truncated, model_used) "
        "VALUES ('a1', 'sess1', 'c1', 'p1', 'single', 'in', 'm')"
    )

    # Sanity: pre-migration the old CHECK rejects 'yearly'.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO lcm_synthesis_cache "
            "(cache_id, session_key, range_start, range_end, leaf_fingerprint, "
            " model_used, prompt_id, tier_label, source_leaf_ids, "
            " source_token_count, output_token_count, actual_range_covered, "
            " leaf_count_synthesized) "
            "VALUES ('c-y', 's1', '2026-01-01', '2026-12-31', 'fp', 'm', 'p1', "
            "  'yearly', '[]', 0, 0, '2026', 0)"
        )

    # Commit the inserts — Python's stdlib sqlite3 auto-opens a transaction
    # on DML, but ``run_lcm_migrations`` starts its own ``BEGIN EXCLUSIVE``
    # and would fail with "cannot start a transaction within a transaction"
    # if one were open. Production callers use ``open_lcm_db`` which sets
    # the isolation level; tests need to commit explicitly.
    conn.commit()

    # Step 2: Run the migration. This triggers the cache-recreate path.
    run_lcm_migrations(conn)

    # Step 3: Verify the cache table now has the widened CHECK and that
    # the orphaned audit row was pruned BEFORE the DROP.
    cache_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='lcm_synthesis_cache'"
    ).fetchone()
    assert cache_sql_row is not None
    assert "monthly" in cache_sql_row[0]
    assert "yearly" in cache_sql_row[0]

    # The cache row was wiped (rebuildable by design).
    assert conn.execute("SELECT COUNT(*) FROM lcm_synthesis_cache").fetchone()[0] == 0

    # The orphan audit row pointing at the dropped cache_id was pruned.
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM lcm_synthesis_audit WHERE target_cache_id IS NOT NULL"
        ).fetchone()[0]
        == 0
    )

    # Now 'yearly' is accepted.
    conn.execute(
        "INSERT INTO lcm_synthesis_cache "
        "(cache_id, session_key, range_start, range_end, leaf_fingerprint, "
        " model_used, prompt_id, tier_label, source_leaf_ids, "
        " source_token_count, output_token_count, actual_range_covered, "
        " leaf_count_synthesized) "
        "VALUES ('c-y', 's1', '2026-01-01', '2026-12-31', 'fp', 'm', 'p1', "
        "  'yearly', '[]', 0, 0, '2026', 0)"
    )
    conn.close()


def test_cache_recreate_path_skipped_when_already_widened(
    migrated_db: sqlite3.Connection,
) -> None:
    """A DB with the widened CHECK (the standard fresh-DB path) is not touched."""
    _insert_prompt(migrated_db)
    migrated_db.execute(
        "INSERT INTO lcm_synthesis_cache "
        "(cache_id, session_key, range_start, range_end, leaf_fingerprint, "
        " model_used, prompt_id, tier_label, source_leaf_ids, "
        " source_token_count, output_token_count, actual_range_covered, "
        " leaf_count_synthesized) "
        "VALUES ('c1', 's1', '2026-01-01', '2026-01-02', 'fp', 'm', 'p1', "
        "  'custom', '[]', 0, 0, '2026-01', 0)"
    )
    # Commit so ``run_lcm_migrations``'s BEGIN EXCLUSIVE can start cleanly.
    migrated_db.commit()
    schema_before = _snapshot_schema(migrated_db)
    cache_rows_before = migrated_db.execute("SELECT * FROM lcm_synthesis_cache").fetchall()

    # Second migration run: should be a no-op.
    run_lcm_migrations(migrated_db)

    schema_after = _snapshot_schema(migrated_db)
    cache_rows_after = migrated_db.execute("SELECT * FROM lcm_synthesis_cache").fetchall()
    assert schema_before == schema_after
    # Cache rows survive (NOT dropped).
    assert cache_rows_before == cache_rows_after


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_run_lcm_migrations_idempotent_with_v41_layer(fresh_db: sqlite3.Connection) -> None:
    """Running the full migration ladder twice is a no-op (including v4.1)."""
    run_lcm_migrations(fresh_db)
    snapshot_1 = _snapshot_schema(fresh_db)

    run_lcm_migrations(fresh_db)
    snapshot_2 = _snapshot_schema(fresh_db)

    assert snapshot_1 == snapshot_2


def test_ensure_v41_tables_idempotent(migrated_db: sqlite3.Connection) -> None:
    """Calling ``_ensure_v41_tables`` on an already-migrated DB is a no-op."""
    schema_before = _snapshot_schema(migrated_db)
    _ensure_v41_tables(migrated_db)
    schema_after = _snapshot_schema(migrated_db)
    assert schema_before == schema_after


def test_ensure_core_triggers_idempotent(migrated_db: sqlite3.Connection) -> None:
    """Calling ``_ensure_core_triggers`` on an already-migrated DB is a no-op."""
    schema_before = _snapshot_schema(migrated_db)
    _ensure_core_triggers(migrated_db)
    schema_after = _snapshot_schema(migrated_db)
    assert schema_before == schema_after


# ---------------------------------------------------------------------------
# Partial index sanity
# ---------------------------------------------------------------------------


def test_extraction_queue_pending_idx_is_partial(migrated_db: sqlite3.Connection) -> None:
    """The pending_idx is a partial index WHERE picked_at IS NULL."""
    row = migrated_db.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='lcm_extraction_queue_pending_idx'"
    ).fetchone()
    assert row is not None
    assert "WHERE picked_at IS NULL" in row[0]


def test_extraction_queue_dead_letter_idx_is_partial(
    migrated_db: sqlite3.Connection,
) -> None:
    """The dead_letter_idx is a partial index WHERE attempts >= 5."""
    row = migrated_db.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='lcm_extraction_queue_dead_letter_idx'"
    ).fetchone()
    assert row is not None
    assert "WHERE attempts >= 5" in row[0]


def test_synthesis_cache_status_building_idx_is_partial(
    migrated_db: sqlite3.Connection,
) -> None:
    """The status_building_idx is a partial index WHERE status = 'building'."""
    row = migrated_db.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='lcm_synthesis_cache_status_building_idx'"
    ).fetchone()
    assert row is not None
    assert "WHERE status = 'building'" in row[0]


def test_synthesis_audit_started_gc_idx_is_partial(
    migrated_db: sqlite3.Connection,
) -> None:
    """The started_gc_idx is a partial index WHERE status = 'started'."""
    row = migrated_db.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='lcm_synthesis_audit_started_gc_idx'"
    ).fetchone()
    assert row is not None
    assert "WHERE status = 'started'" in row[0]


def test_synthesis_audit_completed_gc_idx_is_partial(
    migrated_db: sqlite3.Connection,
) -> None:
    """LCM Wave-3 fix: the parallel GC index WHERE status IN ('completed','failed')."""
    row = migrated_db.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='lcm_synthesis_audit_completed_gc_idx'"
    ).fetchone()
    assert row is not None
    assert "completed" in row[0]
    assert "failed" in row[0]


def test_embedding_meta_active_idx_is_partial(migrated_db: sqlite3.Connection) -> None:
    """The active_idx is a partial index WHERE archived = 0."""
    row = migrated_db.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='lcm_embedding_meta_active_idx'"
    ).fetchone()
    assert row is not None
    assert "WHERE archived = 0" in row[0]


# ---------------------------------------------------------------------------
# Reference-fixture parity — diff against the TS-generated golden
# ---------------------------------------------------------------------------


def _parse_reference_objects() -> dict[str, str]:
    """Parse the reference SQL fixture into {name: normalized_sql}."""
    text = _REFERENCE_SCHEMA_PATH.read_text(encoding="utf-8")
    objects: dict[str, str] = {}
    current_name: str | None = None
    current_sql_lines: list[str] = []

    def _flush() -> None:
        nonlocal current_name, current_sql_lines
        if current_name and current_sql_lines:
            raw = "\n".join(current_sql_lines).strip().rstrip(";").strip()
            objects[current_name] = _normalize_sql(raw)
        current_name = None
        current_sql_lines = []

    for line in text.splitlines():
        m = re.match(r"^-- (table|index|trigger|view): (\S+)\s*$", line)
        if m:
            _flush()
            current_name = m.group(2)
            continue
        if line.strip() == "-- pragmas":
            _flush()
            break
        if current_name is not None and not line.startswith("--"):
            current_sql_lines.append(line)
    _flush()
    return objects


def test_reference_fixture_has_all_v41_objects() -> None:
    """The reference fixture contains every v4.1 object created in this PR."""
    if not _REFERENCE_SCHEMA_PATH.exists():
        pytest.skip("reference fixture not present")

    ref = _parse_reference_objects()
    for table_name in _EXPECTED_V41_TABLES:
        assert table_name in ref, f"v4.1 table {table_name!r} missing from reference fixture"
    for index_name in _EXPECTED_V41_INDEXES:
        assert index_name in ref, f"v4.1 index {index_name!r} missing from reference fixture"
    assert "lcm_embedding_meta_cleanup_summary" in ref, "trigger missing from reference fixture"


@pytest.mark.parametrize("table_name", _EXPECTED_V41_TABLES)
def test_python_v41_table_sql_matches_reference(
    migrated_db: sqlite3.Connection, table_name: str
) -> None:
    """Python-generated v4.1 table DDL matches the TS reference (whitespace-insensitive)."""
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


@pytest.mark.parametrize("index_name", _EXPECTED_V41_INDEXES)
def test_python_v41_index_sql_matches_reference(
    migrated_db: sqlite3.Connection, index_name: str
) -> None:
    """Python-generated v4.1 index DDL matches the TS reference (whitespace-insensitive)."""
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


def test_python_trigger_sql_matches_reference(migrated_db: sqlite3.Connection) -> None:
    """The trigger DDL matches the TS reference (whitespace-insensitive)."""
    if not _REFERENCE_SCHEMA_PATH.exists():
        pytest.skip("reference fixture not present")

    ref = _parse_reference_objects()
    trigger_name = "lcm_embedding_meta_cleanup_summary"
    if trigger_name not in ref:
        pytest.skip(f"trigger {trigger_name!r} not in reference fixture")

    py_sql_row = migrated_db.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
        (trigger_name,),
    ).fetchone()
    assert py_sql_row is not None, f"trigger {trigger_name!r} not created"
    py_sql = py_sql_row[0]
    assert py_sql is not None
    py_sql_norm = _normalize_sql(py_sql.rstrip(";").strip())

    assert py_sql_norm == ref[trigger_name], (
        f"trigger SQL diverges from TS reference:\n  py: {py_sql_norm}\n  ts: {ref[trigger_name]}"
    )


# ---------------------------------------------------------------------------
# Wave-N provenance markers (ADR-029)
# ---------------------------------------------------------------------------


def test_wave_n_markers_present_in_source() -> None:
    """ADR-029: Wave-1/2/3/10 markers must be present in migration.py.

    The TS source carries Wave-1/2/3/10 comments inside the v4.1 layer. The
    Python port MUST preserve them so refactor PRs that touch these lines
    force the contributor to confront the scar-tissue rationale.
    """
    src = (
        Path(__file__).parent.parent / "src" / "lossless_hermes" / "db" / "migration.py"
    ).read_text(encoding="utf-8")

    # Wave-1: cache-recreate prunes orphaned audit rows.
    assert "# LCM Wave-1 (2025-11-08):" in src
    assert "audit" in src.lower()
    # Wave-2: narrow the catch to expected error.
    assert "# LCM Wave-2 (2025-12-04):" in src
    # Wave-10: UNIQUE includes tier_label + prompt_id.
    assert "# LCM Wave-10 (2026-03-22):" in src
    # Wave-3: parallel completed/failed GC index.
    assert "# LCM Wave-3 (2026-01-09):" in src
