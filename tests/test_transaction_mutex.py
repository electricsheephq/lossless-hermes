"""Tests for :mod:`lossless_hermes.transaction_mutex`.

Covers the synchronous wrapper:

* Basic commit / rollback.
* Reentrancy via savepoints (nested-2 / nested-3).
* Inner savepoint rollback leaves outer commits intact.
* The implicit-transaction-flush behavior at depth 0.
* Per-DB lock isolation (different connections don't share state).
"""

from __future__ import annotations

import sqlite3
from typing import Iterator

import pytest

from lossless_hermes.transaction_mutex import (
    get_held_lock_depth,
    with_database_transaction,
)


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    """An in-memory DB with autocommit mode (isolation_level=None)."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, value TEXT)")
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Basic behavior
# ---------------------------------------------------------------------------


def test_basic_commit(db: sqlite3.Connection) -> None:
    """A successful operation commits its inserts."""

    def op() -> None:
        db.execute("INSERT INTO t (value) VALUES ('a')")

    with_database_transaction(db, "BEGIN", op)
    rows = db.execute("SELECT value FROM t").fetchall()
    assert rows == [("a",)]


def test_basic_rollback_on_exception(db: sqlite3.Connection) -> None:
    """An exception rolls back all writes from the operation."""

    class TestError(Exception):
        pass

    def op() -> None:
        db.execute("INSERT INTO t (value) VALUES ('rolled-back')")
        raise TestError("forced")

    with pytest.raises(TestError):
        with_database_transaction(db, "BEGIN", op)

    rows = db.execute("SELECT value FROM t").fetchall()
    assert rows == []


def test_returns_operation_result(db: sqlite3.Connection) -> None:
    """The wrapper returns whatever the operation returns."""

    def op() -> int:
        return 42

    assert with_database_transaction(db, "BEGIN", op) == 42


# ---------------------------------------------------------------------------
# Reentrancy via savepoints
# ---------------------------------------------------------------------------


def test_nested_two_deep_commits(db: sqlite3.Connection) -> None:
    """Inner savepoint releases on success; outer commits."""

    def outer() -> None:
        db.execute("INSERT INTO t (value) VALUES ('outer')")

        def inner() -> None:
            db.execute("INSERT INTO t (value) VALUES ('inner')")

        with_database_transaction(db, "BEGIN", inner)

    with_database_transaction(db, "BEGIN", outer)
    rows = [row[0] for row in db.execute("SELECT value FROM t ORDER BY id").fetchall()]
    assert rows == ["outer", "inner"]


def test_nested_three_deep_commits(db: sqlite3.Connection) -> None:
    """Three nested transactions all commit on success."""

    inserted: list[str] = []

    def outer() -> None:
        db.execute("INSERT INTO t (value) VALUES ('1')")
        inserted.append("1")

        def middle() -> None:
            db.execute("INSERT INTO t (value) VALUES ('2')")
            inserted.append("2")

            def inner() -> None:
                db.execute("INSERT INTO t (value) VALUES ('3')")
                inserted.append("3")

            with_database_transaction(db, "BEGIN", inner)

        with_database_transaction(db, "BEGIN", middle)

    with_database_transaction(db, "BEGIN", outer)
    rows = [row[0] for row in db.execute("SELECT value FROM t ORDER BY id").fetchall()]
    assert rows == ["1", "2", "3"]


def test_inner_rollback_outer_commits(db: sqlite3.Connection) -> None:
    """Inner savepoint rollback leaves outer commits intact."""

    class InnerError(Exception):
        pass

    def outer() -> None:
        db.execute("INSERT INTO t (value) VALUES ('outer')")

        def inner() -> None:
            db.execute("INSERT INTO t (value) VALUES ('inner-rolled')")
            raise InnerError("inner")

        try:
            with_database_transaction(db, "BEGIN", inner)
        except InnerError:
            pass  # swallow

    with_database_transaction(db, "BEGIN", outer)
    rows = [row[0] for row in db.execute("SELECT value FROM t").fetchall()]
    assert rows == ["outer"]


# ---------------------------------------------------------------------------
# Depth tracking
# ---------------------------------------------------------------------------


def test_depth_zero_outside_txn(db: sqlite3.Connection) -> None:
    """Outside any transaction, depth is 0."""
    assert get_held_lock_depth(db) == 0


def test_depth_tracks_nesting(db: sqlite3.Connection) -> None:
    """Depth tracks the current nesting level."""
    seen_depths: list[int] = []

    def outer() -> None:
        seen_depths.append(get_held_lock_depth(db))

        def inner() -> None:
            seen_depths.append(get_held_lock_depth(db))

        with_database_transaction(db, "BEGIN", inner)

    with_database_transaction(db, "BEGIN", outer)
    assert seen_depths == [1, 2]
    # After all, depth back to 0.
    assert get_held_lock_depth(db) == 0


# ---------------------------------------------------------------------------
# Implicit-transaction flush
# ---------------------------------------------------------------------------


def test_handles_pending_implicit_transaction() -> None:
    """A pending implicit DML transaction is committed before BEGIN.

    This is the default-isolation behavior of Python's stdlib ``sqlite3``:
    DML auto-starts a transaction that the wrapper must clear before
    issuing its explicit BEGIN.
    """
    conn = sqlite3.connect(":memory:")  # default isolation_level=''
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, value TEXT)")
    # DML auto-opens an implicit transaction.
    conn.execute("INSERT INTO t (value) VALUES ('implicit')")
    assert conn.in_transaction

    # The wrapper must flush this before BEGIN — no "cannot start a
    # transaction within a transaction" error.
    def op() -> None:
        conn.execute("INSERT INTO t (value) VALUES ('explicit')")

    with_database_transaction(conn, "BEGIN", op)

    rows = [row[0] for row in conn.execute("SELECT value FROM t ORDER BY id").fetchall()]
    assert rows == ["implicit", "explicit"]
    conn.close()


# ---------------------------------------------------------------------------
# Per-DB lock isolation
# ---------------------------------------------------------------------------


def test_per_db_depth_isolation() -> None:
    """Two separate connections have separate depth counters."""
    conn_a = sqlite3.connect(":memory:", isolation_level=None)
    conn_b = sqlite3.connect(":memory:", isolation_level=None)
    conn_a.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn_b.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    try:
        seen: list[tuple[int, int]] = []

        def op_a() -> None:
            seen.append((get_held_lock_depth(conn_a), get_held_lock_depth(conn_b)))

        with_database_transaction(conn_a, "BEGIN", op_a)
        # While conn_a is inside its txn, conn_b's depth was 0.
        assert seen == [(1, 0)]
    finally:
        conn_a.close()
        conn_b.close()
