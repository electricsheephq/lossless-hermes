"""Tests for :mod:`lossless_hermes.tools.grep` — Wave B modes (hybrid/semantic).

Mirrors ``lossless-claw/test/lcm-grep-tool-hybrid.test.ts`` (419 LOC TS →
~330 pytest LOC). Covers:

* **Hybrid happy path** — both arms return rows, RRF-fuse + rerank produces
  expected order, provenance tags ``[from FTS+semantic]`` /
  ``[from FTS only]`` / ``[from semantic only]`` correct.
* **Voyage rerank 5xx** → ``degraded_skipped_rerank=True`` with RRF-only
  result.
* **vec0 absent** → ``degraded_to_fts_only=True``.
* **summaryKinds=["leaf"]** / **["condensed"]** filter restricts hits.
* **Semantic mode happy path** with Voyage embed call.
* **VOYAGE_API_KEY missing** (ctx.voyage = None) → operator-facing error
  with the ``mode='full_text'`` fallback hint (hybrid) /
  ``mode='regex'``/``mode='full_text'`` (semantic).
* **Voyage auth error** in either mode → operator-facing error (not silent
  degrade).

Source pin: ``lossless-claw`` at commit ``1f07fbd`` on branch ``pr-613``.

Voyage is mocked at the :class:`VoyageClient` surface via a tiny stand-in
that records embed + rerank calls and lets us drive each branch of the
degradation matrix without an outbound API.

vec0-dependent tests are gated on the extension being loadable on this
Python build via :data:`VEC0_AVAILABLE` / :data:`skip_if_no_vec0`. The
gating pattern mirrors ``tests/embeddings/test_hybrid_search.py``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterator

import pytest

from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.embeddings.store import (
    ensure_embeddings_table,
    record_embedding,
    register_embedding_profile,
)
from lossless_hermes.store.conversation import (
    ConversationStore,
    CreateConversationInput,
)
from lossless_hermes.store.summary import (
    CreateSummaryInput,
    SummaryStore,
)
from lossless_hermes.tools.conversation_scope import LcmDependencies
from lossless_hermes.tools.grep import handle_lcm_grep
from lossless_hermes.voyage.client import (
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
    reason="sqlite-vec extension not loadable on this Python build.",
)


# ---------------------------------------------------------------------------
# Voyage stand-in — controllable embed() + rerank()
# ---------------------------------------------------------------------------


@dataclass
class _StubVoyage:
    """Tiny stand-in supplying ``embed`` and ``rerank`` for the tests.

    Mirrors the stub in ``tests/embeddings/test_hybrid_search.py`` but
    duplicated here so the Wave-B tests are self-contained.
    """

    embed_vector: list[float] = field(default_factory=lambda: [0.1, 0.2, 0.3])
    rerank_scores: dict[str, float] = field(default_factory=dict)
    rerank_raise: Exception | None = None
    embed_raise: Exception | None = None
    embed_calls: list[dict[str, Any]] = field(default_factory=list)
    rerank_calls: list[dict[str, Any]] = field(default_factory=list)

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
        if self.embed_raise is not None:
            raise self.embed_raise
        return EmbedResult(
            vectors=[list(self.embed_vector)],
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
        if self.rerank_raise is not None:
            raise self.rerank_raise
        items: list[RerankItem] = []
        for idx, (sid, content) in enumerate(candidates):
            score = self.rerank_scores.get(content, 0.1)
            items.append(RerankItem(id=sid, index=idx, score=score))
        items.sort(key=lambda x: x.score, reverse=True)
        return RerankResult(results=items, total_tokens=100, model=model)


# ---------------------------------------------------------------------------
# Test context (mirrors GrepContext)
# ---------------------------------------------------------------------------


@dataclass
class _Ctx:
    """Concrete :class:`GrepContext` for Wave-B tests.

    ``voyage`` is a :class:`_StubVoyage` (or ``None`` for the missing-key
    tests). Real :class:`VoyageClient` is not constructed — the stub
    duck-types it.

    ``embeddings_enabled`` defaults to ``True`` (ADR-033): Wave-B tests
    exercise the *post-opt-in* hybrid/semantic dispatch. The off-by-default
    refusal is covered by the dedicated ADR-033 gate tests, which pass
    ``embeddings_enabled=False`` explicitly.
    """

    conn: sqlite3.Connection
    summary_store: SummaryStore
    conversation_store: ConversationStore
    timezone: str = "UTC"
    voyage: object | None = None
    embeddings_enabled: bool = True  # ADR-033 — see class docstring


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _open_test_conn(*, load_vec0: bool) -> sqlite3.Connection:
    """Open ``:memory:`` SQLite + optional vec0 load.

    ``row_factory = sqlite3.Row`` is required by :class:`SummaryStore` —
    set here so the fixtures don't drift apart.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    if load_vec0:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@pytest.fixture
