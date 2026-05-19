"""Tests for :mod:`lossless_hermes.commands.worker` (issue 08-17).

Exercises the two ``/lcm worker`` subcommand handlers:

* :func:`~lossless_hermes.commands.worker.run_status` — read-only worker
  status snapshot (parent dispatch).
* :func:`~lossless_hermes.commands.worker.run_tick_backfill` —
  owner-gated forced embedding-backfill tick.

Ports the TS test cases from ``lossless-claw/test/lcm-command.test.ts``
``"/lcm worker*"`` (LCM commit ``1f07fbd`` on branch ``pr-613``):

* (TS-port) ``allows /lcm worker status (read-only) when sender is not
  owner`` → :func:`test_status_not_owner_gated_reachable`.
* (TS-port) ``rejects /lcm worker tick embedding-backfill when sender is
  not owner`` — the gate is upstream (ADR-013), so the Python
  equivalent is :func:`test_no_in_handler_owner_check` (the AC-mandated
  ``grep`` invariant) plus the dispatcher-table assertion in
  ``tests/commands/test_owner_gating.py``.

Spec-mandated additional tests (issue 08-17 AC lines 98-101):

* :func:`test_status_renders_stale` — seeded lock with
  ``last_heartbeat_at`` older than ttl → output contains ``STALE``.
* :func:`test_tick_lock_held_by_peer` — tick returns ``lock_not_acquired``
  → output reports ``Skipped: ... lock held by host=...``.
* :func:`test_tick_empty_queue` — no work → output reports
  ``Skipped: queue is empty``.
* :func:`test_tick_processes_count` — 250 unembedded leaves → output
  shows ``Processed: 200 embeddings``.

The tick tests monkeypatch
:func:`lossless_hermes.commands.worker.tick_embedding_backfill` so the
rendering branches (processed / empty-queue / lock-held) are exercised
deterministically without the full Voyage HTTP + sqlite-vec stack — the
orchestrator surface itself is covered by
``tests/operator/test_worker_orchestrator.py``.

See:

* ``epics/08-cli-ops/08-17-worker-status.md`` — this issue.
* ``lossless-claw/src/plugin/lcm-command.ts:516-546, 1726-1765,
  1778-1897`` — TS source pinned at commit ``1f07fbd``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from lossless_hermes.commands import worker as worker_mod
from lossless_hermes.commands.worker import run_status, run_tick_backfill
from lossless_hermes.concurrency.worker_lock import acquire_lock
from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.embeddings.backfill import BackfillResult, BackfillSkippedDoc
from lossless_hermes.embeddings.store import register_embedding_profile


# ---------------------------------------------------------------------------
# Fixtures + stubs
# ---------------------------------------------------------------------------


@dataclass
class _FakeEngine:
    """Minimal engine stub exposing ``_db``.

    The handler's ``_resolve_db`` probes ``_db`` first (the canonical
    attribute on the wired engine), then alternatives — this stub uses
    the canonical name.
    """

    _db: sqlite3.Connection | None


@dataclass
class _FakeParsed:
    """Minimal :class:`ParsedLcmCommand`-shaped stub for handler tests."""

    tokens: list[str] = field(default_factory=list)
    engine: _FakeEngine | None = None
    flags: dict[str, Any] = field(default_factory=dict)
    name: str = "worker"
    raw_args: str = ""


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    """In-memory DB with the migration ladder applied + one conversation."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(conn, fts5_available=False, seed_default_prompts=False)
    conn.execute(
        "INSERT INTO conversations (conversation_id, session_id, session_key, active) "
        "VALUES (1, 's1', 'sk1', 1)"
    )
    try:
        yield conn
    finally:
        conn.close()


def _parsed(
    db: sqlite3.Connection | None,
    tokens: list[str] | None = None,
) -> _FakeParsed:
    """Build a :class:`_FakeParsed` carrying ``db`` + ``tokens``."""
    return _FakeParsed(tokens=tokens or [], engine=_FakeEngine(_db=db))


def _seed_leaf(
    db: sqlite3.Connection,
    *,
    summary_id: str,
    content: str = "leaf content",
    token_count: int = 10,
) -> None:
    """Insert one ``kind='leaf'`` summary row (unembedded by default)."""
    db.execute(
        """
        INSERT INTO summaries
            (summary_id, conversation_id, kind, content, token_count,
             source_message_token_count, descendant_token_count, session_key)
        VALUES (?, 1, 'leaf', ?, ?, 0, 0, 'sk1')
        """,
        (summary_id, content, token_count),
    )


