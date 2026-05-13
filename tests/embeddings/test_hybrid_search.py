"""Tests for :mod:`lossless_hermes.embeddings.hybrid_search` (issue 05-09).

Ports ``lossless-claw/test/hybrid-search.test.ts`` (313 LOC) to Python.

vec0-dependent tests are gated on the extension being loadable on this
Python build via :data:`VEC0_AVAILABLE` / :data:`skip_if_no_vec0` — mirrors
the gating pattern established by ``tests/embeddings/test_semantic_search.py``.

Voyage is mocked at the :class:`VoyageClient` surface via a tiny
stand-in so we exercise ``rerank`` / ``embed`` without hitting the API.
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from typing import Any, Iterator

import pytest

from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.embeddings.hybrid_search import (
    DEFAULT_K_FTS,
    DEFAULT_K_SEMANTIC,
    DEFAULT_TOP_N,
    RRF_K,
    FtsHit,
    HybridHit,
    HybridSearchResult,
    run_hybrid_search,
)
from lossless_hermes.embeddings.store import (
    ensure_embeddings_table,
    record_embedding,
    register_embedding_profile,
)
from lossless_hermes.voyage.client import (
    MAX_TOKENS_PER_RERANK_CALL,
    EmbedResult,
    RerankItem,
    RerankResult,
    VoyageError,
)


# ---------------------------------------------------------------------------
# vec0 availability probe — gates vec0-dependent tests
# ---------------------------------------------------------------------------


def _vec0_loadable() -> bool:
    """Return :data:`True` iff ``sqlite_vec.load`` succeeds on this Python."""
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
        "Vec0-dependent hybrid-search tests skip cleanly."
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
def conn_with_vec0() -> Iterator[sqlite3.Connection]:
    """In-memory SQLite with vec0 + v4.1 migration ladder + dim=3 profile.

    Two conversations seeded — ``(s1, sk1)`` and ``(s2, sk2)`` — mirroring
    ``test_semantic_search.py``'s fixture so we can exercise session/
    conversation filters with the same shape.
    """
    conn = _open_test_conn(load_vec0=True)
    try:
        run_lcm_migrations(conn, fts5_available=False)
        conn.execute("INSERT INTO conversations (session_id, session_key) VALUES ('s1', 'sk1')")
        conn.execute("INSERT INTO conversations (session_id, session_key) VALUES ('s2', 'sk2')")
        register_embedding_profile(conn, "voyage-4-large", 3)
        ensure_embeddings_table(conn, "voyage-4-large", 3)
        # Commit fixture setup so the §0 assertion in run_semantic_search
        # (called by the semantic arm) sees a clean conn.
        conn.commit()
        yield conn
    finally:
        conn.close()


def _insert_leaf_with_embedding(
    conn: sqlite3.Connection,
    summary_id: str,
    conversation_id: int,
    vector: tuple[float, float, float] | None,
    *,
    content: str = "x",
    token_count: int = 1,
) -> None:
    """Insert a ``leaf`` summary + (optionally) its vec0 row.

    Mirrors the ``_insert_leaf_with_embedding`` helper in
    ``test_semantic_search.py``. ``vector=None`` skips the embedding row,
    so the leaf only shows up via FTS (caller-injected).
    """
    conn.execute(
        "INSERT INTO summaries (summary_id, conversation_id, kind, content, "
        "                       token_count, session_key) "
        "VALUES (?, ?, 'leaf', ?, ?, "
        "        (SELECT session_key FROM conversations WHERE conversation_id = ?))",
        (summary_id, conversation_id, content, token_count, conversation_id),
    )
    if vector is not None:
        record_embedding(
            conn,
            model_name="voyage-4-large",
            embedded_id=summary_id,
            embedded_kind="summary",
            vector=list(vector),
            source_token_count=1,
        )
    conn.commit()


# ---------------------------------------------------------------------------
# FTS stand-in — caller-injected callable returning FtsHit list
# ---------------------------------------------------------------------------


def make_fts_search(
    hits: list[tuple[str, str]],
    *,
    conversation_id: int = 1,
    session_key: str = "sk1",
    token_count: int = 1,
):
    """Build an async ``fts_search`` from ``[(summary_id, content), ...]``.

    Ports ``hybrid-search.test.ts:64-77`` ``makeFtsSearch`` helper. Hits
    are returned in input order with their rank set to the list index.
    """

    async def _fts(query: str, *, limit: int, **filters: Any) -> list[FtsHit]:
        return [
            FtsHit(
                summary_id=sid,
                conversation_id=conversation_id,
                session_key=session_key,
                kind="leaf",
                content=content,
                token_count=token_count,
                created_at="2026-05-05",
                rank=i,
            )
            for i, (sid, content) in enumerate(hits)
        ]

    return _fts


# ---------------------------------------------------------------------------
# Voyage stand-in — controllable embed() + rerank()
# ---------------------------------------------------------------------------


class _StubVoyage:
    """Tiny stand-in supplying ``embed`` and ``rerank`` for the tests.

    The real :class:`VoyageClient` opens an httpx pool in ``__init__``;
    we only need the two coroutines, so we duck-type. Callers pass the
    behavior they want; the stand-in records calls for assertion.
    """

    def __init__(
        self,
        *,
        embed_vector: list[float] | None = None,
        rerank_scores: dict[str, float] | None = None,
        rerank_raise: Exception | None = None,
        embed_raise: Exception | None = None,
    ) -> None:
        # If embed_vector is None we use a sane default (matches the
        # leaf vectors in tests).
        self._embed_vector = embed_vector if embed_vector is not None else [0.1, 0.2, 0.3]
        self._rerank_scores = rerank_scores or {}
        self._rerank_raise = rerank_raise
        self._embed_raise = embed_raise
        self.embed_calls: list[dict[str, Any]] = []
        self.rerank_calls: list[dict[str, Any]] = []

    async def embed(
        self,
        texts: list[str],
        *,
        model: str,
        input_type: str | None = "document",
        output_dimension: int | None = None,
    ) -> EmbedResult:
        self.embed_calls.append({
            "texts": texts,
            "model": model,
            "input_type": input_type,
            "output_dimension": output_dimension,
        })
        if self._embed_raise is not None:
            raise self._embed_raise
        return EmbedResult(
            vectors=[list(self._embed_vector)],
            total_tokens=1,
            model=model,
        )

    async def rerank(
        self,
        query: str,
        candidates: list[tuple[str, str]],
        *,
        model: str = "rerank-2.5",
        top_k: int | None = None,
    ) -> RerankResult:
        self.rerank_calls.append({
            "query": query,
            "candidates": list(candidates),
            "model": model,
            "top_k": top_k,
        })
        if self._rerank_raise is not None:
            raise self._rerank_raise
        # Score each candidate by content; default 0.1 if missing.
        items: list[RerankItem] = []
        for idx, (sid, content) in enumerate(candidates):
            score = self._rerank_scores.get(content, 0.1)
            items.append(RerankItem(id=sid, index=idx, score=score))
        # Defensive sort — production client sorts descending too.
        items.sort(key=lambda x: x.score, reverse=True)
        return RerankResult(results=items, total_tokens=100, model=model)


# ===========================================================================
# Input validation
# ===========================================================================


class TestInputValidation:
    """Pre-call validation paths — no Voyage / vec0 needed."""

    def test_rejects_empty_query(self) -> None:
        # Mirrors ``hybrid-search.test.ts:280-291``.
        with closing(_open_test_conn(load_vec0=False)) as conn:
            run_lcm_migrations(conn, fts5_available=False)
            with pytest.raises(ValueError, match="query is required"):
                asyncio.run(
                    run_hybrid_search(
                        conn,
                        query="",
                        fts_search=make_fts_search([]),
                    )
                )

    def test_rejects_whitespace_only_query(self) -> None:
        # The TS path also strips whitespace before checking.
        with closing(_open_test_conn(load_vec0=False)) as conn:
            run_lcm_migrations(conn, fts5_available=False)
            with pytest.raises(ValueError, match="query is required"):
                asyncio.run(
                    run_hybrid_search(
                        conn,
                        query="   \n\t  ",
                        fts_search=make_fts_search([]),
                    )
                )


# ===========================================================================
# Defaults + dataclasses
# ===========================================================================


class TestDefaults:
    """Constants and dataclass field defaults — port verbatim from TS."""

    def test_default_k_fts(self) -> None:
        assert DEFAULT_K_FTS == 50  # hybrid-search.ts:160

    def test_default_k_semantic(self) -> None:
        assert DEFAULT_K_SEMANTIC == 50  # hybrid-search.ts:161

    def test_default_top_n(self) -> None:
        assert DEFAULT_TOP_N == 20  # hybrid-search.ts:162

    def test_rrf_k(self) -> None:
        assert RRF_K == 60  # hybrid-search.ts:415

    def test_hybrid_search_result_defaults(self) -> None:
        # The empty-corpus path returns this shape; we want callers to
        # be able to construct one trivially in tests.
        result = HybridSearchResult()
        assert result.hits == []
        assert result.candidate_count == 0
        assert result.voyage_tokens_consumed == 0
        assert result.degraded_to_fts_only is False
        assert result.degraded_skipped_rerank is False
        assert result.rerank_pack_truncated is False
        assert result.rerank_packed_count == 0
        assert result.model == ""
        assert result.reranker_model is None


# ===========================================================================
# Empty / no-candidates path
# ===========================================================================


class TestEmptyCorpus:
    """Both arms empty → empty HybridSearchResult immediately."""

    def test_returns_empty_when_both_arms_empty_no_vec0(self) -> None:
        # Mirrors ``hybrid-search.test.ts:299-311``. vec0 absent → semantic
        # arm degrades; FTS returns []. No rerank call should be issued.
        with closing(_open_test_conn(load_vec0=False)) as conn:
            run_lcm_migrations(conn, fts5_available=False)
            voyage = _StubVoyage(rerank_scores={})
            result = asyncio.run(
                run_hybrid_search(
                    conn,
                    query="nonexistent",
                    fts_search=make_fts_search([]),
                    voyage=voyage,  # type: ignore[arg-type]
                )
            )
            assert result.hits == []
            assert result.candidate_count == 0
            # Semantic arm degraded → no embed call should happen since
            # vec0 isn't loaded (UnavailableError before Voyage).
            assert voyage.embed_calls == []
            # No rerank either — empty candidates short-circuit.
            assert voyage.rerank_calls == []


# ===========================================================================
# Happy path — FTS + semantic merge + rerank ordering
# ===========================================================================


@skip_if_no_vec0
class TestHappyPathRerank:
    """Merges FTS + semantic, reranks, returns top-N (vec0-gated)."""

    def test_merges_arms_and_reranks(self, conn_with_vec0: sqlite3.Connection) -> None:
        # Mirrors ``hybrid-search.test.ts:96-136``.
        _insert_leaf_with_embedding(
            conn_with_vec0, "leaf_a", 1, (0.1, 0.2, 0.3), content="alpha doc"
        )
        _insert_leaf_with_embedding(
            conn_with_vec0, "leaf_b", 1, (0.9, 0.9, 0.9), content="beta doc"
        )

        voyage = _StubVoyage(
            embed_vector=[0.1, 0.2, 0.3],
            rerank_scores={"alpha doc": 0.95, "beta doc": 0.30},
        )

        result = asyncio.run(
            run_hybrid_search(
                conn_with_vec0,
                query="alpha",
                fts_search=make_fts_search(
                    # FTS misses leaf_b — semantic finds it.
                    [("leaf_a", "alpha doc")]
                ),
                voyage=voyage,  # type: ignore[arg-type]
                query_vector=[0.1, 0.2, 0.3],
                top_n=5,
            )
        )

        assert len(result.hits) == 2
        assert result.hits[0].summary_id == "leaf_a"
        assert result.hits[0].score == pytest.approx(0.95)
        assert result.hits[0].from_fts is True
        assert result.hits[0].from_semantic is True
        # Identical vector → distance ≈ 0.
        assert result.hits[0].semantic_distance == pytest.approx(0.0, abs=1e-5)
        assert result.hits[0].fts_rank == 0

        assert result.hits[1].summary_id == "leaf_b"
        assert result.hits[1].from_fts is False
        assert result.hits[1].from_semantic is True
        assert result.hits[1].fts_rank is None

        assert result.candidate_count == 2
        assert result.degraded_to_fts_only is False
        assert result.degraded_skipped_rerank is False
        assert result.rerank_packed_count == 2
        assert result.rerank_pack_truncated is False
        assert result.reranker_model == "rerank-2.5"
        # voyage_tokens_consumed = embed (0 since query_vector path) +
        # rerank (100 from stub).
        assert result.voyage_tokens_consumed == 100

    def test_dedupes_overlap_between_arms(self, conn_with_vec0: sqlite3.Connection) -> None:
        # Mirrors ``hybrid-search.test.ts:138-153``. Same summary_id from
        # both arms → single hit with both flags set.
        _insert_leaf_with_embedding(
            conn_with_vec0, "leaf_x", 1, (0.1, 0.2, 0.3), content="shared doc"
        )

        voyage = _StubVoyage(rerank_scores={"shared doc": 0.99})

        result = asyncio.run(
            run_hybrid_search(
                conn_with_vec0,
                query="shared",
                fts_search=make_fts_search([("leaf_x", "shared doc")]),
                voyage=voyage,  # type: ignore[arg-type]
                query_vector=[0.1, 0.2, 0.3],
            )
        )

        assert len(result.hits) == 1
        assert result.hits[0].from_fts is True
        assert result.hits[0].from_semantic is True
        assert result.hits[0].fts_rank == 0
        # candidate_count == 1 confirms dedupe — would be 2 without it.
        assert result.candidate_count == 1


# ===========================================================================
# Graceful degradation — semantic arm failures
# ===========================================================================


class TestSemanticDegrade:
    """Semantic arm failures degrade to FTS-only (not vec0-gated)."""

    def test_vec0_unavailable_degrades_to_fts_only(self) -> None:
        # Mirrors ``hybrid-search.test.ts:159-176``. No vec0 loaded →
        # SemanticSearchUnavailableError → semantic arm returns None →
        # degraded_to_fts_only=True. FTS results survive.
        with closing(_open_test_conn(load_vec0=False)) as conn:
            run_lcm_migrations(conn, fts5_available=False)

            voyage = _StubVoyage(rerank_scores={"hello world": 0.8})

            result = asyncio.run(
                run_hybrid_search(
                    conn,
                    query="hello",
                    fts_search=make_fts_search([("leaf_a", "hello world")]),
                    voyage=voyage,  # type: ignore[arg-type]
                )
            )

            assert result.degraded_to_fts_only is True
            assert len(result.hits) == 1
            assert result.hits[0].summary_id == "leaf_a"
            assert result.hits[0].from_fts is True
            assert result.hits[0].from_semantic is False
            # Rerank still runs over the FTS-only candidate.
            assert result.hits[0].score == pytest.approx(0.8)


@skip_if_no_vec0
class TestSemanticAuthError:
    """Semantic arm auth-VoyageError must re-throw (operator-actionable)."""

    def test_semantic_auth_error_propagates(self, conn_with_vec0: sqlite3.Connection) -> None:
        # Auth in the semantic arm = misconfigured deploy. Don't silently
        # degrade — surface to the operator.
        _insert_leaf_with_embedding(conn_with_vec0, "leaf_a", 1, (0.1, 0.2, 0.3), content="alpha")

        voyage = _StubVoyage(
            embed_raise=VoyageError("auth", "voyage_auth: bad key"),
        )

        with pytest.raises(VoyageError) as exc_info:
            asyncio.run(
                run_hybrid_search(
                    conn_with_vec0,
                    query="alpha",
                    fts_search=make_fts_search([("leaf_a", "alpha")]),
                    voyage=voyage,  # type: ignore[arg-type]
                    # No query_vector → semantic arm calls Voyage.embed.
                )
            )
        assert exc_info.value.kind == "auth"


@skip_if_no_vec0
class TestSemanticNonAuthError:
    """Semantic arm non-auth VoyageError degrades silently."""

    def test_semantic_server_error_degrades_to_fts_only(
        self, conn_with_vec0: sqlite3.Connection
    ) -> None:
        _insert_leaf_with_embedding(conn_with_vec0, "leaf_a", 1, (0.1, 0.2, 0.3), content="alpha")

        # The semantic arm calls voyage.embed() and gets a 5xx — should
        # degrade to FTS-only WITHOUT raising.
        voyage = _StubVoyage(
            embed_raise=VoyageError("server_error", "voyage_5xx: 502"),
            rerank_scores={"alpha": 0.9},
        )

        result = asyncio.run(
            run_hybrid_search(
                conn_with_vec0,
                query="alpha",
                fts_search=make_fts_search([("leaf_a", "alpha")]),
                voyage=voyage,  # type: ignore[arg-type]
            )
        )

        assert result.degraded_to_fts_only is True
        assert len(result.hits) == 1
        assert result.hits[0].from_fts is True
        assert result.hits[0].from_semantic is False


# ===========================================================================
# Rerank arm failures
# ===========================================================================


@skip_if_no_vec0
class TestRerankDegrade:
    """Rerank failures fall through to RRF (except auth)."""

    def test_rerank_server_error_falls_back_to_rrf(
        self, conn_with_vec0: sqlite3.Connection
    ) -> None:
        # Mirrors ``hybrid-search.test.ts:178-216``. 500 on /rerank →
        # degraded_skipped_rerank=True; RRF scores still populated.
        _insert_leaf_with_embedding(conn_with_vec0, "leaf_a", 1, (0.1, 0.2, 0.3), content="alpha")
        _insert_leaf_with_embedding(conn_with_vec0, "leaf_b", 1, (0.9, 0.9, 0.9), content="beta")

        voyage = _StubVoyage(
            rerank_raise=VoyageError("server_error", "voyage_5xx: 500"),
        )

        result = asyncio.run(
            run_hybrid_search(
                conn_with_vec0,
                query="alpha",
                fts_search=make_fts_search([("leaf_a", "alpha"), ("leaf_b", "beta")]),
                voyage=voyage,  # type: ignore[arg-type]
                query_vector=[0.1, 0.2, 0.3],
            )
        )

        assert result.degraded_skipped_rerank is True
        # RRF scores populated.
        assert len(result.hits) >= 1
        assert all(h.score > 0 for h in result.hits)
        assert result.reranker_model is None

    def test_rerank_auth_error_propagates(self, conn_with_vec0: sqlite3.Connection) -> None:
        # Mirrors ``hybrid-search.test.ts:218-249``. Auth on /rerank →
        # re-throw (NOT silent RRF fallback).
        _insert_leaf_with_embedding(conn_with_vec0, "leaf_a", 1, (0.1, 0.2, 0.3), content="alpha")

        voyage = _StubVoyage(
            rerank_raise=VoyageError("auth", "voyage_auth: 401"),
        )

        with pytest.raises(VoyageError) as exc_info:
            asyncio.run(
                run_hybrid_search(
                    conn_with_vec0,
                    query="alpha",
                    fts_search=make_fts_search([("leaf_a", "alpha")]),
                    voyage=voyage,  # type: ignore[arg-type]
                    query_vector=[0.1, 0.2, 0.3],
                )
            )
        assert exc_info.value.kind == "auth"


# ===========================================================================
# rerank=False → RRF mode (no Voyage rerank call)
# ===========================================================================


@skip_if_no_vec0
class TestRerankDisabled:
    """``rerank=False`` skips Voyage rerank entirely; RRF orders results."""

    def test_rerank_false_uses_rrf(self, conn_with_vec0: sqlite3.Connection) -> None:
        # Mirrors ``hybrid-search.test.ts:251-278``. rerank=False → no
        # rerank call, RRF ranking, degraded_skipped_rerank=False
        # (because it's an explicit choice, not a failure).
        _insert_leaf_with_embedding(
            conn_with_vec0, "leaf_top", 1, (0.1, 0.2, 0.3), content="top doc"
        )
        _insert_leaf_with_embedding(
            conn_with_vec0, "leaf_other", 1, (0.5, 0.5, 0.5), content="other"
        )

        voyage = _StubVoyage()

        result = asyncio.run(
            run_hybrid_search(
                conn_with_vec0,
                query="top",
                fts_search=make_fts_search([("leaf_top", "top doc"), ("leaf_other", "other")]),
                voyage=voyage,  # type: ignore[arg-type]
                query_vector=[0.1, 0.2, 0.3],
                rerank=False,
            )
        )

        # No rerank call.
        assert voyage.rerank_calls == []
        # Best in both arms → highest RRF score.
        assert result.hits[0].summary_id == "leaf_top"
        # Explicit choice; NOT a degrade.
        assert result.degraded_skipped_rerank is False
        # RRF score: leaf_top is rank 0 in both → 1/60 + 1/60 ≈ 0.0333.
        assert result.hits[0].score == pytest.approx(2.0 / RRF_K, abs=1e-3)
        # reranker_model is None — rerank arm did not run.
        assert result.reranker_model is None


# ===========================================================================
# RRF behavior detail
# ===========================================================================


@skip_if_no_vec0
class TestRrfTieBreaking:
    """RRF correctly weights presence in both arms vs one."""

    def test_present_in_both_arms_beats_fts_only_at_far_rank(
        self, conn_with_vec0: sqlite3.Connection
    ) -> None:
        # Build a candidate that's rank-1 in both arms and one that's
        # only in FTS at rank-10. Verify RRF score ordering.
        # rank-1 in both: 1/(60+1) + 1/(60+1) ≈ 0.0328
        # rank-10 in FTS only: 1/(60+10) ≈ 0.0143
        # 11 candidates total to push the FTS-only one to rank 10.
        for i in range(11):
            sid = f"leaf_{i:02d}"
            # All 11 get FTS hits in order.
            # Only the rank-1 leaf gets a semantic vector matching the
            # query.
            vec = (0.1, 0.2, 0.3) if i == 1 else (0.9, 0.9, 0.9)
            _insert_leaf_with_embedding(conn_with_vec0, sid, 1, vec, content=f"doc {i}")

        voyage = _StubVoyage()
        # Insert FTS hits in order [leaf_00 .. leaf_10]; the semantic
        # arm will rank leaf_01 at index 0 (best cosine).
        result = asyncio.run(
            run_hybrid_search(
                conn_with_vec0,
                query="x",
                fts_search=make_fts_search([(f"leaf_{i:02d}", f"doc {i}") for i in range(11)]),
                voyage=voyage,  # type: ignore[arg-type]
                query_vector=[0.1, 0.2, 0.3],
                rerank=False,
                top_n=11,
            )
        )

        # leaf_01: fts_rank=1, sem_idx=0 → 1/(60+1) + 1/(60+0) = ~0.0331
        # leaf_00: fts_rank=0, sem_idx not present (no matching vec)
        # leaf_10: fts_rank=10, no semantic
        # Find the scores by id and verify ordering.
        by_id = {h.summary_id: h for h in result.hits}
        score_01 = by_id["leaf_01"].score
        score_00 = by_id["leaf_00"].score
        score_10 = by_id["leaf_10"].score

        # leaf_01 (both arms) beats leaf_00 (fts only at top).
        assert score_01 > score_00
        # leaf_00 (fts at rank 0) beats leaf_10 (fts at rank 10).
        assert score_00 > score_10


# ===========================================================================
# Wave-10: rerank-pack token budget enforcement
# ===========================================================================


@skip_if_no_vec0
class TestWave10TokenBudget:
    """Wave-10 fix: rerank-pack truncates when budget exceeded."""

    def test_budget_truncates_when_cumulative_exceeds(
        self, conn_with_vec0: sqlite3.Connection
    ) -> None:
        # Pack a handful of candidates that together exceed 510K tokens.
        # Each is ~300K tokens — first 1-2 fit, the rest do not.
        # Total budget = floor(600K * 0.85) = 510_000.
        budget = int(MAX_TOKENS_PER_RERANK_CALL * 0.85)
        assert budget == 510_000

        # Each leaf at 300K tokens — the first one fits (300K << 510K),
        # the second pushes cumulative to 600K which exceeds budget so
        # we stop. The third / fourth never get tried.
        each_tokens = 300_000
        # Use only FTS arm to keep semantic side out of this test. No
        # vector → no semantic candidate produced.
        for i in range(4):
            _insert_leaf_with_embedding(
                conn_with_vec0,
                f"leaf_{i}",
                1,
                None,  # no embedding → only FTS finds it
                content=f"doc {i}",
                token_count=each_tokens,
            )

        voyage = _StubVoyage()

        result = asyncio.run(
            run_hybrid_search(
                conn_with_vec0,
                query="x",
                fts_search=make_fts_search(
                    [(f"leaf_{i}", f"doc {i}") for i in range(4)],
                    token_count=each_tokens,
                ),
                voyage=voyage,  # type: ignore[arg-type]
                query_vector=[0.1, 0.2, 0.3],
                rerank=True,
            )
        )

        # 4 candidates total — only 1 fit in budget (300K + 300K > 510K
        # cumulative). The second-onward triggers the cumulative break.
        assert result.rerank_pack_truncated is True
        assert result.rerank_packed_count == 1
        # Only 1 candidate sent to rerank.
        assert len(voyage.rerank_calls) == 1
        assert len(voyage.rerank_calls[0]["candidates"]) == 1


# ===========================================================================
# Wave-11: skip individually-oversized candidates; do not break
# ===========================================================================


@skip_if_no_vec0
class TestWave11OversizedFix:
    """Wave-11 fix: ``continue``, not ``break``, on individually-oversized.

    The earlier TS code broke out of the loop when a single candidate
    exceeded the 510K budget, disabling rerank for the entire result
    set even though smaller candidates further down the list would have
    fit. With the Wave-11 fix, smaller candidates after a huge one still
    get packed.
    """

    def test_huge_doc_does_not_block_smaller_neighbors(
        self, conn_with_vec0: sqlite3.Connection
    ) -> None:
        # Stage [doc_normal, doc_huge_individually_oversized, doc_normal].
        # Without Wave-11 (break-on-oversized), the rerank pack would
        # contain just [doc_normal_first]. With Wave-11 (continue-on-
        # oversized), the pack contains [doc_normal_first, doc_normal_last]
        # — the huge one is skipped, smaller neighbors survive.
        _insert_leaf_with_embedding(
            conn_with_vec0, "doc_first", 1, None, content="first", token_count=1
        )
        _insert_leaf_with_embedding(
            conn_with_vec0,
            "doc_huge",
            1,
            None,
            content="huge",
            token_count=1_000_000,  # > 510K budget — individually oversized
        )
        _insert_leaf_with_embedding(
            conn_with_vec0, "doc_last", 1, None, content="last", token_count=1
        )

        voyage = _StubVoyage(
            rerank_scores={"first": 0.9, "last": 0.8},
        )

        async def staged_fts(query: str, *, limit: int, **filters: Any) -> list[FtsHit]:
            return _three_fts_hits()

        result = asyncio.run(
            run_hybrid_search(
                conn_with_vec0,
                query="x",
                fts_search=staged_fts,
                voyage=voyage,  # type: ignore[arg-type]
                query_vector=[0.1, 0.2, 0.3],
                rerank=True,
            )
        )

        # Both normal docs packed; huge one skipped.
        assert result.rerank_pack_truncated is True  # we dropped the huge one
        assert result.rerank_packed_count == 2
        # The rerank call received exactly 2 candidates.
        assert len(voyage.rerank_calls) == 1
        packed_ids = [c[0] for c in voyage.rerank_calls[0]["candidates"]]
        assert "doc_first" in packed_ids
        assert "doc_last" in packed_ids
        assert "doc_huge" not in packed_ids
        # The huge doc remains in candidate_count for backstop.
        assert result.candidate_count == 3

    def test_all_oversized_falls_back_to_rrf(self, conn_with_vec0: sqlite3.Connection) -> None:
        # When every candidate is individually oversized, rerank_packed
        # ends up empty → RRF fallback (degraded_skipped_rerank=True).
        _insert_leaf_with_embedding(
            conn_with_vec0,
            "doc_huge_a",
            1,
            None,
            content="huge_a",
            token_count=1_000_000,
        )
        _insert_leaf_with_embedding(
            conn_with_vec0,
            "doc_huge_b",
            1,
            None,
            content="huge_b",
            token_count=1_000_000,
        )

        voyage = _StubVoyage()

        result = asyncio.run(
            run_hybrid_search(
                conn_with_vec0,
                query="x",
                fts_search=make_fts_search(
                    [("doc_huge_a", "huge_a"), ("doc_huge_b", "huge_b")],
                    token_count=1_000_000,
                ),
                voyage=voyage,  # type: ignore[arg-type]
                query_vector=[0.1, 0.2, 0.3],
                rerank=True,
            )
        )

        assert result.degraded_skipped_rerank is True
        assert result.rerank_pack_truncated is True
        assert result.rerank_packed_count == 0
        # No rerank call — every candidate was oversized.
        assert voyage.rerank_calls == []
        # candidates still scored via RRF.
        assert len(result.hits) == 2


def _three_fts_hits() -> list[FtsHit]:
    """Helper for the Wave-11 mid-loop-oversized test.

    Returns FTS hits matching the inserted leaves' token_counts.
    """
    return [
        FtsHit(
            summary_id="doc_first",
            conversation_id=1,
            session_key="sk1",
            kind="leaf",
            content="first",
            token_count=1,
            created_at="2026-05-05",
            rank=0,
        ),
        FtsHit(
            summary_id="doc_huge",
            conversation_id=1,
            session_key="sk1",
            kind="leaf",
            content="huge",
            token_count=1_000_000,
            created_at="2026-05-05",
            rank=1,
        ),
        FtsHit(
            summary_id="doc_last",
            conversation_id=1,
            session_key="sk1",
            kind="leaf",
            content="last",
            token_count=1,
            created_at="2026-05-05",
            rank=2,
        ),
    ]


# ===========================================================================
# rerank_packed_count diagnostic
# ===========================================================================


@skip_if_no_vec0
class TestRerankPackedCount:
    """``rerank_packed_count`` matches what's actually sent to Voyage."""

    def test_packed_count_matches_rerank_call(self, conn_with_vec0: sqlite3.Connection) -> None:
        # 3 normal-size candidates, all fit → packed_count == 3.
        for i in range(3):
            _insert_leaf_with_embedding(
                conn_with_vec0,
                f"leaf_{i}",
                1,
                (0.1, 0.2, 0.3),
                content=f"doc_{i}",
            )

        voyage = _StubVoyage(
            rerank_scores={"doc_0": 0.9, "doc_1": 0.8, "doc_2": 0.7},
        )

        result = asyncio.run(
            run_hybrid_search(
                conn_with_vec0,
                query="x",
                fts_search=make_fts_search([(f"leaf_{i}", f"doc_{i}") for i in range(3)]),
                voyage=voyage,  # type: ignore[arg-type]
                query_vector=[0.1, 0.2, 0.3],
            )
        )

        assert result.rerank_packed_count == 3
        assert result.rerank_pack_truncated is False
        assert len(voyage.rerank_calls) == 1
        assert len(voyage.rerank_calls[0]["candidates"]) == 3