def db_no_vec0() -> Iterator[sqlite3.Connection]:
    """In-memory DB without vec0 — exercises the FTS-only degrade path."""
    conn = _open_test_conn(load_vec0=False)
    try:
        run_lcm_migrations(conn, fts5_available=True, seed_default_prompts=False)
        yield conn
    finally:
        conn.close()


@pytest.fixture
def db_with_vec0() -> Iterator[sqlite3.Connection]:
    """In-memory DB with vec0 loaded + dim=3 profile + embeddings table."""
    conn = _open_test_conn(load_vec0=True)
    try:
        run_lcm_migrations(conn, fts5_available=True, seed_default_prompts=False)
        register_embedding_profile(conn, "voyage-4-large", 3)
        ensure_embeddings_table(conn, "voyage-4-large", 3)
        conn.commit()
        yield conn
    finally:
        conn.close()


@pytest.fixture
def conv_id_no_vec0(db_no_vec0: sqlite3.Connection) -> int:
    """Seeded conversation_id in db_no_vec0. Commits to clear the
    auto-wrapped write tx so subsequent network calls (Voyage) don't
    violate the §0 invariant."""
    store = ConversationStore(db_no_vec0, fts5_available=True)
    rec = store.create_conversation(
        CreateConversationInput(session_id="s1", session_key="agent:main:main", title="t"),
    )
    db_no_vec0.commit()
    return rec.conversation_id


@pytest.fixture
def conv_id_with_vec0(db_with_vec0: sqlite3.Connection) -> int:
    """Seeded conversation_id in db_with_vec0. Commits before yielding
    so the §0 invariant (no-open-tx-around-Voyage) holds at handler call
    time. See :func:`lossless_hermes.concurrency.model.assert_no_open_tx`.
    """
    store = ConversationStore(db_with_vec0, fts5_available=True)
    rec = store.create_conversation(
        CreateConversationInput(session_id="s1", session_key="agent:main:main", title="t"),
    )
    db_with_vec0.commit()
    return rec.conversation_id


