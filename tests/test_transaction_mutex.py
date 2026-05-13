"""Tests for :mod:`lossless_hermes.transaction_mutex`.

Ports ``lossless-claw/test/transaction-mutex.test.ts`` (commit ``1f07fbd``)
to Python. Preserves the 8 ``it``-block coverage enumerated in the acceptance
criteria:

1. **Serialization** — concurrent acquisitions on the same conversation key
   serialize (acquired→releasing pattern is contiguous per operation).
2. **Independent locks per conversation** — distinct keys parallelise.
3. **Concurrent ``transaction()`` calls don't deadlock or throw nested
   transaction errors** — the core lossless-claw issue #260 regression.
4. **Errors propagate without deadlocking the mutex** — a failed
   transaction releases the lock so the next caller succeeds.
5. **Nested transaction scopes use savepoints** — re-entry on the same
   ``(task, conversation_id)`` issues ``SAVEPOINT`` not ``BEGIN``.
6. **Cross-store reuse** — same lock manager wraps both message inserts
   and summary inserts; they serialize without contention errors.
7. **Timeout** — a second task hitting a held lock raises
   :class:`TransactionMutexTimeout` after the configured wait.
8. **10-way concurrent stress** — storage.md §12 risk #1: 10 simulated
   sessions writing concurrently without errors. Verifies the Python
   sync version handles the same load as the TS asyncio.Lock-backed one.

The TS suite uses ``ConversationStore`` / ``SummaryStore``; those have
not landed yet (#01-08 / #01-09). The Python suite exercises the same
serialization contract against raw SQL writes, which is the same
contract the stores will satisfy when they port.

References:

* ``epics/01-storage/01-13-integrity-prune.md`` §"acceptance criteria"
  — 8 it-blocks enumerated.
* ``docs/porting-guides/storage.md`` §12 risk #1 — concurrent-write
  stress.
* ``docs/adr/018-concurrency-model.md`` — async-Lock-per-conversation.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator

import pytest

from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.transaction_mutex import (
    ConversationLockManager,
    TransactionMutexTimeout,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def migrated_db() -> Iterator[sqlite3.Connection]:
    """In-memory SQLite with core schema applied + foreign keys ON."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


def _insert_conversation(conn: sqlite3.Connection, session_id: str) -> int:
    """Insert a conversation and return its ``conversation_id``."""
    cur = conn.execute(
        "INSERT INTO conversations (session_id, session_key) VALUES (?, ?)",
        (session_id, f"key-{session_id}"),
    )
    return int(cur.lastrowid or 0)


# ---------------------------------------------------------------------------
# 1. Serialization on the same conversation key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_serializes_concurrent_acquisitions_on_same_key() -> None:
    """Concurrent acquisitions on the same conversation_id queue up.

    Each operation's ``acquired→releasing`` pair must be contiguous in
    the global order (mirrors TS test "serializes concurrent transaction
    acquisitions on the same db").
    """
    mgr = ConversationLockManager()
    order: list[str] = []

    async def op(label: str) -> None:
        async with mgr.lock(42):
            order.append(f"{label}:acquired")
            await asyncio.sleep(0.02)
            order.append(f"{label}:releasing")

    await asyncio.gather(op("A"), op("B"), op("C"))

    assert len(order) == 6, order
    # The acquired/releasing pair for each operation must be contiguous.
    for i in range(0, 6, 2):
        acq_label = order[i].split(":")[0]
        rel_label = order[i + 1].split(":")[0]
        assert acq_label == rel_label, f"interleaved at index {i}: {order[i]!r} / {order[i + 1]!r}"
        assert "acquired" in order[i]
        assert "releasing" in order[i + 1]


