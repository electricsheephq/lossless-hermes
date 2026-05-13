"""Tests for :mod:`lossless_hermes.store.compaction_maintenance`.

Covers the acceptance criteria from
``epics/01-storage/01-10-telemetry-stores.md``:

* Port of ``test/compaction-maintenance-store.test.ts`` (the
  "pending and running flags transition back to false" case — storage.md
  §8 row 22).
* :meth:`mark_proactive_compaction_running` returns ``False`` when called
  on a row that's already running OR has no pending debt.
* :meth:`mark_proactive_compaction_running` returns ``True`` and updates
  ``last_started_at`` when the claim succeeds.
* :meth:`mark_proactive_compaction_finished` clears ``pending`` + ``running``
  on success (no failure_summary) and bumps ``last_finished_at``.
* :meth:`mark_proactive_compaction_finished` with ``failure_summary`` keeps
  ``pending=1``, clears ``running``, records the failure summary.
* :meth:`request_proactive_compaction_debt` is coalesced — two consecutive
  calls leave one row, not two.
* :meth:`request_proactive_compaction_debt` preserves prior
  ``token_budget`` / ``current_token_count`` when caller passes ``None``.
* ``get_*`` returns ``None`` when no row exists.
* FK constraint — request on a non-existent ``conversation_id`` raises
  :class:`sqlite3.IntegrityError`.

See:

* ``src/lossless_hermes/store/compaction_maintenance.py`` — implementation.
* ``epics/01-storage/01-10-telemetry-stores.md`` — issue spec + AC.
* ``docs/porting-guides/storage.md`` §4.4 — store contract.
* ``lossless-claw/test/compaction-maintenance-store.test.ts`` — TS source.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from typing import Iterator

import pytest

from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.store.compaction_maintenance import (
    CompactionMaintenanceStore,
    ConversationCompactionMaintenanceRecord,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def migrated_db() -> Iterator[sqlite3.Connection]:
    """In-memory SQLite with the core LCM migration ladder applied."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def store(migrated_db: sqlite3.Connection) -> CompactionMaintenanceStore:
    """A :class:`CompactionMaintenanceStore` bound to a fresh migrated DB."""
    return CompactionMaintenanceStore(migrated_db)


def _make_conversation(conn: sqlite3.Connection, session_id: str = "s1") -> int:
    """Insert a minimal ``conversations`` row and return its PK."""
    conn.execute("INSERT INTO conversations (session_id) VALUES (?)", (session_id,))
    row = conn.execute("SELECT last_insert_rowid()").fetchone()
    return int(row[0])


# ---------------------------------------------------------------------------
# Get-null path
# ---------------------------------------------------------------------------


def test_get_returns_none_when_no_row_exists(store: CompactionMaintenanceStore) -> None:
    """``get`` on a conversation with no maintenance row returns ``None``."""
    assert store.get_conversation_compaction_maintenance(conversation_id=12345) is None


# ---------------------------------------------------------------------------
# State-machine flow — full happy path
# ---------------------------------------------------------------------------


def test_pending_and_running_flags_transition_back_to_false(
    store: CompactionMaintenanceStore, migrated_db: sqlite3.Connection
) -> None:
    """Port of ``compaction-maintenance-store.test.ts`` (the only TS case).

    Walks the row through:
        request_debt → mark_running → mark_finished (success)
    and asserts the final state has ``pending=False, running=False``.

    Mirrors ``test/compaction-maintenance-store.test.ts`` lines 34-65.
    """
    conv_id = _make_conversation(migrated_db, session_id="maintenance-store-session")

    store.request_proactive_compaction_debt(
        conversation_id=conv_id,
        reason="threshold",
    )

    claimed = store.mark_proactive_compaction_running(conv_id)
    assert claimed is True

    store.mark_proactive_compaction_finished(conv_id)

    rec = store.get_conversation_compaction_maintenance(conv_id)
    assert rec is not None
    assert isinstance(rec, ConversationCompactionMaintenanceRecord)
    assert rec.pending is False
    assert rec.running is False


# ---------------------------------------------------------------------------
# request_proactive_compaction_debt
# ---------------------------------------------------------------------------


