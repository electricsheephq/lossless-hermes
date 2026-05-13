"""Semantic search — query embed → KNN → JOIN-back-to-summaries.

Ports ``lossless-claw/src/embeddings/semantic-search.ts`` (commit ``1f07fbd``
on branch ``pr-613``, 419 LOC) to Python.

Wraps the embed-query → KNN → JOIN-back-to-summary flow used by the
``semantic`` and ``hybrid`` modes of ``lcm_grep``. Caller passes a free-text
query plus optional filters; we embed it via Voyage (with
``input_type='query'`` for asymmetric retrieval), run KNN against the active
model's vec0 table, JOIN back to ``summaries`` for content + metadata, and
return ranked hits along with confidence bands derived from cosine
similarity.

### Invariants (load-bearing)

* **Suppression filtered at two layers** (defense-in-depth):

  1. vec0 metadata pre-filter (``suppressed = 0`` inside the MATCH) — cheap
     because the metadata column is indexed alongside the vector.
  2. Final JOIN to ``summaries`` checks ``suppressed_at IS NULL`` — guards
     against the trigger / KNN race where the metadata-update lags a hot
     suppression flip.

* **§0 invariant** (architecture-v4.1.md §0.1): the Voyage embed call MUST
  be issued OUTSIDE any SQLite write transaction. We assert this at runtime
  via :func:`lossless_hermes.concurrency.model.assert_no_open_tx` immediately
  before the network round-trip.

* **NEVER silently fall back to FTS.** When vec0 isn't loaded / no profile
  registered / table missing, raise :class:`SemanticSearchUnavailableError`
  so the caller (hybrid search, ``lcm_grep``) can choose its degrade policy
  explicitly. Silent fall-through would mask a missing extension and ship
  silently-worse recall.

* **Cosine identity** (Voyage embeddings are L2-unit-normalized): on unit
  vectors, ``L² = 2·(1 − cos)`` so ``cos = max(−1, min(1, 1 − L²/2))``.
  Clamping absorbs floating-point error.

### Source map

* TS canonical: ``lossless-claw/src/embeddings/semantic-search.ts:1-419``
* Porting guide §"Semantic search":
  ``docs/porting-guides/embeddings.md`` lines 1073-1097
* TS tests: ``lossless-claw/test/semantic-search.test.ts`` (355 LOC)
* Issue spec: ``epics/05-embeddings/05-08-semantic-search.md``
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Sequence, Union, cast

from lossless_hermes.concurrency.model import assert_no_open_tx
from lossless_hermes.db.connection import Connection, vec0_version
from lossless_hermes.embeddings.store import (
    EmbeddedKind,
    SearchHit,
    embeddings_table_exists,
    search_similar,
)
from lossless_hermes.voyage.client import VoyageClient, VoyageError

__all__ = [
    "COSINE_BAND_HIGH",
    "COSINE_BAND_LOW",
    "COSINE_BAND_MEDIUM",
    "ConfidenceBand",
    "EmbeddingProfile",
    "SemanticHit",
    "SemanticSearchResult",
    "SemanticSearchUnavailableError",
    "get_active_embedding_model",
    "run_semantic_search",
]


# ---------------------------------------------------------------------------
# Cosine-similarity bands (ports ``semantic-search.ts:122-128``)
# ---------------------------------------------------------------------------
#
# Calibrated against Eva's live DB on 2026-05-06; see TS source comment block
# at ``semantic-search.ts:122-128``. These constants drive the agent-facing
# ``confidence`` band that surfaces alongside each hit so the model knows
# whether a result is highly relevant (≥0.65) or noise (<0.35).

COSINE_BAND_HIGH: float = 0.65
"""Cosine ≥ this → ``"high"`` confidence (~L2 distance ≤ 0.84)."""

COSINE_BAND_MEDIUM: float = 0.50
"""Cosine ≥ this → ``"medium"`` confidence (~L2 distance ≤ 1.00)."""

COSINE_BAND_LOW: float = 0.35
"""Cosine ≥ this → ``"low"`` confidence (~L2 distance ≤ 1.14).

