"""Tests for :mod:`lossless_hermes.embeddings.backfill` (issue 05-07).

Ports ``lossless-claw/test/embeddings-backfill.test.ts`` (474 LOC) to
Python. Voyage HTTP is mocked end-to-end via ``respx`` (the same approach
as :mod:`tests.voyage.test_client`). No live API calls.

vec0-dependent — the suite is gated on the extension being loadable on
this Python build via :data:`skip_if_no_vec0`. The TS suite uses the
``LCM_TEST_VEC0_PATH`` env var; the Python port relies on the PyPI
``sqlite_vec`` wheel that ships the extension binary.

Test inventory (covers all the cases listed in the issue spec
``epics/05-embeddings/05-07-backfill-cron.md``):

* **Basic tick (single-flight passes; no lock contention):**
  - embeds all pending leaves; result count matches; ``is_embedded`` true after.
  - skips suppressed leaves (no Voyage call).
  - skips already-embedded leaves on subsequent ticks (idempotent).
  - over-cap leaves are filtered + reported.
  - ``per_tick_limit`` caps work and returns ``per_tick_limit_reached``.
* **Error handling:**
  - Voyage 400 → ``voyage_400`` skipped; tick continues.
  - Voyage 401 → ``VoyageError(auth)`` re-thrown; lock released via finally.
  - Voyage 500 first batch → marks skipped; remaining batches succeed.
* **Single-flight via worker lock:**
  - Peer holds lock → ``lock_not_acquired=True``; no Voyage calls.
  - Releases lock on success.
  - Releases lock on auth re-throw.
* **Batching (token budget):**
  - ``_pack_batches`` respects ``max_batch_tokens``.
  - ``count_pending_docs`` accurate before / after a tick.
* **Wave-12 fix (load-bearing):**
  - Lock stolen mid-embed → writes aborted; docs marked
    ``lock_stolen_mid_embed``.
* **Per-row SAVEPOINT (Wave-1 Auditor #2 finding #4):**
  - One bad doc in a batch of 3 → other 2 commit; bad rolls back.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx

from lossless_hermes.concurrency.worker_lock import lock_info
from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.embeddings.backfill import (
    BackfillResult,
    BackfillSkippedDoc,
    count_pending_docs,
    tick_embedding_backfill,
)
from lossless_hermes.embeddings.store import (
    ensure_embeddings_table,
    is_embedded,
    register_embedding_profile,
)
from lossless_hermes.voyage import VoyageClient, VoyageError


# ---------------------------------------------------------------------------
# vec0 availability probe — same as test_store.py.
# ---------------------------------------------------------------------------


def _vec0_loadable() -> bool:
    """Return :data:`True` iff ``sqlite_vec.load`` succeeds on this Python.

    Mirrors :func:`tests.embeddings.test_store._vec0_loadable`. The probe
    runs once at module import so the skip decorator has a value to
    consume before any test runs.
    """
    if not hasattr(sqlite3.Connection, "enable_load_extension"):
        return False
    try:
        import sqlite_vec  # local import to keep top-level cheap

        conn = sqlite3.connect(":memory:")
        try:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            return True
        finally:
            conn.close()
    except (AttributeError, sqlite3.OperationalError):
        return False


VEC0_AVAILABLE: bool = _vec0_loadable()


skip_if_no_vec0 = pytest.mark.skipif(
    not VEC0_AVAILABLE,
    reason=(
        "sqlite-vec extension not loadable on this Python build "
        "(actions/setup-python on macOS often disables loadable "
        "extensions). Vec0-dependent tests skip cleanly."
    ),
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _setup_db() -> sqlite3.Connection:
    """Open a fresh in-memory DB with vec0 + the v4.1 migrations + a profile.

    Mirrors the TS ``setupDb`` helper from
    ``embeddings-backfill.test.ts:40-48``. Each test gets its own DB so
    we never share state across tests.
    """
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    if VEC0_AVAILABLE:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    run_lcm_migrations(conn, fts5_available=False)
    # Seed a conversation row so the summary FK is satisfied.
    conn.execute(
        "INSERT OR IGNORE INTO conversations (conversation_id, session_id, session_key) "
        "VALUES (1, 's1', 'sk1')"
    )
    register_embedding_profile(conn, "voyage-4-large", 3)
    ensure_embeddings_table(conn, "voyage-4-large", 3)
    return conn


def _insert_leaf(
    conn: sqlite3.Connection,
    summary_id: str,
    token_count: int,
    content: str = "x",
) -> None:
    """Insert one ``kind='leaf'`` summary row matching the TS helper.

    Mirrors ``embeddings-backfill.test.ts:50-57`` ``insertLeaf``. The TS
    INSERT also sets ``session_key='sk1'`` because the migration adds the
    NOT NULL column with default ``''`` — we follow suit.
    """
    conn.execute(
        "INSERT INTO summaries "
        "  (summary_id, conversation_id, kind, content, token_count, session_key) "
        "VALUES (?, 1, 'leaf', ?, ?, 'sk1')",
        (summary_id, content, token_count),
    )


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    """In-memory SQLite with vec0 + v4.1 migrations + profile + conv."""
    conn = _setup_db()
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def respx_router() -> Iterator[respx.MockRouter]:
    """Respx router for the Voyage host.

    Mirrors the fixture in :mod:`tests.voyage.test_client`. We set
    ``assert_all_called=False`` because tests sometimes mock responses
    that aren't all called (e.g. early-exit on lock contention).
    """
    with respx.mock(base_url="https://api.voyageai.com", assert_all_called=False) as router:
        yield router


@pytest.fixture
def voyage_client() -> Iterator[VoyageClient]:
    """A :class:`VoyageClient` whose underlying httpx is intercepted by respx."""
    client = VoyageClient(
        api_key="test-key",
        base_url="https://api.voyageai.com/v1",
        max_retries=1,
        timeout_s=10.0,
    )
    try:
        yield client
    finally:
        asyncio.get_event_loop().run_until_complete(
            client.aclose()
        ) if asyncio.get_event_loop().is_running() else asyncio.run(client.aclose())


@pytest.fixture
async def voyage_client_async() -> Any:
    """Async fixture version — yields a :class:`VoyageClient` + closes after."""
    client = VoyageClient(
        api_key="test-key",
        base_url="https://api.voyageai.com/v1",
        max_retries=0,
        timeout_s=10.0,
    )
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace :func:`asyncio.sleep` with a no-op recorder.

    Backfill paces requests at ``1/max_requests_per_second`` between
    batches. Tests pass ``max_requests_per_second=1000`` to make the
    pacing irrelevant; but the Voyage client also calls
    :func:`asyncio.sleep` on retry, so we patch globally to keep all
    tests fast. Returns the accumulated sleep durations for assertions
    if needed.
    """
    sleeps: list[float] = []

    real_sleep = asyncio.sleep

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)
        # Yield to the event loop once so respx + httpx scheduling still
        # behaves naturally — a fully no-op sleep can starve the loop on
        # tight retry paths.
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return sleeps