def test_request_debt_sets_pending_and_metadata(
    store: CompactionMaintenanceStore, migrated_db: sqlite3.Connection
) -> None:
    """A fresh request sets pending=1, records reason + budget + token count."""
    conv_id = _make_conversation(migrated_db)

    requested_at = datetime(2026, 5, 13, 12, 0, 0, tzinfo=timezone.utc)
    store.request_proactive_compaction_debt(
        conversation_id=conv_id,
        reason="cache-aware-defer",
        token_budget=200_000,
        current_token_count=120_000,
        requested_at=requested_at,
    )

    rec = store.get_conversation_compaction_maintenance(conv_id)
    assert rec is not None
    assert rec.pending is True
    assert rec.running is False
    assert rec.reason == "cache-aware-defer"
    assert rec.requested_at == requested_at
    assert rec.token_budget == 200_000
    assert rec.current_token_count == 120_000
    assert rec.last_started_at is None
    assert rec.last_finished_at is None
    assert rec.last_failure_summary is None
    assert rec.updated_at.tzinfo is timezone.utc


def test_request_debt_is_coalesced_no_queue(
    store: CompactionMaintenanceStore, migrated_db: sqlite3.Connection
) -> None:
    """Two consecutive requests collapse into one row (storage.md §4.4)."""
    conv_id = _make_conversation(migrated_db)

    store.request_proactive_compaction_debt(
        conversation_id=conv_id,
        reason="first",
        token_budget=200_000,
        current_token_count=100_000,
    )
    store.request_proactive_compaction_debt(
        conversation_id=conv_id,
        reason="second",
        token_budget=300_000,
        current_token_count=150_000,
    )

    count_row = migrated_db.execute(
        "SELECT COUNT(*) FROM conversation_compaction_maintenance"
    ).fetchone()
    assert count_row[0] == 1

    rec = store.get_conversation_compaction_maintenance(conv_id)
    assert rec is not None
    # Second writer's metadata wins.
    assert rec.reason == "second"
    assert rec.token_budget == 300_000
    assert rec.current_token_count == 150_000


def test_request_debt_preserves_existing_budget_when_none(
    store: CompactionMaintenanceStore, migrated_db: sqlite3.Connection
) -> None:
    """Calling with ``token_budget=None`` preserves the prior value.

    Mirrors the TS ``input.tokenBudget ?? existing?.tokenBudget ?? null``
    pattern on lines 176-177 of ``compaction-maintenance-store.ts``.
    """
    conv_id = _make_conversation(migrated_db)

    store.request_proactive_compaction_debt(
        conversation_id=conv_id,
        reason="first",
        token_budget=200_000,
        current_token_count=100_000,
    )
    store.request_proactive_compaction_debt(
        conversation_id=conv_id,
        reason="second",
        # token_budget + current_token_count omitted → preserve prior.
    )

    rec = store.get_conversation_compaction_maintenance(conv_id)
    assert rec is not None
    assert rec.token_budget == 200_000
    assert rec.current_token_count == 100_000
    assert rec.reason == "second"


def test_request_debt_default_requested_at_is_recent(
    store: CompactionMaintenanceStore, migrated_db: sqlite3.Connection
) -> None:
    """Default ``requested_at`` is approximately ``now`` (UTC)."""
    conv_id = _make_conversation(migrated_db)
    before = datetime.now(timezone.utc)
    store.request_proactive_compaction_debt(
        conversation_id=conv_id,
        reason="threshold",
    )
    after = datetime.now(timezone.utc)

    rec = store.get_conversation_compaction_maintenance(conv_id)
    assert rec is not None
    assert rec.requested_at is not None
    # Allow 1-second drift either way for microsecond rounding.
    assert before.timestamp() - 1 <= rec.requested_at.timestamp() <= after.timestamp() + 1


# ---------------------------------------------------------------------------
# mark_proactive_compaction_running — atomic compare-and-set
# ---------------------------------------------------------------------------