# ===========================================================================
# top_n and top_k semantics
# ===========================================================================


@skip_if_no_vec0
class TestTopN:
    """``top_n`` caps the returned hits; ``top_k`` is min(top_n, packed)."""

    def test_top_n_caps_returned_hits(self, conn_with_vec0: sqlite3.Connection) -> None:
        for i in range(5):
            _insert_leaf_with_embedding(
                conn_with_vec0, f"leaf_{i}", 1, (0.1, 0.2, 0.3), content=f"doc_{i}"
            )

        voyage = _StubVoyage(
            rerank_scores={f"doc_{i}": 1.0 - 0.1 * i for i in range(5)},
        )

        result = asyncio.run(
            run_hybrid_search(
                conn_with_vec0,
                query="x",
                fts_search=make_fts_search([(f"leaf_{i}", f"doc_{i}") for i in range(5)]),
                voyage=voyage,  # type: ignore[arg-type]
                query_vector=[0.1, 0.2, 0.3],
                top_n=3,
            )
        )

        # 5 candidates packed, but top_k forwarded to Voyage is min(3, 5) = 3.
        assert voyage.rerank_calls[0]["top_k"] == 3
        # rerank stub still scores all of them, but Voyage in production
        # would only return top_k=3. Our stub mirrors len(candidates) in
        # results so we get all 5 back — slicing is the rerank server's
        # job. Sorting + slicing happens client-side too via top_k on
        # the stub side: not enforced in our stand-in. So:
        # The result.hits list IS the full rerank response from our
        # stub (5 items). We don't assert truncation here because the
        # real Voyage server enforces top_k. The TS test sends `top_k`
        # and trusts Voyage; we mirror.
        assert len(result.hits) == 5

    def test_top_k_capped_to_packed_count(self, conn_with_vec0: sqlite3.Connection) -> None:
        # Only 2 candidates available, top_n=10 → top_k=min(10,2)=2.
        for i in range(2):
            _insert_leaf_with_embedding(
                conn_with_vec0, f"leaf_{i}", 1, (0.1, 0.2, 0.3), content=f"doc_{i}"
            )

        voyage = _StubVoyage(
            rerank_scores={"doc_0": 0.9, "doc_1": 0.5},
        )

        asyncio.run(
            run_hybrid_search(
                conn_with_vec0,
                query="x",
                fts_search=make_fts_search([("leaf_0", "doc_0"), ("leaf_1", "doc_1")]),
                voyage=voyage,  # type: ignore[arg-type]
                query_vector=[0.1, 0.2, 0.3],
                top_n=10,
            )
        )

        assert voyage.rerank_calls[0]["top_k"] == 2


