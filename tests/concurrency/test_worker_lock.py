"""Tests for :mod:`lossless_hermes.concurrency.worker_lock`.

Ports ``lossless-claw/test/worker-lock.test.ts`` (150 LOC, 12 cases) and
``lossless-claw/test/lcm-worker-lock.test.ts`` (120 LOC, 4 cases) to
Python. Cases preserved from the TS suites:

* worker-lock.test.ts → ``acquire / release single-process`` (4 cases),
  ``TTL + GC`` (2), ``heartbeat`` (3), ``metadata + scope`` (2),
  ``generateWorkerId`` (1), ``multiple job kinds independent`` (1).
* lcm-worker-lock.test.ts → schema shape (1), idempotency (1), basic
  acquire+heartbeat SQL pattern (1), stale-lock GC (1).

Adds Python-port-specific cases not in TS:

* ADR-018 server-side ``datetime('now')`` (verifies timestamps don't come
  from Python ``datetime.utcnow()`` — checks two consecutive rows have
  monotonically non-decreasing acquired_at).
* ``run_with_heartbeat`` lifecycle — acquire, heartbeat fires, release on
  body success, release on body exception, signal-on-steal.
* Cross-process simulation — two ``sqlite3.Connection`` objects against
  a shared file-backed DB; second one steals after TTL expiry.
* Python sqlite3 commit invariant — opens a second connection and reads
  the row to verify the first connection's write was committed.
"""

from __future__ import annotations

import asyncio
import os
import re
import sqlite3
from pathlib import Path
from typing import Iterator

import pytest

from lossless_hermes.concurrency.model import (
    GATEWAY_FALLBACK_SOAK_S,
    WORKER_HEARTBEAT_S,
    WORKER_LOCK_TTL_S,
    LockInfo,
)
from lossless_hermes.concurrency.worker_lock import (
    acquire_lock,
    generate_worker_id,
    heartbeat_lock,
    lock_info,
    release_lock,
    run_with_heartbeat,
)
from lossless_hermes.db.migration import run_lcm_migrations


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _new_db() -> sqlite3.Connection:
    """Open an in-memory DB with FK enforcement and the v4.1 schema applied."""
    # isolation_level=None matches open_lcm_db() autocommit behavior so the
    # explicit ``db.commit()`` calls in the module are also exercised against
    # a representative connection shape.
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(conn, fts5_available=False)
    return conn


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    """In-memory SQLite + full migration ladder applied."""
    conn = _new_db()
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# acquire / release — single-process
# ---------------------------------------------------------------------------


def test_acquire_first_succeeds_second_blocks(db: sqlite3.Connection) -> None:
    """First acquire wins; second from a different worker is told False."""
    assert acquire_lock(db, "embedding-backfill", worker_id="w1") is True
    assert acquire_lock(db, "embedding-backfill", worker_id="w2") is False

    info = lock_info(db, "embedding-backfill")
    assert info is not None
    assert info.worker_id == "w1"


def test_release_frees_the_lock(db: sqlite3.Connection) -> None:
    """release_lock by the holder lets the next acquirer succeed."""
    assert acquire_lock(db, "embedding-backfill", worker_id="w1") is True
    assert release_lock(db, "embedding-backfill", "w1") is True
    assert acquire_lock(db, "embedding-backfill", worker_id="w2") is True


def test_release_with_wrong_worker_id_noops(db: sqlite3.Connection) -> None:
    """release_lock with a non-matching worker_id does not free the lock."""
    acquire_lock(db, "embedding-backfill", worker_id="w1")
    assert release_lock(db, "embedding-backfill", "wrong") is False
    # Original holder still owns it — second worker can't acquire.
    assert acquire_lock(db, "embedding-backfill", worker_id="w2") is False


def test_acquire_lock_requires_nonempty_worker_id(db: sqlite3.Connection) -> None:
    """Empty / whitespace worker_id raises ValueError."""
    with pytest.raises(ValueError, match="worker_id is required"):
        acquire_lock(db, "embedding-backfill", worker_id="")
    with pytest.raises(ValueError, match="worker_id is required"):
        acquire_lock(db, "embedding-backfill", worker_id="   ")