Below this threshold a hit is classified as ``"noise"`` — the agent should
treat it as essentially unrelated.
"""


ConfidenceBand = Literal["high", "medium", "low", "noise"]
"""Discrete confidence band derived from cosine similarity. See
:func:`_band_for_cosine`. The string literals are exposed to the agent as the
``band`` field on each hit and are stable across the public surface.
"""


def _band_for_cosine(cos: float) -> ConfidenceBand:
    """Map a cosine score in ``[-1, 1]`` to a discrete confidence band.

    Ports the band logic from ``semantic-search.ts:122-128`` and the
    agent-facing prompt-side mapping documented there. Pure helper —
    callers should not branch on cosine thresholds directly; use the
    ``band`` field on :class:`SemanticHit` instead.
    """
    if cos >= COSINE_BAND_HIGH:
        return "high"
    if cos >= COSINE_BAND_MEDIUM:
        return "medium"
    if cos >= COSINE_BAND_LOW:
        return "low"
    return "noise"


def _cosine_from_l2(distance: float) -> float:
    """Derive cosine similarity from a vec0 L2 distance (unit vectors).

    Voyage embeddings are L2-unit-normalized; on unit vectors the identity
    ``L² = 2·(1 − cos)`` holds, so ``cos = 1 − L²/2``. We clamp to
    ``[-1, 1]`` to absorb floating-point error introduced by
    ``serialize_float32`` / vec0's internal arithmetic. Ports the inline
    derivation at ``semantic-search.ts:395``.
    """
    cos = 1.0 - (distance * distance) / 2.0
    if cos < -1.0:
        return -1.0
    if cos > 1.0:
        return 1.0
    return cos


# ---------------------------------------------------------------------------
# Over-fetch constants (ports ``semantic-search.ts:265-266``)
# ---------------------------------------------------------------------------
#
# P1 harness fix (2026-05-06): when ANY filter is active (time / conversation
# / sessionKey / kind), vec0's nearest-K does not know about it. Top-K
# globally may all live OUTSIDE the filter window, leading to 0 hits even
# though hundreds of matching docs exist. Counter by OVER-FETCHING from vec0
# (10× the user's k, capped at 500) when filters are active, then trimming
# AFTER the JOIN.

_VEC0_OVERFETCH_MULT = 10
_VEC0_OVERFETCH_MAX = 500


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EmbeddingProfile:
    """Active embedding-model profile returned by :func:`get_active_embedding_model`.

    Ports the inline ``{ modelName, dim }`` shape used at
    ``semantic-search.ts:167-179``. The dataclass form is more discoverable
    in Python than the ad-hoc dict the TS code uses, and gives ``ty`` a
    concrete type to check against.
    """

    model_name: str
    """The Voyage / arbitrary model identifier (e.g. ``"voyage-4-large"``)."""

    dim: int
    """The embedding dimension associated with the model. Locked at first
    registration — see :func:`store.register_embedding_profile`."""


@dataclass(frozen=True, slots=True)
class SemanticHit:
    """One ranked hit from :func:`run_semantic_search`.

    Ports ``semantic-search.ts:106-143`` ``SemanticHit``. The Python port
    folds ``cosine_similarity`` into the always-required surface (the TS
    Audit-1 fix at ``:290-297`` made it required there too — downstream
    ``.toFixed(3)`` calls crash on ``undefined``) and adds the discrete
    :data:`ConfidenceBand` so agent prompts don't need to re-derive the
    bucket from raw cosine.
    """

    summary_id: str
    """Stable identifier of the matched summary row."""

    embedded_kind: EmbeddedKind
    """Polymorphic kind from the vec0 row — ``"summary"`` for this surface,
    but the field is preserved for the rare operator-tool case that passes
    ``embedded_kinds=["entity", "theme"]`` and gets entity/theme hits back."""

    distance: float
    """Raw L2 distance reported by vec0. ``0.0`` ≈ identical, ``~1.41`` ≈
    orthogonal. Use :attr:`cosine_similarity` for the ``[-1, 1]`` cosine
    score."""

    cosine_similarity: float
    """Cosine similarity in ``[-1, 1]`` derived from :attr:`distance` via
    ``cos = 1 − L²/2`` (Voyage embeddings are unit-normalized). Higher =
    more similar. See :func:`_cosine_from_l2`."""

    band: ConfidenceBand
    """Discrete confidence band — ``"high"`` / ``"medium"`` / ``"low"`` /
    ``"noise"``. See :func:`_band_for_cosine`."""

    conversation_id: int
    """``summaries.conversation_id`` of the matched row. Set to ``-1`` for
    entity/theme hits that don't go through the JOIN."""

    session_key: str
    """``summaries.session_key`` (populated atomically at write time per
    Gap-8 fix). Empty string for entity/theme hits."""

    kind: Literal["leaf", "condensed"]
    """``summaries.kind``. ``"leaf"`` for entity/theme hits (placeholder)."""

    content: str
    """``summaries.content`` of the matched row."""

    token_count: int
    """``summaries.token_count``. ``0`` for entity/theme hits."""

    created_at: str
    """ISO timestamp ``summaries.created_at``. Empty for entity/theme hits."""

    earliest_at: str | None = None
    """Earliest ``earliest_at`` of the source-message bracket."""

    latest_at: str | None = None
    """Latest ``latest_at`` of the source-message bracket. Wave-1 fix:
    semantic + FTS time filters both use ``COALESCE(latest_at, created_at)``
    so they agree on the time window."""

    filtered_after_join: bool = False
    """True iff the row passed the vec0 metadata-only check but failed the
    JOIN — diagnostic field, should always be ``False`` in steady state.
    Ports the Wave-8 ``filteredAfterJoin`` flag from
    ``semantic-search.ts:312``."""


