"""Tests for :mod:`lossless_hermes.operator.worker_orchestrator` (issue 08-10).

Ports ``lossless-claw/test/operator-worker-orchestrator.test.ts`` (LCM
commit ``1f07fbd`` on branch ``pr-613``) plus the per-AC additions from
the issue spec:

* (TS-port) :func:`test_status_snapshot_empty_locks_no_pending` — empty
  locks + extraction count = 0 when no work pending.
* (TS-port) :func:`test_status_snapshot_reflects_acquired_lock` — locks
  surface acquired lock info.
* (TS-port) :func:`test_status_snapshot_extraction_queue_count` — queue
  count reflects pending rows.
* (TS-port) :func:`test_tick_extraction_happy_path` — acquires lock,
  runs extraction, releases lock.
* (TS-port) :func:`test_tick_skipped_when_lock_held_by_peer` — second
  tick with peer holding lock returns ``lock_acquired=False`` and
  doesn't call the extractor.
* (TS-port) :func:`test_tick_releases_lock_even_on_extractor_throw` —
  lock released after extractor raises.
* (TS-port) :func:`test_force_release_returns_true_then_false` — first
  call returns ``True``; subsequent ``False``.
* (TS-port) :func:`test_heartbeat_all_held_locks` — per-kind status
  with worker_id guard.
* (TS-port) :func:`test_heartbeat_all_held_locks_empty_input` — all kinds
  marked ``"skipped"``.

Spec-mandated additional tests:

* :func:`test_backfill_tick_processes_200` — 250 unembedded leaves;
  single tick processes exactly 200.
* :func:`test_force_release_host_guard` — guard prevents releasing a
  lock held by a different worker_id.
* :func:`test_stale_detection_flags_old_heartbeat` — staleness flag
  surfaces when ``last_heartbeat_at`` is older than TTL.
* :func:`test_create_worker_llm_call_extracts_text` — adapter returns
  the expected :class:`LlmCallResult` shape and resolves the model.
* :func:`test_create_worker_llm_call_timeout` — adapter respects the
  configured timeout.

See:

* ``epics/08-cli-ops/08-10-worker-orchestrator.md`` — this issue.
* ``lossless-claw/test/operator-worker-orchestrator.test.ts`` — TS source.
* ``lossless-claw/src/operator/worker-orchestrator.ts`` — implementation.
* ``lossless-claw/src/operator/worker-llm.ts`` — merged adapter.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest

from lossless_hermes.concurrency.worker_lock import (
    acquire_lock,
    generate_worker_id,
    lock_info,
)
from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.extraction.coreference import ExtractedEntity
from lossless_hermes.operator.worker_orchestrator import (
    DEFAULT_WORKER_LLM_TIMEOUT_S,
    ExtractionTickArgs,
    ExtractionTickResultWithLock,
    ForceReleaseResult,
    HeartbeatResult,
    PendingCounts,
    WorkerLlmConfig,
    WorkerLockSnapshot,
    WorkerStatusSnapshot,
    create_worker_llm_call,
    force_release_lock,
    get_worker_status_snapshot,
    heartbeat_all_held_locks,
    tick_extraction,
)
from lossless_hermes.synthesis.dispatch import LlmCallArgs, LlmCallResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _new_db() -> sqlite3.Connection:
    """In-memory SQLite with the full LCM migration ladder applied."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(conn, fts5_available=False, seed_default_prompts=False)
    # Seed one conversation so summaries' FK is satisfied.
    conn.execute(
        "INSERT INTO conversations (conversation_id, session_id, session_key, active) "
        "VALUES (1, 's1', 'sk1', 1)"
    )
    return conn


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    """Migrated in-memory DB with seeded conversation."""
    conn = _new_db()
    try:
        yield conn
    finally:
        conn.close()