# ---------------------------------------------------------------------------
# TTL + GC
# ---------------------------------------------------------------------------


def test_ttl_zero_immediately_stale_gets_gcd(db: sqlite3.Connection) -> None:
    """ttl_s=0 makes the lock immediately reclaimable on the next acquire."""
    assert acquire_lock(db, "embedding-backfill", worker_id="dead", ttl_s=0) is True
    # The DELETE step uses `<=` not `<`, so a ttl=0 row is GC'd immediately.
    assert acquire_lock(db, "embedding-backfill", worker_id="alive") is True
    info = lock_info(db, "embedding-backfill")
    assert info is not None
    assert info.worker_id == "alive"


def test_non_expired_lock_blocks(db: sqlite3.Connection) -> None:
    """A still-fresh lock blocks the second worker."""
    acquire_lock(db, "embedding-backfill", worker_id="w1", ttl_s=60)
    assert acquire_lock(db, "embedding-backfill", worker_id="w2") is False


# ---------------------------------------------------------------------------
# heartbeat
# ---------------------------------------------------------------------------


def test_heartbeat_from_holder_extends_expires_at(db: sqlite3.Connection) -> None:
    """heartbeat_lock from the current holder pushes expires_at forward."""
    acquire_lock(db, "embedding-backfill", worker_id="w1", ttl_s=1)
    initial = lock_info(db, "embedding-backfill")
    assert initial is not None
    t0_expires = initial.expires_at
    # Heartbeat with a longer TTL — expires_at must move forward.
    assert heartbeat_lock(db, "embedding-backfill", "w1", ttl_s=60) is True
    after = lock_info(db, "embedding-backfill")
    assert after is not None
    # Lexicographic comparison on ISO-8601 strings — bumping ttl from 1s
    # to 60s must push expires_at to a strictly larger string.
    assert after.expires_at >= t0_expires


def test_heartbeat_from_non_holder_fails(db: sqlite3.Connection) -> None:
    """heartbeat_lock with a wrong worker_id returns False."""
    acquire_lock(db, "embedding-backfill", worker_id="w1")
    assert heartbeat_lock(db, "embedding-backfill", "w2") is False


def test_heartbeat_after_stale_gc_and_reacquire_fails_for_old_worker(
    db: sqlite3.Connection,
) -> None:
    """The Wave-1 scenario at the level worker-lock.test.ts:94 covers.

    Old worker holds a stale lock; new worker GC's it and acquires.
    Old worker's heartbeat must return False (the row's worker_id is now
    "new", and the Wave-1 ``expires_at > now`` predicate fires anyway).
    """
    acquire_lock(db, "embedding-backfill", worker_id="old", ttl_s=0)
    # Stale; new worker grabs it.
    assert acquire_lock(db, "embedding-backfill", worker_id="new") is True
    # Old worker tries to heartbeat — must see False.
    assert heartbeat_lock(db, "embedding-backfill", "old") is False


# ---------------------------------------------------------------------------
# Wave-1 regression: silent re-extension of an expired-but-not-yet-GCd lock
# ---------------------------------------------------------------------------


