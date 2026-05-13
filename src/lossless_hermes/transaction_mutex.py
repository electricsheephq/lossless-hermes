"""Per-conversation async transaction mutex + savepoint-based reentrancy.

Ports ``lossless-claw/src/transaction-mutex.ts`` (commit ``1f07fbd``, 202 LOC)
to Python. The TS module serialises concurrent SQLite write transactions on
a shared ``DatabaseSync`` handle so that two async paths cannot interleave
``BEGIN IMMEDIATE`` calls and trigger SQLite's "cannot start a transaction
within a transaction" error (regression: lossless-claw issue #260).

### Why we need this in Python

ADR-018 §"Decision" picks **Option A**: ``asyncio.Task`` per worker kind +
``dict[str, asyncio.Lock]`` per conversation + the ``lcm_worker_lock`` table
for cross-process safety. ADR-017 keeps the *DB calls themselves* synchronous,
but everything that surrounds them is ``async`` — Voyage HTTP, the worker
loop, the eventual LLM streaming surface. As soon as two ``await`` points
hand control back to the event loop while a transaction is mid-flight, the
next coroutine that calls ``BEGIN IMMEDIATE`` on the same connection blows up.

The mutex is **not** about thread safety — sync-by-design SQLite is single-
threaded under ADR-017. It's about *task* safety: serialising the points
where each coroutine enters ``BEGIN IMMEDIATE`` / ``COMMIT`` on a per-
conversation key so that distinct conversations parallelise but the same
conversation drains in order.

### Python port shape (per ADR-018 §Consequences + doctor-ops.md §"Transaction-mutex")

* **Locks**: ``dict[str, asyncio.Lock]`` keyed by ``conversation_id``
  (stringified for symmetry with TS' string-keyed WeakMap; integer ids are
  accepted and coerced). A lock is created lazily on first acquire and lives
  for the process lifetime — bounded memory in practice because real
  workloads touch O(thousands) of conversations.
* **Reentrancy via savepoints**: SQLite forbids nested ``BEGIN``, but
  ``SAVEPOINT``/``RELEASE``/``ROLLBACK TO`` is the supported recursion
  primitive. A nested call on the same ``conversation_id`` from the same
  asyncio task uses ``SAVEPOINT sp_<N>`` instead of opening a new BEGIN.
  Depth is tracked via :class:`contextvars.ContextVar` so each asyncio task
  has its own stack (replaces TS' ``AsyncLocalStorage``).
* **Timeout**: a contention-with-deadline path raises
  :class:`TransactionMutexTimeout` (caller picks the timeout — default 30 s
  mirrors the connection-level ``busy_timeout`` per storage.md §3).

### Public surface

| Symbol | TS analogue | Notes |
|---|---|---|
| :class:`ConversationLockManager` | (new wrapper) | Holds the lock dict + depth ContextVar |
| ``manager.lock(conversation_id)`` | ``acquireTransactionLock(db)`` | ``async with mgr.lock(cid): ...`` |
| ``manager.transaction(conn, cid, ...)`` | ``withDatabaseTransaction(db, ...)`` | Async ctxmgr; opens BEGIN/SAVEPOINT |
| :class:`TransactionMutexTimeout` | ``DatabaseTransactionTimeoutError`` | Timeout signal |

The async ``transaction()`` context manager combines the two concerns from
TS — lock acquisition + transaction shape — into a single ``async with``.
A nested call on the same ``conversation_id`` inside the same task detects
non-zero savepoint depth and emits ``SAVEPOINT sp_<N>`` instead of a fresh
``BEGIN``; on exit it issues ``RELEASE`` (success) or ``ROLLBACK TO``
(failure) — preserves TS' verbatim semantics for nested error isolation.

### Wave-1 cross-task safety (ADR-029)

The ``contextvars.ContextVar`` runs of :class:`asyncio.Task` boundaries — a
new task inherits a copy of its parent's context, but mutations do **not**
propagate back. This is exactly what we want: a nested ``transaction()``
inside the same task sees ``depth > 0`` and uses a savepoint; a peer task
that runs in parallel sees ``depth == 0`` (its own copy) and contends for
the lock normally. ``AsyncLocalStorage`` in TS has the same shape.

### Reentrant lock acquisition discipline

:class:`asyncio.Lock` is **not reentrant**. If a task acquires a lock and
then re-enters the same lock without releasing, it will deadlock. The
:meth:`transaction` helper avoids this by checking the savepoint-depth
ContextVar *before* trying to acquire the lock — a nested entry on the same
``(task, conversation_id)`` skips :meth:`lock` entirely and goes straight to
``SAVEPOINT``. Direct callers of :meth:`lock` must not re-enter.

See:

* ADR-017 — synchronous DB layer (the mutex exists *because* the DB is sync).
* ADR-018 — concurrency model (``asyncio.Lock`` per conversation; this file).
* ADR-029 — Wave-N provenance.
* ``docs/porting-guides/doctor-ops.md`` §"Transaction-mutex" — TS reference.
* ``docs/porting-guides/storage.md`` §10 risk #2 — savepoint-depth concern.
* ``lossless-claw/src/transaction-mutex.ts`` — verbatim TS source.
* ``epics/01-storage/01-13-integrity-prune.md`` — issue spec.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import sqlite3
from contextlib import asynccontextmanager
from typing import AsyncIterator, Literal

__all__ = [
    "BeginTransactionStatement",
    "ConversationLockManager",
    "TransactionMutexTimeout",
]


_log = logging.getLogger("lossless_hermes.transaction_mutex")


# ---------------------------------------------------------------------------
# Types + errors
# ---------------------------------------------------------------------------


# Mirrors the TS ``BeginTransactionStatement`` literal type — the two modes
# of opening a transaction. ``BEGIN`` is the lazy/deferred mode (other
# writers may grab the lock first); ``BEGIN IMMEDIATE`` takes the reserved
# lock up front. Production callers use ``BEGIN IMMEDIATE`` per storage.md
# §"Concurrency invariant"; tests sometimes use plain ``BEGIN`` for read-
# heavy paths.
BeginTransactionStatement = Literal["BEGIN", "BEGIN IMMEDIATE"]


class TransactionMutexTimeout(Exception):
    """Raised when lock acquisition exceeds the configured timeout.

    Mirrors the TS ``DatabaseTransactionTimeoutError`` shape. The integer
    millisecond value matches the TS error for log-parity.
    """

    def __init__(self, timeout_s: float) -> None:
        self.timeout_s = timeout_s
        timeout_ms = int(timeout_s * 1000)
        super().__init__(f"Timed out after {timeout_ms}ms waiting for exclusive database access.")


# ---------------------------------------------------------------------------
# Savepoint-depth tracking via contextvars
# ---------------------------------------------------------------------------
#
# Replaces TS ``AsyncLocalStorage<Map<DatabaseSync, number>>`` with a
# task-local ``dict[str, int]`` keyed by conversation id. ContextVar is the
# Python equivalent: each asyncio.Task gets its own copy of the dict, so
# nested ``transaction()`` calls on the same task see the parent's depth,
# while peer tasks start fresh.
#
# We store a *mapping*, not a single int, so multiple distinct conversations
# can be held by the same task without trampling each other's depth.
_depth_var: contextvars.ContextVar[dict[str, int]] = contextvars.ContextVar(
    "lossless_hermes_txn_savepoint_depth",
    default={},  # noqa: B039 - immutable default by convention; we always copy before mutating
)


def _current_depth(conversation_key: str) -> int:
    """Return the savepoint depth held for ``conversation_key`` on this task."""
    return _depth_var.get().get(conversation_key, 0)


def _push_depth(conversation_key: str) -> int:
    """Increment the savepoint depth for ``conversation_key``.

    Returns the **new** depth so the caller can use it as the savepoint
    index. Mutates a fresh dict so the ContextVar's parent value is never
    aliased (preserves the per-task isolation guarantee).
    """
    current = dict(_depth_var.get())
    new_depth = current.get(conversation_key, 0) + 1
    current[conversation_key] = new_depth
    _depth_var.set(current)
    return new_depth


def _pop_depth(conversation_key: str) -> None:
    """Decrement the savepoint depth for ``conversation_key``.

    Removes the entry entirely when depth hits zero so the per-task dict
    stays small and the next outer entry on this key starts a fresh BEGIN.
    """
    current = dict(_depth_var.get())
    depth = current.get(conversation_key, 0)
    if depth <= 1:
        current.pop(conversation_key, None)
    else:
        current[conversation_key] = depth - 1
    _depth_var.set(current)


# Monotonic counter for savepoint names. Module-level so two concurrent
# tasks racing into nested savepoints on different conversations don't
# collide on names (SQLite scopes savepoints per-connection, so a name
# collision would not corrupt state, but unique names are easier to debug).
_savepoint_counter: int = 0


def _next_savepoint_name() -> str:
    """Return a fresh ``sp_<N>`` savepoint name.

    Mirrors TS ``nextSavepointName()`` — a module-level monotonically
    increasing id with a ``lcm_txn_savepoint_`` prefix. We keep the TS
    prefix for log-grep parity.
    """
    global _savepoint_counter
    _savepoint_counter += 1
    return f"lcm_txn_savepoint_{_savepoint_counter}"


# ---------------------------------------------------------------------------
# ConversationLockManager
# ---------------------------------------------------------------------------


class ConversationLockManager:
    """Per-conversation async lock + savepoint-based reentrant transaction.

    Constructed once per database (typically at engine init). Holds a
    ``dict[str, asyncio.Lock]`` of per-conversation locks created lazily.

    Public surface:

    * :meth:`lock` — low-level async context manager. ``async with
      mgr.lock(cid): ...`` — acquires the conv lock, yields, releases.
      Not reentrant; use :meth:`transaction` for nested scopes.
    * :meth:`transaction` — high-level async context manager that combines
      lock acquisition with transaction shape. Reentrancy via savepoints.

    Example::

        mgr = ConversationLockManager()
        # Two coroutines trying to write to conversation 42 will serialize;
        # peers on different conversation ids run in parallel.
        async with mgr.transaction(conn, conversation_id=42):
            conn.execute("INSERT INTO messages ...")
            # Nested transaction on the same conversation uses a savepoint:
            async with mgr.transaction(conn, conversation_id=42):
                conn.execute("UPDATE summaries ...")
    """

    def __init__(self) -> None:
        # ``dict[str, asyncio.Lock]`` keyed by conversation id (string).
        # Integer ids are accepted by the public API and stringified
        # internally for symmetry with TS' WeakMap<DatabaseSync, ...> shape
        # (LCM keys by the DB instance; Hermes keys by the conversation it
        # belongs to — the boundary that maps to "session_id" in the
        # doctor-ops.md guide).
        self._locks: dict[str, asyncio.Lock] = {}

        # Guards lazy creation of per-conversation locks. Without this, two
        # tasks racing for the same conversation id could each see an empty
        # dict, create a new lock, and skip past each other — defeating the
        # mutex. The "init" lock is itself an asyncio.Lock; on Python 3.11+
        # constructing one outside an event loop is fine, but acquiring it
        # requires the loop. We construct lazily in :meth:`_get_lock` to be
        # safe under "create the manager before the event loop starts" use
        # cases.
        self._init_lock: asyncio.Lock | None = None

    def _get_lock(self, conversation_key: str) -> asyncio.Lock:
        """Return the per-conversation lock, creating it if missing.

        Lazy single-task creation. Two concurrent calls on the same key
        would each construct a new lock and the second would shadow the
        first — but we run inside an asyncio event loop and the dict insert
        is atomic at the bytecode level. The ``setdefault`` pattern below
        is the canonical race-free shape: ``dict.setdefault`` is atomic
        for the *insertion*, so even under the GIL-aware async scheduler
        the second caller observes the first caller's lock.
        """
        return self._locks.setdefault(conversation_key, asyncio.Lock())

    @asynccontextmanager
    async def lock(
        self,
        conversation_id: int | str,
        *,
        timeout: float | None = None,
    ) -> AsyncIterator[None]:
        """Acquire the per-conversation lock for the duration of the block.

        **Not reentrant**: nested acquisition on the same ``(task,
        conversation_id)`` will deadlock — use :meth:`transaction` for
        nested transaction scopes (it dispatches to savepoints on re-entry).

        Args:
            conversation_id: integer or string id; coerced to str for the
                dict key.
            timeout: seconds before raising :class:`TransactionMutexTimeout`.
                ``None`` means wait forever (preserves TS
                ``acquireTransactionLock`` semantics; the timeout-aware path
                mirrors ``acquireTransactionLockWithTimeout``).

        Raises:
            TransactionMutexTimeout: if ``timeout`` expires before
                acquisition.
        """
        key = str(conversation_id)
        per_conv_lock = self._get_lock(key)

        if timeout is None:
            await per_conv_lock.acquire()
        else:
            try:
                await asyncio.wait_for(per_conv_lock.acquire(), timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise TransactionMutexTimeout(timeout) from exc

        try:
            yield
        finally:
            per_conv_lock.release()

    @asynccontextmanager
    async def transaction(
        self,
        conn: sqlite3.Connection,
        conversation_id: int | str,
        *,
        begin: BeginTransactionStatement = "BEGIN IMMEDIATE",
        timeout: float | None = None,
    ) -> AsyncIterator[None]:
        """Run a block inside a serialized SQLite transaction.

        First entry on a ``(task, conversation_id)`` acquires the per-conv
        lock and emits ``begin``; nested entries on the same key reuse the
        held lock and emit a ``SAVEPOINT`` instead (preserves TS reentrancy
        semantics — see :func:`withDatabaseTransaction` in the TS source).

        Args:
            conn: the SQLite connection to issue ``BEGIN`` / ``COMMIT`` /
                ``ROLLBACK`` / ``SAVEPOINT`` against. Must be sync per
                ADR-017.
            conversation_id: serialization key. Distinct values parallelise;
                same value queues up.
            begin: ``"BEGIN"`` or ``"BEGIN IMMEDIATE"`` — only applied at
                the **outer** entry (depth == 0). Nested entries always use
                ``SAVEPOINT`` regardless of this value.
            timeout: forwarded to :meth:`lock` for the outer entry. Inner
                entries don't acquire the lock and ignore the timeout.

        Raises:
            TransactionMutexTimeout: if ``timeout`` expires on the outer
                acquisition.
            Any exception from the block — the implementation issues
                ``ROLLBACK`` (outer) or ``ROLLBACK TO SAVEPOINT`` (nested)
                before re-raising.
        """
        key = str(conversation_id)

        if _current_depth(key) > 0:
            # Nested re-entry on the same task + conversation. The outer
            # entry already holds the lock and opened the transaction;
            # we use a savepoint for isolation.
            savepoint_name = _next_savepoint_name()
            conn.execute(f"SAVEPOINT {savepoint_name}")
            _push_depth(key)
            try:
                yield
                conn.execute(f"RELEASE SAVEPOINT {savepoint_name}")
            except BaseException:
                # Wave-N rollback semantics from TS source (lines 175-180):
                # always ROLLBACK TO + RELEASE, then re-raise. RELEASE after
                # ROLLBACK TO is correct — SQLite keeps the savepoint frame
                # alive until released, so without the explicit release the
                # next outer-level RELEASE/COMMIT can fail.
                try:
                    conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint_name}")
                    conn.execute(f"RELEASE SAVEPOINT {savepoint_name}")
                except sqlite3.OperationalError:
                    # Defense-in-depth: if the underlying transaction was
                    # already aborted by SQLite, ROLLBACK TO may itself
                    # fail. Swallow so we surface the original exception
                    # from the block (mirrors TS try/catch with ignored
                    # rollback errors in similar guards).
                    _log.warning(
                        "savepoint %s rollback/release failed; propagating original",
                        savepoint_name,
                        exc_info=True,
                    )
                raise
            finally:
                _pop_depth(key)
            return

        # Outer entry — acquire the per-conv lock + open BEGIN.
        async with self.lock(conversation_id, timeout=timeout):
            # Flush any implicit transaction Python's sqlite3 module may have
            # opened on prior DML. With ``isolation_level=""`` (the stdlib
            # default), Python auto-issues a BEGIN before the first INSERT /
            # UPDATE / DELETE statement; without flushing, our ``BEGIN
            # IMMEDIATE`` here would raise "cannot start a transaction
            # within a transaction". A no-op ``commit()`` when no implicit
            # txn is open is safe and cheap.
            if conn.in_transaction:
                conn.commit()
            conn.execute(begin)
            _push_depth(key)
            try:
                yield
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    _log.warning(
                        "outer ROLLBACK failed for conversation_id=%s; propagating original",
                        key,
                        exc_info=True,
                    )
                raise
            finally:
                _pop_depth(key)