# ---------------------------------------------------------------------------
# 2. Independent locks per conversation key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_different_conversations_get_independent_locks() -> None:
    """Distinct conversation_ids parallelise — no serialization between them.

    Mirrors TS test "different databases get independent locks". Both
    coroutines should acquire concurrently (interleaved), not serialized.
    """
    mgr = ConversationLockManager()
    order: list[str] = []

    async def op(label: str, conversation_id: int) -> None:
        async with mgr.lock(conversation_id):
            order.append(f"{label}:acquired")
            await asyncio.sleep(0.03)
            order.append(f"{label}:releasing")

    await asyncio.gather(op("conv1", 1), op("conv2", 2))

    # First two entries must both be acquisitions (interleaved), proving
    # the two locks did not serialise.
    assert "acquired" in order[0]
    assert "acquired" in order[1]


# ---------------------------------------------------------------------------
# 3. Concurrent transaction() calls don't throw nested-tx errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transaction_context_serializes_concurrent_writes(
    migrated_db: sqlite3.Connection,
) -> None:
    """Concurrent ``transaction()`` calls succeed without SQLite errors.

    The core lossless-claw issue #260 regression: without the mutex,
    the second BEGIN IMMEDIATE while the first is still mid-flight blows
    up with "cannot start a transaction within a transaction".
    """
    mgr = ConversationLockManager()
    conv_id = _insert_conversation(migrated_db, "sess-1")
    seq_counter = 0

    async def insert_message(role: str) -> str:
        nonlocal seq_counter
        async with mgr.transaction(migrated_db, conv_id):
            # Simulate the heavy async work that triggers #260 in TS.
            await asyncio.sleep(0.01)
            seq_counter += 1
            migrated_db.execute(
                "INSERT INTO messages "
                "(conversation_id, seq, role, content, token_count) "
                "VALUES (?, ?, ?, ?, ?)",
                (conv_id, seq_counter, role, f"msg-{role}", 5),
            )
            return role

    results = await asyncio.gather(
        insert_message("user"),
        insert_message("assistant"),
        insert_message("tool"),
    )

    assert set(results) == {"user", "assistant", "tool"}
    row = migrated_db.execute(
        "SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (conv_id,)
    ).fetchone()
    assert row[0] == 3


# ---------------------------------------------------------------------------
# 4. Errors propagate without deadlocking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_propagates_without_deadlocking_mutex(
    migrated_db: sqlite3.Connection,
) -> None:
    """A failed transaction releases the lock so the next caller succeeds.

    Mirrors TS "propagates errors without deadlocking the mutex". The
    first transaction throws inside the block; the implementation must
    ROLLBACK + release the lock so the second transaction (queued
    behind) can proceed.
    """
    mgr = ConversationLockManager()
    conv_id = _insert_conversation(migrated_db, "sess-err")

    intentional = RuntimeError("intentional failure")

    async def failing_tx() -> None:
        async with mgr.transaction(migrated_db, conv_id):
            await asyncio.sleep(0.01)
            raise intentional

    async def successful_tx() -> str:
        async with mgr.transaction(migrated_db, conv_id):
            migrated_db.execute(
                "INSERT INTO messages "
                "(conversation_id, seq, role, content, token_count) "
                "VALUES (?, 1, 'user', 'ok', 5)",
                (conv_id,),
            )
            return "ok"

    # Schedule the failing tx first so it gets the lock; the successful
    # tx queues behind.
    p1 = asyncio.create_task(failing_tx())
    await asyncio.sleep(0)  # nudge scheduler so p1 acquires
    p2 = asyncio.create_task(successful_tx())

    with pytest.raises(RuntimeError, match="intentional failure"):
        await p1
    result = await p2
    assert result == "ok"

    row = migrated_db.execute(
        "SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (conv_id,)
    ).fetchone()
    assert row[0] == 1