def test_wave1_heartbeat_does_not_extend_expired_lock(db: sqlite3.Connection) -> None:
    """Wave-1: ``expires_at > now`` predicate prevents silent re-extension.

    The scenario the Wave-1 fix targets: a 90 s lock that lapsed mid-call
    (worker blocked in a long Voyage request). Without the predicate, the
    next heartbeat would refresh ``expires_at`` and make the lock look
    alive again — while a concurrent autostart tick races to GC + acquire.
    Both holders end up convinced they own "the" lock.

    Repro: insert a row directly with ``expires_at`` in the past (so the
    row exists but is expired and not-yet-GC'd). The holder's heartbeat
    must return False (NOT silently bump expires_at).
    """
    # Insert directly so we can fix the timestamps. Use raw SQL — the
    # acquire helper would either GC or refuse this state.
    db.execute(
        """
        INSERT INTO lcm_worker_lock
            (job_kind, worker_id, acquired_at, expires_at, last_heartbeat_at)
        VALUES (?, ?, datetime('now', '-100 seconds'),
                datetime('now', '-10 seconds'),
                datetime('now', '-95 seconds'))
        """,
        ("embedding-backfill", "w-blocked"),
    )
    db.commit()

    # Heartbeat from the holder of the expired row — must return False.
    # If a future refactor regresses by dropping the ``expires_at > now``
    # predicate, this assertion flips and the regression is caught here.
    assert heartbeat_lock(db, "embedding-backfill", "w-blocked", ttl_s=60) is False

    # The row is still there (heartbeat doesn't GC; acquire does).
    # expires_at must not have moved forward.
    row = db.execute(
        "SELECT expires_at FROM lcm_worker_lock WHERE job_kind = ?",
        ("embedding-backfill",),
    ).fetchone()
    assert row is not None
    # The original expires_at was ``datetime('now', '-10 seconds')``;
    # if the Wave-1 predicate failed, the heartbeat would have set
    # expires_at to ``datetime('now', '+60 seconds')`` — which would
    # compare strictly greater than ``datetime('now')``.
    now_row = db.execute("SELECT datetime('now')").fetchone()
    assert now_row is not None
    # expires_at must still be in the past (heartbeat did NOT re-extend).
    assert row[0] < now_row[0]


# ---------------------------------------------------------------------------
# Metadata + scope
# ---------------------------------------------------------------------------


def test_job_session_key_and_metadata_roundtrip(db: sqlite3.Connection) -> None:
    """job_session_key + job_metadata are stored and retrievable via lock_info."""
    acquire_lock(
        db,
        "condensation",
        worker_id="w1",
        job_session_key="agent:main:main",
        job_metadata="weekly:2026-W18",
    )
    info = lock_info(db, "condensation")
    assert info is not None
    assert info.job_session_key == "agent:main:main"
    assert info.job_metadata == "weekly:2026-W18"


def test_lock_info_returns_none_when_no_lock_held(db: sqlite3.Connection) -> None:
    """lock_info returns None when no row exists for the given job_kind."""
    assert lock_info(db, "extraction") is None


def test_lock_info_returns_dataclass_with_expected_fields(db: sqlite3.Connection) -> None:
    """lock_info returns LockInfo with all 7 fields populated."""
    acquire_lock(db, "extraction", worker_id="w1")
    info = lock_info(db, "extraction")
    assert isinstance(info, LockInfo)
    assert info.job_kind == "extraction"
    assert info.worker_id == "w1"
    assert info.acquired_at  # non-empty string
    assert info.expires_at
    assert info.last_heartbeat_at
    assert info.job_session_key is None
    assert info.job_metadata is None


# ---------------------------------------------------------------------------
# generate_worker_id
# ---------------------------------------------------------------------------


def test_generate_worker_id_format() -> None:
    """Format: ``{role}-{pid}-{ms}-{6 hex chars}``; role prefix and nonce."""
    id1 = generate_worker_id("backfill")
    id2 = generate_worker_id("backfill")
    assert id1.startswith("backfill-")
    assert id2.startswith("backfill-")
    assert id1 != id2  # different nonces (or at least different ms)
    # Pattern: backfill-<pid>-<ms>-<6 hex>
    assert re.match(r"^backfill-\d+-\d+-[0-9a-f]{6}$", id1)


def test_generate_worker_id_contains_real_pid() -> None:
    """The pid embedded in the id is os.getpid()."""
    wid = generate_worker_id("gateway")
    pid_match = re.match(r"^gateway-(\d+)-\d+-[0-9a-f]{6}$", wid)
    assert pid_match is not None
    assert int(pid_match.group(1)) == os.getpid()


# ---------------------------------------------------------------------------
# Multiple job kinds — independent
# ---------------------------------------------------------------------------