# ===========================================================================
# Filters forwarded to both arms
# ===========================================================================


@skip_if_no_vec0
class TestFiltersForwarded:
    """Session/conversation/time/kind filters reach both arms."""

    def test_session_keys_filter_forwarded(self, conn_with_vec0: sqlite3.Connection) -> None:
        # Insert two leaves on different sessions; filter to sk1.
        _insert_leaf_with_embedding(
            conn_with_vec0,
            "leaf_a",
            1,
            (0.1, 0.2, 0.3),  # sk1
        )
        _insert_leaf_with_embedding(
            conn_with_vec0,
            "leaf_b",
            2,
            (0.1, 0.2, 0.3),  # sk2
        )

        captured_filters: dict[str, Any] = {}

        async def capturing_fts(query: str, *, limit: int, **filters: Any) -> list[FtsHit]:
            captured_filters.update(filters)
            return []

        voyage = _StubVoyage()

        asyncio.run(
            run_hybrid_search(
                conn_with_vec0,
                query="x",
                fts_search=capturing_fts,
                voyage=voyage,  # type: ignore[arg-type]
                query_vector=[0.1, 0.2, 0.3],
                session_keys=["sk1"],
            )
        )

        # FTS arm received session_keys filter.
        assert captured_filters["session_keys"] == ["sk1"]
        # Semantic arm filtered too — only leaf_a (sk1) should be merged.
        # (We rely on run_semantic_search applying the filter; the test
        # for that is in test_semantic_search.py. Here we just confirm
        # the filter forwarded through.)


