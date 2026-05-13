"""§0 Concurrency model — LCM v4.1.1.

Ports ``lossless-claw/src/concurrency/model.ts`` (LCM commit ``1f07fbd``,
147 LOC TS → ~190 LOC Python). Merged from issue 05-05 (PR #36 — job kinds,
ms constants, ``assert_no_open_tx``) and issue 05-06 (PR #37 — seconds
aliases, lock dataclasses, ``assert_busy_timeout_for_role``).

This module is the single source of truth for §0 of architecture-v4.1.md +
v4.1.1 A6/A9 amendments. Code that violates these invariants must fail
loudly (assertion / raise) — never silently degrade.

### Invariants (architecture-v4.1.md §0.1, plus v4.1.1 A6 + A9)

1. **No LLM/network call inside any SQLite write transaction.** Leaf-write
   commits in T1; embed/extract queue async to worker T2. Synthesis call
   happens OUTSIDE cache-write transaction (insert ``status='building'``
   row in T1, run LLM, then update to ``status='ready'`` in T2 per
   v3.1 A8). Defended at runtime by :func:`assert_no_open_tx`.
2. **Gateway owns the hot path.** Per-turn assemble + leaf write + agent
   tool calls. Latency budget: assemble <100 ms; leaf write <50 ms.
3. **Worker owns cold rewrites.** Condensation, extraction, embedding
   backfill, theme consolidation, synthesis profile rebuilds. May take
   seconds-to-minutes per job; gateway never waits on it.
4. **Both processes use the same SQLite DB; no IPC beyond DB rows.**
5. **Worker uses SHORTER busy_timeout than gateway** so gateway always
   wins on contention. See :data:`GATEWAY_BUSY_TIMEOUT_MS` and
   :data:`WORKER_BUSY_TIMEOUT_MS`. Defended by :func:`assert_busy_timeout_for_role`.
6. **Migration ratchet owned by gateway only.** Worker startup checks
   ``lcm_migration_state`` for required v4.1 step; refuses to start if
   not present. Worker NEVER calls ``run_lcm_migrations``.
7. **PRAGMA foreign_keys = ON on every connection** (gateway and worker).
   Already set by :func:`lossless_hermes.db.connection.open_lcm_db` —
   verified via :func:`assert_foreign_keys_enabled`.
8. **Worker heartbeat task must use its OWN sqlite3.Connection** so
   long-running job code (synchronous DB writes, LLM blocks) does not
   starve the heartbeat. The asyncio event loop performs the same role
   here as Node ``worker_threads`` in TS: a non-cooperative blocking
   call inside the body would freeze heartbeats unless the heartbeat
   task is awaited from an independent task with its own connection.

### Constants

The Python port exposes **both** seconds (``_S``) and milliseconds
(``_MS``) forms at the public API. Seconds aliases are preferred at
new call sites (``asyncio.sleep`` / ``datetime`` think in seconds);
millisecond aliases are kept for parity with the TS source so porting
guides that cite ``WORKER_LOCK_TTL_MS`` still resolve cleanly.

| Constant | Value |
|---|---|
| ``GATEWAY_BUSY_TIMEOUT_MS`` | 30_000 |
| ``WORKER_BUSY_TIMEOUT_MS`` | 5_000 |
| ``WORKER_HEARTBEAT_S`` / ``WORKER_HEARTBEAT_MS`` | 30 / 30_000 |
| ``WORKER_LOCK_TTL_S`` / ``WORKER_LOCK_TTL_MS`` | 90 / 90_000 |
| ``GATEWAY_FALLBACK_SOAK_S`` / ``GATEWAY_FALLBACK_SOAK_MS`` | 300 / 300_000 |

See:

* ``docs/adr/018-concurrency-model.md`` — Option A chosen (``asyncio.Task``
  per kind, ``defaultdict(asyncio.Lock)`` per conv, ``lcm_worker_lock``
  table for cross-process).
* ``docs/adr/020-worker-loop-dispatcher.md`` — pins the lifecycle + the
  generation-counter rationale.
* ``docs/adr/029-wave-fix-provenance.md`` — every Wave-N fix carries an
  inline ``# LCM Wave-N (date): …`` comment.
* ``lossless-claw/src/concurrency/model.ts`` lines 1-147 — the TS source.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Final, Literal, get_args

__all__ = [
    "GATEWAY_BUSY_TIMEOUT_MS",
    "GATEWAY_FALLBACK_SOAK_MS",
    "GATEWAY_FALLBACK_SOAK_S",
    "LockInfo",
    "LockOwner",
    "WORKER_BUSY_TIMEOUT_MS",
    "WORKER_HEARTBEAT_MS",
    "WORKER_HEARTBEAT_S",
    "WORKER_JOB_KINDS",
    "WORKER_LOCK_TTL_MS",
    "WORKER_LOCK_TTL_S",
    "WorkerJobKind",
    "WorkerLockRow",
    "assert_busy_timeout_for_role",
    "assert_foreign_keys_enabled",
    "assert_no_open_tx",
]


# ---------------------------------------------------------------------------
# Time constants (verbatim port of concurrency/model.ts:55-87)
# ---------------------------------------------------------------------------

#: Gateway SQLite ``busy_timeout`` in ms. Set by
#: :func:`lossless_hermes.db.connection.open_lcm_db`. Long timeout
#: accommodates worker-process write transactions during condensation /
#: extraction passes. Ports ``model.ts:55``.
GATEWAY_BUSY_TIMEOUT_MS: Final[int] = 30_000

#: Worker SQLite ``busy_timeout`` in ms. MUST be shorter than
#: :data:`GATEWAY_BUSY_TIMEOUT_MS` so gateway always wins on contention.
#: Worker that hits SQLITE_BUSY backs off and retries; gateway hot path
#: does not stall waiting for worker writes. Ports ``model.ts:63``.
WORKER_BUSY_TIMEOUT_MS: Final[int] = 5_000

#: Worker heartbeat cadence in seconds. Worker updates
#: ``last_heartbeat_at = now, expires_at = now + WORKER_LOCK_TTL_S`` on
#: held locks at this interval. Ports ``model.ts:70`` (30_000 ms).
WORKER_HEARTBEAT_S: Final[int] = 30

#: Worker heartbeat cadence in ms — alias for parity with the TS source.
WORKER_HEARTBEAT_MS: Final[int] = WORKER_HEARTBEAT_S * 1000

#: Worker lock TTL in seconds. If a worker dies without releasing its
#: lock, other workers / fallback gateway can GC the lock once
#: ``expires_at < now``. 90 s = 3× heartbeat cadence (allows one missed
#: heartbeat without stealing). Ports ``model.ts:77`` (90_000 ms).
WORKER_LOCK_TTL_S: Final[int] = 90

#: Worker lock TTL in ms — alias for parity with the TS source.
WORKER_LOCK_TTL_MS: Final[int] = WORKER_LOCK_TTL_S * 1000

#: Gateway-fallback soak window in seconds. Gateway can take over a worker
#: job only when BOTH:
#:
#: * lock is GC'd (per :data:`WORKER_LOCK_TTL_S` expiry), AND
#: * ``last_heartbeat_at < now - GATEWAY_FALLBACK_SOAK_S``
#:
#: Two conditions prevent gateway from racing a slow-LLM-but-alive worker
#: (v4.1.1 A9 amendment). Ports ``model.ts:87`` (300_000 ms).
GATEWAY_FALLBACK_SOAK_S: Final[int] = 300

#: Gateway-fallback soak window in ms — alias for parity with the TS source.
GATEWAY_FALLBACK_SOAK_MS: Final[int] = GATEWAY_FALLBACK_SOAK_S * 1000


# ---------------------------------------------------------------------------
# Worker job kinds (verbatim port of concurrency/model.ts:95-104)
# ---------------------------------------------------------------------------

#: Job kinds tracked by the cross-process lock table. Adding a new kind
#: requires updating both this :data:`Literal` AND the
#: ``lcm_worker_lock.job_kind`` CHECK constraint (when added; current
#: schema is freeform TEXT for forward-compat). Ports ``model.ts:95-104``.
WorkerJobKind = Literal[
    "condensation",
    "extraction",
    "embedding-backfill",
    "profile-rebuild",
    "theme-consolidation",
    # v4.1.1 §C MED item — §11 ensemble judge runs.
    "eval",
]

#: Tuple form of the :data:`WorkerJobKind` literal, useful for runtime
#: iteration / validation (mirrors the TS ``WORKER_JOB_KINDS`` array).
WORKER_JOB_KINDS: Final[tuple[str, ...]] = get_args(WorkerJobKind)


# ---------------------------------------------------------------------------
# Lock data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LockInfo:
    """Snapshot of an ``lcm_worker_lock`` row.

    Returned by :func:`lossless_hermes.concurrency.worker_lock.lock_info`.
    Field names are the Python ``snake_case`` form of the TS interface
    (``model.ts:LockInfo`` — see ``worker-lock.ts:47-55`` for the TS
    type that produced these names).

    All timestamps are ISO-8601 strings emitted by SQL ``datetime('now')``
    (server-side clock — see ADR-018 §"Cross-process clock skew"). They
    compare lexicographically because they are zero-padded fixed-width
    ``"YYYY-MM-DD HH:MM:SS"`` strings.
    """

    job_kind: str
    """The ``lcm_worker_lock`` row's primary key (one row per kind)."""

    worker_id: str
    """``<role>-<pid>-<startMs>-<nonce>`` — see :func:`generate_worker_id`."""

    acquired_at: str
    """ISO-8601 timestamp (UTC) when the lock was first acquired."""

    expires_at: str
    """ISO-8601 timestamp at which the lock becomes reclaimable."""

    last_heartbeat_at: str
    """ISO-8601 timestamp of the last successful heartbeat update."""

    job_session_key: str | None
    """Informational scope — see ``worker-lock.ts:36-44`` comment."""

    job_metadata: str | None
    """Free-form diagnostic tag (e.g. ``"backfill: model=voyage4large"``)."""