def _embed_response(
    inputs: list[str], dim: int = 3, total_tokens: int | None = None
) -> dict[str, Any]:
    """Build a stub Voyage embed response shape.

    Vectors are deterministic per-input-index (so tests can assert
    distance ordering if needed) and use the right dim so the vec0
    INSERT doesn't fail.
    """
    return {
        "data": [
            {
                "embedding": [0.1 * (i + 1) % 1.0 for _ in range(dim)],
                "index": i,
                "object": "embedding",
            }
            for i, _ in enumerate(inputs)
        ],
        "model": "voyage-4-large",
        "usage": {"total_tokens": total_tokens if total_tokens is not None else 50 * len(inputs)},
    }


# ===========================================================================
# Basic tick — single-flight passes; no lock contention.
# ===========================================================================


@skip_if_no_vec0
class TestBasicTick:
    """Mirrors ``embeddings-backfill.test.ts:59-245``."""

    async def test_embeds_all_pending_leaves(
        self,
        db: sqlite3.Connection,
        respx_router: respx.MockRouter,
    ) -> None:
        _insert_leaf(db, "leaf_a", 100, "alpha")
        _insert_leaf(db, "leaf_b", 200, "beta")
        _insert_leaf(db, "leaf_c", 300, "gamma")

        recorded_inputs: list[list[str]] = []

        def respond(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            recorded_inputs.append(list(body["input"]))
            return httpx.Response(200, json=_embed_response(body["input"]))

        respx_router.post("/v1/embeddings").mock(side_effect=respond)

        client = VoyageClient(api_key="test", base_url="https://api.voyageai.com/v1", max_retries=0)
        try:
            result = await tick_embedding_backfill(
                db,
                model_name="voyage-4-large",
                voyage_model="voyage-4-large",
                voyage=client,
                input_type="document",
                max_requests_per_second=1000.0,
                per_tick_limit=10,
            )
        finally:
            await client.aclose()

        assert result.embedded_count == 3
        assert result.skipped_over_cap == 0
        assert result.skipped == []
        assert result.lock_not_acquired is False
        assert result.per_tick_limit_reached is False
        assert result.voyage_tokens_consumed > 0

        for sid in ("leaf_a", "leaf_b", "leaf_c"):
            assert (
                is_embedded(
                    db,
                    embedded_id=sid,
                    embedded_kind="summary",
                    model_name="voyage-4-large",
                )
                is True
            )

    async def test_skips_suppressed_leaves(
        self,
        db: sqlite3.Connection,
        respx_router: respx.MockRouter,
    ) -> None:
        _insert_leaf(db, "leaf_a", 100)
        _insert_leaf(db, "leaf_suppressed", 100)
        db.execute(
            "UPDATE summaries SET suppressed_at = ? WHERE summary_id = ?",
            ("2026-05-05", "leaf_suppressed"),
        )

        observed_inputs: list[str] = []

        def respond(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            observed_inputs.extend(body["input"])
            return httpx.Response(200, json=_embed_response(body["input"]))

        respx_router.post("/v1/embeddings").mock(side_effect=respond)

        client = VoyageClient(api_key="k", base_url="https://api.voyageai.com/v1", max_retries=0)
        try:
            result = await tick_embedding_backfill(
                db,
                model_name="voyage-4-large",
                voyage_model="voyage-4-large",
                voyage=client,
                max_requests_per_second=1000.0,
            )
        finally:
            await client.aclose()

        assert result.embedded_count == 1
        assert (
            is_embedded(
                db,
                embedded_id="leaf_a",
                embedded_kind="summary",
                model_name="voyage-4-large",
            )
            is True
        )
        assert (
            is_embedded(
                db,
                embedded_id="leaf_suppressed",
                embedded_kind="summary",
                model_name="voyage-4-large",
            )
            is False
        )
        # Defense in depth — the suppressed leaf's content must never
        # appear in any Voyage call body.
        assert "x" in observed_inputs  # leaf_a content is the default "x"
        # The suppressed leaf has the same default content, but it must
        # only appear once (leaf_a's body), not twice.
        assert observed_inputs.count("x") == 1

    async def test_idempotent_across_subsequent_ticks(
        self,
        db: sqlite3.Connection,
        respx_router: respx.MockRouter,
    ) -> None:
        _insert_leaf(db, "leaf_a", 100)
        _insert_leaf(db, "leaf_b", 100)

        call_count = 0

        def respond(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            body = json.loads(request.content)
            return httpx.Response(200, json=_embed_response(body["input"]))

        respx_router.post("/v1/embeddings").mock(side_effect=respond)

        client = VoyageClient(api_key="k", base_url="https://api.voyageai.com/v1", max_retries=0)
        try:
            r1 = await tick_embedding_backfill(
                db,
                model_name="voyage-4-large",
                voyage_model="voyage-4-large",
                voyage=client,
                max_requests_per_second=1000.0,
            )
            assert r1.embedded_count == 2

            calls_after_first = call_count
            r2 = await tick_embedding_backfill(
                db,
                model_name="voyage-4-large",
                voyage_model="voyage-4-large",
                voyage=client,
                max_requests_per_second=1000.0,
            )
            assert r2.embedded_count == 0
            assert r2.voyage_tokens_consumed == 0
            assert call_count == calls_after_first  # no new HTTP calls
        finally:
            await client.aclose()

    async def test_over_cap_leaves_filtered_at_select_level(
        self,
        db: sqlite3.Connection,
        respx_router: respx.MockRouter,
    ) -> None:
        _insert_leaf(db, "leaf_normal", 1_000)
        _insert_leaf(db, "leaf_over", 50_000)  # way over 27K cap

        def respond(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            return httpx.Response(200, json=_embed_response(body["input"]))

        respx_router.post("/v1/embeddings").mock(side_effect=respond)

        client = VoyageClient(api_key="k", base_url="https://api.voyageai.com/v1", max_retries=0)
        try:
            result = await tick_embedding_backfill(
                db,
                model_name="voyage-4-large",
                voyage_model="voyage-4-large",
                voyage=client,
                max_requests_per_second=1000.0,
            )
        finally:
            await client.aclose()

        # SELECT-level BETWEEN filter excludes leaf_over — embedded_count=1.
        assert result.embedded_count == 1
        # The over-cap doc isn't in skipped_over_cap because SELECT
        # filtered it out before the tick saw it. count_pending_docs with
        # the cap bypassed shows it's still pending.
        still_pending = count_pending_docs(
            db,
            model_name="voyage-4-large",
            max_token_count=1_000_000,
        )
        assert still_pending == 1

    async def test_per_tick_limit_caps_work(
        self,
        db: sqlite3.Connection,
        respx_router: respx.MockRouter,
    ) -> None:
        for i in range(10):
            _insert_leaf(db, f"leaf_{i}", 100, f"distinct_{i}")

        def respond(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            return httpx.Response(200, json=_embed_response(body["input"]))

        respx_router.post("/v1/embeddings").mock(side_effect=respond)

        client = VoyageClient(api_key="k", base_url="https://api.voyageai.com/v1", max_retries=0)
        try:
            result = await tick_embedding_backfill(
                db,
                model_name="voyage-4-large",
                voyage_model="voyage-4-large",
                voyage=client,
                max_requests_per_second=1000.0,
                per_tick_limit=5,
            )
        finally:
            await client.aclose()

        assert result.embedded_count == 5
        assert result.per_tick_limit_reached is True


# ===========================================================================
# Error handling — Voyage 400 / 401 / 500.
# ===========================================================================


@skip_if_no_vec0
class TestErrorHandling:
    """Mirrors ``embeddings-backfill.test.ts:247-332``."""

    async def test_voyage_400_records_skipped_but_continues(
        self,
        db: sqlite3.Connection,
        respx_router: respx.MockRouter,
    ) -> None:
        _insert_leaf(db, "leaf_a", 100)

        respx_router.post("/v1/embeddings").mock(
            return_value=httpx.Response(400, json={"error": "bad input"})
        )

        client = VoyageClient(api_key="k", base_url="https://api.voyageai.com/v1", max_retries=0)
        try:
            result = await tick_embedding_backfill(
                db,
                model_name="voyage-4-large",
                voyage_model="voyage-4-large",
                voyage=client,
                voyage_max_retries=0,
                max_requests_per_second=1000.0,
            )
        finally:
            await client.aclose()

        assert result.embedded_count == 0
        assert len(result.skipped) == 1
        assert result.skipped[0].reason == "voyage_400"
        assert result.skipped[0].summary_id == "leaf_a"

    async def test_voyage_401_fatal_re_thrown(
        self,
        db: sqlite3.Connection,
        respx_router: respx.MockRouter,
    ) -> None:
        _insert_leaf(db, "leaf_a", 100)

        respx_router.post("/v1/embeddings").mock(
            return_value=httpx.Response(401, json={"error": "bad key"})
        )

        client = VoyageClient(api_key="k", base_url="https://api.voyageai.com/v1", max_retries=0)
        try:
            with pytest.raises(VoyageError) as excinfo:
                await tick_embedding_backfill(
                    db,
                    model_name="voyage-4-large",
                    voyage_model="voyage-4-large",
                    voyage=client,
                    max_requests_per_second=1000.0,
                )
            assert excinfo.value.kind == "auth"
        finally:
            await client.aclose()

    async def test_voyage_500_first_batch_marks_skipped_continues(
        self,
        db: sqlite3.Connection,
        respx_router: respx.MockRouter,
    ) -> None:
        for i in range(6):
            _insert_leaf(db, f"leaf_{i}", 100, f"distinct_{i}")

        # Track first batch's input set. Anything matching it 500's; the
        # rest succeed. Matches TS ``test:301-312``.
        first_key: str | None = None

        def respond(request: httpx.Request) -> httpx.Response:
            nonlocal first_key
            body = json.loads(request.content)
            key = "|".join(body["input"])
            if first_key is None:
                first_key = key
            if key == first_key:
                return httpx.Response(500, json={"error": "internal"})
            return httpx.Response(200, json=_embed_response(body["input"]))

        respx_router.post("/v1/embeddings").mock(side_effect=respond)

        client = VoyageClient(api_key="k", base_url="https://api.voyageai.com/v1", max_retries=0)
        try:
            result = await tick_embedding_backfill(
                db,
                model_name="voyage-4-large",
                voyage_model="voyage-4-large",
                voyage=client,
                voyage_max_retries=0,
                max_requests_per_second=1000.0,
                max_batch_tokens=200,
            )
        finally:
            await client.aclose()

        # 6 leaves total, batches of 2 (200 tokens / 100 each). First
        # batch fails → 2 skipped. Other 2 batches succeed → 4 embedded.
        assert result.embedded_count == 4
        assert len(result.skipped) == 2
        assert all(s.reason == "voyage_other" for s in result.skipped)


# ===========================================================================
# Single-flight via worker lock.
# ===========================================================================


@skip_if_no_vec0
class TestSingleFlight:
    """Mirrors ``embeddings-backfill.test.ts:334-420``."""

    async def test_peer_holds_lock_returns_lock_not_acquired(
        self,
        db: sqlite3.Connection,
        respx_router: respx.MockRouter,
    ) -> None:
        _insert_leaf(db, "leaf_a", 100)

        # Simulate another worker holding the lock.
        db.execute(
            "INSERT INTO lcm_worker_lock (job_kind, worker_id, expires_at) "
            "VALUES ('embedding-backfill', 'other-worker', datetime('now', '+1 hour'))"
        )

        call_count = 0

        def respond(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(200, json={"data": [], "usage": {"total_tokens": 0}})

        respx_router.post("/v1/embeddings").mock(side_effect=respond)

        client = VoyageClient(api_key="k", base_url="https://api.voyageai.com/v1", max_retries=0)
        try:
            result = await tick_embedding_backfill(
                db,
                model_name="voyage-4-large",
                voyage_model="voyage-4-large",
                voyage=client,
                max_requests_per_second=1000.0,
            )
        finally:
            await client.aclose()

        assert result.lock_not_acquired is True
        assert result.embedded_count == 0
        assert call_count == 0  # no Voyage calls

    async def test_releases_lock_on_success(
        self,
        db: sqlite3.Connection,
        respx_router: respx.MockRouter,
    ) -> None:
        _insert_leaf(db, "leaf_a", 100)

        def respond(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            return httpx.Response(200, json=_embed_response(body["input"]))

        respx_router.post("/v1/embeddings").mock(side_effect=respond)

        client = VoyageClient(api_key="k", base_url="https://api.voyageai.com/v1", max_retries=0)
        try:
            await tick_embedding_backfill(
                db,
                model_name="voyage-4-large",
                voyage_model="voyage-4-large",
                voyage=client,
                max_requests_per_second=1000.0,
            )
        finally:
            await client.aclose()

        assert lock_info(db, "embedding-backfill") is None

    async def test_releases_lock_on_auth_re_throw(
        self,
        db: sqlite3.Connection,
        respx_router: respx.MockRouter,
    ) -> None:
        _insert_leaf(db, "leaf_a", 100)

        respx_router.post("/v1/embeddings").mock(
            return_value=httpx.Response(401, json={"error": "bad key"})
        )

        client = VoyageClient(api_key="k", base_url="https://api.voyageai.com/v1", max_retries=0)
        try:
            with pytest.raises(VoyageError) as excinfo:
                await tick_embedding_backfill(
                    db,
                    model_name="voyage-4-large",
                    voyage_model="voyage-4-large",
                    voyage=client,
                    max_requests_per_second=1000.0,
                )
            assert excinfo.value.kind == "auth"
        finally:
            await client.aclose()

        # Lock must still be released so the next tick can run after
        # operator fixes the API key.
        assert lock_info(db, "embedding-backfill") is None


# ===========================================================================
# Batching (token budget).
# ===========================================================================


@skip_if_no_vec0
class TestBatching:
    """Mirrors ``embeddings-backfill.test.ts:422-474``."""

    async def test_pack_batches_respects_max_batch_tokens(
        self,
        db: sqlite3.Connection,
        respx_router: respx.MockRouter,
    ) -> None:
        # Wave-1 Auditor #2 finding #3: MAX_TOKENS_PER_EMBED_DOC dropped
        # 30K → 27K. Use 25K-token leaves so the per-doc filter doesn't
        # drop them before batching.
        _insert_leaf(db, "leaf_a", 25_000, "aa")
        _insert_leaf(db, "leaf_b", 25_000, "bb")
        _insert_leaf(db, "leaf_c", 25_000, "cc")

        seen_batch_sizes: list[int] = []

        def respond(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            seen_batch_sizes.append(len(body["input"]))
            return httpx.Response(
                200,
                json=_embed_response(body["input"], total_tokens=25_000 * len(body["input"])),
            )

        respx_router.post("/v1/embeddings").mock(side_effect=respond)

        client = VoyageClient(api_key="k", base_url="https://api.voyageai.com/v1", max_retries=0)
        try:
            await tick_embedding_backfill(
                db,
                model_name="voyage-4-large",
                voyage_model="voyage-4-large",
                voyage=client,
                max_requests_per_second=1000.0,
                max_batch_tokens=60_000,  # → batches of 2 + 1
            )
        finally:
            await client.aclose()

        # 75K total tokens, 60K limit per batch — at least 2 batches.
        # Bin packing is greedy: 25+25=50 ≤ 60, add 25 → 75 > 60, flush,
        # new batch of 1. Two batches: [2, 1].
        assert seen_batch_sizes == [2, 1]

    def test_count_pending_docs_accurate(self, db: sqlite3.Connection) -> None:
        _insert_leaf(db, "leaf_a", 100)
        _insert_leaf(db, "leaf_b", 100)
        _insert_leaf(db, "leaf_c", 100)
        assert count_pending_docs(db, model_name="voyage-4-large") == 3

        # Simulate one being already embedded
        db.execute(
            "INSERT INTO lcm_embedding_meta (embedded_id, embedded_kind, "
            "embedding_model, source_token_count) "
            "VALUES ('leaf_a', 'summary', 'voyage-4-large', 100)"
        )
        assert count_pending_docs(db, model_name="voyage-4-large") == 2


# ===========================================================================
# Wave-12 fix — post-embed heartbeat re-check (load-bearing).
# ===========================================================================


@skip_if_no_vec0
class TestWave12HeartbeatRecheck:
    """Wave-12 regression test (load-bearing per ADR-029).

    The fix: after a successful Voyage embed call, the worker re-checks
    its lock heartbeat BEFORE writing the vec0 + meta rows. If the lock
    was stolen during the Voyage call (60s Retry-After + 30s timeout =
    up to 90s = lock TTL exactly), writing now would race a peer worker
    that has already started embedding the same docs.

    Without the re-check, both workers would INSERT into vec0 (creating
    duplicate KNN rows) and race ``lcm_embedding_meta`` (one would lose
    via ``INSERT OR REPLACE``). The regression: this test fails if the
    second heartbeat is removed from
    :func:`~lossless_hermes.embeddings.backfill.tick_embedding_backfill`.
    """

    async def test_lock_stolen_mid_embed_aborts_writes(
        self,
        db: sqlite3.Connection,
        respx_router: respx.MockRouter,
    ) -> None:
        _insert_leaf(db, "leaf_a", 100, "alpha")
        _insert_leaf(db, "leaf_b", 200, "beta")

        def respond(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            return httpx.Response(200, json=_embed_response(body["input"]))

        respx_router.post("/v1/embeddings").mock(side_effect=respond)

        # Patch heartbeat_lock so the FIRST call (pre-embed) returns True
        # (lock still ours; proceed with embed), and the SECOND call
        # (post-embed Wave-12 re-check) returns False (lock stolen).
        from lossless_hermes.embeddings import backfill as backfill_mod

        real_heartbeat = backfill_mod.heartbeat_lock
        call_log: list[bool] = []

        def faked_heartbeat(*args: Any, **kwargs: Any) -> bool:
            # First call: pre-embed (return True, proceed with Voyage).
            # Second call: post-embed Wave-12 re-check (return False, abort).
            if not call_log:
                call_log.append(True)
                return real_heartbeat(*args, **kwargs)
            call_log.append(False)
            return False

        client = VoyageClient(api_key="k", base_url="https://api.voyageai.com/v1", max_retries=0)
        try:
            with patch.object(backfill_mod, "heartbeat_lock", side_effect=faked_heartbeat):
                result = await tick_embedding_backfill(
                    db,
                    model_name="voyage-4-large",
                    voyage_model="voyage-4-large",
                    voyage=client,
                    max_requests_per_second=1000.0,
                )
        finally:
            await client.aclose()

        # Writes were aborted: both docs are marked lock_stolen_mid_embed.
        assert result.lock_not_acquired is True
        assert result.embedded_count == 0
        lock_stolen = [s for s in result.skipped if s.reason == "lock_stolen_mid_embed"]
        assert len(lock_stolen) == 2
        # No embeddings were actually persisted.
        for sid in ("leaf_a", "leaf_b"):
            assert (
                is_embedded(
                    db,
                    embedded_id=sid,
                    embedded_kind="summary",
                    model_name="voyage-4-large",
                )
                is False
            )
        # Heartbeat was called at least twice (pre-embed + Wave-12 re-check).
        assert len(call_log) >= 2

    async def test_pre_embed_heartbeat_failure_aborts_before_voyage_call(
        self,
        db: sqlite3.Connection,
        respx_router: respx.MockRouter,
    ) -> None:
        """Companion to Wave-12 — pre-embed heartbeat failure also aborts.

        This isn't the Wave-12 fix proper; it's the original heartbeat
        check at the top of the batch loop (``backfill.ts:320-330``).
        Tested here for completeness so future refactors that fold both
        checks into one don't accidentally remove the pre-embed guard.
        """
        _insert_leaf(db, "leaf_a", 100)

        voyage_calls = 0

        def respond(request: httpx.Request) -> httpx.Response:
            nonlocal voyage_calls
            voyage_calls += 1
            body = json.loads(request.content)
            return httpx.Response(200, json=_embed_response(body["input"]))

        respx_router.post("/v1/embeddings").mock(side_effect=respond)

        from lossless_hermes.embeddings import backfill as backfill_mod

        def faked_heartbeat(*args: Any, **kwargs: Any) -> bool:
            # First heartbeat call returns False — lock stolen pre-embed.
            return False

        client = VoyageClient(api_key="k", base_url="https://api.voyageai.com/v1", max_retries=0)
        try:
            with patch.object(backfill_mod, "heartbeat_lock", side_effect=faked_heartbeat):
                result = await tick_embedding_backfill(
                    db,
                    model_name="voyage-4-large",
                    voyage_model="voyage-4-large",
                    voyage=client,
                    max_requests_per_second=1000.0,
                )
        finally:
            await client.aclose()

        # Pre-embed heartbeat returned False — we never made the Voyage call.
        assert result.lock_not_acquired is True
        assert result.embedded_count == 0
        assert voyage_calls == 0


# ===========================================================================
# Per-row SAVEPOINT — Wave-1 Auditor #2 finding #4 regression.
# ===========================================================================


@skip_if_no_vec0
class TestPerRowSavepoint:
    """One bad doc in a batch of N → other N-1 still commit.

    Wave-1 Auditor #2 finding #4: per-row write failure inside the batch
    tx left a phantom vec0 row (no corresponding meta) when
    :func:`record_embedding` partially succeeded. The fix wraps each row
    in its own SAVEPOINT — a single failure rolls back that row's
    partial writes without killing the whole batch.

    Repro: mock the Voyage response so vector lengths mismatch the
    profile dim for ONE doc in a batch of 3. The store layer's
    :func:`record_embedding` should raise on that row; the SAVEPOINT
    rolls back; the other two land cleanly.
    """

    async def test_one_bad_doc_in_batch_of_three(
        self,
        db: sqlite3.Connection,
        respx_router: respx.MockRouter,
    ) -> None:
        _insert_leaf(db, "leaf_good_1", 100, "g1")
        _insert_leaf(db, "leaf_bad", 100, "bad")
        _insert_leaf(db, "leaf_good_2", 100, "g2")

        def respond(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            # Build vectors per-input index. The doc at index 1 (the
            # alphabetically-middle id, but order may vary) gets the
            # wrong dim — but the SELECT order is DESC, so:
            #   leaf_good_2 (idx 0) — dim 3 (good)
            #   leaf_good_1 (idx 1) — dim 3 (good)
            #   leaf_bad    (idx 2) — dim 4 (wrong)
            # Inspect the input list to find which slot is the bad one.
            data = []
            for i, text in enumerate(body["input"]):
                if "bad" in text:
                    # wrong dim (4 floats instead of 3) — store layer
                    # raises on dim mismatch.
                    emb = [0.1, 0.2, 0.3, 0.4]
                else:
                    emb = [0.1, 0.2, 0.3]
                data.append({"embedding": emb, "index": i, "object": "embedding"})
            return httpx.Response(
                200,
                json={
                    "data": data,
                    "model": "voyage-4-large",
                    "usage": {"total_tokens": 150},
                },
            )

        respx_router.post("/v1/embeddings").mock(side_effect=respond)

        client = VoyageClient(api_key="k", base_url="https://api.voyageai.com/v1", max_retries=0)
        try:
            result = await tick_embedding_backfill(
                db,
                model_name="voyage-4-large",
                voyage_model="voyage-4-large",
                voyage=client,
                max_requests_per_second=1000.0,
            )
        finally:
            await client.aclose()

        # Voyage client rejects mixed-dim arrays at parse time (it raises
        # ``unexpected`` because Voyage shouldn't return different dims per
        # element). The whole batch is then marked voyage_other — every
        # doc skipped. This is the conservative behavior; the per-row
        # SAVEPOINT defends a DIFFERENT failure mode (post-Voyage write
        # error), which we exercise via the dedicated test below.
        # Verify the conservative outcome at least preserves invariants:
        # no doc is embedded, and the tick completes cleanly.
        assert result.embedded_count == 0
        # The skipped list has an entry per doc in the batch.
        assert len(result.skipped) == 3

    def test_per_row_savepoint_isolates_record_embedding_failure(
        self, db: sqlite3.Connection
    ) -> None:
        """Direct unit test of :func:`_write_batch` with one bad row.

        The Voyage-side test above is somewhat noisy because the Voyage
        client rejects mixed-dim responses before the write phase. This
        test exercises :func:`_write_batch` directly with three valid
        vectors + one with the wrong dim, mirroring the post-Voyage
        write-error path.
        """
        _insert_leaf(db, "leaf_good_1", 100, "g1")
        _insert_leaf(db, "leaf_bad", 100, "bad")
        _insert_leaf(db, "leaf_good_2", 100, "g2")

        from lossless_hermes.embeddings.backfill import _PendingDoc, _write_batch

        # Profile dim is 3 (set in _setup_db). Make one vector dim-4 so
        # record_embedding raises on it.
        batch = [
            _PendingDoc(summary_id="leaf_good_1", content="g1", token_count=100),
            _PendingDoc(summary_id="leaf_bad", content="bad", token_count=100),
            _PendingDoc(summary_id="leaf_good_2", content="g2", token_count=100),
        ]
        vectors = [
            [0.1, 0.2, 0.3],
            [0.1, 0.2, 0.3, 0.4],  # wrong dim — raises
            [0.1, 0.2, 0.3],
        ]

        report = _write_batch(
            db,
            model_name="voyage-4-large",
            embedded_kind="summary",
            batch=batch,
            vectors=vectors,
        )

        # The two good rows committed; the bad row rolled back via SAVEPOINT.
        assert report.succeeded == 2
        assert len(report.errors) == 1
        assert report.errors[0].summary_id == "leaf_bad"
        assert report.errors[0].reason == "voyage_other"

        # is_embedded reflects the per-row outcome:
        assert (
            is_embedded(
                db,
                embedded_id="leaf_good_1",
                embedded_kind="summary",
                model_name="voyage-4-large",
            )
            is True
        )
        assert (
            is_embedded(
                db,
                embedded_id="leaf_good_2",
                embedded_kind="summary",
                model_name="voyage-4-large",
            )
            is True
        )
        assert (
            is_embedded(
                db,
                embedded_id="leaf_bad",
                embedded_kind="summary",
                model_name="voyage-4-large",
            )
            is False
        )


# ===========================================================================
# §0 invariant — assert_no_open_tx fires when called inside a write tx.
# ===========================================================================


@skip_if_no_vec0
class TestZeroInvariant:
    """Regression test for the §0 invariant.

    A write transaction must be CLOSED before the Voyage embed call. The
    backfill cron calls :func:`assert_no_open_tx` immediately before
    each Voyage call. If a caller (mis)leaves a transaction open when
    invoking the tick, the assertion fires loudly rather than silently
    violating §0.
    """

    async def test_assert_no_open_tx_fires_when_caller_holds_tx(
        self,
        db: sqlite3.Connection,
        respx_router: respx.MockRouter,
    ) -> None:
        _insert_leaf(db, "leaf_a", 100)

        # Caller leaves a write transaction open before invoking the tick.
        # The default sqlite3.connect uses isolation_level="" but our
        # fixture uses isolation_level=None so BEGIN must be explicit.
        db.execute("BEGIN IMMEDIATE")

        def respond(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            return httpx.Response(200, json=_embed_response(body["input"]))

        respx_router.post("/v1/embeddings").mock(side_effect=respond)

        client = VoyageClient(api_key="k", base_url="https://api.voyageai.com/v1", max_retries=0)
        try:
            with pytest.raises(RuntimeError, match="§0 violation"):
                await tick_embedding_backfill(
                    db,
                    model_name="voyage-4-large",
                    voyage_model="voyage-4-large",
                    voyage=client,
                    max_requests_per_second=1000.0,
                    skip_lock=True,  # bypass lock so we hit the §0 check directly
                )
        finally:
            await client.aclose()
            try:
                db.execute("ROLLBACK")
            except sqlite3.Error:
                pass


# ===========================================================================
# Empty corpus — no-op tick.
# ===========================================================================


@skip_if_no_vec0
class TestEmptyCorpus:
    async def test_empty_corpus_no_op(
        self,
        db: sqlite3.Connection,
        respx_router: respx.MockRouter,
    ) -> None:
        """No pending leaves → no Voyage calls, lock released, count=0."""
        call_count = 0

        def respond(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            body = json.loads(request.content)
            return httpx.Response(200, json=_embed_response(body["input"]))

        respx_router.post("/v1/embeddings").mock(side_effect=respond)

        client = VoyageClient(api_key="k", base_url="https://api.voyageai.com/v1", max_retries=0)
        try:
            result = await tick_embedding_backfill(
                db,
                model_name="voyage-4-large",
                voyage_model="voyage-4-large",
                voyage=client,
                max_requests_per_second=1000.0,
            )
        finally:
            await client.aclose()

        assert call_count == 0
        assert result.embedded_count == 0
        assert result.skipped == []
        assert result.lock_not_acquired is False
        assert result.per_tick_limit_reached is False
        assert result.voyage_tokens_consumed == 0
        # Lock released after the no-op tick.
        assert lock_info(db, "embedding-backfill") is None


# ===========================================================================
# Result dataclass smoke.
# ===========================================================================


def test_backfill_result_default_values() -> None:
    """:class:`BackfillResult` defaults match the spec (`backfill.ts:180-195`)."""
    r = BackfillResult()
    assert r.embedded_count == 0
    assert r.skipped_over_cap == 0
    assert r.skipped == []
    assert r.per_tick_limit_reached is False
    assert r.lock_not_acquired is False
    assert r.voyage_tokens_consumed == 0
    assert r.duration_ms == 0


def test_backfill_skipped_doc_fields() -> None:
    """:class:`BackfillSkippedDoc` shape matches the issue spec."""
    s = BackfillSkippedDoc(summary_id="x", reason="voyage_400", detail="msg")
    assert s.summary_id == "x"
    assert s.reason == "voyage_400"
    assert s.detail == "msg"
    s2 = BackfillSkippedDoc(summary_id="y", reason="over_cap")
    assert s2.detail is None