# ---------------------------------------------------------------------------
# 5. Nested transaction scopes use savepoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nested_transaction_scopes_use_savepoints(
    migrated_db: sqlite3.Connection,
) -> None:
    """Re-entry on same task + conversation uses SAVEPOINT, not BEGIN.

    Mirrors TS "supports nested transaction scopes on the same async path".
    Without savepoints, a literal port would try to nest BEGINs and
    SQLite would reject the inner one.
    """
    mgr = ConversationLockManager()
    conv_id = _insert_conversation(migrated_db, "sess-nested")

    async with mgr.transaction(migrated_db, conv_id):
        migrated_db.execute(
            "INSERT INTO messages "
            "(conversation_id, seq, role, content, token_count) "
            "VALUES (?, 1, 'user', 'outer', 5)",
            (conv_id,),
        )
        async with mgr.transaction(migrated_db, conv_id):
            migrated_db.execute(
                "INSERT INTO messages "
                "(conversation_id, seq, role, content, token_count) "
                "VALUES (?, 2, 'assistant', 'inner', 5)",
                (conv_id,),
            )

    row = migrated_db.execute(
        "SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (conv_id,)
    ).fetchone()
    assert row[0] == 2


@pytest.mark.asyncio
async def test_nested_savepoint_rollback_preserves_outer_writes(
    migrated_db: sqlite3.Connection,
) -> None:
    """Inner savepoint rollback leaves outer writes intact.

    The savepoint ROLLBACK TO pattern from TS source lines 175-180 must
    preserve everything written before the savepoint was opened.
    """
    mgr = ConversationLockManager()
    conv_id = _insert_conversation(migrated_db, "sess-sp-rollback")

    async def inner_fails() -> None:
        async with mgr.transaction(migrated_db, conv_id):
            migrated_db.execute(
                "INSERT INTO messages "
                "(conversation_id, seq, role, content, token_count) "
                "VALUES (?, 1, 'user', 'outer-preserved', 5)",
                (conv_id,),
            )
            try:
                async with mgr.transaction(migrated_db, conv_id):
                    migrated_db.execute(
                        "INSERT INTO messages "
                        "(conversation_id, seq, role, content, token_count) "
                        "VALUES (?, 2, 'assistant', 'inner-rolled-back', 5)",
                        (conv_id,),
                    )
                    raise RuntimeError("inner explodes")
            except RuntimeError:
                # Swallow so the outer transaction still commits cleanly.
                pass
            migrated_db.execute(
                "INSERT INTO messages "
                "(conversation_id, seq, role, content, token_count) "
                "VALUES (?, 3, 'tool', 'outer-after-savepoint-rollback', 5)",
                (conv_id,),
            )

    await inner_fails()

    rows = migrated_db.execute(
        "SELECT seq, content FROM messages WHERE conversation_id = ? ORDER BY seq",
        (conv_id,),
    ).fetchall()
    # Outer writes (seq 1 and 3) survive; inner write (seq 2) was rolled back.
    assert [r[0] for r in rows] == [1, 3]
    assert rows[0][1] == "outer-preserved"
    assert rows[1][1] == "outer-after-savepoint-rollback"


# ---------------------------------------------------------------------------
# 6. Cross-store reuse — same manager wraps different write surfaces
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_store_serialization_via_one_manager(
    migrated_db: sqlite3.Connection,
) -> None:
    """One lock manager serializes ``messages`` writes + ``summaries`` writes.

    Mirrors TS "serializes ConversationStore.withTransaction and
    SummaryStore.replaceContextRangeWithSummary on same db" + the
    "wider summary write sequences" case. With a single manager keyed by
    conversation_id, both write surfaces share the same lock — preventing
    the same #260 race that affected the message store in TS.
    """
    mgr = ConversationLockManager()
    conv_id = _insert_conversation(migrated_db, "sess-cross")

    async def message_tx() -> str:
        async with mgr.transaction(migrated_db, conv_id):
            await asyncio.sleep(0.02)
            migrated_db.execute(
                "INSERT INTO messages "
                "(conversation_id, seq, role, content, token_count) "
                "VALUES (?, 100, 'user', 'cross-msg', 5)",
                (conv_id,),
            )
            return "messages-done"

    async def summary_tx() -> str:
        async with mgr.transaction(migrated_db, conv_id):
            await asyncio.sleep(0.01)
            migrated_db.execute(
                "INSERT INTO summaries "
                "(summary_id, conversation_id, kind, content, token_count) "
                "VALUES (?, ?, 'leaf', 'cross-sum', 10)",
                ("sum_cross_001", conv_id),
            )
            return "summaries-done"

    results = await asyncio.gather(message_tx(), summary_tx())
    assert set(results) == {"messages-done", "summaries-done"}