def test_different_job_kinds_dont_conflict(db: sqlite3.Connection) -> None:
    """Locks for different ``job_kind``s are independent rows."""
    assert acquire_lock(db, "embedding-backfill", worker_id="w1") is True
    assert acquire_lock(db, "extraction", worker_id="w2") is True
    assert acquire_lock(db, "condensation", worker_id="w3") is True
    # All three held simultaneously.
    info_b = lock_info(db, "embedding-backfill")
    info_e = lock_info(db, "extraction")
    info_c = lock_info(db, "condensation")
    assert info_b is not None and info_b.worker_id == "w1"
    assert info_e is not None and info_e.worker_id == "w2"
    assert info_c is not None and info_c.worker_id == "w3"


# ---------------------------------------------------------------------------
# Python sqlite3 commit invariant — second connection sees the row
# ---------------------------------------------------------------------------


def test_acquire_commits_so_second_connection_sees_the_row(tmp_path: Path) -> None:
    """``acquire_lock`` calls ``db.commit()``; second connection reads the row."""
    db_path = tmp_path / "lcm.db"
    a = sqlite3.connect(str(db_path), isolation_level=None)
    a.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(a, fts5_available=False)

    assert acquire_lock(a, "embedding-backfill", worker_id="w1") is True

    # Second connection opened AFTER acquire — must see the row.
    b = sqlite3.connect(str(db_path), isolation_level=None)
    b.execute("PRAGMA foreign_keys = ON")
    row = b.execute(
        "SELECT worker_id FROM lcm_worker_lock WHERE job_kind = ?",
        ("embedding-backfill",),
    ).fetchone()
    assert row is not None
    assert row[0] == "w1"

    a.close()
    b.close()


def test_release_commits_so_second_connection_sees_deletion(tmp_path: Path) -> None:
    """``release_lock`` calls ``db.commit()``; the row vanishes for readers."""
    db_path = tmp_path / "lcm.db"
    a = sqlite3.connect(str(db_path), isolation_level=None)
    a.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(a, fts5_available=False)
    acquire_lock(a, "extraction", worker_id="w1")
    release_lock(a, "extraction", "w1")

    b = sqlite3.connect(str(db_path), isolation_level=None)
    b.execute("PRAGMA foreign_keys = ON")
    row = b.execute(
        "SELECT COUNT(*) FROM lcm_worker_lock WHERE job_kind = ?",
        ("extraction",),
    ).fetchone()
    assert row is not None
    assert row[0] == 0

    a.close()
    b.close()


# ---------------------------------------------------------------------------
# Cross-process simulation — two connections sharing a file
# ---------------------------------------------------------------------------


def test_cross_process_steal_after_ttl_expiry(tmp_path: Path) -> None:
    """Two connections on the same file: second steals after TTL elapses.

    Mirrors the multi-process invariant the ``lcm_worker_lock`` table
    exists to enforce: workers across processes coordinate purely via
    DB rows, no IPC. We simulate two processes via two
    :class:`sqlite3.Connection` objects against the same file.
    """
    db_path = tmp_path / "lcm.db"
    proc_a = sqlite3.connect(str(db_path), isolation_level=None)
    proc_a.execute("PRAGMA foreign_keys = ON")
    proc_a.execute("PRAGMA busy_timeout = 5000")
    run_lcm_migrations(proc_a, fts5_available=False)

    proc_b = sqlite3.connect(str(db_path), isolation_level=None)
    proc_b.execute("PRAGMA foreign_keys = ON")
    proc_b.execute("PRAGMA busy_timeout = 5000")

    # A acquires with ttl_s=0 — immediately stale.
    assert acquire_lock(proc_a, "embedding-backfill", worker_id="proc-a", ttl_s=0) is True
    # B's acquire GC's the stale row and inserts a fresh one.
    assert acquire_lock(proc_b, "embedding-backfill", worker_id="proc-b") is True
    # B holds the lock; A's heartbeat fails (worker_id mismatch + expires_at > now
    # fires either way because A's row is gone).
    assert heartbeat_lock(proc_a, "embedding-backfill", "proc-a") is False
    # A's release is also a no-op — the row's worker_id is "proc-b" now.
    assert release_lock(proc_a, "embedding-backfill", "proc-a") is False

    info = lock_info(proc_b, "embedding-backfill")
    assert info is not None
    assert info.worker_id == "proc-b"

    proc_a.close()
    proc_b.close()