@dataclass(frozen=True, slots=True)
class LockOwner:
    """Pair of ``(job_kind, worker_id)`` identifying who holds a lock.

    Used by the heartbeat / release path so caller code can pass a
    single value around instead of two separate strings — reduces the
    chance of release-with-mismatched-kind bugs in long call chains.
    """

    job_kind: str
    worker_id: str


# Backwards-compat alias matching the ``WorkerLockRow`` name in the
# issue spec — currently identical to :class:`LockInfo`. Kept as a
# distinct symbol so a future schema bump can distinguish the on-disk
# row shape from the public snapshot view without touching call sites.
WorkerLockRow = LockInfo


# ---------------------------------------------------------------------------
# §0 invariant helpers
# ---------------------------------------------------------------------------


def assert_no_open_tx(conn: sqlite3.Connection) -> None:
    """Raise if ``conn`` currently has a write transaction open.

    §0 forbids any LLM/network call inside a SQLite write transaction.
    Call this immediately before any ``await`` on an HTTP client (Voyage,
    LLM provider) to defend the invariant at runtime. The static-grep CI
    check (``await `` inside a ``with conn:`` block) catches the syntactic
    cases; this helper catches the dynamic ones (e.g. transactions opened
    via ``BEGIN`` statements not visible to the linter).

    Args:
        conn: Open :class:`sqlite3.Connection` to inspect.

    Raises:
        RuntimeError: ``conn`` has an open write transaction. The message
            cites the invariant and the suggested fix (commit/rollback
            before the network call, then re-open after).
    """
    if conn.in_transaction:
        raise RuntimeError(
            "[concurrency.model] §0 violation: SQLite write transaction is open. "
            "LLM/network calls are forbidden inside write transactions per "
            "architecture-v4.1.md §0.1. Commit the transaction first, then run "
            "the network call, then re-open a fresh transaction for the result."
        )