def _make_backfill_result(
    *,
    embedded_count: int = 0,
    skipped: list[BackfillSkippedDoc] | None = None,
    skipped_over_cap: int = 0,
    per_tick_limit_reached: bool = False,
    lock_not_acquired: bool = False,
    voyage_tokens_consumed: int = 0,
    duration_ms: int = 0,
) -> BackfillResult:
    """Build a :class:`BackfillResult` for the monkeypatched tick."""
    return BackfillResult(
        embedded_count=embedded_count,
        skipped=skipped or [],
        skipped_over_cap=skipped_over_cap,
        per_tick_limit_reached=per_tick_limit_reached,
        lock_not_acquired=lock_not_acquired,
        voyage_tokens_consumed=voyage_tokens_consumed,
        duration_ms=duration_ms,
    )


def _patch_tick(
    monkeypatch: pytest.MonkeyPatch,
    result: BackfillResult,
) -> None:
    """Monkeypatch ``worker.tick_embedding_backfill`` to return ``result``.

    The handler runs the orchestrator tick inside ``asyncio.run`` via the
    ``_run_backfill_tick`` async wrapper; we patch the orchestrator
    function the wrapper calls so no Voyage HTTP / sqlite-vec is touched.
    """

    async def _fake_tick(*_args: Any, **_kwargs: Any) -> BackfillResult:
        return result

    monkeypatch.setattr(worker_mod, "tick_embedding_backfill", _fake_tick)


# ===========================================================================
# run_status — read-only snapshot
# ===========================================================================


def test_status_renders_header_and_sections(db: sqlite3.Connection) -> None:
    """`/lcm worker status` renders the worker-status + pending sections."""
    out = run_status(_parsed(db, ["status"]))
    assert "Lossless Hermes v" in out
    assert "### Worker Status" in out
    assert "### Pending Work" in out
    # Every WORKER_JOB_KINDS literal gets a line; idle kinds show "idle".
    assert "embedding-backfill" in out
    assert "idle (no lock held)" in out


def test_status_bare_worker_aliases_status(db: sqlite3.Connection) -> None:
    """Bare `/lcm worker` (no tokens) renders the same status snapshot."""
    out = run_status(_parsed(db, []))
    assert "### Worker Status" in out
    assert "### Pending Work" in out


def test_status_db_unavailable_renders_hint() -> None:
    """`/lcm worker status` with no DB renders a friendly hint, not a crash."""
    out = run_status(_parsed(None, ["status"]))
    assert "### Worker Status" in out
    assert "not yet opened" in out


def test_status_no_engine_does_not_crash() -> None:
    """A parsed object with no engine renders the DB-unavailable path."""
    out = run_status(_FakeParsed(tokens=["status"], engine=None))
    assert "### Worker Status" in out
    assert "not yet opened" in out


def test_status_held_lock_renders_worker_id(db: sqlite3.Connection) -> None:
    """A held (fresh) lock renders ``HELD by`` + the worker id, no STALE."""
    acquire_lock(db, "extraction", worker_id="wk-extraction-1")
    out = run_status(_parsed(db, ["status"]))
    assert "HELD by `wk-extraction-1`" in out
    # A freshly-acquired lock has a current heartbeat → NOT stale.
    extraction_line = next(line for line in out.splitlines() if "**extraction**" in line)
    assert "STALE" not in extraction_line


def test_status_renders_stale(db: sqlite3.Connection) -> None:
    """AC line 98: a lock with ``last_heartbeat_at`` older than ttl → ``STALE``.

    Seed a lock, then backdate its ``last_heartbeat_at`` well past the
    90 s ``WORKER_LOCK_TTL_S`` window. The snapshot's ``is_stale`` flag
    (default ttl) drives the ``STALE`` marker in the rendered output.
    """
    acquire_lock(db, "condensation", worker_id="wk-cond-stale")
    # Backdate the heartbeat 1000 s into the past (TTL is 90 s) so the
    # lock is unambiguously stale. expires_at is left in the future so
    # acquire_lock's stale-GC didn't already evict it, and so the lock
    # still shows as HELD (the staleness is a heartbeat-age signal).
    db.execute(
        "UPDATE lcm_worker_lock SET last_heartbeat_at = datetime('now', '-1000 seconds') "
        "WHERE job_kind = 'condensation'"
    )

    out = run_status(_parsed(db, ["status"]))
    assert "HELD by `wk-cond-stale`" in out
    cond_line = next(line for line in out.splitlines() if "**condensation**" in line)
    assert "STALE" in cond_line