def _seed_leaf(
    db: sqlite3.Connection,
    *,
    summary_id: str,
    content: str = "x",
    token_count: int = 1,
    session_key: str = "sk1",
    conversation_id: int = 1,
) -> None:
    """Insert one ``kind='leaf'`` summary row."""
    db.execute(
        """
        INSERT INTO summaries
            (summary_id, conversation_id, kind, content, token_count,
             source_message_token_count, descendant_token_count, session_key)
        VALUES (?, ?, 'leaf', ?, ?, 0, 0, ?)
        """,
        (summary_id, conversation_id, content, token_count, session_key),
    )


def _seed_extraction_queue(
    db: sqlite3.Connection,
    *,
    queue_id: str,
    leaf_id: str,
    kind: str = "entity",
) -> None:
    """Insert one ``lcm_extraction_queue`` row."""
    db.execute(
        """
        INSERT INTO lcm_extraction_queue (queue_id, leaf_id, kind, queued_at)
        VALUES (?, ?, ?, datetime('now'))
        """,
        (queue_id, leaf_id, kind),
    )


# ---------------------------------------------------------------------------
# Extractor fakes
# ---------------------------------------------------------------------------


class _CountingExtractor:
    """Deterministic extractor that records every call.

    Returns the same pre-configured entities for every leaf so tests can
    assert on the call count and per-leaf invocation order without
    threading extraction logic through.
    """

    def __init__(self, entities_per_call: list[ExtractedEntity] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.entities_per_call = entities_per_call or [
            ExtractedEntity(surface="thing", entity_type="x")
        ]

    async def __call__(
        self,
        *,
        summary_id: str,
        session_key: str,
        content: str,
    ) -> list[ExtractedEntity]:
        self.calls.append({
            "summary_id": summary_id,
            "session_key": session_key,
            "content": content,
        })
        return list(self.entities_per_call)


class _ThrowingExtractor:
    """Extractor that always raises on first call."""

    def __init__(self, error_message: str = "test") -> None:
        self.calls = 0
        self.error_message = error_message

    async def __call__(
        self,
        *,
        summary_id: str,
        session_key: str,
        content: str,
    ) -> list[ExtractedEntity]:
        self.calls += 1
        raise RuntimeError(self.error_message)


# ---------------------------------------------------------------------------
# get_worker_status_snapshot
# ---------------------------------------------------------------------------


class TestStatusSnapshot:
    """Tests for :func:`get_worker_status_snapshot`."""

    def test_empty_locks_no_pending(self, db: sqlite3.Connection) -> None:
        """No locks, no queue items, no model_name → empty + -1 sentinels.

        Ports TS test "returns empty locks + extraction count = 0 when
        no work pending" at ``operator-worker-orchestrator.test.ts:35``.
        """
        snap = get_worker_status_snapshot(db)
        assert isinstance(snap, WorkerStatusSnapshot)
        # Every kind present, all None.
        from lossless_hermes.concurrency.model import WORKER_JOB_KINDS

        for kind in WORKER_JOB_KINDS:
            assert snap.locks[kind] is None, f"expected {kind!r} idle"
        # Pending counts: extraction = 0, procedure_mining = -1,
        # embedding_backfill = -1 (no model_name).
        assert snap.pending.extraction_queue == 0
        assert snap.pending.procedure_mining == -1
        assert snap.pending.embedding_backfill == -1

    def test_reflects_acquired_lock(self, db: sqlite3.Connection) -> None:
        """Acquiring a lock surfaces in the snapshot.

        Ports TS test "reflects acquired locks in the snapshot" at
        ``operator-worker-orchestrator.test.ts:47``.
        """
        acquired = acquire_lock(db, "extraction", worker_id="w1", job_metadata="test")
        assert acquired is True
        snap = get_worker_status_snapshot(db)
        extraction_lock = snap.locks["extraction"]
        assert isinstance(extraction_lock, WorkerLockSnapshot)
        assert extraction_lock.lock.worker_id == "w1"
        assert extraction_lock.lock.job_metadata == "test"
        # Convenience aliases.
        assert extraction_lock.held is True
        assert extraction_lock.kind == "extraction"
        assert extraction_lock.worker_id == "w1"

    def test_extraction_queue_count(self, db: sqlite3.Connection) -> None:
        """Pending queue items surface in :attr:`pending.extraction_queue`.

        Ports TS test "counts pending extractions when queue has items"
        at ``operator-worker-orchestrator.test.ts:56``.
        """
        _seed_leaf(db, summary_id="leaf_a")
        _seed_extraction_queue(db, queue_id="q1", leaf_id="leaf_a")
        snap = get_worker_status_snapshot(db)
        assert snap.pending.extraction_queue == 1

    def test_workers_property_orders_by_kind(self, db: sqlite3.Connection) -> None:
        """The :attr:`workers` alias iterates by
        :data:`WORKER_JOB_KINDS`.

        Spec AC: ``WorkerStatusSnapshot.workers`` is a list "one entry per
        WORKER_JOB_KINDS".
        """
        from lossless_hermes.concurrency.model import WORKER_JOB_KINDS

        acquired = acquire_lock(db, "embedding-backfill", worker_id="wB", job_metadata="m1")
        assert acquired is True
        snap = get_worker_status_snapshot(db)
        workers_list = snap.workers
        assert len(workers_list) == len(WORKER_JOB_KINDS)
        # Each entry corresponds to the same-position kind.
        for i, kind in enumerate(WORKER_JOB_KINDS):
            assert workers_list[i] is snap.locks[kind]


# ---------------------------------------------------------------------------
# Stale detection
# ---------------------------------------------------------------------------


class TestStaleDetection:
    """Tests for the staleness flag on :class:`WorkerLockSnapshot`."""

    def test_fresh_lock_not_stale(self, db: sqlite3.Connection) -> None:
        """A just-acquired lock has a fresh heartbeat → not stale."""
        acquire_lock(db, "extraction", worker_id="w1")
        snap = get_worker_status_snapshot(db)
        lock_snap = snap.locks["extraction"]
        assert lock_snap is not None
        assert lock_snap.is_stale is False

    def test_stale_detection_flags_old_heartbeat(self, db: sqlite3.Connection) -> None:
        """A lock with ``last_heartbeat_at`` older than TTL is flagged.

        Spec AC: "Stale detection: a lock with last_heartbeat_at older
        than ttl is flagged stale=True."

        We force staleness by passing a tiny ``ttl_s`` to the snapshot
        — the lock's heartbeat is "now", so any TTL ≤ 0 means
        ``last_heartbeat_at < (now - ttl)``.
        """
        acquire_lock(db, "extraction", worker_id="w1")
        # Use a NEGATIVE ttl so the threshold falls in the future of
        # the heartbeat → guaranteed-stale (the implementation uses
        # ``last_heartbeat_at < threshold_iso``; a negative ttl pushes
        # threshold past now, so any heartbeat at-or-before now is
        # strictly less than it).
        snap = get_worker_status_snapshot(db, ttl_s=-1.0)
        lock_snap = snap.locks["extraction"]
        assert lock_snap is not None
        assert lock_snap.is_stale is True


# ---------------------------------------------------------------------------
# tick_extraction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestTickExtraction:
    """Tests for :func:`tick_extraction`."""

    async def test_happy_path(self, db: sqlite3.Connection) -> None:
        """Acquires lock, runs extraction, releases lock.

        Ports TS test "acquires lock, runs extraction, releases lock —
        happy path" at ``operator-worker-orchestrator.test.ts:72``.
        """
        _seed_leaf(db, summary_id="leaf_a", content="hello world", token_count=2)
        _seed_extraction_queue(db, queue_id="q1", leaf_id="leaf_a")
        extractor = _CountingExtractor()

        result = await tick_extraction(db, extractor)
        assert isinstance(result, ExtractionTickResultWithLock)
        assert result.lock_acquired is True
        assert result.processed_count == 1
        # Lock released after tick.
        assert lock_info(db, "extraction") is None

    async def test_skipped_when_lock_held_by_peer(self, db: sqlite3.Connection) -> None:
        """Second tick with peer holding lock returns lock_acquired=False.

        Per AC: "test_tick_skipped_when_lock_held_by_peer — second tick
        with acquire_lock returning None returns early."

        Ports TS test "returns lockAcquired=false + zeros when another
        worker holds the lock" at
        ``operator-worker-orchestrator.test.ts:94``.
        """
        # Peer worker holds the extraction lock.
        acquired = acquire_lock(db, "extraction", worker_id="other-worker")
        assert acquired is True
        extractor = _CountingExtractor()

        result = await tick_extraction(db, extractor)
        assert result.lock_acquired is False
        # Extractor was never invoked.
        assert extractor.calls == []
        assert result.processed_count == 0
        # Peer's lock is still held.
        peer = lock_info(db, "extraction")
        assert peer is not None
        assert peer.worker_id == "other-worker"

    async def test_releases_lock_even_on_extractor_throw(self, db: sqlite3.Connection) -> None:
        """Lock is released after extractor raises.

        Ports TS test "releases lock even if extractor throws on first
        item" at ``operator-worker-orchestrator.test.ts:111``.
        """
        _seed_leaf(db, summary_id="leaf_a", content="hello", token_count=1)
        _seed_extraction_queue(db, queue_id="q1", leaf_id="leaf_a")
        extractor = _ThrowingExtractor("boom")

        result = await tick_extraction(db, extractor)
        # Extractor raised — but the run_coreference_tick absorbs the
        # error and reports it via per_item; the orchestrator returns
        # lock_acquired=True because the lock didn't lapse.
        assert result.lock_acquired is True
        # Lock released after tick.
        assert lock_info(db, "extraction") is None

    async def test_respects_pass_id_override(self, db: sqlite3.Connection) -> None:
        """:attr:`ExtractionTickArgs.pass_id` is forwarded to the tick."""
        _seed_leaf(db, summary_id="leaf_a")
        _seed_extraction_queue(db, queue_id="q1", leaf_id="leaf_a")
        extractor = _CountingExtractor()
        await tick_extraction(db, extractor, ExtractionTickArgs(pass_id="custom-pass-id"))
        # The pass_id is opaque to the orchestrator; verifying it
        # reached the tick layer means checking that the call succeeded
        # — pass_id is consumed by the queue-row UPDATE inside
        # ``run_coreference_tick``.
        assert lock_info(db, "extraction") is None  # released

    async def test_no_extraction_pending_returns_idle(self, db: sqlite3.Connection) -> None:
        """No queue items → tick runs but processes 0 items."""
        extractor = _CountingExtractor()
        result = await tick_extraction(db, extractor)
        assert result.lock_acquired is True
        assert result.processed_count == 0
        assert extractor.calls == []
        assert lock_info(db, "extraction") is None  # released


# ---------------------------------------------------------------------------
# force_release_lock
# ---------------------------------------------------------------------------


class TestForceReleaseLock:
    """Tests for :func:`force_release_lock`."""

    def test_returns_true_then_false(self, db: sqlite3.Connection) -> None:
        """First call returns ``True`` + ``"released"``; subsequent returns
        ``False`` + ``"no_lock_held"``.

        Ports TS test "returns true when lock existed; subsequent call
        returns false" at ``operator-worker-orchestrator.test.ts:138``.
        """
        acquire_lock(db, "embedding-backfill", worker_id="stuck-worker")
        first = force_release_lock(db, "embedding-backfill")
        assert isinstance(first, ForceReleaseResult)
        assert first.released is True
        assert first.reason == "released"

        second = force_release_lock(db, "embedding-backfill")
        assert second.released is False
        assert second.reason == "no_lock_held"

        # Lock truly gone.
        snap = get_worker_status_snapshot(db)
        assert snap.locks["embedding-backfill"] is None

    def test_force_release_host_guard(self, db: sqlite3.Connection) -> None:
        """Guard prevents releasing a lock held by a different worker.

        Per AC: ``force_release_lock(kind, host="other-host")`` doesn't
        release a lock held by ``"this-host"``.

        We use ``expected_worker_id`` (the implementation's name for the
        spec's "host" parameter; see the function's docstring "Note on
        the spec's host naming" for the alias rationale).
        """
        acquire_lock(db, "extraction", worker_id="this-host")

        # Caller asks to release "other-host"'s lock — must not match.
        result = force_release_lock(db, "extraction", expected_worker_id="other-host")
        assert result.released is False
        assert "guard_mismatch" in result.reason
        # Original holder still has the lock.
        info = lock_info(db, "extraction")
        assert info is not None
        assert info.worker_id == "this-host"

    def test_guard_match_releases(self, db: sqlite3.Connection) -> None:
        """Guard with matching ``expected_worker_id`` does release."""
        acquire_lock(db, "extraction", worker_id="this-host")
        result = force_release_lock(db, "extraction", expected_worker_id="this-host")
        assert result.released is True
        assert result.reason == "released"
        assert lock_info(db, "extraction") is None

    def test_guard_no_lock_held(self, db: sqlite3.Connection) -> None:
        """Guard against absent lock → ``"no_lock_held"`` reason."""
        result = force_release_lock(db, "extraction", expected_worker_id="anyone")
        assert result.released is False
        assert result.reason == "no_lock_held"


# ---------------------------------------------------------------------------
# heartbeat_all_held_locks
# ---------------------------------------------------------------------------


class TestHeartbeatAllHeldLocks:
    """Tests for :func:`heartbeat_all_held_locks`."""

    def test_refreshes_only_matching_worker_ids(self, db: sqlite3.Connection) -> None:
        """Matching worker_id → ``"ok"``; mismatched → ``"lost"``.

        Ports TS test "refreshes only locks whose worker_id matches the
        supplied map (per-kind status surfaced)" at
        ``operator-worker-orchestrator.test.ts:149``.
        """
        acquire_lock(db, "embedding-backfill", worker_id="wA")
        acquire_lock(db, "extraction", worker_id="wB")

        result = heartbeat_all_held_locks(
            db,
            {
                "embedding-backfill": "wA",
                "extraction": "wRONG",  # mismatched id — should NOT refresh
            },
        )
        assert isinstance(result, HeartbeatResult)
        assert result.refreshed == 1
        assert result.per_kind["embedding-backfill"] == "ok"
        assert result.per_kind["extraction"] == "lost"

    def test_empty_input_marks_all_skipped(self, db: sqlite3.Connection) -> None:
        """No worker_ids supplied → every kind marked ``"skipped"``.

        Ports TS test "returns refreshed=0 + skipped per-kind when no
        workerIds supplied" at
        ``operator-worker-orchestrator.test.ts:165``.
        """
        from lossless_hermes.concurrency.model import WORKER_JOB_KINDS

        result = heartbeat_all_held_locks(db, {})
        assert result.refreshed == 0
        # Every kind reports "skipped".
        for kind in WORKER_JOB_KINDS:
            assert result.per_kind[kind] == "skipped", f"expected {kind!r} skipped"

    def test_partial_input(self, db: sqlite3.Connection) -> None:
        """Kinds not in the input map are marked ``"skipped"``."""
        from lossless_hermes.concurrency.model import WORKER_JOB_KINDS

        acquire_lock(db, "extraction", worker_id="wX")
        result = heartbeat_all_held_locks(db, {"extraction": "wX"})
        assert result.refreshed == 1
        assert result.per_kind["extraction"] == "ok"
        # Every other kind: skipped (no worker_id supplied).
        for kind in WORKER_JOB_KINDS:
            if kind == "extraction":
                continue
            assert result.per_kind[kind] == "skipped"


# ---------------------------------------------------------------------------
# tick_embedding_backfill — uses skip_lock + mock Voyage
# ---------------------------------------------------------------------------


class _MockVoyage:
    """Minimal mock of :class:`VoyageClient` for backfill tests.

    Returns deterministic 4-dim vectors for every input. The
    :class:`tick_embedding_backfill` path requires both ``embed`` and
    the underlying meta-table writes to succeed; we use ``skip_lock`` to
    bypass the lock so tests can run without coordinating across the
    full worker stack.
    """

    def __init__(self) -> None:
        self.call_count = 0
        self.total_inputs = 0

    async def embed(
        self,
        texts: list[str],
        *,
        model: str,
        input_type: str | None = None,
        output_dimension: int | None = None,
    ) -> "_MockEmbedResult":
        self.call_count += 1
        self.total_inputs += len(texts)
        dim = output_dimension or 4
        # Deterministic vector: i-th input gets ``[float(i), 0, 0, 0, ...]``.
        vectors = [[float(i + 1)] + [0.0] * (dim - 1) for i in range(len(texts))]
        return _MockEmbedResult(vectors=vectors, total_tokens=len(texts))


class _MockEmbedResult:
    def __init__(self, vectors: list[list[float]], total_tokens: int) -> None:
        self.vectors = vectors
        self.total_tokens = total_tokens


def _vec0_loadable() -> bool:
    """Probe whether sqlite_vec can load on this Python build."""
    if not hasattr(sqlite3.Connection, "enable_load_extension"):
        return False
    try:
        import sqlite_vec  # noqa: F401
    except ImportError:
        return False
    try:
        conn = sqlite3.connect(":memory:")
        conn.enable_load_extension(True)
        import sqlite_vec as _sv

        _sv.load(conn)
        conn.close()
        return True
    except Exception:  # noqa: BLE001
        return False


skip_if_no_vec0 = pytest.mark.skipif(
    not _vec0_loadable(), reason="sqlite-vec extension not loadable"
)


@skip_if_no_vec0
@pytest.mark.asyncio
async def test_backfill_tick_processes_200() -> None:
    """250 unembedded leaves; single tick processes exactly 200.

    Per AC: "test_backfill_tick_processes_200 — 250 unembedded leaves,
    single tick processes exactly 200."

    Verifies the orchestrator's :func:`tick_embedding_backfill` wrapper
    forwards the default :attr:`per_tick_limit=200` from
    :mod:`lossless_hermes.embeddings.backfill` (plugin-glue.md line 430
    contract).
    """
    import sqlite_vec

    from lossless_hermes.embeddings.store import (
        ensure_embeddings_table,
        register_embedding_profile,
    )
    from lossless_hermes.operator.worker_orchestrator import tick_embedding_backfill

    # Mirror tests/embeddings/test_backfill._setup_db: isolation_level=None
    # so individual INSERTs auto-commit (the backfill tick's BEGIN IMMEDIATE
    # + §0 assert_no_open_tx contract requires this).
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    try:
        run_lcm_migrations(conn, fts5_available=False, seed_default_prompts=False)
        # Seed a conversation + embedding profile + per-model vec0 table.
        conn.execute(
            "INSERT INTO conversations (conversation_id, session_id, session_key, active) "
            "VALUES (1, 's1', 'sk1', 1)"
        )
        register_embedding_profile(conn, model_name="voyage-3", dim=4)
        ensure_embeddings_table(conn, model_name="voyage-3", dim=4)

        # Seed 250 unembedded leaves.
        for i in range(250):
            conn.execute(
                """
                INSERT INTO summaries
                    (summary_id, conversation_id, kind, content, token_count,
                     source_message_token_count, descendant_token_count, session_key)
                VALUES (?, 1, 'leaf', ?, ?, 0, 0, 'sk1')
                """,
                (f"leaf_{i:03d}", f"content {i}", 10),
            )

        voyage = _MockVoyage()
        # Use skip_lock=True so the test doesn't have to coordinate the
        # cross-process lock dance. The orchestrator forwards skip_lock
        # to the inner tick via **kwargs.
        result = await tick_embedding_backfill(
            conn,
            model_name="voyage-3",
            voyage_model="voyage-3",
            voyage=voyage,
            voyage_output_dimension=4,
            max_requests_per_second=0,  # disable rate-limit pacing in tests
            skip_lock=True,
        )

        # The plugin-glue.md line 430 contract: exactly 200 embedded per
        # call. Verify both the returned count and the Voyage call's
        # total-inputs counter (sum across batches in this tick).
        assert result.embedded_count == 200, f"expected 200 embedded, got {result.embedded_count}"
        assert voyage.total_inputs == 200, (
            f"expected Voyage to receive 200 inputs, got {voyage.total_inputs}"
        )
        # The tick hit the per_tick_limit gate → caller should re-schedule.
        assert result.per_tick_limit_reached is True
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# create_worker_llm_call
# ---------------------------------------------------------------------------


class _FakeCompleteResult:
    """Mock LLM-complete result with an ``output`` attribute."""

    def __init__(self, output: str, actual_model: str | None = None) -> None:
        self.output = output
        if actual_model is not None:
            self.actual_model = actual_model


@pytest.mark.asyncio
class TestCreateWorkerLlmCall:
    """Tests for :func:`create_worker_llm_call`."""

    async def test_returns_llm_call_with_default_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Adapter returns the expected :class:`LlmCallResult` shape.

        Per AC: "create_worker_llm_call(deps) returns a callable matching
        Epic 07-04's LlmCall signature."
        """
        monkeypatch.delenv("LCM_SUMMARY_MODEL", raising=False)

        captured: dict[str, Any] = {}

        async def fake_complete(args: dict[str, Any]) -> _FakeCompleteResult:
            captured.update(args)
            return _FakeCompleteResult(output="hello world")

        config = WorkerLlmConfig(complete=fake_complete)
        llm_call = create_worker_llm_call(config)

        result = await llm_call(
            LlmCallArgs(
                model="gpt-5.4-mini",
                prompt="What is 2+2?",
                pass_kind="single",
                max_output_tokens=256,
            )
        )
        assert isinstance(result, LlmCallResult)
        assert result.output == "hello world"
        assert result.latency_ms >= 0
        assert result.cost_cents is None
        # The adapter forwards the model + prompt to the inner complete.
        assert captured["model"] == "gpt-5.4-mini"
        assert captured["prompt"] == "What is 2+2?"
        assert captured["pass_kind"] == "single"
        assert captured["max_output_tokens"] == 256
        # System prompt is always included.
        assert "system" in captured

    async def test_falls_back_to_config_default_model(self) -> None:
        """Empty :attr:`LlmCallArgs.model` → falls back to config default."""

        async def fake_complete(args: dict[str, Any]) -> _FakeCompleteResult:
            return _FakeCompleteResult(output="ok", actual_model=args["model"])

        config = WorkerLlmConfig(complete=fake_complete, default_model="my-default")
        llm_call = create_worker_llm_call(config)
        result = await llm_call(
            LlmCallArgs(
                model="",  # empty — fall back to config default
                prompt="X",
                pass_kind="single",
            )
        )
        assert result.actual_model == "my-default"

    async def test_falls_back_to_env_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty model AND empty config default → env fallback."""
        monkeypatch.setenv("LCM_SUMMARY_MODEL", "env-chosen-model")

        async def fake_complete(args: dict[str, Any]) -> _FakeCompleteResult:
            return _FakeCompleteResult(output="ok", actual_model=args["model"])

        config = WorkerLlmConfig(complete=fake_complete)
        llm_call = create_worker_llm_call(config)
        result = await llm_call(LlmCallArgs(model="", prompt="X", pass_kind="single"))
        assert result.actual_model == "env-chosen-model"

    async def test_timeout(self) -> None:
        """A stuck inner ``complete`` is cancelled after ``timeout_s``."""

        async def slow_complete(args: dict[str, Any]) -> _FakeCompleteResult:
            await asyncio.sleep(10.0)
            return _FakeCompleteResult(output="never")

        config = WorkerLlmConfig(complete=slow_complete, timeout_s=0.05)
        llm_call = create_worker_llm_call(config)
        with pytest.raises(RuntimeError) as excinfo:
            await llm_call(LlmCallArgs(model="m", prompt="X", pass_kind="single"))
        assert "timeout" in str(excinfo.value).lower()

    async def test_extracts_text_from_dict_response(self) -> None:
        """Adapter tolerantly extracts text from dict-shaped responses."""

        async def fake_complete(args: dict[str, Any]) -> dict[str, Any]:
            return {"output": "from-dict", "model": "vendor-actual-model"}

        config = WorkerLlmConfig(complete=fake_complete)
        llm_call = create_worker_llm_call(config)
        result = await llm_call(LlmCallArgs(model="m", prompt="X", pass_kind="single"))
        assert result.output == "from-dict"
        assert result.actual_model == "vendor-actual-model"

    async def test_extracts_text_from_text_attribute(self) -> None:
        """Adapter falls back to ``.text`` attribute when ``.output``
        is absent."""

        class _TextOnlyResult:
            text = "from-text-attr"

        async def fake_complete(args: dict[str, Any]) -> _TextOnlyResult:
            return _TextOnlyResult()

        config = WorkerLlmConfig(complete=fake_complete)
        llm_call = create_worker_llm_call(config)
        result = await llm_call(LlmCallArgs(model="m", prompt="X", pass_kind="single"))
        assert result.output == "from-text-attr"

    async def test_raises_when_no_text_field(self) -> None:
        """A response with no recognized text field raises."""

        async def fake_complete(args: dict[str, Any]) -> dict[str, Any]:
            return {"unexpected_field": "nope"}

        config = WorkerLlmConfig(complete=fake_complete)
        llm_call = create_worker_llm_call(config)
        with pytest.raises(RuntimeError) as excinfo:
            await llm_call(LlmCallArgs(model="m", prompt="X", pass_kind="single"))
        assert "no text content" in str(excinfo.value).lower()

    async def test_judge_pass_kind_uses_low_reasoning(self) -> None:
        """``pass_kind='best_of_n_judge'`` → ``reasoning_if_supported='low'``."""
        captured: dict[str, Any] = {}

        async def fake_complete(args: dict[str, Any]) -> _FakeCompleteResult:
            captured.update(args)
            return _FakeCompleteResult(output="judged")

        config = WorkerLlmConfig(complete=fake_complete)
        llm_call = create_worker_llm_call(config)
        await llm_call(LlmCallArgs(model="m", prompt="X", pass_kind="best_of_n_judge"))
        assert captured["reasoning_if_supported"] == "low"

    async def test_default_timeout_constant(self) -> None:
        """:data:`DEFAULT_WORKER_LLM_TIMEOUT_S` matches the TS spec."""
        assert DEFAULT_WORKER_LLM_TIMEOUT_S == 60.0


# ---------------------------------------------------------------------------
# Snapshot integration — ensure pending counts surface the embedding probe
# ---------------------------------------------------------------------------


def test_status_snapshot_with_model_name_resolves_embedding_pending(
    db: sqlite3.Connection,
) -> None:
    """Passing ``model_name`` resolves the pending-embedding count.

    Per the TS source contract ``operator-worker-orchestrator.test.ts:43``:
    "expect(snap.pending.embeddingBackfill).toBe(-1); // no modelName
    given." We verify the inverse: WITH a model_name, the count is the
    real per-model pending count (here 0 — no leaves, no profile, but
    the function must not raise).

    We use a model_name for which the underlying table query just
    returns 0 (no leaves). The point is to assert the sentinel is
    bypassed.
    """
    snap = get_worker_status_snapshot(db, model_name="voyage-3")
    # No leaves seeded → pending count is 0 (not -1).
    assert snap.pending.embedding_backfill == 0
