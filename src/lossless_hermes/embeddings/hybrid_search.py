"""Hybrid retrieval — FTS + semantic + optional Voyage rerank.

Ports ``lossless-claw/src/embeddings/hybrid-search.ts`` (commit ``1f07fbd``
on branch ``pr-613``, 437 LOC) to Python.

Combines BM25-style FTS over ``summaries_fts`` (caller-injected) with
semantic KNN over the active embedding model, then optionally reranks
the deduplicated union via Voyage ``rerank-2.5`` to produce a final
ranked list. Empirically (Phase-A spike: voyage-spike-results.md) lifts
paraphrastic queries by +52.5pp over FTS-only on Eva's 31-query eval.

Why this lives outside ``lcm_grep`` (Epic 06): the same pipeline is
needed by ``lcm_synthesize_around`` (``window_kind='semantic'``) and any
future tool that needs ranked-by-relevance content windows. Centralizing
the dedup + rerank-pack + RRF-fallback logic here keeps suppression
filters, dedup, and Voyage error-handling coherent across callers.

### Pipeline

1. **Parallel arms.** FTS (caller-injected callable) and semantic
   (:func:`run_semantic_search`) run via :func:`asyncio.gather`. Both
   restricted to summaries.
2. **Dedupe.** Each FTS hit yields a :class:`HybridHit` with
   ``fts_rank=i``; each semantic hit either merges into an existing
   FTS hit (setting ``from_semantic=True``, filling ``semantic_distance``)
   or creates a new entry with ``fts_rank=None``.
3. **Rerank pack** (load-bearing Wave-10 + Wave-11 fixes). Pack
   candidates against a budget of
   ``floor(MAX_TOKENS_PER_RERANK_CALL * 0.85)`` = 510K. Skip individually
   oversized candidates (Wave-11), stop when cumulative would exceed
   (Wave-10 truncation).
4. **Voyage rerank-2.5.** POST ``/v1/rerank`` with the packed subset;
   ``top_k = min(top_n, len(packed))``. Rerank failures (non-auth) fall
   through to RRF.
5. **RRF fallback.** ``score = (1 / (60 + fts_rank)) + (1 / (60 + sem_idx))``
   when rerank skipped (rerank=False OR Voyage non-auth error OR packed-empty).

### Degraded-result contract (preserve)

| Field | True when | Caller action |
|---|---|---|
| ``degraded_to_fts_only`` | vec0 unavailable OR semantic Voyage non-auth | Operator warning; FTS-only results |
| ``degraded_skipped_rerank`` | Rerank non-auth error OR ``rerank=False`` OR packed-empty | RRF used; slightly lower precision |
| ``rerank_pack_truncated`` | Rerank input packed to fit 510K budget | Operator warning; tail candidates dropped from rerank but visible in ``candidate_count`` |
| ``rerank_packed_count`` | Always set when rerank ran | Diagnostic |

**Auth errors re-thrown.** :class:`VoyageError` with ``kind="auth"`` in
either arm propagates out so the operator sees an actionable "set
VOYAGE_API_KEY" message. Silent degradation would hide misconfigured
deploys.

### Source map

* TS canonical: ``lossless-claw/src/embeddings/hybrid-search.ts:1-437``
* Porting guide §"Hybrid search pipeline":
  ``docs/porting-guides/embeddings.md`` lines 897-1069
* TS tests: ``lossless-claw/test/hybrid-search.test.ts`` (313 LOC)
* Issue spec: ``epics/05-embeddings/05-09-hybrid-search.md``
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Awaitable, Callable, Literal, Sequence

from lossless_hermes.db.connection import Connection
from lossless_hermes.embeddings.semantic_search import (
    SemanticHit,
    SemanticSearchResult,
    SemanticSearchUnavailableError,
    run_semantic_search,
)
from lossless_hermes.voyage.client import (
    MAX_TOKENS_PER_RERANK_CALL,
    VoyageClient,
    VoyageError,
)

__all__ = [
    "DEFAULT_K_FTS",
    "DEFAULT_K_SEMANTIC",
    "DEFAULT_TOP_N",
    "FtsHit",
    "FtsSearchFn",
    "HybridHit",
    "HybridSearchResult",
    "RRF_K",
    "run_hybrid_search",
]


# ---------------------------------------------------------------------------
# Constants — port from ``hybrid-search.ts:160-163`` and ``:415``.
# ---------------------------------------------------------------------------

DEFAULT_K_FTS: int = 50
"""Default FTS-side candidate count (``hybrid-search.ts:160``)."""

DEFAULT_K_SEMANTIC: int = 50
"""Default semantic-side candidate count (``hybrid-search.ts:161``)."""

DEFAULT_TOP_N: int = 20
"""Default final hit count after rerank/RRF (``hybrid-search.ts:162``)."""

RRF_K: int = 60
"""Reciprocal-Rank-Fusion constant — standard literature value
(``hybrid-search.ts:415``). LCM uses it without modification."""


# ---------------------------------------------------------------------------
# Dataclasses — port from ``hybrid-search.ts:96-153``.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FtsHit:
    """One ranked hit from the caller-injected FTS arm.

    Ports ``hybrid-search.ts:96-108`` ``FtsHit``. Caller's ``fts_search``
    must return a list of these (the existing Epic 01 FTS5 store's
    ``search_summaries`` will be wrapped by Epic 06's ``lcm_grep`` glue
    to produce this shape — see the issue spec §Dependencies).

    Attributes match ``summaries`` columns + ``rank`` (the 0-indexed
    FTS rank). ``rank=0`` is the best match.
    """

    summary_id: str
    """Stable identifier of the matched summary row."""

    conversation_id: int
    """``summaries.conversation_id``."""

    session_key: str
    """``summaries.session_key``."""

    kind: Literal["leaf", "condensed"]
    """``summaries.kind``."""

    content: str
    """``summaries.content``."""

    token_count: int
    """``summaries.token_count``. Used by the rerank-pack budget."""

    created_at: str
    """ISO timestamp ``summaries.created_at``."""

    rank: int
    """FTS rank (0-indexed; 0 = best match). Used for RRF fusion when
    rerank is off or fails."""


@dataclass(slots=True)
class HybridHit:
    """One ranked hit returned by :func:`run_hybrid_search`.

    Ports ``hybrid-search.ts:110-128`` ``HybridHit``. Mutable (non-frozen)
    because the pipeline computes ``score`` (rerank or RRF) and toggles
    ``from_semantic`` / ``semantic_distance`` after construction during
    the FTS+semantic merge.
    """

    summary_id: str
    """Stable identifier of the matched summary row."""

    conversation_id: int
    """``summaries.conversation_id``."""

    session_key: str
    """``summaries.session_key``."""

    kind: Literal["leaf", "condensed"]
    """``summaries.kind``."""

    content: str
    """``summaries.content``."""

    token_count: int
    """``summaries.token_count``. Used by the rerank-pack budget."""

    created_at: str
    """ISO timestamp ``summaries.created_at``."""

    score: float
    """Final score: rerank relevance (range ~[0, 1]) OR RRF score
    (``1/(60+rank)`` summed across arms; max ~0.033 when present at
    rank 0 in both arms)."""

    from_fts: bool
    """``True`` iff this hit appeared in the FTS arm."""

    from_semantic: bool
    """``True`` iff this hit appeared in the semantic arm."""

    semantic_distance: float | None
    """L2 distance from the semantic hit if applicable; ``None`` when
    not in semantic results. Use ``cosine_similarity`` for the ``[-1, 1]``
    cosine score."""

    cosine_similarity: float | None
    """Cosine similarity derived from :attr:`semantic_distance`; ``None``
    when not in semantic results."""

    fts_rank: int | None
    """FTS rank (0-indexed); ``None`` if not in FTS results."""


@dataclass(frozen=True, slots=True)
class HybridSearchResult:
    """Outcome of :func:`run_hybrid_search` — hits + diagnostics.

    Ports ``hybrid-search.ts:130-153`` ``HybridSearchResult``. The
    Python port surfaces every degraded flag at the top level (no
    optional/undefined gymnastics like TS) so callers can branch on
    boolean fields without ``getattr``.
    """

    hits: list[HybridHit] = field(default_factory=list)
    """Hits sorted by ``score`` descending. Length ≤ ``top_n``."""

    candidate_count: int = 0
    """Pre-rerank/RRF dedupe count — total unique ``summary_id``s
    across both arms after merging. Useful for "rerank dropped the
    rest" diagnostics."""

    voyage_tokens_consumed: int = 0
    """Voyage tokens used across the query embed (semantic arm) +
    rerank call. ``0`` when both are skipped."""

    degraded_to_fts_only: bool = False
    """``True`` when the semantic arm failed (vec0 unavailable OR
    Voyage non-auth error). Result is FTS-only."""

    degraded_skipped_rerank: bool = False
    """``True`` when RRF was used instead of rerank (``rerank=False``
    OR Voyage rerank non-auth error OR packed-empty)."""

    rerank_pack_truncated: bool = False
    """Wave-10: ``True`` when at least one candidate was dropped from
    the rerank input due to the 510K-token budget. The dropped
    candidates remain in ``candidate_count`` for backstop visibility
    but do not get a rerank score."""

    rerank_packed_count: int = 0
    """Wave-10: how many candidates actually made it into the rerank
    call. ``0`` when rerank skipped."""

    model: str = ""
    """Active embedding model used by the semantic arm. Empty when the
    semantic arm degraded out."""

    reranker_model: str | None = None
    """Voyage reranker model used (e.g. ``"rerank-2.5"``); ``None``
    when rerank skipped."""