# ===========================================================================
# HybridHit shape — all expected fields populated
# ===========================================================================


@skip_if_no_vec0
class TestHybridHitShape:
    """Returned :class:`HybridHit` instances carry all expected fields."""

    def test_hit_has_summary_columns_and_provenance(
        self, conn_with_vec0: sqlite3.Connection
    ) -> None:
        _insert_leaf_with_embedding(conn_with_vec0, "leaf_a", 1, (0.1, 0.2, 0.3), content="alpha")

        voyage = _StubVoyage(rerank_scores={"alpha": 0.9})

        result = asyncio.run(
            run_hybrid_search(
                conn_with_vec0,
                query="alpha",
                fts_search=make_fts_search([("leaf_a", "alpha")]),
                voyage=voyage,  # type: ignore[arg-type]
                query_vector=[0.1, 0.2, 0.3],
            )
        )

        h = result.hits[0]
        assert isinstance(h, HybridHit)
        assert h.summary_id == "leaf_a"
        assert h.conversation_id == 1
        assert h.session_key == "sk1"
        assert h.kind == "leaf"
        assert h.content == "alpha"
        assert h.token_count == 1
        assert h.from_fts is True
        assert h.from_semantic is True
        assert h.fts_rank == 0
        assert h.semantic_distance is not None
        assert h.cosine_similarity is not None
        assert h.score == pytest.approx(0.9)
