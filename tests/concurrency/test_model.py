"""Tests for :mod:`lossless_hermes.concurrency.model`.

Covers:

* Constants port verbatim from ``concurrency/model.ts`` (numeric values).
* :data:`WorkerJobKind` Literal exposes all six kinds.
* :data:`WORKER_JOB_KINDS` mirrors the Literal at runtime.
* :func:`assert_no_open_tx` enforces the §0 invariant.
* :func:`assert_foreign_keys_enabled` rejects connections with FKs off.
"""

from __future__ import annotations

import sqlite3
from typing import Iterator

import pytest

from lossless_hermes.concurrency.model import (
    GATEWAY_BUSY_TIMEOUT_MS,
    GATEWAY_FALLBACK_SOAK_MS,
    WORKER_BUSY_TIMEOUT_MS,
    WORKER_HEARTBEAT_MS,
    WORKER_JOB_KINDS,
    WORKER_LOCK_TTL_MS,
    assert_foreign_keys_enabled,
    assert_no_open_tx,
)


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    """In-memory SQLite connection in autocommit mode."""
    c = sqlite3.connect(":memory:", isolation_level=None)
    try:
        yield c
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Constants — values must match TS source exactly
# ---------------------------------------------------------------------------


def test_constants_match_ts_source() -> None:
    """Numeric constants ported verbatim from concurrency/model.ts:55-87."""
    assert GATEWAY_BUSY_TIMEOUT_MS == 30_000
    assert WORKER_BUSY_TIMEOUT_MS == 5_000
    assert WORKER_HEARTBEAT_MS == 30_000
    assert WORKER_LOCK_TTL_MS == 90_000
    assert GATEWAY_FALLBACK_SOAK_MS == 300_000


def test_worker_busy_timeout_is_shorter_than_gateway() -> None:
    """Cross-process invariant: worker yields to gateway under contention."""
    assert WORKER_BUSY_TIMEOUT_MS < GATEWAY_BUSY_TIMEOUT_MS


def test_lock_ttl_is_three_times_heartbeat() -> None:
    """3× heartbeat allows one missed heartbeat without stealing the lock."""
    assert WORKER_LOCK_TTL_MS == WORKER_HEARTBEAT_MS * 3


# ---------------------------------------------------------------------------
# WorkerJobKind / WORKER_JOB_KINDS
# ---------------------------------------------------------------------------


def test_worker_job_kinds_exposes_all_six_kinds() -> None:
    """Catalog matches concurrency/model.ts:95-104."""
    expected = (
        "condensation",
        "extraction",
        "embedding-backfill",
        "profile-rebuild",
        "theme-consolidation",
        "eval",
    )
    assert WORKER_JOB_KINDS == expected


# ---------------------------------------------------------------------------
# §0 invariant helpers
# ---------------------------------------------------------------------------


def test_assert_no_open_tx_passes_when_no_transaction(
    conn: sqlite3.Connection,
) -> None:
    """No exception when conn has no write transaction open."""
    assert_no_open_tx(conn)


def test_assert_no_open_tx_raises_when_transaction_open(
    conn: sqlite3.Connection,
) -> None:
    """RuntimeError when conn is mid-write-transaction."""
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.execute("BEGIN")
    try:
        with pytest.raises(RuntimeError, match="§0 violation"):
            assert_no_open_tx(conn)
    finally:
        conn.execute("COMMIT")


def test_assert_foreign_keys_enabled_passes_when_on(
    conn: sqlite3.Connection,
) -> None:
    """No exception when PRAGMA foreign_keys = ON."""
    conn.execute("PRAGMA foreign_keys = ON")
    assert_foreign_keys_enabled(conn)


def test_assert_foreign_keys_enabled_raises_when_off(
    conn: sqlite3.Connection,
) -> None:
    """RuntimeError when PRAGMA foreign_keys is OFF (the default)."""
    conn.execute("PRAGMA foreign_keys = OFF")
    with pytest.raises(RuntimeError, match="foreign_keys is not ON"):
        assert_foreign_keys_enabled(conn)
