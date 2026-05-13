"""Cross-process worker job lock — LCM v4.1 §0.

Ports ``lossless-claw/src/concurrency/worker-lock.ts`` (LCM commit
``1f07fbd``, 215 LOC TS → ~245 LOC Python including the
:func:`run_with_heartbeat` convenience wrapper that did not exist in TS).

### Why this exists

Backed by the ``lcm_worker_lock`` table (one row per ``job_kind``). All
worker jobs (condensation, extraction, embedding-backfill, profile-rebuild,
theme-consolidation, eval) coordinate via this table so only one process
at a time runs each kind.

Acquisition is atomic via PRIMARY KEY uniqueness on (``job_kind``):
``INSERT OR IGNORE`` returns ``changes=1`` if we got the lock,
``changes=0`` if someone else already holds it. No advisory lock dance, no
application semaphore — SQLite's row-uniqueness IS the lock.

### TTL + heartbeat (v4.1.1 A9)

* :data:`~lossless_hermes.concurrency.model.WORKER_LOCK_TTL_S` = 90 s default
* Worker calls :func:`heartbeat_lock` every
  :data:`~lossless_hermes.concurrency.model.WORKER_HEARTBEAT_S` = 30 s while running
* If a worker dies without releasing, its lock auto-expires after 90 s
* :func:`acquire_lock` GC's stale (expired) locks BEFORE attempting INSERT

Why TTL is short and heartbeat is shorter: gateway-fallback soak window
(:data:`~lossless_hermes.concurrency.model.GATEWAY_FALLBACK_SOAK_S` = 5 min)
prevents a slow-LLM-but-alive worker from being preempted. The fast TTL
only matters when the worker is actually dead.

### Server-side ``datetime('now')`` invariant (ADR-018)

Every timestamp on this table is emitted by SQL ``datetime('now')`` — the
SQLite server-side clock — never by Python ``datetime.utcnow()``. This is
load-bearing: two processes contend on the same DB; if each side computes
its own timestamp from its own wall clock, drift between machine clocks
makes "expires_at" race with itself. Server-side ``datetime('now')`` reads
one clock — the DB's — and is consistent across all writers.

### Python sqlite3 commit invariant

Python's stdlib ``sqlite3.Connection`` defaults to ``isolation_level=""``
which implicit-opens a transaction on DML and **does not auto-commit it**.
Every INSERT / UPDATE / DELETE in this module therefore calls
:meth:`sqlite3.Connection.commit` explicitly. (Connections opened with
:func:`lossless_hermes.db.connection.open_lcm_db` use
``isolation_level=None`` so the explicit commit is a no-op — but it's
free, and it makes the module robust to call paths that pass a
non-sanctioned connection.) The TS source never had this concern.

See:

* ``docs/adr/018-concurrency-model.md`` — design + clock-skew rationale.
* ``docs/adr/029-wave-fix-provenance.md`` — Wave-N comment policy.
* ``lossless-claw/src/concurrency/worker-lock.ts`` lines 1-215 — TS source.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import sqlite3
import time
from typing import Awaitable, Callable, TypeVar

from lossless_hermes.concurrency.model import (
    WORKER_HEARTBEAT_S,
    WORKER_LOCK_TTL_S,
    LockInfo,
    WorkerJobKind,
)

__all__ = [
    "acquire_lock",
    "generate_worker_id",
    "heartbeat_lock",
    "lock_info",
    "release_lock",
    "run_with_heartbeat",
]

_log = logging.getLogger("lossless_hermes.concurrency.worker_lock")

T = TypeVar("T")


# ---------------------------------------------------------------------------
# acquire_lock
# ---------------------------------------------------------------------------


def acquire_lock(
    db: sqlite3.Connection,
    job_kind: WorkerJobKind | str,
    *,
    worker_id: str,
    ttl_s: float = WORKER_LOCK_TTL_S,
    job_session_key: str | None = None,
    job_metadata: str | None = None,
) -> bool:
    """Try to acquire a worker job lock. Atomic via PK uniqueness.

    Ports ``worker-lock.ts:69-109`` ``acquireLock``.

    Steps:

    1. **GC stale.** ``DELETE FROM lcm_worker_lock WHERE job_kind = ?
       AND expires_at <= datetime('now')``. The ``<=`` (not ``<``) is
       intentional — ``ttl_s=0`` is immediately reclaimable.
    2. **Insert.** ``INSERT OR IGNORE`` with ``datetime('now')`` and
       ``datetime('now', '+N seconds')``. If another worker holds an
       unexpired lock, the INSERT no-ops (``rowcount=0``).
    3. **Commit.** Python sqlite3 implicit-txn quirk; see module docstring.
    4. **Return** whether the INSERT actually inserted (``rowcount > 0``).

    Args:
        db: Open :class:`sqlite3.Connection`. Should have
            ``PRAGMA foreign_keys = ON`` (via
            :func:`lossless_hermes.db.connection.open_lcm_db`).
        job_kind: One of :data:`WorkerJobKind`. Accepts ``str`` for
            forward-compat with future kinds.
        worker_id: Unique worker identifier — see :func:`generate_worker_id`.
            Empty / whitespace raises :class:`ValueError`.
        ttl_s: Lock expiration in seconds from now. Defaults to
            :data:`~lossless_hermes.concurrency.model.WORKER_LOCK_TTL_S`.
            Passed through ``round()`` because SQL
            ``datetime('now', '+N seconds')`` only accepts integers.
        job_session_key: Optional scope — informational only. The lock is
            still per ``job_kind`` (table PK); this column lets readers
            distinguish e.g. "condensation on session A" vs "condensation
            on session B" via :func:`lock_info`.
        job_metadata: Arbitrary worker-set diagnostic tag (e.g.
            ``"backfill: model=voyage4large"``).

    Returns:
        ``True`` if the lock was acquired by this call (caller now owns
        it and must :func:`release_lock` or let it expire). ``False`` if
        another worker holds it and the lock has not yet expired.

    Raises:
        ValueError: ``worker_id`` is empty or whitespace.
        sqlite3.DatabaseError: SQL failure (DB error, schema missing,
            connection closed).

    Side effect:
        GC's any expired lock for this ``job_kind`` before attempting
        acquisition (so a stale dead-worker lock doesn't permanently
        block). The race "another process acquires in the gap between
        DELETE and INSERT" is handled by the ``INSERT OR IGNORE`` on PK
        uniqueness — the second writer's INSERT just no-ops. Worst case:
        caller is told ``False`` when they could have had the lock;
        never silently double-acquires.
    """
    if not worker_id or not worker_id.strip():
        # Mirrors ``worker-lock.ts:74-76`` — empty worker_id is a bug at
        # the call site, not a runtime exception worth retrying.
        raise ValueError("acquire_lock: worker_id is required (non-empty, non-whitespace)")

    # GC stale lock (if any). datetime('now') comparison; SQLite TEXT
    # comparison works lexicographically on ISO-8601 strings.
    # Note: `<=` not `<` so a lock with ttl=0 is immediately reclaimable.
    db.execute(
        """
        DELETE FROM lcm_worker_lock
         WHERE job_kind = ? AND expires_at <= datetime('now')
        """,
        (job_kind,),
    )

    ttl_seconds = max(0, round(ttl_s))
    # INSERT OR IGNORE: succeeds (rowcount=1) if no row holds the PK;
    # no-ops (rowcount=0) if someone else is already there.
    cur = db.execute(
        """
        INSERT OR IGNORE INTO lcm_worker_lock
            (job_kind, worker_id, acquired_at, expires_at, last_heartbeat_at,
             job_session_key, job_metadata)
        VALUES (?, ?, datetime('now'),
                datetime('now', '+' || ? || ' seconds'),
                datetime('now'), ?, ?)
        """,
        (job_kind, worker_id, ttl_seconds, job_session_key, job_metadata),
    )
    db.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# release_lock
# ---------------------------------------------------------------------------


def release_lock(
    db: sqlite3.Connection,
    job_kind: WorkerJobKind | str,
    worker_id: str,
) -> bool:
    """Release a held lock. Worker-id guards prevent releasing someone else's.

    Ports ``worker-lock.ts:121-130`` ``releaseLock``.

    A stale-but-not-yet-GC'd lock CAN be deleted here if you pass the
    ``worker_id`` that originally acquired it — intentional, per the TS
    comment ``worker-lock.ts:117-119``: a worker that just woke from
    sleep should be able to release its old lock if it remembers having
    it.

    Args:
        db: Open :class:`sqlite3.Connection`.
        job_kind: The job kind whose lock to release.
        worker_id: The ``worker_id`` that acquired the lock.

    Returns:
        ``True`` if a row matched (so we deleted it). ``False`` if the
        lock was already gone (e.g. expired + GC'd) or never held by
        this worker.
    """
    cur = db.execute(
        "DELETE FROM lcm_worker_lock WHERE job_kind = ? AND worker_id = ?",
        (job_kind, worker_id),
    )
    db.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# heartbeat_lock
# ---------------------------------------------------------------------------


def heartbeat_lock(
    db: sqlite3.Connection,
    job_kind: WorkerJobKind | str,
    worker_id: str,
    *,
    ttl_s: float = WORKER_LOCK_TTL_S,
) -> bool:
    """Extend ``expires_at`` on a lock the worker still holds. Idempotent.

    Ports ``worker-lock.ts:139-168`` ``heartbeatLock``.

    Returns ``True`` if the worker still owns the lock (heartbeat
    succeeded). Returns ``False`` if the lock was lost (preempted by
    fallback gateway, or a different worker now owns it, or it expired
    and has not yet been GC'd) — caller MUST abort its work in that
    case to avoid double-processing.

    Args:
        db: Open :class:`sqlite3.Connection`.
        job_kind: The job kind whose lock to extend.
        worker_id: The ``worker_id`` that acquired the lock.
        ttl_s: New expiration window in seconds from now. Defaults to
            :data:`~lossless_hermes.concurrency.model.WORKER_LOCK_TTL_S`.

    Returns:
        ``True`` on successful heartbeat (caller still owns the lock).
        ``False`` if the lock has been lost — caller MUST abort.
    """
    ttl_seconds = max(0, round(ttl_s))
    # LCM Wave-1 (2025-11-08): require ``expires_at > datetime('now')`` AND
    # ``worker_id = ?`` so an already-expired lock is NOT silently re-
    # extended. Without this predicate, a 90 s lock that lapsed mid-call
    # (worker blocked in a long Voyage request) would get refreshed by
    # the next heartbeat — making the lock look alive again — while a
    # concurrent autostart tick races to GC + reacquire it via
    # :func:`acquire_lock`. Both holders end up convinced they own "the"
    # lock, double-processing the same row. Reporting ``False`` instead
    # forces the caller to abort and the lazy-GC in :func:`acquire_lock`
    # cleans up.
    # Original: lossless-claw/src/concurrency/worker-lock.ts:146-165.
    cur = db.execute(
        """
        UPDATE lcm_worker_lock
           SET last_heartbeat_at = datetime('now'),
               expires_at = datetime('now', '+' || ? || ' seconds')
         WHERE job_kind = ?
           AND worker_id = ?
           AND expires_at > datetime('now')
        """,
        (ttl_seconds, job_kind, worker_id),
    )
    db.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# lock_info
# ---------------------------------------------------------------------------


def lock_info(db: sqlite3.Connection, job_kind: WorkerJobKind | str) -> LockInfo | None:
    """Inspect the current lock holder for ``job_kind``.

    Ports ``worker-lock.ts:174-202`` ``lockInfo``. Used by ``/lcm health``
    (Epic 08) to report worker state and by tests.

    Args:
        db: Open :class:`sqlite3.Connection`.
        job_kind: Job kind to inspect.

    Returns:
        :class:`LockInfo` snapshot of the row, or ``None`` if no lock
        is held for this kind.
    """
    row = db.execute(
        """
        SELECT job_kind, worker_id, acquired_at, expires_at,
               last_heartbeat_at, job_session_key, job_metadata
          FROM lcm_worker_lock
         WHERE job_kind = ?
        """,
        (job_kind,),
    ).fetchone()
    if row is None:
        return None
    return LockInfo(
        job_kind=row[0],
        worker_id=row[1],
        acquired_at=row[2],
        expires_at=row[3],
        last_heartbeat_at=row[4],
        job_session_key=row[5],
        job_metadata=row[6],
    )


# ---------------------------------------------------------------------------
# generate_worker_id
# ---------------------------------------------------------------------------


def generate_worker_id(role: str) -> str:
    """Return a worker_id suitable for :func:`acquire_lock`.

    Format: ``<role>-<pid>-<startMs>-<nonce>`` where ``nonce`` is a 6-char
    hex string from :func:`secrets.token_hex`. Uniqueness is across-time +
    across-process; collisions are astronomically unlikely.

    Ports ``worker-lock.ts:210-215`` ``generateWorkerId``. The TS source
    uses ``Math.floor(Math.random() * 0xffffff)``; we use
    :func:`secrets.token_hex` (3 bytes → 6 hex chars) because Python's
    :mod:`random` is not seeded for collision-resistance, and the
    extra few CPU cycles for ``secrets`` don't matter at a 30 s
    heartbeat cadence.

    Args:
        role: Short string identifying the worker's role (e.g.
            ``"gateway"``, ``"worker"``, ``"backfill-autostart"``). Used
            to prefix the worker_id for log readability.

    Returns:
        A new worker_id string of the form
        ``"{role}-{pid}-{ms}-{6 hex chars}"``.
    """
    return f"{role}-{os.getpid()}-{int(time.time() * 1000)}-{secrets.token_hex(3)}"


# ---------------------------------------------------------------------------
# run_with_heartbeat — async convenience wrapper (no TS analogue)
# ---------------------------------------------------------------------------


async def run_with_heartbeat(
    db: sqlite3.Connection,
    job_kind: WorkerJobKind | str,
    worker_id: str,
    *,
    body: Callable[[asyncio.Event], Awaitable[T]],
    ttl_s: float = WORKER_LOCK_TTL_S,
    heartbeat_s: float = WORKER_HEARTBEAT_S,
    job_session_key: str | None = None,
    job_metadata: str | None = None,
) -> T | None:
    """Acquire lock, run ``body`` with a background heartbeat, release on exit.

    Convenience wrapper used by the cron-style worker dispatchers
    (#05-07 backfill, etc.). Has no direct TS analogue — TS uses
    ``setInterval`` for the heartbeat loop; the Python port uses an
    :class:`asyncio.Task`.

    Lifecycle:

    1. :func:`acquire_lock` — if it returns ``False`` (another worker
       holds the lock), this function returns ``None`` immediately
       without running ``body``.
    2. Spawn a background :class:`asyncio.Task` that sleeps ``heartbeat_s``
       and calls :func:`heartbeat_lock`. If a heartbeat returns
       ``False`` (the lock was stolen), the task sets the
       :class:`asyncio.Event` passed to ``body`` so a cooperative body
       can abort early.
    3. ``await body(stolen_event)``.
    4. ``finally`` — cancel the heartbeat task and :func:`release_lock`.
       Both are best-effort; an unrelated exception in cleanup is logged
       but does not mask the body's exception (Python's :class:`finally`
       suppresses the original exception only if cleanup explicitly
       raises; we wrap each cleanup step in ``try/except``).

    Args:
        db: Open :class:`sqlite3.Connection` — used for the lock
            INSERT / UPDATE / DELETE. The body may use the same
            connection (Python single-threaded asyncio means there's
            no inter-task contention on this handle) or open its own.
        job_kind: Job kind to lock.
        worker_id: Worker identifier — see :func:`generate_worker_id`.
        body: Async callable taking the ``stolen_event``
            :class:`asyncio.Event`. The body should periodically check
            ``stolen_event.is_set()`` and abort cleanly on ``True``.
            Whatever the body returns is returned by this wrapper.
        ttl_s: Lock TTL on each acquire / heartbeat. Defaults to
            :data:`~lossless_hermes.concurrency.model.WORKER_LOCK_TTL_S`.
        heartbeat_s: Sleep between heartbeats. Defaults to
            :data:`~lossless_hermes.concurrency.model.WORKER_HEARTBEAT_S`.
            Must be < ``ttl_s`` or the heartbeat may miss its window.
        job_session_key: Forwarded to :func:`acquire_lock`.
        job_metadata: Forwarded to :func:`acquire_lock`.

    Returns:
        Whatever ``body`` returns, or ``None`` if acquisition failed.

    Raises:
        Any exception raised by ``body`` propagates after lock release.
    """
    acquired = acquire_lock(
        db,
        job_kind,
        worker_id=worker_id,
        ttl_s=ttl_s,
        job_session_key=job_session_key,
        job_metadata=job_metadata,
    )
    if not acquired:
        return None

    stolen_event = asyncio.Event()

    async def _heartbeat_loop() -> None:
        """Periodic heartbeat; flag ``stolen_event`` on first failure."""
        try:
            while not stolen_event.is_set():
                await asyncio.sleep(heartbeat_s)
                if stolen_event.is_set():
                    return
                try:
                    ok = heartbeat_lock(db, job_kind, worker_id, ttl_s=ttl_s)
                except sqlite3.Error:
                    _log.exception(
                        "worker-lock heartbeat: SQL error for kind=%s worker=%s",
                        job_kind,
                        worker_id,
                    )
                    stolen_event.set()
                    return
                if not ok:
                    _log.warning(
                        "worker-lock heartbeat: lost lock for kind=%s worker=%s "
                        "(another worker GC'd + acquired)",
                        job_kind,
                        worker_id,
                    )
                    stolen_event.set()
                    return
        except asyncio.CancelledError:
            # Normal shutdown path — body finished and the wrapper
            # is cancelling us. Re-raise so the task ends cleanly.
            raise

    hb_task = asyncio.create_task(_heartbeat_loop(), name=f"worker-lock-heartbeat:{job_kind}")
    try:
        return await body(stolen_event)
    finally:
        # Cancel the heartbeat first so we don't race a final heartbeat
        # against our own release. ``asyncio.shield`` is unnecessary
        # because we await the cancellation completion.
        hb_task.cancel()
        try:
            await hb_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            # CancelledError is the normal cancel path; any other
            # exception inside the heartbeat task was already logged
            # by the task itself. Either way, don't mask the body
            # outcome.
            pass
        try:
            release_lock(db, job_kind, worker_id)
        except sqlite3.Error:
            _log.exception(
                "worker-lock release: SQL error for kind=%s worker=%s",
                job_kind,
                worker_id,
            )