# ---------------------------------------------------------------------------
# Callable signature for the FTS arm
# ---------------------------------------------------------------------------


FtsSearchFn = Callable[..., Awaitable[list[FtsHit]]]
"""Caller-injected async FTS search function.

Signature: ``async def fts_search(query: str, *, limit: int, **filters) -> list[FtsHit]``.

Epic 06's ``lcm_grep`` provides this backed by Epic 01's FTS5 store.
The decoupling lets Epic 05 ship without depending on Epic 06. Returning
``[]`` is fine (treated as "no FTS results").
"""


# ---------------------------------------------------------------------------
# Internal helpers — semantic-arm wrapper + cosine derivation
# ---------------------------------------------------------------------------


def _cosine_from_l2(distance: float) -> float:
    """Derive cosine similarity from an L2 distance on unit vectors.

    Voyage embeddings are L2-unit-normalized: ``L² = 2·(1 − cos)`` →
    ``cos = 1 − L²/2``. Clamp to ``[-1, 1]`` to absorb floating-point
    drift introduced by vec0's internal arithmetic. Mirrors the helper
    in ``semantic_search.py`` (kept module-local rather than imported
    because the semantic path computes cosine on its own and we only
    need it in the merge step).
    """
    cos = 1.0 - (distance * distance) / 2.0
    if cos < -1.0:
        return -1.0
    if cos > 1.0:
        return 1.0
    return cos


