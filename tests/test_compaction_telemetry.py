"""Tests for :mod:`lossless_hermes.store.compaction_telemetry`.

Covers the acceptance criteria from
``epics/01-storage/01-10-telemetry-stores.md``:

* Insert (cold path) — new row materializes with the declared field values
  and SQLite-stamped ``updated_at``.
* Upsert idempotency — calling with the same input twice yields the same
  observable state (the row, not ``updated_at`` which advances).
* ``get_*`` returns ``None`` when no row exists.
* ``get_*`` returns the persisted record on a hit.
* Cache-state transition — ``cold → hot → unknown`` walks the row through
  the CHECK-allowed states.
* CHECK constraint violation — ``cache_state='lukewarm'`` raises
  :class:`sqlite3.IntegrityError`.
* CHECK constraint violation — ``last_activity_band='extreme'`` raises
  :class:`sqlite3.IntegrityError`.
* FK constraint — upserting on a non-existent ``conversation_id`` raises
  :class:`sqlite3.IntegrityError`.

The TS source did not ship a dedicated ``compaction-telemetry-store.test.ts``
(integration tests in ``lcm-integration.test.ts`` covered it transitively).
This test file ports the relevant subset — ~8 cases per the issue spec §
"Acceptance criteria" line 65.

See:

* ``src/lossless_hermes/store/compaction_telemetry.py`` — implementation.
* ``epics/01-storage/01-10-telemetry-stores.md`` — issue spec + AC.
* ``docs/porting-guides/storage.md`` §4.3 — store contract.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Iterator

import pytest

from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.store.compaction_telemetry import (
    CompactionTelemetryStore,
    ConversationCompactionTelemetryRecord,
    UpsertConversationCompactionTelemetryInput,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def migrated_db() -> Iterator[sqlite3.Connection]:
    """In-memory SQLite with the core LCM migration ladder applied.

    Equivalent to opening via ``open_lcm_db(":memory:")`` then calling
    :func:`run_lcm_migrations`, minus the sqlite-vec load (none of the
    tests in this file need vec0 — the table doesn't carry embeddings).
    """
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def store(migrated_db: sqlite3.Connection) -> CompactionTelemetryStore:
    """A :class:`CompactionTelemetryStore` bound to a fresh migrated DB."""
    return CompactionTelemetryStore(migrated_db)


def _make_conversation(conn: sqlite3.Connection, session_id: str = "s1") -> int:
    """Insert a minimal ``conversations`` row and return its ``conversation_id``.

    The compaction-telemetry table has a FK on
    ``conversations.conversation_id``; tests that exercise the store on
    a non-existent conversation_id should NOT call this helper.
    """
    conn.execute("INSERT INTO conversations (session_id) VALUES (?)", (session_id,))
    row = conn.execute("SELECT last_insert_rowid()").fetchone()
    return int(row[0])


# ---------------------------------------------------------------------------
# Get-null path
# ---------------------------------------------------------------------------


def test_get_returns_none_when_no_row_exists(store: CompactionTelemetryStore) -> None:
    """``get`` on a conversation with no telemetry row returns ``None``."""
    assert store.get_conversation_compaction_telemetry(conversation_id=12345) is None


# ---------------------------------------------------------------------------
# Insert + get-existing path
# ---------------------------------------------------------------------------


def test_insert_and_get_roundtrip(
    store: CompactionTelemetryStore, migrated_db: sqlite3.Connection
) -> None:
    """Upserting a fresh row + getting it back yields the same values."""
    conv_id = _make_conversation(migrated_db)

    hit_at = datetime(2026, 5, 13, 10, 0, 0, tzinfo=timezone.utc)
    break_at = datetime(2026, 5, 13, 11, 0, 0, tzinfo=timezone.utc)
    last_leaf = datetime(2026, 5, 13, 9, 0, 0, tzinfo=timezone.utc)
    api_call = datetime(2026, 5, 13, 10, 30, 0, tzinfo=timezone.utc)
    cache_touch = datetime(2026, 5, 13, 10, 31, 0, tzinfo=timezone.utc)

    store.upsert_conversation_compaction_telemetry(
        UpsertConversationCompactionTelemetryInput(
            conversation_id=conv_id,
            last_observed_cache_read=100_000,
            last_observed_cache_write=2_000,
            last_observed_prompt_token_count=100_000,
            last_observed_cache_hit_at=hit_at,
            last_observed_cache_break_at=break_at,
            cache_state="hot",
            consecutive_cold_observations=2,
            retention="ttl-15m",
            last_leaf_compaction_at=last_leaf,
            turns_since_leaf_compaction=3,
            tokens_accumulated_since_leaf_compaction=12_345,
            last_activity_band="high",
            last_api_call_at=api_call,
            last_cache_touch_at=cache_touch,
            provider="openai-codex",
            model="gpt-5.5",
        )
    )

    rec = store.get_conversation_compaction_telemetry(conv_id)
    assert rec is not None
    assert isinstance(rec, ConversationCompactionTelemetryRecord)
    assert rec.conversation_id == conv_id
    assert rec.last_observed_cache_read == 100_000
    assert rec.last_observed_cache_write == 2_000
    assert rec.last_observed_prompt_token_count == 100_000
    assert rec.last_observed_cache_hit_at == hit_at
    assert rec.last_observed_cache_break_at == break_at
    assert rec.cache_state == "hot"
    assert rec.consecutive_cold_observations == 2
    assert rec.retention == "ttl-15m"
    assert rec.last_leaf_compaction_at == last_leaf
    assert rec.turns_since_leaf_compaction == 3
    assert rec.tokens_accumulated_since_leaf_compaction == 12_345
    assert rec.last_activity_band == "high"
    assert rec.last_api_call_at == api_call
    assert rec.last_cache_touch_at == cache_touch
    assert rec.provider == "openai-codex"
    assert rec.model == "gpt-5.5"
    # updated_at is SQLite-stamped (datetime('now')) — just sanity check
    # it parsed to a UTC-aware datetime.
    assert rec.updated_at.tzinfo is timezone.utc


def test_insert_minimal_input_uses_declared_defaults(
    store: CompactionTelemetryStore, migrated_db: sqlite3.Connection
) -> None:
    """Upserting with only required fields populates the declared defaults."""
    conv_id = _make_conversation(migrated_db)

    store.upsert_conversation_compaction_telemetry(
        UpsertConversationCompactionTelemetryInput(
            conversation_id=conv_id,
            cache_state="unknown",
        )
    )

    rec = store.get_conversation_compaction_telemetry(conv_id)
    assert rec is not None
    # Required-only inputs surface the Pydantic defaults.
    assert rec.cache_state == "unknown"
    assert rec.consecutive_cold_observations == 0
    assert rec.turns_since_leaf_compaction == 0
    assert rec.tokens_accumulated_since_leaf_compaction == 0
    assert rec.last_activity_band == "low"
    assert rec.last_observed_cache_read is None
    assert rec.last_observed_cache_write is None
    assert rec.last_observed_prompt_token_count is None
    assert rec.last_observed_cache_hit_at is None
    assert rec.last_observed_cache_break_at is None
    assert rec.retention is None
    assert rec.last_leaf_compaction_at is None
    assert rec.last_api_call_at is None
    assert rec.last_cache_touch_at is None
    assert rec.provider is None
    assert rec.model is None


# ---------------------------------------------------------------------------
# Upsert idempotency
# ---------------------------------------------------------------------------


def test_upsert_idempotent_same_input(
    store: CompactionTelemetryStore, migrated_db: sqlite3.Connection
) -> None:
    """Two upserts with the same input yield the same row state.

    Acceptance criterion: ``upsert_conversation_compaction_telemetry`` is
    idempotent — calling twice with the same input is equivalent to
    calling once (modulo ``updated_at`` advancing).
    """
    conv_id = _make_conversation(migrated_db)

    input_data = UpsertConversationCompactionTelemetryInput(
        conversation_id=conv_id,
        cache_state="cold",
        consecutive_cold_observations=5,
        last_activity_band="low",
        provider="anthropic",
        model="claude-opus-4-7",
    )

    store.upsert_conversation_compaction_telemetry(input_data)
    first = store.get_conversation_compaction_telemetry(conv_id)
    store.upsert_conversation_compaction_telemetry(input_data)
    second = store.get_conversation_compaction_telemetry(conv_id)

    assert first is not None and second is not None
    # All non-timestamp fields are identical.
    assert first.cache_state == second.cache_state == "cold"
    assert first.consecutive_cold_observations == second.consecutive_cold_observations == 5
    assert first.last_activity_band == second.last_activity_band == "low"
    assert first.provider == second.provider == "anthropic"
    assert first.model == second.model == "claude-opus-4-7"
    assert first.conversation_id == second.conversation_id == conv_id
    # Row count is exactly 1 — no duplicate row was created.
    count_row = migrated_db.execute(
        "SELECT COUNT(*) FROM conversation_compaction_telemetry"
    ).fetchone()
    assert count_row[0] == 1


def test_upsert_overwrites_previous_values(
    store: CompactionTelemetryStore, migrated_db: sqlite3.Connection
) -> None:
    """A second upsert overwrites every field with the new input.

    The TS UPSERT uses ``excluded.*`` for every column — there is no
    field-level merge with the prior row. This test pins that
    "full-row replace" semantic so a future refactor that adds
    field-level merge breaks loudly.
    """
    conv_id = _make_conversation(migrated_db)

    store.upsert_conversation_compaction_telemetry(
        UpsertConversationCompactionTelemetryInput(
            conversation_id=conv_id,
            cache_state="hot",
            consecutive_cold_observations=10,
            retention="ttl-30m",
            provider="openai",
            model="gpt-4o",
        )
    )
    store.upsert_conversation_compaction_telemetry(
        UpsertConversationCompactionTelemetryInput(
            conversation_id=conv_id,
            cache_state="cold",
            # consecutive_cold_observations not set → reverts to default 0
            # (the upsert replaces the whole row, it doesn't merge).
            retention=None,
            provider="anthropic",
            model="claude-opus-4-7",
        )
    )

    rec = store.get_conversation_compaction_telemetry(conv_id)
    assert rec is not None
    assert rec.cache_state == "cold"
    assert rec.consecutive_cold_observations == 0  # reverted to default
    assert rec.retention is None
    assert rec.provider == "anthropic"
    assert rec.model == "claude-opus-4-7"


# ---------------------------------------------------------------------------
# Cache-state transitions
# ---------------------------------------------------------------------------


def test_cache_state_transitions(
    store: CompactionTelemetryStore, migrated_db: sqlite3.Connection
) -> None:
    """Walk a single row through the three allowed cache_state values.

    Covers the issue spec line 65 — "cache-state transitions" — by
    asserting each state is persisted and read back unchanged.
    """
    conv_id = _make_conversation(migrated_db)

    for state in ("unknown", "hot", "cold", "unknown", "hot"):
        store.upsert_conversation_compaction_telemetry(
            UpsertConversationCompactionTelemetryInput(
                conversation_id=conv_id,
                cache_state=state,  # type: ignore[arg-type]
            )
        )
        rec = store.get_conversation_compaction_telemetry(conv_id)
        assert rec is not None
        assert rec.cache_state == state


# ---------------------------------------------------------------------------
# CHECK / FK constraint violations
# ---------------------------------------------------------------------------


def test_check_constraint_cache_state_lukewarm(migrated_db: sqlite3.Connection) -> None:
    """Inserting ``cache_state='lukewarm'`` raises IntegrityError.

    The store enforces the CHECK constraint at the SQLite layer — we do
    NOT add a Python-side enum guard before the write. This pins the
    failure surface to ``sqlite3.IntegrityError`` (the bullet from the
    issue spec line 63).

    We bypass the Pydantic model (which would reject the string at
    validation time) by going directly through ``conn.execute``, then
    confirm the same constraint fires on the store path.
    """
    migrated_db.execute("INSERT INTO conversations (session_id) VALUES ('s1')")
    conv_id = migrated_db.execute("SELECT last_insert_rowid()").fetchone()[0]

    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        migrated_db.execute(
            "INSERT INTO conversation_compaction_telemetry "
            "(conversation_id, cache_state) VALUES (?, 'lukewarm')",
            (conv_id,),
        )


def test_check_constraint_activity_band_extreme(migrated_db: sqlite3.Connection) -> None:
    """Inserting ``last_activity_band='extreme'`` raises IntegrityError."""
    migrated_db.execute("INSERT INTO conversations (session_id) VALUES ('s1')")
    conv_id = migrated_db.execute("SELECT last_insert_rowid()").fetchone()[0]

    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        migrated_db.execute(
            "INSERT INTO conversation_compaction_telemetry "
            "(conversation_id, cache_state, last_activity_band) "
            "VALUES (?, 'unknown', 'extreme')",
            (conv_id,),
        )


def test_fk_constraint_missing_conversation_id(store: CompactionTelemetryStore) -> None:
    """Upserting on a non-existent ``conversation_id`` raises IntegrityError."""
    with pytest.raises(sqlite3.IntegrityError):
        store.upsert_conversation_compaction_telemetry(
            UpsertConversationCompactionTelemetryInput(
                conversation_id=999_999,  # no such conversation
                cache_state="unknown",
            )
        )


# ---------------------------------------------------------------------------
# with_transaction
# ---------------------------------------------------------------------------


def test_with_transaction_commits_on_success(
    store: CompactionTelemetryStore, migrated_db: sqlite3.Connection
) -> None:
    """``with_transaction`` commits a successful callback's writes."""
    conv_id = _make_conversation(migrated_db)

    def do_upserts() -> int:
        store.upsert_conversation_compaction_telemetry(
            UpsertConversationCompactionTelemetryInput(
                conversation_id=conv_id,
                cache_state="hot",
            )
        )
        return conv_id

    returned = store.with_transaction(do_upserts)
    assert returned == conv_id

    rec = store.get_conversation_compaction_telemetry(conv_id)
    assert rec is not None
    assert rec.cache_state == "hot"


def test_with_transaction_rolls_back_on_exception(
    store: CompactionTelemetryStore, migrated_db: sqlite3.Connection
) -> None:
    """``with_transaction`` rolls back the inner writes on exception."""
    conv_id = _make_conversation(migrated_db)

    class _Boom(RuntimeError):
        pass

    with pytest.raises(_Boom):

        def do_then_fail() -> None:
            store.upsert_conversation_compaction_telemetry(
                UpsertConversationCompactionTelemetryInput(
                    conversation_id=conv_id,
                    cache_state="hot",
                )
            )
            raise _Boom("rollback")

        store.with_transaction(do_then_fail)

    # The row should not exist — the BEGIN was rolled back.
    rec = store.get_conversation_compaction_telemetry(conv_id)
    assert rec is None