@dataclass(frozen=True, slots=True)
class SemanticSearchResult:
    """Outcome of :func:`run_semantic_search` — ranked hits + diagnostics.

    Ports ``semantic-search.ts:145-153`` ``SemanticSearchResult``.
    """

    hits: list[SemanticHit] = field(default_factory=list)
    """Hits sorted by ``distance`` ascending (most similar first)."""

    candidate_count: int = 0
    """Total candidates returned by vec0 KNN BEFORE the JOIN filter. Useful
    for debugging "why did the JOIN drop everything?" cases."""

    voyage_tokens_consumed: int = 0
    """Voyage tokens used by the embed call. ``0`` when the caller supplied
    ``query_vector`` (test path or hybrid arm reuse)."""

    model_name: str = ""
    """The active embedding model used. Surfaced so callers can correlate
    with ``lcm_embedding_profile`` rows."""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SemanticSearchUnavailableError(Exception):
    """Raised when semantic search cannot proceed due to environment issues.

    Ports ``semantic-search.ts:155-160``. Triggered when:

    * vec0 extension isn't loaded on the connection.
    * No active profile registered in ``lcm_embedding_profile``.
    * The per-model ``lcm_embeddings_<slug>`` table doesn't exist (caller
      should have run :func:`store.ensure_embeddings_table` during setup).
    * The injected ``query_vector`` length doesn't match the active
      profile's dim.

    Caller (hybrid search at issue 05-09, ``lcm_grep`` at Epic 09) catches
    this and degrades gracefully to FTS-only with ``degraded_to_fts_only =
    True``. Auth-class :class:`VoyageError` propagates separately — see the
    handling in :func:`run_semantic_search`.
    """

    pass


# ---------------------------------------------------------------------------
# Active-profile lookup (ports ``semantic-search.ts:167-179``)
# ---------------------------------------------------------------------------