async def _semantic_with_degrade(
    conn: Connection,
    *,
    query: str,
    voyage: VoyageClient | None,
    k_semantic: int,
    since: datetime | None,
    before: datetime | None,
    conversation_ids: Sequence[int] | None,
    session_keys: Sequence[str] | None,
    summary_kinds: Sequence[Literal["leaf", "condensed"]] | None,
    exclude_suppressed: bool,
    query_vector: Sequence[float] | None,
    input_type: Literal["query", "document"] | None,
) -> SemanticSearchResult | None:
    """Run the semantic arm with graceful degrade for non-auth failures.

    Ports the inline ``semanticPromise`` IIFE at
    ``hybrid-search.ts:218-252``. Returns:

    * :class:`SemanticSearchResult` on success.
    * ``None`` when semantic search is unavailable (vec0 missing, no
      profile, table missing) or Voyage returns a non-auth error.

    Re-raises :class:`VoyageError` with ``kind="auth"`` unchanged — the
    operator must see the actionable message. Silent degradation on
    auth would hide a misconfigured deploy.
    """
    try:
        return await run_semantic_search(
            conn,
            query=query,
            k=k_semantic,
            voyage=voyage,
            since=since,
            before=before,
            conversation_ids=conversation_ids,
            session_keys=session_keys,
            summary_kinds=summary_kinds,
            exclude_suppressed=exclude_suppressed,
            embedded_kinds=("summary",),
            query_vector=query_vector,
            input_type=input_type,
        )
    except SemanticSearchUnavailableError:
        return None
    except VoyageError as e:
        # v4.1 Final.review.3 (Slice 1 Gap A / Loop 8 B-1 HIGH): mirror
        # the rerank arm — auth propagates (operator-actionable), other
        # kinds degrade to FTS-only. Without this, a single Voyage 5xx
        # would kill the whole hybrid query when FTS could have returned
        # useful results.
        # Original: lossless-claw/src/embeddings/hybrid-search.ts:240-249.
        if e.kind == "auth":
            raise
        return None


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------