def test_status_pending_counts_render(db: sqlite3.Connection) -> None:
    """Pending section reports the embedding-backfill + extraction counts.

    With an active embedding profile registered and unembedded leaves
    seeded, the embedding-backfill pending counter is a real number (not
    the ``(model not registered)`` sentinel).
    """
    register_embedding_profile(db, model_name="voyage-3", dim=4)
    for i in range(3):
        _seed_leaf(db, summary_id=f"leaf_{i}")
    out = run_status(_parsed(db, ["status"]))
    assert "Embedding backfill pending: 3" in out
    assert "Extraction queue:" in out


def test_status_pending_no_profile_shows_sentinel(db: sqlite3.Connection) -> None:
    """No active embedding profile → backfill pending shows the sentinel."""
    out = run_status(_parsed(db, ["status"]))
    assert "Embedding backfill pending: (model not registered)" in out


# ===========================================================================
# run_status — parent dispatch (tick sub-cases)
# ===========================================================================


def test_status_tick_no_kind_returns_error(db: sqlite3.Connection) -> None:
    """`/lcm worker tick` (no kind) → help-style error naming valid kinds."""
    out = run_status(_parsed(db, ["tick"]))
    assert "[lcm] worker tick:" in out
    assert "requires a job kind" in out
    assert "embedding-backfill" in out


def test_status_tick_unknown_kind_returns_error(db: sqlite3.Connection) -> None:
    """AC line 94: `/lcm worker tick foo` → exact unknown-kind wording."""
    out = run_status(_parsed(db, ["tick", "foo"]))
    assert out == ("[lcm] worker tick: unknown kind 'foo'. Valid kinds: embedding-backfill")


def test_status_tick_unknown_kind_case_insensitive(db: sqlite3.Connection) -> None:
    """The kind comparison is case-insensitive (TS lowercases ``rest[1]``)."""
    out = run_status(_parsed(db, ["tick", "BOGUS"]))
    assert "unknown kind 'bogus'" in out


def test_status_bogus_subcommand_returns_error(db: sqlite3.Connection) -> None:
    """`/lcm worker bogus` → help-style error (TS lcm-command.ts:542-545)."""
    out = run_status(_parsed(db, ["bogus"]))
    assert "[lcm] worker tick:" in out
    assert "accepts `status`" in out