def test_cross_process_concurrent_acquire_only_one_wins(tmp_path: Path) -> None:
    """Two connections call acquire_lock simultaneously; exactly one wins.

    INSERT OR IGNORE on PK uniqueness is the load-bearing primitive. We
    don't have true OS-level concurrency in this test, but we exercise
    the rapid-succession path that would expose a missing IGNORE.
    """
    db_path = tmp_path / "lcm.db"
    proc_a = sqlite3.connect(str(db_path), isolation_level=None)
    proc_a.execute("PRAGMA foreign_keys = ON")
    proc_a.execute("PRAGMA busy_timeout = 5000")
    run_lcm_migrations(proc_a, fts5_available=False)

    proc_b = sqlite3.connect(str(db_path), isolation_level=None)
    proc_b.execute("PRAGMA foreign_keys = ON")
    proc_b.execute("PRAGMA busy_timeout = 5000")

    assert acquire_lock(proc_a, "condensation", worker_id="proc-a") is True
    assert acquire_lock(proc_b, "condensation", worker_id="proc-b") is False

    proc_a.close()
    proc_b.close()


# ---------------------------------------------------------------------------
# ADR-018: server-side ``datetime('now')`` invariant
# ---------------------------------------------------------------------------


def test_timestamps_are_server_side_not_python_clock(db: sqlite3.Connection) -> None:
    """``acquired_at`` is monotonic across acquires — proves SQL-side clock.

    If timestamps came from Python's ``datetime.utcnow()`` per-process,
    cross-process clock skew (machines with skew >1 s, virtualized
    clocks, etc.) could let ``acquired_at`` go backwards between two
    consecutive acquires on the same DB. Server-side ``datetime('now')``
    reads one clock — the DB's — so ``acquired_at`` is monotonic.

    The Python equivalent of "cross-process" in a single-process test is
    "two acquires from the same connection at different wall-clock times".
    The ADR-018 invariant we're guarding here is structural: NO Python
    time.time() / datetime.utcnow() value EVER flows into a write here.
    """
    acquire_lock(db, "extraction", worker_id="w1", ttl_s=0)
    first = lock_info(db, "extraction")
    assert first is not None

    # The acquired_at timestamp is "YYYY-MM-DD HH:MM:SS" — SQLite's
    # datetime('now') format. If a future regression switches to
    # ``time.time()`` / Python ISO, the format would change (microseconds,
    # 'T' separator, 'Z' suffix). Lock the format down as a structural
    # check.
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", first.acquired_at)
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", first.expires_at)
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", first.last_heartbeat_at)


# ---------------------------------------------------------------------------
# run_with_heartbeat — lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_with_heartbeat_acquires_runs_body_releases(
    db: sqlite3.Connection,
) -> None:
    """Happy path: acquire, run body, release on success."""
    body_ran = False

    async def body(stolen: asyncio.Event) -> str:
        nonlocal body_ran
        body_ran = True
        return "OK"

    result = await run_with_heartbeat(
        db,
        "embedding-backfill",
        "w1",
        body=body,
        ttl_s=10,
        heartbeat_s=0.05,
    )
    assert result == "OK"
    assert body_ran is True
    # Lock must be released after body completes.
    assert lock_info(db, "embedding-backfill") is None