# ---------------------------------------------------------------------------
# 7. Timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_raises_transaction_mutex_timeout() -> None:
    """A second task hitting a held lock raises after the timeout.

    The TS suite encodes this as
    ``acquireTransactionLockWithTimeout(db, ms)``; the Python equivalent
    is ``lock(cid, timeout=s)``. The timeout value is intentionally
    small so the test stays fast.
    """
    mgr = ConversationLockManager()

    async def holder() -> None:
        async with mgr.lock(99):
            # Hold the lock long enough for the contender's timeout to expire.
            await asyncio.sleep(0.2)

    async def contender() -> None:
        # Tiny timeout; should fire long before the holder releases.
        async with mgr.lock(99, timeout=0.05):
            pytest.fail("contender should have timed out, not acquired")

    holder_task = asyncio.create_task(holder())
    await asyncio.sleep(0)  # let holder enter
    with pytest.raises(TransactionMutexTimeout):
        await contender()
    await holder_task


# ---------------------------------------------------------------------------
# 8. 10-way concurrent stress test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ten_way_concurrent_stress(migrated_db: sqlite3.Connection) -> None:
    """10 simulated sessions write concurrently without errors.

    Mirrors TS "handles 10 concurrent transactions from different
    simulated sessions". Storage.md §12 risk #1 — the Python sync version
    must handle the same load as the TS asyncio.Lock-backed one.

    Each session has its own conversation_id, so locks parallelise; the
    stress is on the transaction shape + dispatch, not contention.
    """
    mgr = ConversationLockManager()
    conv_ids = [_insert_conversation(migrated_db, f"stress-{i}") for i in range(10)]

    async def session_write(i: int, cid: int) -> str:
        async with mgr.transaction(migrated_db, cid):
            await asyncio.sleep((i % 5) * 0.005)
            migrated_db.execute(
                "INSERT INTO messages "
                "(conversation_id, seq, role, content, token_count) "
                "VALUES (?, 1, 'user', ?, 10)",
                (cid, f"stress-msg-{i}"),
            )
            return f"session-{i}-done"

    results = await asyncio.gather(
        *(session_write(i, cid) for i, cid in enumerate(conv_ids)),
        return_exceptions=True,
    )
    failures = [r for r in results if isinstance(r, BaseException)]
    assert not failures, f"stress test had failures: {failures}"

    # Verify every conversation got its message.
    for cid in conv_ids:
        row = migrated_db.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (cid,)
        ).fetchone()
        assert row[0] == 1


# ---------------------------------------------------------------------------
# Bonus: contender after holder releases acquires cleanly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contender_acquires_after_holder_releases() -> None:
    """A contender that doesn't time out acquires once the holder releases.

    Complements the timeout test: without it, a regression that makes
    ``release()`` a no-op would pass test_timeout (the timeout fires
    correctly) but miss the inverse — that release actually unblocks.
    """
    mgr = ConversationLockManager()
    completed_order: list[str] = []

    async def holder() -> None:
        async with mgr.lock(7):
            completed_order.append("holder:acquired")
            await asyncio.sleep(0.05)
        completed_order.append("holder:released")

    async def contender() -> None:
        async with mgr.lock(7, timeout=2.0):
            completed_order.append("contender:acquired")
        completed_order.append("contender:released")

    holder_task = asyncio.create_task(holder())
    await asyncio.sleep(0)
    contender_task = asyncio.create_task(contender())
    await asyncio.gather(holder_task, contender_task)

    assert completed_order == [
        "holder:acquired",
        "holder:released",
        "contender:acquired",
        "contender:released",
    ]