@pytest.fixture
def deps() -> LcmDependencies:
    return LcmDependencies(resolve_session_id_from_session_key=lambda _k: None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_summary_with_embedding(
    db: sqlite3.Connection,
    *,
    summary_id: str,
    conv_id: int,
    content: str,
    kind: str = "leaf",
    vector: tuple[float, float, float] | None = None,
    token_count: int = 1,
) -> None:
    """Insert a summary row + populate FTS5 + (optionally) its vec0 embedding.

    FTS5 has NO triggers — the application layer is responsible for
    keeping ``summaries_fts`` in sync (see ``db/migration.py`` line 1391).
    Tests that exercise FTS-side hits MUST populate the FTS table
    manually.
    """
    db.execute(
        "INSERT INTO summaries (summary_id, conversation_id, kind, content, "
        "                       token_count, session_key) "
        "VALUES (?, ?, ?, ?, ?, "
        "        (SELECT session_key FROM conversations "
        "         WHERE conversation_id = ?))",
        (summary_id, conv_id, kind, content, token_count, conv_id),
    )
    # Populate FTS5 — required because the migration ladder deliberately
    # has no triggers (population is the application's responsibility).
    db.execute(
        "INSERT INTO summaries_fts(summary_id, content) VALUES (?, ?)",
        (summary_id, content),
    )
    if vector is not None:
        record_embedding(
            db,
            model_name="voyage-4-large",
            embedded_id=summary_id,
            embedded_kind="summary",
            vector=list(vector),
            source_token_count=1,
        )
    db.commit()


def _insert_summary_no_embedding(
    summary_store: SummaryStore,
    db: sqlite3.Connection,
    *,
    summary_id: str,
    conv_id: int,
    content: str,
    kind: str = "leaf",
    token_count: int = 1,
) -> None:
    """Insert a summary via the store (FTS5-indexed) — no embedding row.

    Commits at the end so subsequent Voyage calls don't trip the §0
    invariant. The store doesn't commit on its own — that's a caller
    responsibility per LCM convention.
    """
    summary_store.insert_summary(
        CreateSummaryInput(
            summary_id=summary_id,
            conversation_id=conv_id,
            kind=kind,  # type: ignore[arg-type]
            content=content,
            token_count=token_count,
        )
    )
    db.commit()


def _call(
    args: dict[str, Any],
    *,
    ctx: _Ctx,
    deps: LcmDependencies,
    session_key: str = "agent:main:main",
) -> dict[str, Any]:
    """Invoke :func:`handle_lcm_grep` and parse the JSON result."""
    raw = handle_lcm_grep(
        args,
        ctx=ctx,
        deps=deps,
        session_key=session_key,
        session_id=None,
    )
    return json.loads(raw)


# ===========================================================================
# Missing VOYAGE_API_KEY — operator-facing error with fallback hint
# ===========================================================================


class TestMissingVoyageKey:
    """When ctx.voyage is None, hybrid + semantic refuse with the AC error."""

    def test_hybrid_without_voyage_returns_missing_key_error(
        self,
        db_no_vec0: sqlite3.Connection,
        conv_id_no_vec0: int,
        deps: LcmDependencies,
    ) -> None:
        """TS line 631-635 — fallback hint is load-bearing for agent retry."""
        del conv_id_no_vec0
        store = ConversationStore(db_no_vec0, fts5_available=True)
        sstore = SummaryStore(db_no_vec0, fts5_available=True, trigram_tokenizer_available=False)
        ctx = _Ctx(conn=db_no_vec0, summary_store=sstore, conversation_store=store, voyage=None)
        result = _call({"pattern": "race", "mode": "hybrid"}, ctx=ctx, deps=deps)
        assert "VOYAGE_API_KEY" in result["error"]
        assert "hybrid mode requires it" in result["error"]
        assert "mode='full_text'" in result["error"]

    def test_semantic_without_voyage_returns_missing_key_error(
        self,
        db_no_vec0: sqlite3.Connection,
        conv_id_no_vec0: int,
        deps: LcmDependencies,
    ) -> None:
        """TS line 825-828 — semantic fallback hint is regex/full_text."""
        del conv_id_no_vec0
        store = ConversationStore(db_no_vec0, fts5_available=True)
        sstore = SummaryStore(db_no_vec0, fts5_available=True, trigram_tokenizer_available=False)
        ctx = _Ctx(conn=db_no_vec0, summary_store=sstore, conversation_store=store, voyage=None)
        result = _call({"pattern": "race", "mode": "semantic"}, ctx=ctx, deps=deps)
        assert "VOYAGE_API_KEY" in result["error"]
        assert "semantic mode requires it" in result["error"]
        assert "mode='regex'" in result["error"]
        assert "mode='full_text'" in result["error"]


# ===========================================================================
# ADR-033 (#133) — embeddings opt-in gate: hybrid/semantic OFF by default
# ===========================================================================


class TestEmbeddingsOptInGate:
    """ADR-033: ``hybrid`` / ``semantic`` are opt-in and OFF by default.

    With ``ctx.embeddings_enabled`` False (the production default), both
    modes are refused with an operator-actionable error — and crucially,
    the refusal happens *before* the missing-Voyage-key path, so a keyless
    install gets one coherent "opt-in required" message instead of the
    hard-fail-then-recover behavior ADR-033 set out to remove.
    """

    def test_hybrid_refused_when_embeddings_disabled(
        self,
        db_no_vec0: sqlite3.Connection,
        conv_id_no_vec0: int,
        deps: LcmDependencies,
    ) -> None:
        """``mode='hybrid'`` with ``embeddings_enabled=False`` → disabled error."""
        del conv_id_no_vec0
        store = ConversationStore(db_no_vec0, fts5_available=True)
        sstore = SummaryStore(db_no_vec0, fts5_available=True, trigram_tokenizer_available=False)
        # voyage is a stub here — proves the gate fires even WITH a usable
        # client, because the flag (not the key) is what is missing.
        ctx = _Ctx(
            conn=db_no_vec0,
            summary_store=sstore,
            conversation_store=store,
            voyage=_StubVoyage(),
            embeddings_enabled=False,
        )
        result = _call({"pattern": "race", "mode": "hybrid"}, ctx=ctx, deps=deps)
        assert "hybrid mode is disabled" in result["error"]
        # Names the opt-in mechanism (ADR-033) and the working fallback.
        assert "embeddings_enabled" in result["error"]
        assert "ADR-033" in result["error"]
        assert "mode='full_text'" in result["error"]

    def test_semantic_refused_when_embeddings_disabled(
        self,
        db_no_vec0: sqlite3.Connection,
        conv_id_no_vec0: int,
        deps: LcmDependencies,
    ) -> None:
        """``mode='semantic'`` with ``embeddings_enabled=False`` → disabled error."""
        del conv_id_no_vec0
        store = ConversationStore(db_no_vec0, fts5_available=True)
        sstore = SummaryStore(db_no_vec0, fts5_available=True, trigram_tokenizer_available=False)
        ctx = _Ctx(
            conn=db_no_vec0,
            summary_store=sstore,
            conversation_store=store,
            voyage=_StubVoyage(),
            embeddings_enabled=False,
        )
        result = _call({"pattern": "race", "mode": "semantic"}, ctx=ctx, deps=deps)
        assert "semantic mode is disabled" in result["error"]
        assert "embeddings_enabled" in result["error"]
        assert "ADR-033" in result["error"]
        assert "mode='full_text'" in result["error"]

    def test_gate_fires_before_missing_voyage_key_path(
        self,
        db_no_vec0: sqlite3.Connection,
        conv_id_no_vec0: int,
        deps: LcmDependencies,
    ) -> None:
        """ADR-033 ordering: the disabled-embeddings gate is checked BEFORE
        the missing-Voyage-key check.

        A keyless install (``voyage=None``) with embeddings off must get the
        "disabled / opt-in" message — NOT the "VOYAGE_API_KEY missing"
        message. This is the whole point of ADR-033: the agent never even
        reaches the keyless hard-fail path by default.
        """
        del conv_id_no_vec0
        store = ConversationStore(db_no_vec0, fts5_available=True)
        sstore = SummaryStore(db_no_vec0, fts5_available=True, trigram_tokenizer_available=False)
        ctx = _Ctx(
            conn=db_no_vec0,
            summary_store=sstore,
            conversation_store=store,
            voyage=None,  # keyless
            embeddings_enabled=False,  # and not opted in
        )
        for mode in ("hybrid", "semantic"):
            result = _call({"pattern": "race", "mode": mode}, ctx=ctx, deps=deps)
            # The ADR-033 disabled message, not the TS missing-key message.
            assert f"{mode} mode is disabled" in result["error"]
            assert "VOYAGE_API_KEY" not in result["error"]
            assert f"{mode} mode requires it" not in result["error"]

    def test_enabled_flag_bypasses_gate_and_reaches_voyage_path(
        self,
        db_no_vec0: sqlite3.Connection,
        conv_id_no_vec0: int,
        deps: LcmDependencies,
    ) -> None:
        """With ``embeddings_enabled=True`` the gate is a no-op: dispatch
        proceeds to the real hybrid/semantic path.

        Proven here via the keyless (``voyage=None``) downstream behavior —
        the opted-in-but-keyless install correctly reaches the
        missing-Voyage-key error (TS line 631 / 825), confirming the
        ADR-033 gate did NOT short-circuit.
        """
        del conv_id_no_vec0
        store = ConversationStore(db_no_vec0, fts5_available=True)
        sstore = SummaryStore(db_no_vec0, fts5_available=True, trigram_tokenizer_available=False)
        ctx = _Ctx(
            conn=db_no_vec0,
            summary_store=sstore,
            conversation_store=store,
            voyage=None,
            embeddings_enabled=True,  # opted in
        )
        hybrid = _call({"pattern": "race", "mode": "hybrid"}, ctx=ctx, deps=deps)
        assert "hybrid mode requires it" in hybrid["error"]
        assert "is disabled" not in hybrid["error"]
        semantic = _call({"pattern": "race", "mode": "semantic"}, ctx=ctx, deps=deps)
        assert "semantic mode requires it" in semantic["error"]
        assert "is disabled" not in semantic["error"]

    def test_non_embedding_modes_unaffected_when_embeddings_disabled(
        self,
        db_no_vec0: sqlite3.Connection,
        conv_id_no_vec0: int,
        deps: LcmDependencies,
    ) -> None:
        """The gate touches ONLY hybrid/semantic — ``regex`` / ``full_text``
        / ``verbatim`` run normally with embeddings off (ADR-033 invariant:
        the keyless FTS path is always functional and always the default).
        """
        del conv_id_no_vec0
        store = ConversationStore(db_no_vec0, fts5_available=True)
        sstore = SummaryStore(db_no_vec0, fts5_available=True, trigram_tokenizer_available=False)
        ctx = _Ctx(
            conn=db_no_vec0,
            summary_store=sstore,
            conversation_store=store,
            voyage=None,
            embeddings_enabled=False,
        )
        for mode in ("regex", "full_text", "verbatim"):
            result = _call({"pattern": "race", "mode": mode}, ctx=ctx, deps=deps)
            # No error key — the FTS-family modes complete (empty corpus is
            # fine; the point is they are NOT gated).
            assert "error" not in result, f"mode={mode} should not be gated"


# ===========================================================================
# Hybrid mode — degrades to FTS-only when vec0 missing
# ===========================================================================


class TestHybridDegradeToFtsOnly:
    """No vec0 + Voyage stub → degraded_to_fts_only=True + FTS rerank still
    runs (single arm only)."""

    def test_no_vec0_degrades_to_fts_only(
        self,
        db_no_vec0: sqlite3.Connection,
        conv_id_no_vec0: int,
        deps: LcmDependencies,
    ) -> None:
        """Mirrors hybrid-search.test.ts:159-176 + lcm-grep-tool-hybrid.test
        AC: vec0 absent → semantic arm degrades; FTS arm still runs."""
        sstore = SummaryStore(
            db_no_vec0,
            fts5_available=True,
            trigram_tokenizer_available=False,
        )
        store = ConversationStore(db_no_vec0, fts5_available=True)
        _insert_summary_no_embedding(
            sstore,
            db_no_vec0,
            summary_id="leaf_a",
            conv_id=conv_id_no_vec0,
            content="alpha doc",
        )
        voyage = _StubVoyage(rerank_scores={"alpha doc": 0.8})
        ctx = _Ctx(conn=db_no_vec0, summary_store=sstore, conversation_store=store, voyage=voyage)
        result = _call(
            {"pattern": "alpha", "mode": "hybrid"},
            ctx=ctx,
            deps=deps,
        )
        # Tool reports degraded; rerank STILL runs over the FTS-only
        # candidate (semantic arm is what degraded, not the rerank arm).
        details = result["details"]
        assert details["mode"] == "hybrid"
        assert details["degradedToFtsOnly"] is True
        assert details["degradedSkippedRerank"] is False
        assert details["totalMatches"] == 1
        # Markdown body announces the degrade.
        assert "(semantic search unavailable; degraded to FTS-only)" in result["text"]


# ===========================================================================
# Hybrid mode — happy path + provenance tags (vec0-gated)
# ===========================================================================


@skip_if_no_vec0
class TestHybridHappyPath:
    """Both arms hit; rerank fuses; provenance tags reflect which arm hit."""

    def test_merges_arms_and_provenance_correct(
        self,
        db_with_vec0: sqlite3.Connection,
        conv_id_with_vec0: int,
        deps: LcmDependencies,
    ) -> None:
        """Hybrid: alpha doc hits both arms, beta only semantic, gamma only
        FTS. After rerank we get tags ``[from FTS+semantic]`` /
        ``[from semantic only]`` / ``[from FTS only]``."""
        sstore = SummaryStore(
            db_with_vec0,
            fts5_available=True,
            trigram_tokenizer_available=False,
        )
        store = ConversationStore(db_with_vec0, fts5_available=True)
        # alpha_doc — FTS-matchable + semantic-matchable
        _insert_summary_with_embedding(
            db_with_vec0,
            summary_id="leaf_a",
            conv_id=conv_id_with_vec0,
            content="alpha and zeta",
            vector=(0.1, 0.2, 0.3),  # identical to query vector
        )
        # beta_doc — only semantic (no "alpha" word so FTS misses)
        _insert_summary_with_embedding(
            db_with_vec0,
            summary_id="leaf_b",
            conv_id=conv_id_with_vec0,
            content="zeta beta",
            vector=(0.1, 0.2, 0.3),  # same vector to ensure semantic hit
        )
        voyage = _StubVoyage(
            embed_vector=[0.1, 0.2, 0.3],
            rerank_scores={"alpha and zeta": 0.95, "zeta beta": 0.30},
        )
        ctx = _Ctx(conn=db_with_vec0, summary_store=sstore, conversation_store=store, voyage=voyage)
        result = _call(
            {"pattern": "alpha", "mode": "hybrid"},
            ctx=ctx,
            deps=deps,
        )

        details = result["details"]
        assert details["mode"] == "hybrid"
        assert details["degradedToFtsOnly"] is False
        assert details["degradedSkippedRerank"] is False
        # Voyage embed was called once for the query.
        assert len(voyage.embed_calls) == 1
        # Rerank was called.
        assert len(voyage.rerank_calls) == 1

        # Top hit is leaf_a (best rerank score) and is from both arms.
        assert details["hits"][0]["summaryId"] == "leaf_a"
        assert details["hits"][0]["fromFts"] is True
        assert details["hits"][0]["fromSemantic"] is True
        # Markdown text carries the provenance tag.
        assert "[from FTS+semantic]" in result["text"]

    def test_fts_only_provenance_tag_emitted(
        self,
        db_with_vec0: sqlite3.Connection,
        conv_id_with_vec0: int,
        deps: LcmDependencies,
    ) -> None:
        """A hit returned only by FTS (no embedding) gets [from FTS only]."""
        sstore = SummaryStore(
            db_with_vec0,
            fts5_available=True,
            trigram_tokenizer_available=False,
        )
        store = ConversationStore(db_with_vec0, fts5_available=True)
        # No embedding — only FTS will find it.
        _insert_summary_with_embedding(
            db_with_vec0,
            summary_id="leaf_x",
            conv_id=conv_id_with_vec0,
            content="kappa unique",
            vector=None,
        )
        voyage = _StubVoyage(
            embed_vector=[0.9, 0.9, 0.9],  # query vector doesn't match anything
            rerank_scores={"kappa unique": 0.7},
        )
        ctx = _Ctx(conn=db_with_vec0, summary_store=sstore, conversation_store=store, voyage=voyage)
        result = _call(
            {"pattern": "kappa", "mode": "hybrid"},
            ctx=ctx,
            deps=deps,
        )
        details = result["details"]
        assert details["totalMatches"] == 1
        assert details["hits"][0]["fromFts"] is True
        assert details["hits"][0]["fromSemantic"] is False
        assert "[from FTS only]" in result["text"]


# ===========================================================================
# Rerank failure (non-auth) → RRF-only with degraded_skipped_rerank=True
# ===========================================================================


@skip_if_no_vec0
class TestHybridRerankDegrade:
    """Voyage rerank 5xx → fall through to RRF; preserves auth-propagation."""

    def test_rerank_server_error_falls_back_to_rrf(
        self,
        db_with_vec0: sqlite3.Connection,
        conv_id_with_vec0: int,
        deps: LcmDependencies,
    ) -> None:
        """500 on /rerank → degraded_skipped_rerank=True; RRF still scores."""
        sstore = SummaryStore(
            db_with_vec0,
            fts5_available=True,
            trigram_tokenizer_available=False,
        )
        store = ConversationStore(db_with_vec0, fts5_available=True)
        _insert_summary_with_embedding(
            db_with_vec0,
            summary_id="leaf_a",
            conv_id=conv_id_with_vec0,
            content="alpha doc",
            vector=(0.1, 0.2, 0.3),
        )
        voyage = _StubVoyage(
            embed_vector=[0.1, 0.2, 0.3],
            rerank_raise=VoyageError("server_error", "voyage_5xx: 500"),
        )
        ctx = _Ctx(conn=db_with_vec0, summary_store=sstore, conversation_store=store, voyage=voyage)
        result = _call(
            {"pattern": "alpha", "mode": "hybrid"},
            ctx=ctx,
            deps=deps,
        )
        details = result["details"]
        assert details["degradedSkippedRerank"] is True
        # RRF still produces a result.
        assert details["totalMatches"] >= 1
        # Markdown announces the rerank degrade.
        assert "rerank failed; using RRF fusion fallback" in result["text"]

    def test_rerank_auth_error_propagates_as_missing_key(
        self,
        db_with_vec0: sqlite3.Connection,
        conv_id_with_vec0: int,
        deps: LcmDependencies,
    ) -> None:
        """Rerank auth-VoyageError → operator-facing error (NOT silent RRF)."""
        sstore = SummaryStore(
            db_with_vec0,
            fts5_available=True,
            trigram_tokenizer_available=False,
        )
        store = ConversationStore(db_with_vec0, fts5_available=True)
        _insert_summary_with_embedding(
            db_with_vec0,
            summary_id="leaf_a",
            conv_id=conv_id_with_vec0,
            content="alpha doc",
            vector=(0.1, 0.2, 0.3),
        )
        voyage = _StubVoyage(
            embed_vector=[0.1, 0.2, 0.3],
            rerank_raise=VoyageError("auth", "voyage_auth: 401"),
        )
        ctx = _Ctx(conn=db_with_vec0, summary_store=sstore, conversation_store=store, voyage=voyage)
        result = _call(
            {"pattern": "alpha", "mode": "hybrid"},
            ctx=ctx,
            deps=deps,
        )
        assert "VOYAGE_API_KEY" in result["error"]
        assert "hybrid mode requires it" in result["error"]


# ===========================================================================
# Hybrid mode — summaryKinds filter (W1A5 P1 / TS lines 283-289 / 617-620)
# ===========================================================================


@skip_if_no_vec0
class TestHybridSummaryKindsFilter:
    """summaryKinds filter restricts hits to the named kinds."""

    def test_leaf_only_excludes_condensed(
        self,
        db_with_vec0: sqlite3.Connection,
        conv_id_with_vec0: int,
        deps: LcmDependencies,
    ) -> None:
        sstore = SummaryStore(
            db_with_vec0,
            fts5_available=True,
            trigram_tokenizer_available=False,
        )
        store = ConversationStore(db_with_vec0, fts5_available=True)
        _insert_summary_with_embedding(
            db_with_vec0,
            summary_id="leaf_a",
            conv_id=conv_id_with_vec0,
            content="alpha leaf doc",
            kind="leaf",
            vector=(0.1, 0.2, 0.3),
        )
        _insert_summary_with_embedding(
            db_with_vec0,
            summary_id="cond_a",
            conv_id=conv_id_with_vec0,
            content="alpha condensed doc",
            kind="condensed",
            vector=(0.1, 0.2, 0.3),
        )
        voyage = _StubVoyage(
            embed_vector=[0.1, 0.2, 0.3],
            rerank_scores={"alpha leaf doc": 0.9, "alpha condensed doc": 0.8},
        )
        ctx = _Ctx(conn=db_with_vec0, summary_store=sstore, conversation_store=store, voyage=voyage)
        result = _call(
            {"pattern": "alpha", "mode": "hybrid", "summaryKinds": ["leaf"]},
            ctx=ctx,
            deps=deps,
        )
        details = result["details"]
        # Only the leaf-kind summary survives the filter.
        ids = [h["summaryId"] for h in details["hits"]]
        assert "leaf_a" in ids
        assert "cond_a" not in ids

    def test_condensed_only_excludes_leaf(
        self,
        db_with_vec0: sqlite3.Connection,
        conv_id_with_vec0: int,
        deps: LcmDependencies,
    ) -> None:
        sstore = SummaryStore(
            db_with_vec0,
            fts5_available=True,
            trigram_tokenizer_available=False,
        )
        store = ConversationStore(db_with_vec0, fts5_available=True)
        _insert_summary_with_embedding(
            db_with_vec0,
            summary_id="leaf_a",
            conv_id=conv_id_with_vec0,
            content="alpha leaf doc",
            kind="leaf",
            vector=(0.1, 0.2, 0.3),
        )
        _insert_summary_with_embedding(
            db_with_vec0,
            summary_id="cond_a",
            conv_id=conv_id_with_vec0,
            content="alpha condensed doc",
            kind="condensed",
            vector=(0.1, 0.2, 0.3),
        )
        voyage = _StubVoyage(
            embed_vector=[0.1, 0.2, 0.3],
            rerank_scores={"alpha leaf doc": 0.9, "alpha condensed doc": 0.8},
        )
        ctx = _Ctx(conn=db_with_vec0, summary_store=sstore, conversation_store=store, voyage=voyage)
        result = _call(
            {"pattern": "alpha", "mode": "hybrid", "summaryKinds": ["condensed"]},
            ctx=ctx,
            deps=deps,
        )
        details = result["details"]
        ids = [h["summaryId"] for h in details["hits"]]
        assert "cond_a" in ids
        assert "leaf_a" not in ids


# ===========================================================================
# Semantic mode — happy path
# ===========================================================================


@skip_if_no_vec0
class TestSemanticHappyPath:
    """Semantic mode: pure KNN, no rerank call, returns ranked hits."""

    def test_semantic_returns_hits_no_rerank(
        self,
        db_with_vec0: sqlite3.Connection,
        conv_id_with_vec0: int,
        deps: LcmDependencies,
    ) -> None:
        sstore = SummaryStore(
            db_with_vec0,
            fts5_available=True,
            trigram_tokenizer_available=False,
        )
        store = ConversationStore(db_with_vec0, fts5_available=True)
        _insert_summary_with_embedding(
            db_with_vec0,
            summary_id="leaf_a",
            conv_id=conv_id_with_vec0,
            content="alpha doc",
            vector=(0.1, 0.2, 0.3),
        )
        voyage = _StubVoyage(embed_vector=[0.1, 0.2, 0.3])
        ctx = _Ctx(conn=db_with_vec0, summary_store=sstore, conversation_store=store, voyage=voyage)
        result = _call(
            {"pattern": "alpha", "mode": "semantic"},
            ctx=ctx,
            deps=deps,
        )
        details = result["details"]
        assert details["mode"] == "semantic"
        assert details["totalMatches"] == 1
        assert details["hits"][0]["summaryId"] == "leaf_a"
        # Voyage embed called once for the query; rerank NEVER called in
        # semantic mode (that's the cost-profile distinction).
        assert len(voyage.embed_calls) == 1
        assert len(voyage.rerank_calls) == 0
        # input_type='query' set so asymmetric retrieval is honored.
        assert voyage.embed_calls[0]["input_type"] == "query"

    def test_semantic_vec0_missing_returns_unavailable_error(
        self,
        db_no_vec0: sqlite3.Connection,
        conv_id_no_vec0: int,
        deps: LcmDependencies,
    ) -> None:
        """vec0 absent → SemanticSearchUnavailableError → operator error.

        The voyage client is present but vec0 is not loaded — the tool
        must surface the actionable error (TS lines 811-815), not crash.
        """
        del conv_id_no_vec0
        sstore = SummaryStore(db_no_vec0, fts5_available=True, trigram_tokenizer_available=False)
        store = ConversationStore(db_no_vec0, fts5_available=True)
        voyage = _StubVoyage()
        ctx = _Ctx(conn=db_no_vec0, summary_store=sstore, conversation_store=store, voyage=voyage)
        result = _call(
            {"pattern": "alpha", "mode": "semantic"},
            ctx=ctx,
            deps=deps,
        )
        assert "Semantic search unavailable" in result["error"]
        assert "vec0" in result["error"]
        assert "mode='regex'" in result["error"]


# ===========================================================================
# Semantic mode — summaryKinds filter
# ===========================================================================


@skip_if_no_vec0
class TestSemanticSummaryKindsFilter:
    """summaryKinds filter on semantic mode restricts hits to named kinds."""

    def test_leaf_only_excludes_condensed(
        self,
        db_with_vec0: sqlite3.Connection,
        conv_id_with_vec0: int,
        deps: LcmDependencies,
    ) -> None:
        sstore = SummaryStore(
            db_with_vec0,
            fts5_available=True,
            trigram_tokenizer_available=False,
        )
        store = ConversationStore(db_with_vec0, fts5_available=True)
        _insert_summary_with_embedding(
            db_with_vec0,
            summary_id="leaf_a",
            conv_id=conv_id_with_vec0,
            content="alpha leaf",
            kind="leaf",
            vector=(0.1, 0.2, 0.3),
        )
        _insert_summary_with_embedding(
            db_with_vec0,
            summary_id="cond_a",
            conv_id=conv_id_with_vec0,
            content="alpha condensed",
            kind="condensed",
            vector=(0.1, 0.2, 0.3),
        )
        voyage = _StubVoyage(embed_vector=[0.1, 0.2, 0.3])
        ctx = _Ctx(conn=db_with_vec0, summary_store=sstore, conversation_store=store, voyage=voyage)
        result = _call(
            {"pattern": "alpha", "mode": "semantic", "summaryKinds": ["leaf"]},
            ctx=ctx,
            deps=deps,
        )
        ids = [h["summaryId"] for h in result["details"]["hits"]]
        assert "leaf_a" in ids
        assert "cond_a" not in ids


# ===========================================================================
# Semantic mode — Voyage auth error propagates
# ===========================================================================


@skip_if_no_vec0
class TestSemanticVoyageAuthError:
    """Auth-class VoyageError in semantic mode surfaces the missing-key prose."""

    def test_semantic_voyage_auth_surfaces_missing_key(
        self,
        db_with_vec0: sqlite3.Connection,
        conv_id_with_vec0: int,
        deps: LcmDependencies,
    ) -> None:
        """TS lines 824-828 — auth → operator-facing message, not raise."""
        del conv_id_with_vec0
        sstore = SummaryStore(
            db_with_vec0,
            fts5_available=True,
            trigram_tokenizer_available=False,
        )
        store = ConversationStore(db_with_vec0, fts5_available=True)
        voyage = _StubVoyage(embed_raise=VoyageError("auth", "voyage_auth: 401"))
        ctx = _Ctx(conn=db_with_vec0, summary_store=sstore, conversation_store=store, voyage=voyage)
        result = _call(
            {"pattern": "alpha", "mode": "semantic"},
            ctx=ctx,
            deps=deps,
        )
        assert "VOYAGE_API_KEY" in result["error"]
        assert "semantic mode requires it" in result["error"]

    def test_semantic_voyage_5xx_surfaces_kind_label(
        self,
        db_with_vec0: sqlite3.Connection,
        conv_id_with_vec0: int,
        deps: LcmDependencies,
    ) -> None:
        """Wave-9 Agent #4 P1 (TS lines 830-833): non-auth VoyageError →
        operator-facing message that names the kind (so caller can decide
        whether to retry)."""
        del conv_id_with_vec0
        sstore = SummaryStore(
            db_with_vec0,
            fts5_available=True,
            trigram_tokenizer_available=False,
        )
        store = ConversationStore(db_with_vec0, fts5_available=True)
        voyage = _StubVoyage(embed_raise=VoyageError("server_error", "voyage_5xx: 502"))
        ctx = _Ctx(conn=db_with_vec0, summary_store=sstore, conversation_store=store, voyage=voyage)
        result = _call(
            {"pattern": "alpha", "mode": "semantic"},
            ctx=ctx,
            deps=deps,
        )
        assert "Voyage embed call failed" in result["error"]
        assert "server_error" in result["error"]
        assert "mode='full_text'" in result["error"]


# ===========================================================================
# Hybrid mode — Voyage auth on embed arm propagates
# ===========================================================================


@skip_if_no_vec0
class TestHybridSemanticArmAuth:
    """Auth error in the SEMANTIC (embed) arm of hybrid → operator error.

    The semantic arm's degrade-on-non-auth policy still re-raises auth
    so the operator sees the actionable message.
    """

    def test_hybrid_embed_auth_surfaces_missing_key(
        self,
        db_with_vec0: sqlite3.Connection,
        conv_id_with_vec0: int,
        deps: LcmDependencies,
    ) -> None:
        sstore = SummaryStore(
            db_with_vec0,
            fts5_available=True,
            trigram_tokenizer_available=False,
        )
        store = ConversationStore(db_with_vec0, fts5_available=True)
        _insert_summary_with_embedding(
            db_with_vec0,
            summary_id="leaf_a",
            conv_id=conv_id_with_vec0,
            content="alpha doc",
            vector=(0.1, 0.2, 0.3),
        )
        voyage = _StubVoyage(embed_raise=VoyageError("auth", "voyage_auth: 401"))
        ctx = _Ctx(conn=db_with_vec0, summary_store=sstore, conversation_store=store, voyage=voyage)
        result = _call(
            {"pattern": "alpha", "mode": "hybrid"},
            ctx=ctx,
            deps=deps,
        )
        assert "VOYAGE_API_KEY" in result["error"]
        assert "hybrid mode requires it" in result["error"]