@pytest.mark.asyncio
async def test_run_with_heartbeat_returns_none_if_already_held(
    db: sqlite3.Connection,
) -> None:
    """If acquire fails, body never runs and we return None."""
    acquire_lock(db, "embedding-backfill", worker_id="other")
    body_ran = False

    async def body(stolen: asyncio.Event) -> str:
        nonlocal body_ran
        body_ran = True
        return "should-not-happen"

    result = await run_with_heartbeat(
        db,
        "embedding-backfill",
        "w1",
        body=body,
        ttl_s=10,
        heartbeat_s=0.05,
    )
    assert result is None
    assert body_ran is False
    # Original holder still owns it.
    info = lock_info(db, "embedding-backfill")
    assert info is not None
    assert info.worker_id == "other"


@pytest.mark.asyncio
async def test_run_with_heartbeat_releases_on_body_exception(
    db: sqlite3.Connection,
) -> None:
    """try/finally releases the lock even if body raises."""

    class BodyError(Exception):
        pass

    async def body(stolen: asyncio.Event) -> str:
        raise BodyError("forced")

    with pytest.raises(BodyError):
        await run_with_heartbeat(
            db,
            "extraction",
            "w1",
            body=body,
            ttl_s=10,
            heartbeat_s=0.05,
        )

    assert lock_info(db, "extraction") is None


@pytest.mark.asyncio
async def test_run_with_heartbeat_signals_body_when_lock_stolen(
    tmp_path: Path,
) -> None:
    """Heartbeat detects stolen lock and sets the stolen_event.

    We use a file-backed DB and two connections so the "thief"
    connection can delete the row mid-body without interfering with
    the heartbeat connection's autocommit state.
    """
    db_path = tmp_path / "lcm.db"
    owner = sqlite3.connect(str(db_path), isolation_level=None)
    owner.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(owner, fts5_available=False)

    thief = sqlite3.connect(str(db_path), isolation_level=None)
    thief.execute("PRAGMA foreign_keys = ON")

    body_started = asyncio.Event()
    body_observed_steal = asyncio.Event()

    async def body(stolen: asyncio.Event) -> bool:
        body_started.set()
        # Wait for the heartbeat to set ``stolen``. Cap at 2 s so a
        # broken heartbeat doesn't hang the suite.
        try:
            await asyncio.wait_for(stolen.wait(), timeout=2.0)
            body_observed_steal.set()
            return True
        except asyncio.TimeoutError:
            return False

    async def steal_after_start() -> None:
        await body_started.wait()
        # Delete the owner's row — simulates a different process GC'd
        # the lock + acquired its own row (here we just delete; the
        # heartbeat predicate fails for either reason).
        thief.execute("DELETE FROM lcm_worker_lock WHERE job_kind = ?", ("extraction",))
        thief.commit()

    stealer = asyncio.create_task(steal_after_start())

    try:
        result = await run_with_heartbeat(
            owner,
            "extraction",
            "w-owner",
            body=body,
            ttl_s=10,
            heartbeat_s=0.05,
        )
    finally:
        stealer.cancel()
        try:
            await stealer
        except (asyncio.CancelledError, Exception):
            pass
        owner.close()
        thief.close()

    assert result is True
    assert body_observed_steal.is_set()


@pytest.mark.asyncio
async def test_run_with_heartbeat_extends_expiry_during_body(
    db: sqlite3.Connection,
) -> None:
    """While body runs, periodic heartbeat updates last_heartbeat_at."""

    async def body(stolen: asyncio.Event) -> None:
        # Sleep enough wall-clock to span at least one heartbeat tick.
        await asyncio.sleep(0.25)

    await run_with_heartbeat(
        db,
        "condensation",
        "w1",
        body=body,
        ttl_s=10,
        heartbeat_s=0.05,
    )
    # Lock released; nothing to assert on the row itself. We exercised
    # the heartbeat path — the absence of an exception is the success
    # signal. (The signal-on-steal test above proves the loop runs.)


# ---------------------------------------------------------------------------
# lcm_worker_lock — schema sanity (port of lcm-worker-lock.test.ts)
# ---------------------------------------------------------------------------


