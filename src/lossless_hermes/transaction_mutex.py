"""Per-DB synchronous transaction wrapper with savepoint reentrancy.

Port of ``lossless-claw/src/transaction-mutex.ts`` (LCM commit ``1f07fbd``,
202 LOC TS → ~140 LOC Python).

Per ADR-017 (synchronous-by-design), the Python port replaces TS's
async lock + ``AsyncLocalStorage`` with a synchronous threading-based
construct:

* A module-level ``threading.RLock`` per ``id(db)`` serializes writers on
  the same connection. (RLock: re-acquirable by the same thread; the lock
  itself is the natural reentrancy guard alongside the depth counter.)
* A thread-local depth map ``threading.local().depth_by_db_id`` tracks
  current nesting level. The first ``with_database_transaction`` opens a
  real transaction; subsequent calls on the same thread open a
  ``SAVEPOINT``.
* Savepoint names are monotonic via a module-level counter; collision-
  free even if two different DBs nest concurrently.

The semantics are identical to the TS source:

* First-level scope opens the requested transaction mode (``BEGIN`` or
  ``BEGIN IMMEDIATE``).
* Nested scopes open a savepoint (``SAVEPOINT lcm_txn_<n>``) and use
  ``ROLLBACK TO`` + ``RELEASE`` on failure / ``RELEASE`` on success.
* All-or-nothing: any exception at any nesting level rolls back its own
  level and re-raises; outer scopes see the partial rollback and decide
  whether to roll back further.

See:

* ``docs/adr/017-sync-vs-async-db.md`` — the synchronous decision.
* ``docs/porting-guides/storage.md`` §10 — the savepoint pattern.
* ``lossless-claw/src/transaction-mutex.ts`` lines 155-202 — the TS
  ``withDatabaseTransaction`` whose semantics we mirror.
"""

from __future__ import annotations

import itertools
import sqlite3
import threading
from typing import Callable, Dict, Literal, TypeVar

__all__ = [
    "BeginTransactionStatement",
    "get_held_lock_depth",
    "with_database_transaction",
]

T = TypeVar("T")

BeginTransactionStatement = Literal["BEGIN", "BEGIN IMMEDIATE"]


# ---------------------------------------------------------------------------
# Module-level locking + savepoint state
# ---------------------------------------------------------------------------

# One ``threading.RLock`` per Connection identity. Cleaned up via
# ``_release_lock_if_unused`` when the depth returns to zero.
_locks_by_db: Dict[int, threading.RLock] = {}
_locks_registry_lock = threading.Lock()

# Monotonic savepoint name source. Locked by a separate small lock so the
# generator increment doesn't race even on free-threaded Python builds.
_savepoint_counter = itertools.count(1)


def _get_db_lock(db_id: int) -> threading.RLock:
    """Return the RLock for ``db_id``, creating it on first use."""
    with _locks_registry_lock:
        lock = _locks_by_db.get(db_id)
        if lock is None:
            lock = threading.RLock()
            _locks_by_db[db_id] = lock
        return lock


# Per-thread depth tracking. ``threading.local`` storage is naturally
# isolated between threads — no further locking needed.
class _ThreadLocalDepth(threading.local):
    """Thread-local depth counter keyed by ``id(db)``."""

    def __init__(self) -> None:
        # Default attribute — initialized lazily per thread.
        self.depth_by_db_id: Dict[int, int] = {}


_thread_local = _ThreadLocalDepth()


def get_held_lock_depth(db: sqlite3.Connection) -> int:
    """Return current transaction depth for ``db`` on this thread.

    Mirrors the TS ``getHeldLockDepth`` helper. Returns 0 when no
    transaction is currently open by this thread for this connection,
    1 when the outermost transaction is open, 2+ for nested savepoints.
    """
    return _thread_local.depth_by_db_id.get(id(db), 0)


def _next_savepoint_name() -> str:
    """Return the next ``SAVEPOINT lcm_txn_<n>`` name (monotonic)."""
    return f"lcm_txn_{next(_savepoint_counter)}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def with_database_transaction(
    db: sqlite3.Connection,
    begin_statement: BeginTransactionStatement,
    operation: Callable[[], T],
) -> T:
    """Run ``operation`` inside a serialized DB transaction.

    The first call on a given thread+connection acquires the
    per-connection :class:`threading.RLock`, opens the requested
    transaction (``BEGIN`` or ``BEGIN IMMEDIATE``), runs ``operation``,
    then COMMITs (or ROLLBACK on exception).

    Subsequent reentrant calls on the same thread open a SAVEPOINT
    instead, and use ``RELEASE`` / ``ROLLBACK TO`` + ``RELEASE`` to
    nest the operation under the outer transaction.

    Args:
        db: Open :class:`sqlite3.Connection`.
        begin_statement: ``"BEGIN"`` (deferred — acquires lock on first
            read/write) or ``"BEGIN IMMEDIATE"`` (reserved-mode — fail
            fast on contention). Used only at the outermost level.
        operation: Zero-arg callable returning ``T``. May call
            :func:`with_database_transaction` recursively on the same
            ``db``; those nested calls become savepoints.

    Returns:
        Whatever ``operation`` returns.

    Raises:
        Any exception raised by ``operation`` is re-raised after the
        appropriate ROLLBACK / ROLLBACK TO step.
        :class:`sqlite3.OperationalError` if ``db`` is already inside
        a transaction opened outside this helper (BEGIN-in-BEGIN).
    """
    db_id = id(db)
    if get_held_lock_depth(db) > 0:
        # Reentrant: open a savepoint inside the held transaction.
        savepoint = _next_savepoint_name()
        db.execute(f"SAVEPOINT {savepoint}")
        _thread_local.depth_by_db_id[db_id] = _thread_local.depth_by_db_id[db_id] + 1
        try:
            result = operation()
            db.execute(f"RELEASE SAVEPOINT {savepoint}")
            return result
        except BaseException:
            db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            db.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        finally:
            _thread_local.depth_by_db_id[db_id] = _thread_local.depth_by_db_id[db_id] - 1
            if _thread_local.depth_by_db_id[db_id] <= 0:
                _thread_local.depth_by_db_id.pop(db_id, None)

    # Outermost scope: acquire the per-connection lock and open the txn.
    lock = _get_db_lock(db_id)
    lock.acquire()
    try:
        _thread_local.depth_by_db_id[db_id] = 1
        # Python's ``sqlite3`` module with the default ``isolation_level=''``
        # auto-opens an implicit transaction on DML statements (INSERT /
        # UPDATE / DELETE). If a DML ran outside any
        # ``with_database_transaction`` call before this one (typical in
        # bulk-write code paths), the conn is in an implicit txn here and
        # ``BEGIN IMMEDIATE`` would fail with "cannot start a transaction
        # within a transaction". COMMIT the implicit txn first to leave a
        # clean slate. (Production should use
        # :func:`lossless_hermes.db.connection.open_lcm_db` with
        # ``isolation_level=None`` to avoid this entirely.)
        if db.in_transaction:
            try:
                db.execute("COMMIT")
            except sqlite3.Error:  # pragma: no cover - defensive
                pass
        db.execute(begin_statement)
        try:
            result = operation()
            db.execute("COMMIT")
            return result
        except BaseException:
            try:
                db.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover - defensive
                # Already-rolled-back txn — re-raise the original exc.
                pass
            raise
        finally:
            _thread_local.depth_by_db_id.pop(db_id, None)
    finally:
        lock.release()