def test_mark_running_returns_false_with_no_pending_debt(
    store: CompactionMaintenanceStore, migrated_db: sqlite3.Connection
) -> None:
    """No row → no pending → claim fails → False."""
    conv_id = _make_conversation(migrated_db)
    # No request_proactive_compaction_debt call.

    claimed = store.mark_proactive_compaction_running(conv_id)
    assert claimed is False

    # No row should have been created by the failed claim.
    rec = store.get_conversation_compaction_maintenance(conv_id)
    assert rec is None


def test_mark_running_returns_true_when_pending_and_idle(
    store: CompactionMaintenanceStore, migrated_db: sqlite3.Connection
) -> None:
    """A pending + non-running row → claim succeeds → True."""
    conv_id = _make_conversation(migrated_db)
    store.request_proactive_compaction_debt(
        conversation_id=conv_id,
        reason="threshold",
    )

    claimed = store.mark_proactive_compaction_running(conv_id)
    assert claimed is True

    rec = store.get_conversation_compaction_maintenance(conv_id)
    assert rec is not None
    assert rec.running is True
    # Spec acceptance: last_started_at is set.
    assert rec.last_started_at is not None
    assert rec.last_started_at.tzinfo is timezone.utc


def test_mark_running_returns_false_when_already_running(
    store: CompactionMaintenanceStore, migrated_db: sqlite3.Connection
) -> None:
    """A row that is already running cannot be claimed again → False."""
    conv_id = _make_conversation(migrated_db)
    store.request_proactive_compaction_debt(
        conversation_id=conv_id,
        reason="threshold",
    )

    first_claim = store.mark_proactive_compaction_running(conv_id)
    assert first_claim is True

    second_claim = store.mark_proactive_compaction_running(conv_id)
    assert second_claim is False


def test_mark_running_returns_false_when_pending_is_false(
    store: CompactionMaintenanceStore, migrated_db: sqlite3.Connection
) -> None:
    """A row with pending=0 (after a successful finish) cannot be claimed."""
    conv_id = _make_conversation(migrated_db)
    store.request_proactive_compaction_debt(conversation_id=conv_id, reason="t")
    assert store.mark_proactive_compaction_running(conv_id) is True
    store.mark_proactive_compaction_finished(conv_id)

    # pending is now 0; a fresh claim must fail.
    claimed = store.mark_proactive_compaction_running(conv_id)
    assert claimed is False


def test_mark_running_advances_last_started_at_on_each_claim_cycle(
    store: CompactionMaintenanceStore, migrated_db: sqlite3.Connection
) -> None:
    """A new debt + claim cycle advances ``last_started_at``.

    Two complete cycles (request → claim → finish → request → claim)
    must produce strictly-non-decreasing ``last_started_at`` values
    (``datetime('now')`` advances at second resolution).
    """
    conv_id = _make_conversation(migrated_db)

    store.request_proactive_compaction_debt(conversation_id=conv_id, reason="cycle-1")
    assert store.mark_proactive_compaction_running(conv_id) is True
    rec1 = store.get_conversation_compaction_maintenance(conv_id)
    store.mark_proactive_compaction_finished(conv_id)

    # SQLite's datetime('now') has 1-second resolution; sleep 1.1s to
    # guarantee the second claim's last_started_at is strictly greater.
    time.sleep(1.1)

    store.request_proactive_compaction_debt(conversation_id=conv_id, reason="cycle-2")
    assert store.mark_proactive_compaction_running(conv_id) is True
    rec2 = store.get_conversation_compaction_maintenance(conv_id)

    assert rec1 is not None and rec2 is not None
    assert rec1.last_started_at is not None and rec2.last_started_at is not None
    assert rec2.last_started_at > rec1.last_started_at


# ---------------------------------------------------------------------------
# mark_proactive_compaction_finished — success vs failure
# ---------------------------------------------------------------------------


def test_finish_success_clears_pending_and_running(
    store: CompactionMaintenanceStore, migrated_db: sqlite3.Connection
) -> None:
    """Success path (no failure_summary): pending=0, running=0, finished_at set."""
    conv_id = _make_conversation(migrated_db)
    store.request_proactive_compaction_debt(conversation_id=conv_id, reason="t")
    store.mark_proactive_compaction_running(conv_id)

    store.mark_proactive_compaction_finished(conv_id)

    rec = store.get_conversation_compaction_maintenance(conv_id)
    assert rec is not None
    assert rec.pending is False
    assert rec.running is False
    assert rec.last_finished_at is not None
    assert rec.last_failure_summary is None


