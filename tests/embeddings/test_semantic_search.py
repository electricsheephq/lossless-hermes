"""Tests for :mod:`lossless_hermes.embeddings.semantic_search` (issue 05-08).

Ports ``lossless-claw/test/semantic-search.test.ts`` (355 LOC) to Python.

vec0-dependent tests are gated on the extension being loadable on this
Python build via :data:`VEC0_AVAILABLE` / :data:`skip_if_no_vec0` — mirrors
the gating pattern established by ``tests/embeddings/test_store.py``.

Test isolation: each test opens its own ``:memory:`` connection, applies
the v4.1 migration ladder, registers the ``voyage-4-large`` profile at
dim=3 (matches the TS suite — small enough for hand-computed vectors,
exercises the same code paths as production dim=1024).

Voyage is mocked at the ``VoyageClient`` level via a tiny stand-in class
so we can assert ``input_type='query'`` and the Wave-11 ``output_dimension``
plumbing without actually hitting the API.
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Iterator

import pytest

from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.embeddings.semantic_search import (
    COSINE_BAND_HIGH,
    COSINE_BAND_LOW,
    COSINE_BAND_MEDIUM,
    EmbeddingProfile,
    SemanticHit,
    SemanticSearchResult,
    SemanticSearchUnavailableError,
    get_active_embedding_model,
    run_semantic_search,
)
from lossless_hermes.embeddings.store import (
    ensure_embeddings_table,
    record_embedding,
    register_embedding_profile,
)
from lossless_hermes.voyage.client import EmbedResult, VoyageClient, VoyageError


# ---------------------------------------------------------------------------
# vec0 availability probe — gates vec0-dependent tests
# ---------------------------------------------------------------------------


def _vec0_loadable() -> bool:
    """Return :data:`True` iff ``sqlite_vec.load`` succeeds on this Python.

    Mirrors :data:`tests.embeddings.test_store.VEC0_AVAILABLE`.
    """
    if not hasattr(sqlite3.Connection, "enable_load_extension"):
        return False
    try:
        import sqlite_vec

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
        "sqlite-vec extension not loadable on this Python build. "
        "Vec0-dependent semantic-search tests skip cleanly."
    ),
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _open_test_conn(*, load_vec0: bool = False) -> sqlite3.Connection:
    """Open a bare ``:memory:`` connection; load sqlite-vec if requested."""
    conn = sqlite3.connect(":memory:")
    if load_vec0:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    return conn


@pytest.fixture
def conn_no_vec0() -> Iterator[sqlite3.Connection]:
    """In-memory SQLite with the v4.1 migration ladder but NO vec0."""
    conn = _open_test_conn(load_vec0=False)
    try:
        run_lcm_migrations(conn, fts5_available=False)
        yield conn
    finally:
        conn.close()


@pytest.fixture
def conn_with_vec0() -> Iterator[sqlite3.Connection]:
    """In-memory SQLite with vec0 + the v4.1 migration ladder applied,
    plus the ``voyage-4-large`` dim=3 profile and embeddings table.

    Two test conversations seeded: ``(s1, sk1)`` and ``(s2, sk2)`` — matches
    the TS suite's ``setupDb`` helper at ``semantic-search.test.ts:28-37``
    so we can exercise session_keys + conversation_ids filters with the
    same fixture shape.
    """
    conn = _open_test_conn(load_vec0=True)
    try:
        run_lcm_migrations(conn, fts5_available=False)
        # Two conversations — TS suite uses dim=3 so each vector is
        # writeable by hand and the test exercises the same code paths
        # as dim=1024 / dim=512.
        conn.execute("INSERT INTO conversations (session_id, session_key) VALUES ('s1', 'sk1')")
        conn.execute("INSERT INTO conversations (session_id, session_key) VALUES ('s2', 'sk2')")
        register_embedding_profile(conn, "voyage-4-large", 3)
        ensure_embeddings_table(conn, "voyage-4-large", 3)
        # Commit fixture setup so the §0 assertion in run_semantic_search
        # sees a clean conn — Python's stdlib sqlite3 uses deferred
        # transactions, so ``execute("INSERT ...")`` auto-opens a tx that
        # stays open until ``commit()``. The TS suite doesn't need this
        # because node:sqlite autocommits.
        conn.commit()
        yield conn
    finally:
        conn.close()


def _insert_leaf_with_embedding(
    conn: sqlite3.Connection,
    summary_id: str,
    conversation_id: int,
    vector: tuple[float, float, float],
    *,
    content: str = "x",
    suppressed: bool = False,
) -> None:
    """Insert a ``leaf`` summary + its vec0 row in one shot.

    Mirrors ``semantic-search.test.ts:39-59`` ``insertLeafWithEmbedding``.
    The ``session_key`` is looked up from the ``conversations`` row so we
    don't depend on the conversation_id → session_key mapping bouncing
    through migration code.

    Commits after the write batch so the §0 assertion (no open tx during
    Voyage call) sees a clean state. Python's stdlib ``sqlite3`` uses
    deferred transactions: any executed DML auto-opens one and the
    fixture would otherwise still have it open when the test runs the
    semantic search. The TS suite doesn't need this because node:sqlite
    autocommits.
    """
    conn.execute(
        "INSERT INTO summaries (summary_id, conversation_id, kind, content, "
        "                       token_count, session_key) "
        "VALUES (?, ?, 'leaf', ?, 1, "
        "        (SELECT session_key FROM conversations WHERE conversation_id = ?))",
        (summary_id, conversation_id, content, conversation_id),
    )
    record_embedding(
        conn,
        model_name="voyage-4-large",
        embedded_id=summary_id,
        embedded_kind="summary",
        vector=list(vector),
        source_token_count=1,
        suppressed=suppressed,
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Voyage stand-in
# ---------------------------------------------------------------------------


class _StubVoyageClient:
    """Tiny stand-in implementing the surface used by run_semantic_search.

    The real :class:`VoyageClient` opens an httpx pool in ``__init__``. We
    only need :meth:`embed` for these tests, so we duck-type. The
    ``Protocol``-shaped ``voyage`` parameter on :func:`run_semantic_search`
    accepts any object with an ``async def embed(...)`` — exposed via the
    ``VoyageClient`` type annotation purely for IDE help.
    """

    def __init__(self, *, vectors: list[list[float]], total_tokens: int) -> None:
        self._vectors = vectors
        self._total_tokens = total_tokens
        self.last_call: dict[str, object] | None = None

    async def embed(
        self,
        texts: list[str],
        *,
        model: str,
        input_type: str | None = "document",
        output_dimension: int | None = None,
    ) -> EmbedResult:
        self.last_call = {
            "texts": texts,
            "model": model,
            "input_type": input_type,
            "output_dimension": output_dimension,
        }
        return EmbedResult(
            vectors=list(self._vectors),
            total_tokens=self._total_tokens,
            model=model,
        )


class _AuthFailingVoyageClient:
    """Voyage stand-in that always raises :class:`VoyageError` (``kind='auth'``).

    Exercises the propagation path — auth errors are re-raised unchanged so
    operators see the actionable "set VOYAGE_API_KEY" message.
    """

    async def embed(self, *args, **kwargs) -> EmbedResult:  # type: ignore[no-untyped-def]
        raise VoyageError("auth", "voyage_auth: VOYAGE_API_KEY is empty")


# ===========================================================================
# get_active_embedding_model — non-vec0 tests
# ===========================================================================


class TestGetActiveEmbeddingModel:
    """``get_active_embedding_model`` — profile lookup."""

    def test_returns_none_when_no_profile_registered(
        self, conn_no_vec0: sqlite3.Connection
    ) -> None:
        # Mirrors ``semantic-search.test.ts:62-67``.
        assert get_active_embedding_model(conn_no_vec0) is None

    def test_returns_active_profile(self, conn_no_vec0: sqlite3.Connection) -> None:
        # Mirrors ``semantic-search.test.ts:69-75``.
        register_embedding_profile(conn_no_vec0, "voyage-4-large", 1024)
        result = get_active_embedding_model(conn_no_vec0)
        assert result == EmbeddingProfile(model_name="voyage-4-large", dim=1024)

    def test_returns_most_recent_when_multiple_active(
        self, conn_no_vec0: sqlite3.Connection
    ) -> None:
        # Mirrors ``semantic-search.test.ts:77-88``. Most-recent
        # registered_at wins on ties — covers mid-cutover scenarios where
        # two profiles are active simultaneously.
        register_embedding_profile(conn_no_vec0, "voyage-3-lite", 512)
        conn_no_vec0.execute(
            "UPDATE lcm_embedding_profile SET registered_at = '2026-01-01 00:00:00' "
            "WHERE model_name = 'voyage-3-lite'"
        )
        register_embedding_profile(conn_no_vec0, "voyage-4-large", 1024)
        conn_no_vec0.execute(
            "UPDATE lcm_embedding_profile SET registered_at = '2026-05-05 00:00:00' "
            "WHERE model_name = 'voyage-4-large'"
        )

        result = get_active_embedding_model(conn_no_vec0)
        assert result == EmbeddingProfile(model_name="voyage-4-large", dim=1024)

    def test_excludes_archived_profiles(self, conn_no_vec0: sqlite3.Connection) -> None:
        # Mirrors ``semantic-search.test.ts:90-98``. Profiles with
        # ``archive_after IS NOT NULL`` are excluded — even if ``active=1``
        # they don't get returned. Required so an archived profile being
        # left active by accident doesn't override the newer model.
        register_embedding_profile(conn_no_vec0, "voyage-3-lite", 512)
        conn_no_vec0.execute(
            "UPDATE lcm_embedding_profile SET archive_after = '2026-01-01' "
            "WHERE model_name = 'voyage-3-lite'"
        )
        register_embedding_profile(conn_no_vec0, "voyage-4-large", 1024)

        result = get_active_embedding_model(conn_no_vec0)
        assert result is not None
        assert result.model_name == "voyage-4-large"

    def test_returns_none_when_only_archived_active_profile(
        self, conn_no_vec0: sqlite3.Connection
    ) -> None:
        # Edge case: only profile is archived. Should return None so the
        # caller raises SemanticSearchUnavailableError downstream.
        register_embedding_profile(conn_no_vec0, "voyage-3-lite", 512)
        conn_no_vec0.execute(
            "UPDATE lcm_embedding_profile SET archive_after = '2026-01-01' "
            "WHERE model_name = 'voyage-3-lite'"
        )
        assert get_active_embedding_model(conn_no_vec0) is None


# ===========================================================================
# Error paths — vec0-not-loaded
# ===========================================================================


class TestUnavailableErrors:
    """``run_semantic_search`` precondition gates raise UnavailableError."""

    def test_raises_when_vec0_not_loaded(self, conn_no_vec0: sqlite3.Connection) -> None:
        # Mirrors ``semantic-search.test.ts:102-109``.
        with pytest.raises(SemanticSearchUnavailableError):
            asyncio.run(run_semantic_search(conn_no_vec0, query="anything"))


# ===========================================================================
# vec0-dependent tests
# ===========================================================================


@skip_if_no_vec0
class TestUnavailableErrorsVec0:
    """``run_semantic_search`` errors that need vec0 to manifest correctly."""

    def test_raises_when_no_active_profile(self) -> None:
        # Mirrors ``semantic-search.test.ts:113-121``. vec0 loads, but
        # no profile registered → caller can degrade.
        with closing(_open_test_conn(load_vec0=True)) as conn:
            run_lcm_migrations(conn, fts5_available=False)
            with pytest.raises(SemanticSearchUnavailableError):
                asyncio.run(run_semantic_search(conn, query="anything"))

    def test_raises_when_table_missing(self) -> None:
        # Profile registered but ``ensure_embeddings_table`` never called —
        # should still raise UnavailableError (not crash with raw vec0
        # error). Caller gets a clean degrade signal.
        with closing(_open_test_conn(load_vec0=True)) as conn:
            run_lcm_migrations(conn, fts5_available=False)
            register_embedding_profile(conn, "voyage-4-large", 3)
            with pytest.raises(SemanticSearchUnavailableError, match="doesn't exist"):
                asyncio.run(run_semantic_search(conn, query="anything"))

    def test_raises_on_empty_query_without_vector(self, conn_with_vec0: sqlite3.Connection) -> None:
        # Mirrors ``semantic-search.test.ts:123-130``.
        _insert_leaf_with_embedding(conn_with_vec0, "leaf_a", 1, (0.1, 0.2, 0.3))
        with pytest.raises(ValueError, match="query is required"):
            asyncio.run(run_semantic_search(conn_with_vec0, query=""))

    def test_raises_on_query_vector_dim_mismatch(self, conn_with_vec0: sqlite3.Connection) -> None:
        # Mirrors ``semantic-search.test.ts:132-138``. Dim=3 profile, but
        # caller passes a dim=2 vector — caught before vec0 sees it.
        with pytest.raises(SemanticSearchUnavailableError, match="dim 2"):
            asyncio.run(
                run_semantic_search(
                    conn_with_vec0,
                    query="anything",
                    query_vector=[0.1, 0.2],
                )
            )

    def test_raises_when_no_voyage_client_and_no_vector(
        self, conn_with_vec0: sqlite3.Connection
    ) -> None:
        # Neither vector nor client → can't produce a query vector. The
        # Python port surfaces this distinctly so the caller sees a clear
        # "misconfiguration" message rather than a generic auth error.
        _insert_leaf_with_embedding(conn_with_vec0, "leaf_a", 1, (0.1, 0.2, 0.3))
        with pytest.raises(SemanticSearchUnavailableError, match="VoyageClient"):
            asyncio.run(run_semantic_search(conn_with_vec0, query="anything", voyage=None))


# ===========================================================================
# Happy path — ranked hits + JOIN content
# ===========================================================================


@skip_if_no_vec0
class TestRankedHits:
    """Happy-path search returns ranked hits joined with summary content."""

    def test_returns_ranked_hits_with_summary_content(
        self, conn_with_vec0: sqlite3.Connection
    ) -> None:
        # Mirrors ``semantic-search.test.ts:140-159``.
        _insert_leaf_with_embedding(
            conn_with_vec0, "leaf_close", 1, (0.1, 0.2, 0.3), content="the alpha doc"
        )
        _insert_leaf_with_embedding(
            conn_with_vec0, "leaf_far", 1, (0.9, 0.9, 0.9), content="the omega doc"
        )

        result = asyncio.run(
            run_semantic_search(
                conn_with_vec0,
                query="ignored when query_vector provided",
                query_vector=[0.1, 0.2, 0.3],
                k=5,
            )
        )

        assert len(result.hits) == 2
        assert result.hits[0].summary_id == "leaf_close"
        assert result.hits[0].content == "the alpha doc"
        assert result.hits[0].session_key == "sk1"
        # Identical vector → distance ≈ 0.
        assert result.hits[0].distance == pytest.approx(0.0, abs=1e-5)
        # No Voyage call (query_vector path).
        assert result.voyage_tokens_consumed == 0
        assert result.model_name == "voyage-4-large"
        # candidate_count covers what vec0 returned (2 candidates → both
        # survive the JOIN).
        assert result.candidate_count == 2


# ===========================================================================
# Suppression handling
# ===========================================================================


@skip_if_no_vec0
class TestSuppression:
    """``exclude_suppressed`` filters at vec0 metadata + JOIN layers."""

    def test_excludes_suppressed_by_default(self, conn_with_vec0: sqlite3.Connection) -> None:
        # Mirrors ``semantic-search.test.ts:161-189``. Two leaves with the
        # same vector — one suppressed at insert, one not. Default behavior
        # excludes the suppressed row.
        _insert_leaf_with_embedding(conn_with_vec0, "leaf_v", 1, (0.1, 0.2, 0.3), content="visible")
        _insert_leaf_with_embedding(
            conn_with_vec0,
            "leaf_h",
            1,
            (0.1, 0.2, 0.3),
            content="hidden",
            suppressed=True,
        )
        # Defense-in-depth: also flip ``summaries.suppressed_at`` so the
        # JOIN-layer filter would catch it independently of the vec0
        # metadata flag.
        conn_with_vec0.execute(
            "UPDATE summaries SET suppressed_at = ? WHERE summary_id = ?",
            ("2026-05-05", "leaf_h"),
        )

        visible = asyncio.run(
            run_semantic_search(
                conn_with_vec0,
                query="x",
                query_vector=[0.1, 0.2, 0.3],
                k=5,
            )
        )
        assert [h.summary_id for h in visible.hits] == ["leaf_v"]

    def test_includes_suppressed_when_opted_in(self, conn_with_vec0: sqlite3.Connection) -> None:
        _insert_leaf_with_embedding(conn_with_vec0, "leaf_v", 1, (0.1, 0.2, 0.3), content="visible")
        _insert_leaf_with_embedding(
            conn_with_vec0,
            "leaf_h",
            1,
            (0.1, 0.2, 0.3),
            content="hidden",
            suppressed=True,
        )
        # Note: do NOT flip summaries.suppressed_at here — we want both
        # rows to survive the JOIN. The vec0 layer is opt-in via
        # exclude_suppressed=False.

        include_all = asyncio.run(
            run_semantic_search(
                conn_with_vec0,
                query="x",
                query_vector=[0.1, 0.2, 0.3],
                k=5,
                exclude_suppressed=False,
            )
        )
        assert sorted(h.summary_id for h in include_all.hits) == [
            "leaf_h",
            "leaf_v",
        ]


# ===========================================================================
# Filters (session, conversation, time, kind)
# ===========================================================================


@skip_if_no_vec0
class TestFilters:
    """Session/conversation/time/kind filters apply at the JOIN layer."""

    def test_session_keys_filter(self, conn_with_vec0: sqlite3.Connection) -> None:
        # Mirrors ``semantic-search.test.ts:191-204``.
        _insert_leaf_with_embedding(conn_with_vec0, "leaf_a", 1, (0.1, 0.2, 0.3))  # sk1
        _insert_leaf_with_embedding(conn_with_vec0, "leaf_b", 2, (0.1, 0.2, 0.3))  # sk2

        result = asyncio.run(
            run_semantic_search(
                conn_with_vec0,
                query="x",
                query_vector=[0.1, 0.2, 0.3],
                session_keys=["sk1"],
                k=5,
            )
        )
        assert [h.summary_id for h in result.hits] == ["leaf_a"]

    def test_conversation_ids_filter(self, conn_with_vec0: sqlite3.Connection) -> None:
        # Mirrors ``semantic-search.test.ts:206-219``.
        _insert_leaf_with_embedding(conn_with_vec0, "leaf_a", 1, (0.1, 0.2, 0.3))
        _insert_leaf_with_embedding(conn_with_vec0, "leaf_b", 2, (0.1, 0.2, 0.3))

        result = asyncio.run(
            run_semantic_search(
                conn_with_vec0,
                query="x",
                query_vector=[0.1, 0.2, 0.3],
                conversation_ids=[1],
                k=5,
            )
        )
        assert [h.summary_id for h in result.hits] == ["leaf_a"]

    def test_time_filters_since_and_before(self, conn_with_vec0: sqlite3.Connection) -> None:
        # Mirrors ``semantic-search.test.ts:221-244``. Uses COALESCE
        # semantics: ``latest_at`` not set on fresh rows, so the filter
        # falls through to ``created_at`` — Wave-1 fix to align with FTS.
        _insert_leaf_with_embedding(conn_with_vec0, "leaf_old", 1, (0.1, 0.2, 0.3))
        _insert_leaf_with_embedding(conn_with_vec0, "leaf_new", 1, (0.1, 0.2, 0.3))
        conn_with_vec0.execute(
            "UPDATE summaries SET created_at = '2026-01-01 00:00:00' WHERE summary_id = ?",
            ("leaf_old",),
        )
        conn_with_vec0.execute(
            "UPDATE summaries SET created_at = '2026-05-01 00:00:00' WHERE summary_id = ?",
            ("leaf_new",),
        )

        recent = asyncio.run(
            run_semantic_search(
                conn_with_vec0,
                query="x",
                query_vector=[0.1, 0.2, 0.3],
                since=datetime(2026, 4, 1),
                k=5,
            )
        )
        assert [h.summary_id for h in recent.hits] == ["leaf_new"]

        ancient = asyncio.run(
            run_semantic_search(
                conn_with_vec0,
                query="x",
                query_vector=[0.1, 0.2, 0.3],
                before=datetime(2026, 4, 1),
                k=5,
            )
        )
        assert [h.summary_id for h in ancient.hits] == ["leaf_old"]

    def test_time_filter_with_tzinfo_normalised_to_utc(
        self, conn_with_vec0: sqlite3.Connection
    ) -> None:
        # Datetime with tzinfo must be normalised to UTC before binding —
        # SQLite ``julianday`` interprets bare strings as UTC, so a
        # tz-aware datetime can't leak its offset into the comparison.
        _insert_leaf_with_embedding(conn_with_vec0, "leaf_x", 1, (0.1, 0.2, 0.3))
        conn_with_vec0.execute(
            "UPDATE summaries SET created_at = '2026-05-01 12:00:00' WHERE summary_id = ?",
            ("leaf_x",),
        )
        # Filter with UTC tz → still hits.
        result = asyncio.run(
            run_semantic_search(
                conn_with_vec0,
                query="x",
                query_vector=[0.1, 0.2, 0.3],
                since=datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc),
                k=5,
            )
        )
        assert [h.summary_id for h in result.hits] == ["leaf_x"]

    def test_time_filter_uses_coalesce_latest_at(self, conn_with_vec0: sqlite3.Connection) -> None:
        # LCM Wave-1 alignment: COALESCE(latest_at, created_at).
        # condensed summary written 2026-05-01 but covering older content
        # (latest_at=2026-01-01). Time filter `since=2026-04-01` should
        # EXCLUDE it because latest_at falls before the window.
        conn_with_vec0.execute(
            "INSERT INTO summaries (summary_id, conversation_id, kind, content, "
            "                       token_count, session_key, "
            "                       earliest_at, latest_at, created_at) "
            "VALUES (?, 1, 'condensed', 'old content', 1, 'sk1', "
            "        '2026-01-01 00:00:00', '2026-01-01 00:00:00', "
            "        '2026-05-01 00:00:00')",
            ("cond_old",),
        )
        record_embedding(
            conn_with_vec0,
            model_name="voyage-4-large",
            embedded_id="cond_old",
            embedded_kind="summary",
            vector=[0.1, 0.2, 0.3],
            source_token_count=1,
        )

        result = asyncio.run(
            run_semantic_search(
                conn_with_vec0,
                query="x",
                query_vector=[0.1, 0.2, 0.3],
                since=datetime(2026, 4, 1),
                k=5,
            )
        )
        # Excluded because COALESCE(latest_at, created_at) = 2026-01-01 <
        # 2026-04-01. If the filter used s.created_at, this row WOULD pass
        # — that's exactly the bug the Wave-1 fix repairs.
        assert [h.summary_id for h in result.hits] == []

    def test_summary_kinds_filter(self, conn_with_vec0: sqlite3.Connection) -> None:
        # Mirrors ``semantic-search.test.ts:323-354``.
        _insert_leaf_with_embedding(conn_with_vec0, "leaf_a", 1, (0.1, 0.2, 0.3))
        conn_with_vec0.execute(
            "INSERT INTO summaries (summary_id, conversation_id, kind, content, "
            "                       token_count, session_key) "
            "VALUES (?, 1, 'condensed', 'sum', 1, 'sk1')",
            ("cond_a",),
        )
        record_embedding(
            conn_with_vec0,
            model_name="voyage-4-large",
            embedded_id="cond_a",
            embedded_kind="summary",
            vector=[0.1, 0.2, 0.3],
            source_token_count=1,
        )

        leaves_only = asyncio.run(
            run_semantic_search(
                conn_with_vec0,
                query="x",
                query_vector=[0.1, 0.2, 0.3],
                summary_kinds=["leaf"],
                k=5,
            )
        )
        assert [h.summary_id for h in leaves_only.hits] == ["leaf_a"]

        both = asyncio.run(
            run_semantic_search(
                conn_with_vec0,
                query="x",
                query_vector=[0.1, 0.2, 0.3],
                k=5,
            )
        )
        assert sorted(h.summary_id for h in both.hits) == ["cond_a", "leaf_a"]


# ===========================================================================
# Over-fetch on filtered KNN (P1 fix)
# ===========================================================================


@skip_if_no_vec0
class TestOverFetch:
    """When filters are active, over-fetch from vec0 (10×, cap 500)."""

    def test_filtered_knn_over_fetches_post_filter_survivors(
        self, conn_with_vec0: sqlite3.Connection
    ) -> None:
        # Mirrors ``semantic-search.test.ts:279-304``. 30 leaves total, all
        # close to the query vector. 25 moved OUT of the time window. With
        # over-fetch, k=5 requests 50 candidates from vec0, 30 survive,
        # post-filter keeps the 5 in-window ones.
        for i in range(1, 31):
            _insert_leaf_with_embedding(conn_with_vec0, f"leaf_{i}", 1, (0.1, 0.2, 0.3))
        # Move 25 leaves OUT of the time window.
        for i in range(6, 31):
            conn_with_vec0.execute(
                "UPDATE summaries SET created_at = '2026-01-01 00:00:00' WHERE summary_id = ?",
                (f"leaf_{i}",),
            )

        result = asyncio.run(
            run_semantic_search(
                conn_with_vec0,
                query="x",
                query_vector=[0.1, 0.2, 0.3],
                since=datetime(2026, 4, 1),
                k=5,
            )
        )
        # Without over-fetch: k=5 requests 5 candidates, almost certainly
        # not the 5 in-window. With over-fetch: 30 returned, 5 in-window
        # survive.
        assert len(result.hits) == 5
        assert result.candidate_count >= 30
        # Survivors should all be in-window (leaf_1..leaf_5).
        assert all(h.summary_id in {f"leaf_{i}" for i in range(1, 6)} for h in result.hits)


# ===========================================================================
# Cosine similarity + confidence bands
# ===========================================================================


@skip_if_no_vec0
class TestCosineBands:
    """Each hit exposes cosine similarity + a discrete confidence band."""

    def test_identical_vector_produces_cosine_one_band_high(
        self, conn_with_vec0: sqlite3.Connection
    ) -> None:
        # Mirrors ``semantic-search.test.ts:308-321``.
        _insert_leaf_with_embedding(conn_with_vec0, "leaf_id", 1, (1.0, 0.0, 0.0))
        result = asyncio.run(
            run_semantic_search(
                conn_with_vec0,
                query="x",
                query_vector=[1.0, 0.0, 0.0],
                k=1,
            )
        )
        assert len(result.hits) == 1
        hit = result.hits[0]
        # Identical unit vectors → distance ≈ 0 → cosine ≈ 1.
        assert hit.distance == pytest.approx(0.0, abs=1e-5)
        assert hit.cosine_similarity == pytest.approx(1.0, abs=1e-5)
        assert hit.band == "high"

    def test_orthogonal_vector_produces_low_or_noise_band(
        self, conn_with_vec0: sqlite3.Connection
    ) -> None:
        # Orthogonal unit vectors → cosine ≈ 0 → "noise" band.
        # (1,0,0) · (0,1,0) = 0; L2 distance = sqrt(2) ≈ 1.414.
        _insert_leaf_with_embedding(conn_with_vec0, "leaf_orth", 1, (0.0, 1.0, 0.0))
        result = asyncio.run(
            run_semantic_search(
                conn_with_vec0,
                query="x",
                query_vector=[1.0, 0.0, 0.0],
                k=1,
            )
        )
        assert len(result.hits) == 1
        hit = result.hits[0]
        assert hit.cosine_similarity == pytest.approx(0.0, abs=1e-5)
        assert hit.band == "noise"

    def test_band_thresholds_match_constants(self) -> None:
        # Sanity-check that the public constants haven't drifted from
        # ``semantic-search.ts:122-128``.
        assert COSINE_BAND_HIGH == 0.65
        assert COSINE_BAND_MEDIUM == 0.50
        assert COSINE_BAND_LOW == 0.35


# ===========================================================================
# Voyage integration: input_type, output_dimension, token tracking, errors
# ===========================================================================


@skip_if_no_vec0
class TestVoyageIntegration:
    """Voyage is called with the right args; tokens flow back; errors propagate."""

    def test_calls_voyage_with_input_type_query(self, conn_with_vec0: sqlite3.Connection) -> None:
        # Mirrors ``semantic-search.test.ts:246-273``. Verify
        # input_type='query' is sent (asymmetric retrieval).
        _insert_leaf_with_embedding(conn_with_vec0, "leaf_a", 1, (0.5, 0.5, 0.5))
        stub = _StubVoyageClient(vectors=[[0.5, 0.5, 0.5]], total_tokens=17)

        result = asyncio.run(
            run_semantic_search(
                conn_with_vec0,
                query="test",
                voyage=stub,  # type: ignore[arg-type]
                k=5,
            )
        )
        assert result.voyage_tokens_consumed == 17
        assert result.hits[0].summary_id == "leaf_a"
        # Verify the Voyage call shape.
        assert stub.last_call is not None
        assert stub.last_call["input_type"] == "query"
        assert stub.last_call["model"] == "voyage-4-large"
        # LCM Wave-11: output_dimension MUST equal the active profile dim.
        assert stub.last_call["output_dimension"] == 3
        assert stub.last_call["texts"] == ["test"]

    def test_wave_11_output_dimension_pulled_from_profile(
        self, conn_with_vec0: sqlite3.Connection
    ) -> None:
        # Wave-11 P1 fix: the embed call must request the same dim as the
        # indexed corpus. The fixture profile is dim=3 — output_dimension
        # MUST be 3 even though Voyage's default is 1024. Without this,
        # vec0 columns with non-default dim would receive 1024-d vectors
        # and the MATCH crashes with dim mismatch.
        _insert_leaf_with_embedding(conn_with_vec0, "leaf_a", 1, (0.5, 0.5, 0.5))
        stub = _StubVoyageClient(vectors=[[0.5, 0.5, 0.5]], total_tokens=1)

        asyncio.run(
            run_semantic_search(
                conn_with_vec0,
                query="x",
                voyage=stub,  # type: ignore[arg-type]
                k=1,
            )
        )
        assert stub.last_call is not None
        assert stub.last_call["output_dimension"] == 3

    def test_skips_voyage_when_query_vector_supplied(
        self, conn_with_vec0: sqlite3.Connection
    ) -> None:
        # Mirrors the queryVector branch in ``semantic-search.test.ts:140-159``.
        _insert_leaf_with_embedding(conn_with_vec0, "leaf_a", 1, (0.1, 0.2, 0.3))
        stub = _StubVoyageClient(vectors=[[0.0, 0.0, 0.0]], total_tokens=99)

        result = asyncio.run(
            run_semantic_search(
                conn_with_vec0,
                query="ignored",
                query_vector=[0.1, 0.2, 0.3],
                voyage=stub,  # type: ignore[arg-type]
                k=5,
            )
        )
        # voyage_tokens_consumed must reflect that no call happened.
        assert result.voyage_tokens_consumed == 0
        # Stub never called.
        assert stub.last_call is None

    def test_voyage_auth_error_propagates(self, conn_with_vec0: sqlite3.Connection) -> None:
        # Auth-class errors propagate so the operator sees the actionable
        # "set VOYAGE_API_KEY" message. Hybrid layer (issue 05-09) treats
        # auth differently from other VoyageError kinds.
        _insert_leaf_with_embedding(conn_with_vec0, "leaf_a", 1, (0.1, 0.2, 0.3))
        failing = _AuthFailingVoyageClient()

        with pytest.raises(VoyageError) as exc_info:
            asyncio.run(
                run_semantic_search(
                    conn_with_vec0,
                    query="will-fail",
                    voyage=failing,  # type: ignore[arg-type]
                    k=5,
                )
            )
        assert exc_info.value.kind == "auth"

    def test_voyage_returns_zero_vectors_raises_unavailable(
        self, conn_with_vec0: sqlite3.Connection
    ) -> None:
        # Voyage returning 0 vectors for a 1-text input is malformed —
        # surface as unavailable so the caller degrades.
        _insert_leaf_with_embedding(conn_with_vec0, "leaf_a", 1, (0.1, 0.2, 0.3))
        empty_stub = _StubVoyageClient(vectors=[], total_tokens=0)

        with pytest.raises(SemanticSearchUnavailableError, match="0 vectors"):
            asyncio.run(
                run_semantic_search(
                    conn_with_vec0,
                    query="x",
                    voyage=empty_stub,  # type: ignore[arg-type]
                    k=1,
                )
            )


# ===========================================================================
# Empty corpus + edge cases
# ===========================================================================


@skip_if_no_vec0
class TestEdgeCases:
    """Empty corpus, zero candidates, model_name override."""

    def test_empty_corpus_returns_empty_result(self, conn_with_vec0: sqlite3.Connection) -> None:
        # Fixture has profile + table but no rows. KNN returns nothing;
        # we return an empty result with model_name populated for
        # diagnostic correlation.
        result = asyncio.run(
            run_semantic_search(
                conn_with_vec0,
                query="x",
                query_vector=[0.1, 0.2, 0.3],
                k=5,
            )
        )
        assert result.hits == []
        assert result.candidate_count == 0
        assert result.voyage_tokens_consumed == 0
        assert result.model_name == "voyage-4-large"

    def test_model_name_override_used_for_voyage_call(
        self, conn_with_vec0: sqlite3.Connection
    ) -> None:
        # ``model_name`` overrides the Voyage model used for the embed
        # call. Active profile is voyage-4-large; caller passes
        # voyage-3-lite. The override is forwarded verbatim.
        _insert_leaf_with_embedding(conn_with_vec0, "leaf_a", 1, (0.5, 0.5, 0.5))
        stub = _StubVoyageClient(vectors=[[0.5, 0.5, 0.5]], total_tokens=1)

        asyncio.run(
            run_semantic_search(
                conn_with_vec0,
                query="x",
                voyage=stub,  # type: ignore[arg-type]
                model_name="voyage-3-lite",
                k=1,
            )
        )
        assert stub.last_call is not None
        assert stub.last_call["model"] == "voyage-3-lite"
        # output_dimension still tied to active profile dim, not the
        # caller's model_name. The profile defines the on-disk vec0
        # column shape — query must match it.
        assert stub.last_call["output_dimension"] == 3

    def test_input_type_override(self, conn_with_vec0: sqlite3.Connection) -> None:
        # Caller can override input_type — e.g. for symmetric retrieval
        # eval rigs. Default is "query".
        _insert_leaf_with_embedding(conn_with_vec0, "leaf_a", 1, (0.5, 0.5, 0.5))
        stub = _StubVoyageClient(vectors=[[0.5, 0.5, 0.5]], total_tokens=1)
        asyncio.run(
            run_semantic_search(
                conn_with_vec0,
                query="x",
                voyage=stub,  # type: ignore[arg-type]
                input_type="document",
                k=1,
            )
        )
        assert stub.last_call is not None
        assert stub.last_call["input_type"] == "document"


# ===========================================================================
# Result dataclass shapes
# ===========================================================================


class TestResultShapes:
    """Frozen dataclasses surfaced via the public API."""

    def test_semantic_hit_is_frozen(self) -> None:
        hit = SemanticHit(
            summary_id="x",
            embedded_kind="summary",
            distance=0.0,
            cosine_similarity=1.0,
            band="high",
            conversation_id=1,
            session_key="sk1",
            kind="leaf",
            content="hello",
            token_count=2,
            created_at="2026-05-14",
        )
        with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
            hit.summary_id = "y"  # type: ignore[misc]

    def test_semantic_search_result_default_factory(self) -> None:
        result = SemanticSearchResult()
        assert result.hits == []
        assert result.candidate_count == 0
        assert result.voyage_tokens_consumed == 0
        assert result.model_name == ""

    def test_embedding_profile_equality(self) -> None:
        a = EmbeddingProfile(model_name="voyage-4-large", dim=1024)
        b = EmbeddingProfile(model_name="voyage-4-large", dim=1024)
        c = EmbeddingProfile(model_name="voyage-4-large", dim=2048)
        assert a == b
        assert a != c
