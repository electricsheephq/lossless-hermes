"""Concurrency-model constants + §0 invariant helpers.

Port of ``lossless-claw/src/concurrency/model.ts`` (LCM commit ``1f07fbd``,
147 LOC TS → ~150 LOC Python). The module is the single source of truth
for v4.1.1 §0 of architecture-v4.1.md plus A6/A9 amendments: code that
violates these invariants must fail loudly (assertion / exception),
never silently degrade.

### §0 invariants (load-bearing — DO NOT relax without a superseding ADR)

1. **NO LLM/network call inside any SQLite write transaction.** Leaf-write
   commits in T1; embed/extract queue async to worker T2. Synthesis call
   happens OUTSIDE cache-write transaction (insert ``status='building'``
   row in T1, run LLM, then update to ``status='ready'`` in T2 per v3.1 A8).
2. Gateway owns the hot path. Per-turn assemble + leaf write + agent
   tool calls. Latency budget: assemble <100ms; leaf write <50ms.
3. Worker owns cold rewrites. Condensation, extraction, embedding
   backfill, theme consolidation, synthesis profile rebuilds.
4. Both processes use the same SQLite DB; no IPC beyond DB rows.
5. Worker uses SHORTER ``busy_timeout`` than gateway so gateway always
   wins on contention.
6. Migration ratchet owned by gateway only. Worker startup checks
   ``lcm_migration_state``; refuses to start if missing.
7. ``PRAGMA foreign_keys = ON`` on every connection (per ADR-018).
8. Worker heartbeat must run with its OWN connection (v4.1.1 A9). The
   ``WorkerLoop`` does NOT thread; the Python port uses ``asyncio.Task``
   per job kind (per ADR-020).

Job-kind catalog ported verbatim from ``concurrency/model.ts:95-104``.
Timing constants are seconds in Python (the TS module used milliseconds);
the SQL clock-tick fields in ``lcm_worker_lock`` still record ISO-8601
strings.

See:

* ``docs/adr/018-concurrency-model.md`` — Option A chosen (``asyncio.Task``
  per kind, ``defaultdict(asyncio.Lock)`` per conv, ``lcm_worker_lock``
  table for cross-process).
* ``docs/adr/020-worker-loop-dispatcher.md`` — pins the lifecycle + the
  generation-counter rationale.
* ``docs/adr/029-wave-fix-provenance.md`` — every Wave-N fix carries an
  inline ``# LCM Wave-N (date): …`` comment.
* ``lossless-claw/src/concurrency/model.ts`` — the TS source this module
  ports.
"""

from __future__ import annotations

import sqlite3
from typing import Literal, get_args

__all__ = [
    "GATEWAY_BUSY_TIMEOUT_MS",
    "GATEWAY_FALLBACK_SOAK_MS",
    "WORKER_BUSY_TIMEOUT_MS",
    "WORKER_HEARTBEAT_MS",
    "WORKER_JOB_KINDS",
    "WORKER_LOCK_TTL_MS",
    "WorkerJobKind",
    "assert_foreign_keys_enabled",
    "assert_no_open_tx",
]


# ---------------------------------------------------------------------------
# Job-kind catalog (verbatim port of concurrency/model.ts:95-104)
# ---------------------------------------------------------------------------

WorkerJobKind = Literal[
    "condensation",
    "extraction",
    "embedding-backfill",
    "profile-rebuild",
    "theme-consolidation",
    # v4.1.1 §C MED item — §11 ensemble judge runs.
    "eval",
]
"""Job kinds tracked by the cross-process lock table.

Adding a new kind requires updating both this Literal AND the
``lcm_worker_lock.job_kind`` CHECK constraint (when added; current
schema is freeform TEXT for forward-compat).
"""

WORKER_JOB_KINDS: tuple[WorkerJobKind, ...] = get_args(WorkerJobKind)
"""Runtime-iterable tuple of the WorkerJobKind values."""


# ---------------------------------------------------------------------------
# Timing constants (verbatim port of concurrency/model.ts:55-87)
# ---------------------------------------------------------------------------

GATEWAY_BUSY_TIMEOUT_MS: int = 30_000
"""Gateway SQLite busy_timeout (ms). Set by the connection helper. Long
timeout accommodates worker-process write transactions during condensation /
extraction passes.
"""

WORKER_BUSY_TIMEOUT_MS: int = 5_000
"""Worker SQLite busy_timeout (ms). MUST be shorter than
:data:`GATEWAY_BUSY_TIMEOUT_MS` so gateway always wins on contention.
Worker that hits ``SQLITE_BUSY`` backs off and retries; gateway hot path
does not stall waiting for worker writes.
"""

WORKER_HEARTBEAT_MS: int = 30_000
"""Worker heartbeat cadence (ms). Worker writes
``last_heartbeat_at = now, expires_at = now + WORKER_LOCK_TTL_MS`` on its
held locks at this interval. See ``lcm_worker_lock`` table.
"""

WORKER_LOCK_TTL_MS: int = 90_000
"""Worker lock TTL (ms). If a worker dies without releasing its lock,
other workers / fallback gateway can GC the lock once ``expires_at < now``.
90s = 3× heartbeat cadence (allows one missed heartbeat without stealing).
"""

GATEWAY_FALLBACK_SOAK_MS: int = 300_000
"""Gateway-fallback soak window (ms). Gateway can take over a worker job
only when BOTH:

* lock is GC'd (per :data:`WORKER_LOCK_TTL_MS` expiry), AND
* ``last_heartbeat_at < now - GATEWAY_FALLBACK_SOAK_MS``

Two conditions prevent gateway from racing a slow-LLM-but-alive worker
(v4.1.1 A9 amendment).
"""


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


def assert_foreign_keys_enabled(conn: sqlite3.Connection) -> None:
    """Raise if ``PRAGMA foreign_keys`` is OFF for ``conn``.

    Per v4.1.1 A6 invariant: every code path that opens a SQLite
    connection MUST end up with FKs enabled, otherwise every
    ``ON DELETE CASCADE`` clause in the schema becomes documentation-only.
    Counterpart of ``concurrency/model.ts:assertForeignKeysEnabled``.

    Args:
        conn: Open :class:`sqlite3.Connection` to inspect.

    Raises:
        RuntimeError: ``PRAGMA foreign_keys`` is OFF. The message points
            the caller to ``lossless_hermes.db.connection`` (the standard
            connection helper which already enables FKs).
    """
    row = conn.execute("PRAGMA foreign_keys").fetchone()
    if not row or row[0] != 1:
        raise RuntimeError(
            "[concurrency.model] foreign_keys is not ON for this connection — "
            "every ON DELETE CASCADE in the schema would silently no-op. "
            "Ensure the connection goes through lossless_hermes.db.connection, "
            "or set PRAGMA foreign_keys = ON explicitly."
        )