def test_status_tick_known_kind_delegates_to_tick(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`worker tick embedding-backfill` reaching run_status delegates to the tick.

    Normally the router resolves the 3-token path straight to
    ``run_tick_backfill``; this verifies the parent-dispatch fallback in
    ``run_status`` produces identical behaviour (here: the empty-queue
    skip, since no leaves are seeded).
    """
    monkeypatch.setenv("VOYAGE_API_KEY", "test-key")
    register_embedding_profile(db, model_name="voyage-3", dim=4)
    _patch_tick(monkeypatch, _make_backfill_result())
    out = run_status(_parsed(db, ["tick", "embedding-backfill"]))
    assert out.startswith("[lcm] worker tick embedding-backfill")
    assert "Skipped: queue is empty" in out


# ===========================================================================
# run_tick_backfill — pre-flight checks
# ===========================================================================


def test_tick_db_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """No DB → tick skips with a friendly message, no crash."""
    monkeypatch.setenv("VOYAGE_API_KEY", "test-key")
    out = run_tick_backfill(_parsed(None))
    assert out.startswith("[lcm] worker tick embedding-backfill")
    assert "Skipped: engine DB connection not available" in out


def test_tick_missing_voyage_key(db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty VOYAGE_API_KEY → tick skips (the tick makes paid HTTP calls)."""
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    out = run_tick_backfill(_parsed(db))
    assert "Skipped: VOYAGE_API_KEY env var is empty" in out


def test_tick_no_active_profile(db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """No active embedding profile → tick skips with a register hint."""
    monkeypatch.setenv("VOYAGE_API_KEY", "test-key")
    out = run_tick_backfill(_parsed(db))
    assert "Skipped: no active embedding model registered" in out


# ===========================================================================
# run_tick_backfill — outcome rendering
# ===========================================================================


def test_tick_empty_queue(db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC line 100: no work → output reports ``Skipped: queue is empty``.

    The orchestrator tick returns a zero-count result (no embedded docs,
    no per-doc failures, no over-cap rows) — the handler reads that as
    an empty queue.
    """
    monkeypatch.setenv("VOYAGE_API_KEY", "test-key")
    register_embedding_profile(db, model_name="voyage-3", dim=4)
    _patch_tick(monkeypatch, _make_backfill_result(embedded_count=0))
    out = run_tick_backfill(_parsed(db))
    assert out.startswith("[lcm] worker tick embedding-backfill")
    assert "Skipped: queue is empty (no unembedded leaves)" in out


def test_tick_lock_held_by_peer(db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC line 99: ``lock_not_acquired`` → output reports the peer holder.

    Seed the ``embedding-backfill`` lock as held by a peer worker and
    make the tick return ``lock_not_acquired=True`` (what the inner tick
    does when ``acquire_lock`` fails). The handler reports the holder it
    reads back from the lock table.
    """
    monkeypatch.setenv("VOYAGE_API_KEY", "test-key")
    register_embedding_profile(db, model_name="voyage-3", dim=4)
    # A peer already holds the embedding-backfill lock.
    acquire_lock(db, "embedding-backfill", worker_id="wk-peer-host")
    _patch_tick(monkeypatch, _make_backfill_result(lock_not_acquired=True))
    out = run_tick_backfill(_parsed(db))
    assert out.startswith("[lcm] worker tick embedding-backfill")
    assert "Skipped: embedding-backfill lock held by host=wk-peer-host" in out


def test_tick_lock_held_no_row_falls_back(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``lock_not_acquired`` but the lock row vanished → generic holder text.

    Race: the peer released the lock between the tick's acquire-fail and
    the handler's holder lookup. The handler reports a generic ``a peer
    worker`` rather than raising.
    """
    monkeypatch.setenv("VOYAGE_API_KEY", "test-key")
    register_embedding_profile(db, model_name="voyage-3", dim=4)
    # No lock row seeded — lock_info returns None.
    _patch_tick(monkeypatch, _make_backfill_result(lock_not_acquired=True))
    out = run_tick_backfill(_parsed(db))
    assert "Skipped: embedding-backfill lock held by a peer worker" in out


def test_tick_processes_count(db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC line 101: 250 unembedded leaves → output shows ``Processed: 200``.

    The orchestrator tick caps at ``per_tick_limit=200``; with 250 leaves
    seeded and the tick reporting 200 embedded, the handler renders the
    processed line and the remaining-queue count (the 50 leaves a
    follow-up tick would still process).
    """
    monkeypatch.setenv("VOYAGE_API_KEY", "test-key")
    register_embedding_profile(db, model_name="voyage-3", dim=4)
    # Seed 250 unembedded leaves. count_pending_docs reports them as
    # pending (none have an embedding row), so after a 200-embed tick the
    # remaining-queue count the handler prints is 250 (the mock tick does
    # not actually write embeddings — it just reports the count).
    for i in range(250):
        _seed_leaf(db, summary_id=f"leaf_{i:03d}")
    _patch_tick(
        monkeypatch,
        _make_backfill_result(
            embedded_count=200,
            per_tick_limit_reached=True,
            voyage_tokens_consumed=2_000,
            duration_ms=14_200,
        ),
    )
    out = run_tick_backfill(_parsed(db))
    assert out.startswith("[lcm] worker tick embedding-backfill")
    assert "Processed: 200 embeddings" in out
    # Voyage spend is surfaced.
    assert "Voyage tokens: 2,000" in out
    assert "estimated cost: $" in out
    # Remaining-queue count is rendered (the mock tick wrote nothing, so
    # all 250 leaves are still pending).
    assert "Remaining queue: 250 leaves" in out
    # Tick latency rendered from duration_ms.
    assert "Tick latency: 14.2 s" in out


def test_tick_processes_reports_skipped_docs(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tick with per-doc failures surfaces the skipped count.

    A result with ``embedded_count=0`` but a non-empty ``skipped`` list
    is NOT an empty queue — work was attempted and some docs failed. The
    handler renders the processed path with the failure count.
    """
    monkeypatch.setenv("VOYAGE_API_KEY", "test-key")
    register_embedding_profile(db, model_name="voyage-3", dim=4)
    _patch_tick(
        monkeypatch,
        _make_backfill_result(
            embedded_count=0,
            skipped=[BackfillSkippedDoc(summary_id="leaf_x", reason="voyage_other")],
        ),
    )
    out = run_tick_backfill(_parsed(db))
    # Not the empty-queue path — work was attempted.
    assert "Skipped: queue is empty" not in out
    assert "Processed: 0 embeddings" in out
    assert "Skipped (per-doc failures): 1" in out


def test_tick_processes_reports_over_cap(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tick that filtered over-cap leaves surfaces the over-cap count."""
    monkeypatch.setenv("VOYAGE_API_KEY", "test-key")
    register_embedding_profile(db, model_name="voyage-3", dim=4)
    _patch_tick(
        monkeypatch,
        _make_backfill_result(embedded_count=5, skipped_over_cap=2),
    )
    out = run_tick_backfill(_parsed(db))
    assert "Processed: 5 embeddings" in out
    assert "Skipped (over-cap, NOT embeddable): 2" in out


def test_tick_inner_exception_rendered_not_raised(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fatal tick error (e.g. Voyage auth) renders as a skip, not a crash.

    ``tick_embedding_backfill`` re-raises fatal Voyage auth errors; the
    handler catches anything that escapes and renders a one-line failure.
    """
    monkeypatch.setenv("VOYAGE_API_KEY", "test-key")
    register_embedding_profile(db, model_name="voyage-3", dim=4)

    async def _raising_tick(*_args: Any, **_kwargs: Any) -> BackfillResult:
        raise RuntimeError("voyage auth: 401 unauthorized")

    monkeypatch.setattr(worker_mod, "tick_embedding_backfill", _raising_tick)
    out = run_tick_backfill(_parsed(db))
    assert out.startswith("[lcm] worker tick embedding-backfill")
    assert "Skipped: backfill tick failed" in out
    assert "voyage auth: 401 unauthorized" in out


# ===========================================================================
# ADR-013 invariant — no in-handler owner check
# ===========================================================================


def test_no_in_handler_owner_check() -> None:
    """AC line 95: ``grep -n "is_owner" worker.py`` returns 0 lines.

    ADR-013: owner-gating is upstream (Hermes ``SlashAccessPolicy``).
    The handler must never re-check owner status itself — the dispatcher
    table marks ``worker tick embedding-backfill`` as owner-gated and
    that drives the upstream gate.
    """
    source_path = worker_mod.__file__
    assert source_path is not None
    with open(source_path, "r", encoding="utf-8") as f:
        source = f.read()
    assert "is_owner" not in source, (
        "commands/worker.py must not reference is_owner — owner-gating is upstream per ADR-013."
    )
    # Defensive: no other owner/sender-context probes either.
    for forbidden in ("sender_is_owner", "senderIsOwner", "ctx.senderIsOwner"):
        assert forbidden not in source, (
            f"commands/worker.py must not reference {forbidden!r} (ADR-013)."
        )


def test_status_not_owner_gated_reachable(db: sqlite3.Connection) -> None:
    """TS parity: `/lcm worker status` is reachable regardless of owner status.

    Mirrors the TS test ``allows /lcm worker status (read-only) when
    sender is not owner``. There is no security context on the parsed
    object (ADR-013) — the handler simply renders the snapshot. The
    dispatcher table marks ``worker`` / ``worker status`` as
    ``owner_gated=False``; this confirms the body has no gate of its own.
    """
    out = run_status(_parsed(db, ["status"]))
    # Read-only status output — no "operator-only" / "rejected" gate text.
    assert "operator-only" not in out.lower()
    assert "### Worker Status" in out


# ===========================================================================
# Dispatcher inventory — worker subcommands are wired
# ===========================================================================


def test_worker_subcommands_in_dispatch_table() -> None:
    """The 3 worker canonical paths are wired with the right gating flags.

    ``worker`` / ``worker status`` → NOT owner-gated;
    ``worker tick embedding-backfill`` → owner-gated. This pins the
    08-01 router table against the issue spec (AC line 96).
    """
    from lossless_hermes.plugin.commands import _SUBCOMMANDS

    by_path = {path: (handler, gated) for (path, handler, gated, _d) in _SUBCOMMANDS}

    assert by_path["worker"] == (
        "lossless_hermes.commands.worker:run_status",
        False,
    )
    assert by_path["worker status"] == (
        "lossless_hermes.commands.worker:run_status",
        False,
    )
    assert by_path["worker tick embedding-backfill"] == (
        "lossless_hermes.commands.worker:run_tick_backfill",
        True,
    )