async def run_hybrid_search(
    conn: Connection,
    *,
    query: str,
    fts_search: FtsSearchFn,
    voyage: VoyageClient | None = None,
    k_fts: int = DEFAULT_K_FTS,
    k_semantic: int = DEFAULT_K_SEMANTIC,
    top_n: int = DEFAULT_TOP_N,
    rerank: bool = True,
    reranker_model: str = "rerank-2.5",
    # Filters — forwarded verbatim to both arms.
    session_keys: Sequence[str] | None = None,
    conversation_ids: Sequence[int] | None = None,
    since: datetime | None = None,
    before: datetime | None = None,
    summary_kinds: Sequence[Literal["leaf", "condensed"]] | None = None,
    exclude_suppressed: bool = True,
    # Semantic-arm overrides (rarely used; mirror TS ``semantic`` field).
    query_vector: Sequence[float] | None = None,
    input_type: Literal["query", "document"] | None = "query",
) -> HybridSearchResult:
    """Run a hybrid retrieval. See module docstring for pipeline detail.

    Ports ``hybrid-search.ts:188-437`` ``runHybridSearch``. Caller is
    responsible for passing a working ``fts_search`` that respects the
    existing FTS5 sanitization rules — this function does not rebuild
    FTS, it delegates.

    Args:
        conn: Open SQLite connection (semantic arm needs vec0 loaded).
        query: Free-text query. Required; raises :class:`ValueError`
            when empty after ``strip()``.
        fts_search: Async callable that runs FTS over summaries and
            returns ranked :class:`FtsHit` list. See :data:`FtsSearchFn`.
        voyage: Optional :class:`VoyageClient`. Required when ``rerank``
            is True OR ``query_vector`` is None (semantic arm needs to
            embed). Caller manages lifecycle.
        k_fts: FTS-side candidate count. Default 50.
        k_semantic: Semantic-side candidate count. Default 50.
        top_n: Final hit count after rerank/RRF. Default 20.
        rerank: When ``True`` (default), call Voyage ``rerank-2.5`` over
            the candidate union. When ``False``, fuse via RRF (much
            cheaper, slightly less accurate).
        reranker_model: Voyage reranker model id. Default ``"rerank-2.5"``.
        session_keys: Forwarded to both arms.
        conversation_ids: Forwarded to both arms.
        since: Inclusive lower bound on
            ``COALESCE(s.latest_at, s.created_at)`` — forwarded to both arms.
        before: Exclusive upper bound — forwarded to both arms.
        summary_kinds: Filter by summary kind. Forwarded to both arms.
        exclude_suppressed: Defaults to ``True`` per v4.1 §10 invariant.
        query_vector: Inject a precomputed query vector instead of
            calling Voyage for the semantic arm (test path).
        input_type: Voyage ``input_type``. Default ``"query"`` for
            asymmetric retrieval.

    Returns:
        :class:`HybridSearchResult` with hits sorted by score
        descending. ``hits`` may be empty when both arms return empty.

    Raises:
        ValueError: ``query`` is empty after ``strip()``.
        VoyageError: Voyage call failed with ``kind="auth"`` (in either
            the semantic arm or rerank arm). Non-auth Voyage errors
            degrade silently.
    """
    # -----------------------------------------------------------------
    # Step 1: validate input (hybrid-search.ts:194-198)
    # -----------------------------------------------------------------
    query_stripped = query.strip() if query is not None else ""
    if not query_stripped:
        raise ValueError("[hybrid-search] query is required")

    # -----------------------------------------------------------------
    # Step 2: parallel arms (hybrid-search.ts:200-252)
    # -----------------------------------------------------------------
    # Collect filter kwargs to forward to the caller's FTS function.
    # We don't enforce a strict shape on these — the caller's
    # FtsSearchFn knows which filters it accepts; surplus keys are
    # acceptable per duck-typing. Pass via **kwargs to mirror the TS
    # object-literal forwarding.
    fts_filter_kwargs: dict[str, Any] = {
        "session_keys": session_keys,
        "conversation_ids": conversation_ids,
        "since": since,
        "before": before,
        "summary_kinds": summary_kinds,
        "exclude_suppressed": exclude_suppressed,
    }

    fts_coro = fts_search(query_stripped, limit=k_fts, **fts_filter_kwargs)
    sem_coro = _semantic_with_degrade(
        conn,
        query=query_stripped,
        voyage=voyage,
        k_semantic=k_semantic,
        since=since,
        before=before,
        conversation_ids=conversation_ids,
        session_keys=session_keys,
        summary_kinds=summary_kinds,
        exclude_suppressed=exclude_suppressed,
        query_vector=query_vector,
        input_type=input_type,
    )

    fts_hits, sem_result = await asyncio.gather(fts_coro, sem_coro)
    degraded_to_fts_only = sem_result is None
    voyage_tokens_consumed = sem_result.voyage_tokens_consumed if sem_result is not None else 0
    model_name = sem_result.model_name if sem_result is not None else ""

    # -----------------------------------------------------------------
    # Step 3: dedupe by summary_id (hybrid-search.ts:259-298)
    # -----------------------------------------------------------------
    merged: dict[str, HybridHit] = {}
    for i, f in enumerate(fts_hits):
        merged[f.summary_id] = HybridHit(
            summary_id=f.summary_id,
            conversation_id=f.conversation_id,
            session_key=f.session_key,
            kind=f.kind,
            content=f.content,
            token_count=f.token_count,
            created_at=f.created_at,
            score=0.0,  # computed below
            from_fts=True,
            from_semantic=False,
            semantic_distance=None,
            cosine_similarity=None,
            fts_rank=i,
        )

    sem_hits: list[SemanticHit] = sem_result.hits if sem_result is not None else []
    for s in sem_hits:
        existing = merged.get(s.summary_id)
        if existing is not None:
            existing.from_semantic = True
            existing.semantic_distance = s.distance
            existing.cosine_similarity = s.cosine_similarity
        else:
            merged[s.summary_id] = HybridHit(
                summary_id=s.summary_id,
                conversation_id=s.conversation_id,
                session_key=s.session_key,
                kind=s.kind,
                content=s.content,
                token_count=s.token_count,
                created_at=s.created_at,
                score=0.0,
                from_fts=False,
                from_semantic=True,
                semantic_distance=s.distance,
                cosine_similarity=s.cosine_similarity,
                fts_rank=None,
            )

    candidates = list(merged.values())
    candidate_count = len(candidates)
    # -----------------------------------------------------------------
    # Empty-corpus short-circuit (hybrid-search.ts:299-310)
    # -----------------------------------------------------------------
    if candidate_count == 0:
        return HybridSearchResult(
            hits=[],
            candidate_count=0,
            voyage_tokens_consumed=voyage_tokens_consumed,
            degraded_to_fts_only=degraded_to_fts_only,
            degraded_skipped_rerank=False,
            rerank_pack_truncated=False,
            rerank_packed_count=0,
            model=model_name,
            reranker_model=None,
        )

    # -----------------------------------------------------------------
    # Step 4: rerank packing (hybrid-search.ts:326-360) — Wave-10/11
    # -----------------------------------------------------------------
    rerank_requested = rerank
    rerank_packed: list[HybridHit] = candidates
    rerank_pack_truncated = False
    degraded_skipped_rerank = False

    if rerank_requested:
        # LCM Wave-10 (2026-03-XX): cap rerank input at 85% of Voyage's
        # 600K-token rerank budget (= 510K). Previously sent ALL candidate
        # content unconditionally — a query with many large condensed
        # summaries either (a) hit Voyage's 400 bad_request and silently
        # degraded to RRF, losing the +52.5pp paraphrastic lift, or
        # (b) burned the entire month's quota in one call. The 85% leaves
        # headroom for the query-token-estimate drift; pure cap on 600K
        # would 400 on edge cases.
        # Original: lossless-claw/src/embeddings/hybrid-search.ts:326-330.
        budget = math.floor(MAX_TOKENS_PER_RERANK_CALL * 0.85)
        # Rough token estimate matches the TS heuristic at
        # ``hybrid-search.ts:329``. Voyage's tokenizer counts ~9.5% higher
        # than ``len(content) / 4`` (spike 004); overestimating pushes us
        # to drop earlier rather than 400 on rerank, which is the safer
        # failure mode.
        query_est = math.ceil(len(query_stripped) / 4)
        cumulative = query_est
        packed: list[HybridHit] = []
        for c in candidates:
            cand_tokens = c.token_count if c.token_count else math.ceil(len(c.content) / 4)
            # LCM Wave-11 (2026-04-XX): skip individually-oversized
            # candidates (>510K token single doc) — do NOT break. Earlier
            # TS code broke out of the loop on the first oversized
            # candidate, disabling rerank for the entire result set even
            # though smaller later candidates would fit. With ``continue``
            # a single huge FTS hit no longer takes down the whole rerank.
            # The oversized candidate stays in ``candidates`` for the RRF
            # backstop scoring.
            # Original: lossless-claw/src/embeddings/hybrid-search.ts:341-352.
            if cand_tokens > budget:
                rerank_pack_truncated = True
                continue
            if cumulative + cand_tokens > budget:
                # Cumulative budget exceeded — stop packing. Rerank still
                # runs on what we have so far.
                rerank_pack_truncated = True
                break
            packed.append(c)
            cumulative += cand_tokens
        rerank_packed = packed

    # -----------------------------------------------------------------
    # Step 5: Voyage rerank (hybrid-search.ts:362-405)
    # -----------------------------------------------------------------
    if rerank_requested and rerank_packed and voyage is not None:
        try:
            resp = await voyage.rerank(
                query_stripped,
                [(c.summary_id, c.content) for c in rerank_packed],
                model=reranker_model,
                top_k=min(top_n, len(rerank_packed)),
            )
            voyage_tokens_consumed += resp.total_tokens
            # Apply rerank scores; map back to packed candidates by id.
            # Only items that appear in ``resp.results`` survive — the
            # packed subset is a strict superset of the response, so any
            # missing ids would indicate a Voyage server bug.
            by_id = {c.summary_id: c for c in rerank_packed}
            final_hits: list[HybridHit] = []
            for r in resp.results:
                c = by_id.get(r.id)
                if c is None:
                    # Defensive: Voyage returned an id that wasn't in
                    # input. Skip rather than crash; surface via tests
                    # if it becomes a recurring issue.
                    continue
                final_hits.append(replace(c, score=r.score))
            return HybridSearchResult(
                hits=final_hits,
                candidate_count=candidate_count,
                voyage_tokens_consumed=voyage_tokens_consumed,
                degraded_to_fts_only=degraded_to_fts_only,
                degraded_skipped_rerank=False,
                rerank_pack_truncated=rerank_pack_truncated,
                rerank_packed_count=len(rerank_packed),
                model=model_name,
                reranker_model=reranker_model,
            )
        except VoyageError as e:
            # Mirror semantic-arm policy: auth → propagate (operator
            # must see the actionable error); other kinds → fall through
            # to RRF.
            # Original: lossless-claw/src/embeddings/hybrid-search.ts:401-405.
            if e.kind == "auth":
                raise
            degraded_skipped_rerank = True
    elif rerank_requested and not rerank_packed:
        # Wave-10: every candidate was individually oversized, OR the
        # cumulative budget was hit before any candidate fit. Skip
        # rerank entirely; RRF below handles ranking.
        # Original: lossless-claw/src/embeddings/hybrid-search.ts:407-410.
        degraded_skipped_rerank = True
    elif rerank_requested and voyage is None:
        # Caller wanted rerank but didn't supply a client. Fall back to
        # RRF rather than crash — matches the spirit of the TS path
        # where ``voyageApiKey`` defaults to env and ``rerankCandidates``
        # raises if missing (we surface earlier as RRF degrade).
        degraded_skipped_rerank = True
    else:
        # rerank_requested is False — explicit RRF mode. Not a degrade,
        # but we still use the RRF code path below.
        degraded_skipped_rerank = False

    # -----------------------------------------------------------------
    # Step 6: RRF fallback (hybrid-search.ts:414-435)
    # -----------------------------------------------------------------
    # Recover semantic rank by scanning sem_result.hits for the summary
    # id. The semantic arm is sorted by distance ascending so the index
    # is the rank.
    sem_idx_by_id: dict[str, int] = (
        {h.summary_id: i for i, h in enumerate(sem_hits)} if sem_hits else {}
    )
    for c in candidates:
        score = 0.0
        if c.fts_rank is not None:
            score += 1.0 / (RRF_K + c.fts_rank)
        if c.from_semantic:
            sem_idx = sem_idx_by_id.get(c.summary_id)
            if sem_idx is not None:
                score += 1.0 / (RRF_K + sem_idx)
        c.score = score
    candidates.sort(key=lambda c: c.score, reverse=True)
    return HybridSearchResult(
        hits=candidates[:top_n],
        candidate_count=candidate_count,
        voyage_tokens_consumed=voyage_tokens_consumed,
        degraded_to_fts_only=degraded_to_fts_only,
        degraded_skipped_rerank=degraded_skipped_rerank,
        rerank_pack_truncated=rerank_pack_truncated,
        rerank_packed_count=0,
        model=model_name,
        reranker_model=None,
    )