def assert_foreign_keys_enabled(db: sqlite3.Connection) -> None:
    """Confirm ``PRAGMA foreign_keys`` is ``ON`` for ``db``.

    Reads back ``PRAGMA foreign_keys`` and raises if it isn't ``1``. If
    the PRAGMA is off, every ``ON DELETE CASCADE`` in the schema becomes
    documentation-only — a class of data-integrity bug invisible at the
    SQL level.

    Mirrors :func:`lossless_hermes.db.connection.assert_foreign_keys_enabled`
    but lives here so concurrency-layer call sites don't need to pull in
    ``db.connection`` (mirrors the TS split — ``concurrency/model.ts:116``
    has its own copy of the assertion).

    Raises:
        RuntimeError: ``foreign_keys`` is not ``1`` for ``db``.
    """
    row = db.execute("PRAGMA foreign_keys").fetchone()
    if not row or row[0] != 1:
        raise RuntimeError(
            "[lossless_hermes.concurrency.model] foreign_keys is not ON "
            "for this connection — every ON DELETE CASCADE in the schema "
            "would silently no-op. Ensure the connection passes through "
            "open_lcm_db() in lossless_hermes/db/connection.py, or set "
            "PRAGMA foreign_keys = ON explicitly."
        )


def assert_busy_timeout_for_role(
    db: sqlite3.Connection,
    role: Literal["gateway", "worker"],
) -> None:
    """Assert ``PRAGMA busy_timeout`` is at least the per-role minimum.

    Ports ``concurrency/model.ts:134-147`` ``assertBusyTimeoutForRole``.
    Worker connections must have ``busy_timeout >= WORKER_BUSY_TIMEOUT_MS``
    (5 s) and gateway connections must have at least
    ``GATEWAY_BUSY_TIMEOUT_MS`` (30 s). Without the inequality, a
    multi-process contention storm can starve one side without bound.

    Args:
        db: Open :class:`sqlite3.Connection` (or apsw-equivalent).
        role: ``"gateway"`` or ``"worker"``.

    Raises:
        RuntimeError: actual ``busy_timeout`` is below the expected
            minimum for ``role``.
    """
    expected = GATEWAY_BUSY_TIMEOUT_MS if role == "gateway" else WORKER_BUSY_TIMEOUT_MS
    row = db.execute("PRAGMA busy_timeout").fetchone()
    actual = int(row[0]) if row else 0
    if actual < expected:
        raise RuntimeError(
            f"[lossless_hermes.concurrency.model] busy_timeout for {role} "
            f"is {actual} ms, expected at least {expected} ms. Set via "
            f"PRAGMA busy_timeout = {expected}."
        )