def get_active_embedding_model(conn: Connection) -> EmbeddingProfile | None:
    """Return the currently-active embedding-model profile, or ``None``.

    Ports ``semantic-search.ts:167-179`` ``getActiveEmbeddingModel``.
    Queries ``lcm_embedding_profile`` for rows with ``active = 1`` AND
    ``archive_after IS NULL`` (so archived profiles are excluded). When
    multiple are active (e.g. mid-cutover), returns the one with the most
    recent ``registered_at`` — matches the TS tie-breaker.

    Used by both :func:`run_semantic_search` (precondition gate) and the
    ``/lcm health`` surface (Epic 08) to confirm a profile exists.

    Returns ``None`` when no row satisfies the predicate. Caller decides
    whether to treat this as fatal (this function in the semantic path) or
    informational (a fresh DB before any embedding has happened).
    """
    row = conn.execute(
        "SELECT model_name, dim FROM lcm_embedding_profile "
        "  WHERE active = 1 AND archive_after IS NULL "
        "  ORDER BY registered_at DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    model_name, dim = row[0], row[1]
    if not model_name or not isinstance(dim, int):
        # Defensive: a row with NULL model_name or non-int dim is a corrupt
        # profile. Treat as "no active model" rather than crash here — the
        # caller's UnavailableError surface gives a better operator
        # experience. Mirrors the TS truthy check at ``:177``.
        return None
    return EmbeddingProfile(model_name=model_name, dim=dim)


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------


def _is_filtered(
    *,
    since: datetime | None,
    before: datetime | None,
    conversation_ids: Sequence[int] | None,
    session_keys: Sequence[str] | None,
    summary_kinds: Sequence[str] | None,
) -> bool:
    """Return ``True`` iff any post-KNN filter is active.

    Ports the inline filter-detection block at ``semantic-search.ts:258-264``.
    When any of these are non-empty, we over-fetch from vec0 so the
    post-filter survivors aren't crowded out (see :data:`_VEC0_OVERFETCH_MULT`).
    """
    if since is not None or before is not None:
        return True
    if conversation_ids and len(conversation_ids) > 0:
        return True
    if session_keys and len(session_keys) > 0:
        return True
    if summary_kinds and len(summary_kinds) > 0:
        return True
    return False


def _iso_z(dt: datetime) -> str:
    """Format a :class:`datetime` as an ISO-8601 string SQLite ``julianday``
    will accept.

    SQLite ``julianday()`` accepts ``YYYY-MM-DD HH:MM:SS`` and
    ``YYYY-MM-DDTHH:MM:SSZ`` interchangeably. Python's
    :meth:`datetime.isoformat` produces the ``T``-separator form. We
    normalise to UTC if the input has a tzinfo, then drop the tz suffix
    so the string is comparable to ``summaries.created_at`` (which is
    ``datetime('now')`` — stored as ``YYYY-MM-DD HH:MM:SS`` without a
    tz suffix).
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat(sep=" ", timespec="seconds")


async def run_semantic_search(
    conn: Connection,
    *,
    query: str,
    k: int = 50,
    voyage: VoyageClient | None = None,
    model_name: str | None = None,
    query_vector: Union[Sequence[float], None] = None,
    input_type: Literal["query", "document"] | None = "query",
    exclude_suppressed: bool = True,
    embedded_kinds: Sequence[EmbeddedKind] | None = None,
    since: datetime | None = None,
    before: datetime | None = None,
    conversation_ids: Sequence[int] | None = None,
    session_keys: Sequence[str] | None = None,
    summary_kinds: Sequence[Literal["leaf", "condensed"]] | None = None,
) -> SemanticSearchResult:
    """Run a semantic search. Returns ranked hits sorted by distance ascending.

    Ports ``semantic-search.ts:188-419`` ``runSemanticSearch``. The pipeline
    is verbatim from the TS source — see the porting guide §"Semantic
    search" for the line-by-line mapping.

    Pipeline:

    1. **Validate environment.** vec0 must be loaded, an active profile
       must exist in ``lcm_embedding_profile``, and the per-model
       ``lcm_embeddings_<slug>`` table must exist. Otherwise raise
       :class:`SemanticSearchUnavailableError`.
    2. **Embed query.** If ``query_vector`` is supplied, skip the Voyage
       call (test path or hybrid arm reuse). Otherwise call
       :meth:`VoyageClient.embed` with ``input_type='query'`` and
       ``output_dimension`` set to the active profile dim (Wave-11 fix).
       The :func:`assert_no_open_tx` guard fires before the await (§0
       invariant).
    3. **Over-fetch on filtered KNN.** When any filter is active, request
       ``min(500, max(k, k * 10))`` candidates from vec0 so the
       post-filter survivors aren't crowded out (P1 harness fix).
    4. **KNN search** via :func:`store.search_similar`.
    5. **JOIN back to summaries** with dynamic ``WHERE`` clauses for
       suppression, session_keys, conversation_ids, since/before
       (``COALESCE(latest_at, created_at)`` — Wave-1 fix to align with
       FTS semantics), and summary_kinds.
    6. **Trim to user's k** after filtering. Compute
       :class:`ConfidenceBand` from cosine similarity.

    Args:
        conn: Open SQLite connection with sqlite-vec loaded. Must have the
            v4.1 migration ladder applied.
        query: Free-text query. Required unless ``query_vector`` is
            supplied; rejected as :class:`ValueError` if empty after
            ``strip()``.
        k: Final number of hits to return (after JOIN filter). Defaults
            to 50. Forwarded to ``store.search_similar`` after the
            over-fetch transform.
        voyage: Optional :class:`VoyageClient`. Required when
            ``query_vector`` is None. The caller manages lifecycle —
            this function does not :meth:`~VoyageClient.aclose` it.
        model_name: Override the Voyage model used for the query embed.
            Defaults to the active profile's ``model_name`` so query and
            indexed vectors stay aligned.
        query_vector: Inject a precomputed query vector instead of
            calling Voyage. Length MUST match the active profile's dim;
            mismatch raises :class:`SemanticSearchUnavailableError`.
        input_type: Voyage ``input_type`` flag. Defaults to ``"query"``
            for asymmetric retrieval (the documents at index time used
            ``"document"``).
        exclude_suppressed: When ``True`` (default), filters suppressed
            rows at both the vec0 metadata layer and the JOIN. v4.1 §10
            invariant — every retrieval surface defaults to ON. Operator
            tools opt-in to ``False``.
        embedded_kinds: vec0 metadata filter — defaults to
            ``("summary",)``. Operator tools may pass
            ``("summary", "entity")`` etc.
        since: Inclusive lower bound on
            ``COALESCE(s.latest_at, s.created_at)``.
        before: Exclusive upper bound on
            ``COALESCE(s.latest_at, s.created_at)``.
        conversation_ids: Filter to specific conversation IDs.
        session_keys: Filter to specific session keys.
        summary_kinds: Filter by summary kind (``"leaf"`` / ``"condensed"``).

    Returns:
        A :class:`SemanticSearchResult` with hits sorted by distance
        ascending. ``hits`` may be empty if vec0 returns nothing or every
        candidate is filtered out by the JOIN. ``candidate_count`` reports
        the pre-JOIN vec0 row count for diagnostics.

    Raises:
        SemanticSearchUnavailableError: vec0 unavailable, no active
            profile, table missing, or ``query_vector`` dim mismatch.
        ValueError: ``query`` is empty AND ``query_vector`` is None.
        VoyageError: Voyage call failed. ``kind="auth"`` indicates
            VOYAGE_API_KEY is unset/invalid — re-raised so the operator
            sees actionable feedback. Other kinds are also re-raised; the
            hybrid layer (issue 05-09) catches them and degrades to
            FTS-only.
        RuntimeError: A SQLite write transaction is open at the moment of
            the Voyage call (§0 invariant violation).
    """
    # -----------------------------------------------------------------
    # Step 1: validate vec0 + active model (semantic-search.ts:193-208)
    # -----------------------------------------------------------------
    if vec0_version(conn) is None:
        raise SemanticSearchUnavailableError(
            "[semantic-search] sqlite-vec is not loaded — semantic retrieval unavailable"
        )

    active = get_active_embedding_model(conn)
    if active is None:
        raise SemanticSearchUnavailableError(
            "[semantic-search] no active embedding model registered in lcm_embedding_profile"
        )

    if not embeddings_table_exists(conn, active.model_name):
        raise SemanticSearchUnavailableError(
            f"[semantic-search] vec0 table for {active.model_name!r} doesn't exist "
            "— call ensure_embeddings_table() during setup"
        )

    # -----------------------------------------------------------------
    # Step 2: embed query (or use injected vector) (semantic-search.ts:211-246)
    # -----------------------------------------------------------------
    voyage_tokens_consumed = 0
    resolved_query_vector: Sequence[float]

    if query_vector is not None:
        if len(query_vector) != active.dim:
            # Dim mismatch is fatal — vec0 MATCH would crash with a less
            # actionable error. Raise the unavailable error so the caller
            # can degrade. Mirrors ``semantic-search.ts:214-217`` (TS raises
            # a bare Error; we use UnavailableError so the hybrid degrade
            # path catches it the same way as missing-vec0).
            raise SemanticSearchUnavailableError(
                f"[semantic-search] query_vector dim {len(query_vector)} "
                f"!= active model dim {active.dim}"
            )
        resolved_query_vector = query_vector
    else:
        stripped = query.strip() if query is not None else ""
        if not stripped:
            # Mirrors ``semantic-search.ts:222-224``. The TS path throws a
            # plain Error here; Python uses ValueError so callers can
            # distinguish "missing input" from "environment broken".
            raise ValueError("[semantic-search] query is required (or pass query_vector)")

        if voyage is None:
            # No client supplied AND no query_vector — there's no way to
            # produce a vector. Treat as misconfiguration; matches the TS
            # path where ``embedTexts`` is unconditionally called and would
            # immediately fail on missing api key, just with a clearer
            # message here.
            raise SemanticSearchUnavailableError(
                "[semantic-search] no VoyageClient supplied and no query_vector — "
                "cannot embed query"
            )

        voyage_model = model_name or active.model_name

        # LCM §0.1 invariant: NO LLM/network call inside an open SQLite
        # write transaction. Defended at runtime by assert_no_open_tx so
        # accidents are caught at the call boundary rather than wedging
        # the WAL on Voyage latency.
        #
        # The Connection Protocol does not expose ``in_transaction`` but
        # the runtime instance always will (stdlib + apsw adapter both do).
        # ``assert_no_open_tx`` expects sqlite3.Connection — narrow with
        # cast to keep ty happy.
        assert_no_open_tx(cast(sqlite3.Connection, conn))

        try:
            # LCM Wave-11 (2026-04-XX): query embedding MUST request the
            # same dim as the indexed corpus. Pulled from the active
            # profile so query vectors match the per-model vec0 column
            # shape. Without this, vec0 columns with non-default dim
            # (256/512/2048) would receive a 1024-d vector and the MATCH
            # crashes with dim mismatch.
            # Original: lossless-claw/src/embeddings/semantic-search.ts:237.
            embed = await voyage.embed(
                [stripped],
                model=voyage_model,
                input_type=input_type,
                output_dimension=active.dim,
            )
        except VoyageError:
            # Auth-class errors propagate unchanged so the operator sees
            # the actionable "set VOYAGE_API_KEY" message. Other kinds
            # (rate_limit / network / server_error / unexpected) are also
            # re-raised — the hybrid layer at issue 05-09 catches them and
            # degrades to FTS-only with ``degraded_to_fts_only=True``.
            # Either way: re-raise here; do not silently absorb.
            raise

        if len(embed.vectors) != 1:
            raise SemanticSearchUnavailableError(
                f"[semantic-search] Voyage returned {len(embed.vectors)} vectors (expected 1)"
            )
        resolved_query_vector = embed.vectors[0]
        voyage_tokens_consumed = embed.total_tokens

    # -----------------------------------------------------------------
    # Step 3: over-fetch on filtered KNN (semantic-search.ts:257-269)
    # -----------------------------------------------------------------
    user_k = k
    has_filter = _is_filtered(
        since=since,
        before=before,
        conversation_ids=conversation_ids,
        session_keys=session_keys,
        summary_kinds=summary_kinds,
    )
    # P1 FIX (2026-05-06 harness finding): vec0 KNN doesn't know about
    # post-filters. Without over-fetching, top-K globally may all live
    # OUTSIDE the filter window → 0 results despite hundreds of matches.
    # Over-fetch 10× (cap 500) when filters are active.
    # Original: lossless-claw/src/embeddings/semantic-search.ts:267-269.
    k_request = (
        min(_VEC0_OVERFETCH_MAX, max(user_k, user_k * _VEC0_OVERFETCH_MULT))
        if has_filter
        else user_k
    )

    # -----------------------------------------------------------------
    # Step 4: KNN search (semantic-search.ts:270-276)
    # -----------------------------------------------------------------
    kinds = tuple(embedded_kinds) if embedded_kinds else ("summary",)
    candidates: list[SearchHit] = search_similar(
        conn,
        model_name=active.model_name,
        query_vector=resolved_query_vector,
        k=k_request,
        embedded_kinds=kinds,
        exclude_suppressed=exclude_suppressed,
    )

    if not candidates:
        return SemanticSearchResult(
            hits=[],
            candidate_count=0,
            voyage_tokens_consumed=voyage_tokens_consumed,
            model_name=active.model_name,
        )

    # Split summary candidates (which JOIN to summaries) from entity/theme
    # candidates (which do not). The TS source short-circuits when there
    # are no summary candidates — we mirror it for parity, including the
    # Wave-8 ``filtered_after_join=False`` correction on the entity branch.
    summary_candidates = [c for c in candidates if c.embedded_kind == "summary"]
    summary_ids = [c.embedded_id for c in summary_candidates]

    if not summary_ids:
        # All candidates were entity/theme — return them with no JOIN.
        # Audit-1 fix (Wave-8): ``filtered_after_join`` was being set to
        # ``True`` on every entity/theme hit despite no JOIN being
        # attempted. The field's documented semantic is "row passed the
        # metadata-only check but failed JOIN" — meaningless when there
        # is no JOIN. Set ``False`` here. cosine_similarity is computed
        # so downstream ``f"{cos:.3f}"`` formatters don't crash.
        # Original: lossless-claw/src/embeddings/semantic-search.ts:292-313.
        return SemanticSearchResult(
            hits=[
                SemanticHit(
                    summary_id=c.embedded_id,
                    embedded_kind=c.embedded_kind,
                    distance=c.distance,
                    cosine_similarity=_cosine_from_l2(c.distance),
                    band=_band_for_cosine(_cosine_from_l2(c.distance)),
                    conversation_id=-1,
                    session_key="",
                    kind="leaf",
                    content="",
                    token_count=0,
                    created_at="",
                    earliest_at=None,
                    latest_at=None,
                    filtered_after_join=False,
                )
                for c in candidates
            ],
            candidate_count=len(candidates),
            voyage_tokens_consumed=voyage_tokens_consumed,
            model_name=active.model_name,
        )

    # -----------------------------------------------------------------
    # Step 5: JOIN back to summaries with filter clauses
    #         (semantic-search.ts:320-380)
    # -----------------------------------------------------------------
    # Dynamic WHERE clauses + bind params. Mirrors the TS construction
    # at ``semantic-search.ts:321-360`` line-for-line.
    placeholders = ",".join("?" for _ in summary_ids)
    filters: list[str] = []
    binds: list[Union[str, int]] = list(summary_ids)

    if exclude_suppressed:
        # Defense-in-depth: vec0 metadata might race with the trigger.
        # Same suppression filter at the JOIN catches stale rows that
        # leaked through the metadata pre-filter. Original:
        # lossless-claw/src/embeddings/semantic-search.ts:328-330.
        filters.append("s.suppressed_at IS NULL")

    if session_keys:
        filters.append(f"s.session_key IN ({','.join('?' for _ in session_keys)})")
        binds.extend(session_keys)

    if conversation_ids:
        filters.append(f"s.conversation_id IN ({','.join('?' for _ in conversation_ids)})")
        binds.extend(int(cid) for cid in conversation_ids)

    # LCM Wave-1 (2026-01-12): semantic and FTS arms had divergent time
    # semantics — semantic used ``s.created_at`` (row-write time), FTS
    # used ``COALESCE(s.latest_at, s.created_at)`` (the content's
    # covered-time bracket). On condensed summaries written long after
    # the content they cover, the two arms returned different sets for
    # the same since/before window. ``COALESCE(latest_at, created_at)``
    # aligns the two; ``julianday()`` wrapping handles legacy ISO-8601
    # vs SQLite-canonical comparison robustly.
    # Original: lossless-claw/src/embeddings/semantic-search.ts:345-356.
    if since is not None:
        filters.append("julianday(COALESCE(s.latest_at, s.created_at)) >= julianday(?)")
        binds.append(_iso_z(since))
    if before is not None:
        filters.append("julianday(COALESCE(s.latest_at, s.created_at)) < julianday(?)")
        binds.append(_iso_z(before))

    if summary_kinds:
        filters.append(f"s.kind IN ({','.join('?' for _ in summary_kinds)})")
        binds.extend(summary_kinds)

    where_extra = (" AND " + " AND ".join(filters)) if filters else ""

    rows = conn.execute(
        f"SELECT s.summary_id, s.conversation_id, s.session_key, s.kind, "
        f"       s.content, s.token_count, s.created_at, s.earliest_at, "
        f"       s.latest_at "
        f"  FROM summaries s "
        f"  WHERE s.summary_id IN ({placeholders}){where_extra}",
        tuple(binds),
    ).fetchall()

    rows_by_id = {row[0]: row for row in rows}

    # -----------------------------------------------------------------
    # Step 6: build hits in candidate (distance) order + trim to user_k
    #         (semantic-search.ts:388-411)
    # -----------------------------------------------------------------
    # Build hits in vec0 ranking order — we drop candidates that did NOT
    # survive the JOIN filter, then trim to user_k. The over-fetch above
    # was specifically to give this step a deep pool of survivors.
    hits: list[SemanticHit] = []
    for cand in summary_candidates:
        row = rows_by_id.get(cand.embedded_id)
        if row is None:
            # Row was filtered out by the JOIN (suppressed, wrong session,
            # outside time window, etc.). Skip silently — the user only
            # gets back rows that pass every filter.
            continue
        cos = _cosine_from_l2(cand.distance)
        hits.append(
            SemanticHit(
                summary_id=cand.embedded_id,
                embedded_kind=cand.embedded_kind,
                distance=cand.distance,
                cosine_similarity=cos,
                band=_band_for_cosine(cos),
                conversation_id=int(row[1]),
                session_key=row[2] or "",
                kind=cast(Literal["leaf", "condensed"], row[3]),
                content=row[4] or "",
                token_count=int(row[5]) if row[5] is not None else 0,
                created_at=row[6] or "",
                earliest_at=row[7],
                latest_at=row[8],
                filtered_after_join=False,
            )
        )
        if len(hits) >= user_k:
            break

    return SemanticSearchResult(
        hits=hits,
        candidate_count=len(candidates),
        voyage_tokens_consumed=voyage_tokens_consumed,
        model_name=active.model_name,
    )