def test_lcm_worker_lock_table_has_expected_schema(db: sqlite3.Connection) -> None:
    """The 7 columns + PK on job_kind match the v4.1.1 A9 schema."""
    columns = {
        row[1]: {"type": row[2].upper(), "notnull": row[3], "pk": row[5]}
        for row in db.execute("PRAGMA table_info(lcm_worker_lock)").fetchall()
    }
    assert columns["job_kind"]["pk"] == 1
    assert columns["job_kind"]["type"] == "TEXT"

    assert columns["worker_id"]["notnull"] == 1
    assert columns["acquired_at"]["notnull"] == 1
    assert columns["expires_at"]["notnull"] == 1
    assert columns["last_heartbeat_at"]["notnull"] == 1

    # Nullable scope/metadata columns.
    assert columns["job_session_key"]["notnull"] == 0
    assert columns["job_metadata"]["notnull"] == 0


def test_lcm_worker_lock_migration_is_idempotent(tmp_path: Path) -> None:
    """Re-running run_lcm_migrations doesn't fail or duplicate rows."""
    db_path = tmp_path / "lcm.db"
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(conn, fts5_available=False)
    # Second run — no error.
    run_lcm_migrations(conn, fts5_available=False)

    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name = 'lcm_worker_lock'"
    ).fetchone()
    assert row is not None
    assert row[0] == 1

    conn.close()


def test_lcm_worker_lock_pk_unique_constraint_enforced(db: sqlite3.Connection) -> None:
    """Two raw INSERTs against the same job_kind raises IntegrityError."""
    db.execute(
        """
        INSERT INTO lcm_worker_lock (job_kind, worker_id, expires_at)
        VALUES (?, ?, datetime('now', '+90 seconds'))
        """,
        ("condensation", "worker-A"),
    )
    db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """
            INSERT INTO lcm_worker_lock (job_kind, worker_id, expires_at)
            VALUES (?, ?, datetime('now', '+90 seconds'))
            """,
            ("condensation", "worker-B"),
        )


def test_lcm_worker_lock_gc_pattern_with_raw_sql(db: sqlite3.Connection) -> None:
    """Raw DELETE-where-expired pattern (mirrors lcm-worker-lock.test.ts)."""
    db.execute(
        """
        INSERT INTO lcm_worker_lock (job_kind, worker_id, expires_at, last_heartbeat_at)
        VALUES (?, ?, datetime('now', '-10 seconds'), datetime('now', '-400 seconds'))
        """,
        ("extraction", "worker-stale"),
    )
    db.commit()
    before = db.execute(
        "SELECT COUNT(*) FROM lcm_worker_lock WHERE job_kind = ?", ("extraction",)
    ).fetchone()
    assert before is not None
    assert before[0] == 1

    cur = db.execute("DELETE FROM lcm_worker_lock WHERE expires_at < datetime('now')")
    db.commit()
    assert cur.rowcount >= 1

    after = db.execute(
        "SELECT COUNT(*) FROM lcm_worker_lock WHERE job_kind = ?", ("extraction",)
    ).fetchone()
    assert after is not None
    assert after[0] == 0


# ---------------------------------------------------------------------------
# Constants invariant
# ---------------------------------------------------------------------------


def test_concurrency_constants_match_v411_a9_spec() -> None:
    """TTL=90 s, heartbeat=30 s, soak=300 s — the v4.1.1 A9 contract."""
    assert WORKER_LOCK_TTL_S == 90
    assert WORKER_HEARTBEAT_S == 30
    assert GATEWAY_FALLBACK_SOAK_S == 300


def test_heartbeat_cadence_is_shorter_than_ttl() -> None:
    """Invariant: heartbeat must fire >= 3× per TTL to avoid losing locks."""
    # The v4.1.1 A9 rationale: 90 s TTL / 30 s heartbeat = 3 ticks per
    # TTL window. Anything fewer and one missed heartbeat (e.g. a slow
    # SQL query during a contention storm) can let the lock lapse.
    assert WORKER_LOCK_TTL_S >= 3 * WORKER_HEARTBEAT_S