def test_finish_failure_keeps_pending_records_summary(
    store: CompactionMaintenanceStore, migrated_db: sqlite3.Connection
) -> None:
    """Failure path: pending stays 1, running clears, summary recorded."""
    conv_id = _make_conversation(migrated_db)
    store.request_proactive_compaction_debt(conversation_id=conv_id, reason="t")
    store.mark_proactive_compaction_running(conv_id)

    store.mark_proactive_compaction_finished(conv_id, failure_summary="llm rate-limited")

    rec = store.get_conversation_compaction_maintenance(conv_id)
    assert rec is not None
    assert rec.pending is True
    assert rec.running is False
    assert rec.last_finished_at is not None
    assert rec.last_failure_summary == "llm rate-limited"


def test_finish_success_after_failure_clears_failure_summary(
    store: CompactionMaintenanceStore, migrated_db: sqlite3.Connection
) -> None:
    """A successful retry after a failed attempt clears the prior summary."""
    conv_id = _make_conversation(migrated_db)
    store.request_proactive_compaction_debt(conversation_id=conv_id, reason="t")
    store.mark_proactive_compaction_running(conv_id)
    store.mark_proactive_compaction_finished(conv_id, failure_summary="boom")

    # Re-claim and succeed.
    assert store.mark_proactive_compaction_running(conv_id) is True
    store.mark_proactive_compaction_finished(conv_id)  # success

    rec = store.get_conversation_compaction_maintenance(conv_id)
    assert rec is not None
    assert rec.pending is False
    assert rec.running is False
    assert rec.last_failure_summary is None


def test_finish_on_nonexistent_row_is_noop(
    store: CompactionMaintenanceStore, migrated_db: sqlite3.Connection
) -> None:
    """Finishing a conversation with no maintenance row is a no-op.

    Pins the documented "if no row exists, this is a no-op" semantic
    on :meth:`CompactionMaintenanceStore.mark_proactive_compaction_finished`.
    """
    conv_id = _make_conversation(migrated_db)
    # No request_debt call.

    # Should not raise.
    store.mark_proactive_compaction_finished(conv_id)
    store.mark_proactive_compaction_finished(conv_id, failure_summary="x")

    # No row should have been created.
    rec = store.get_conversation_compaction_maintenance(conv_id)
    assert rec is None


# ---------------------------------------------------------------------------
# FK constraint
# ---------------------------------------------------------------------------


def test_fk_constraint_missing_conversation_id(store: CompactionMaintenanceStore) -> None:
    """``request_proactive_compaction_debt`` on a missing conv_id → IntegrityError."""
    with pytest.raises(sqlite3.IntegrityError):
        store.request_proactive_compaction_debt(
            conversation_id=999_999,  # no such conversation
            reason="threshold",
        )


# ---------------------------------------------------------------------------
# with_transaction
# ---------------------------------------------------------------------------


def test_with_transaction_commits_on_success(
    store: CompactionMaintenanceStore, migrated_db: sqlite3.Connection
) -> None:
    """``with_transaction`` commits the inner writes on success."""
    conv_id = _make_conversation(migrated_db)

    def do_request() -> int:
        store.request_proactive_compaction_debt(conversation_id=conv_id, reason="t")
        return conv_id

    returned = store.with_transaction(do_request)
    assert returned == conv_id

    rec = store.get_conversation_compaction_maintenance(conv_id)
    assert rec is not None
    assert rec.pending is True


def test_with_transaction_rolls_back_on_exception(
    store: CompactionMaintenanceStore, migrated_db: sqlite3.Connection
) -> None:
    """``with_transaction`` rolls back on exception."""
    conv_id = _make_conversation(migrated_db)

    class _Boom(RuntimeError):
        pass

    with pytest.raises(_Boom):

        def do_then_fail() -> None:
            store.request_proactive_compaction_debt(conversation_id=conv_id, reason="t")
            raise _Boom("rollback")

        store.with_transaction(do_then_fail)

    rec = store.get_conversation_compaction_maintenance(conv_id)
    assert rec is None
