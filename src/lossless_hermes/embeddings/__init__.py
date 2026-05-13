"""LCM embeddings subsystem — vec0 store, Voyage client, backfill worker.

This package owns LCM's semantic-recall plumbing:

* :mod:`lossless_hermes.embeddings.store` — per-model
  ``lcm_embeddings_<slug>`` vec0 virtual tables, the polymorphic
  ``(embedded_id, embedded_kind)`` shape, AFTER UPDATE / AFTER DELETE
  triggers on ``summaries``, slug-collision guard, and the SAVEPOINT-per-row
  Wave-4 / Wave-5 duplicate guards. Port of
  ``lossless-claw/src/embeddings/store.ts``.
* (Future) ``backfill.py`` — cron-driven backfill of unembedded leaves
  (issue 05-07).
* :mod:`lossless_hermes.embeddings.semantic_search` — KNN retrieval surface
  that calls into :func:`store.search_similar`, embeds the query through
  Voyage, JOINs back to ``summaries``, and exposes cosine-similarity bands.
  Port of ``lossless-claw/src/embeddings/semantic-search.ts``.

The Voyage HTTP client lives in :mod:`lossless_hermes.voyage`, not here —
the embeddings store consumes vectors from any source and is intentionally
decoupled from the embedding provider.

See:

* ``docs/porting-guides/embeddings.md`` §"sqlite-vec store"
* ``docs/spike-results/001-sqlite-vec-python.md`` — extension loading PASS
* ``docs/adr/004-sqlite3-backend.md`` — stdlib primary, apsw fallback
"""

from __future__ import annotations

from lossless_hermes.embeddings.hybrid_search import (
    DEFAULT_K_FTS,
    DEFAULT_K_SEMANTIC,
    DEFAULT_TOP_N,
    RRF_K,
    FtsHit,
    FtsSearchFn,
    HybridHit,
    HybridSearchResult,
    run_hybrid_search,
)
from lossless_hermes.embeddings.semantic_search import (
    COSINE_BAND_HIGH,
    COSINE_BAND_LOW,
    COSINE_BAND_MEDIUM,
    ConfidenceBand,
    EmbeddingProfile,
    SemanticHit,
    SemanticSearchResult,
    SemanticSearchUnavailableError,
    get_active_embedding_model,
    run_semantic_search,
)
from lossless_hermes.embeddings.store import (
    EmbeddedKind,
    SearchHit,
    SearchSimilarOptions,
    delete_embedding,
    drop_embeddings_triggers,
    embeddings_table_exists,
    embeddings_table_name,
    ensure_embeddings_table,
    is_embedded,
    mark_embedding_suppressed,
    record_embedding,
    register_embedding_profile,
    replace_embedding,
    search_similar,
)

__all__ = [
    "COSINE_BAND_HIGH",
    "COSINE_BAND_LOW",
    "COSINE_BAND_MEDIUM",
    "DEFAULT_K_FTS",
    "DEFAULT_K_SEMANTIC",
    "DEFAULT_TOP_N",
    "RRF_K",
    "ConfidenceBand",
    "EmbeddedKind",
    "EmbeddingProfile",
    "FtsHit",
    "FtsSearchFn",
    "HybridHit",
    "HybridSearchResult",
    "SearchHit",
    "SearchSimilarOptions",
    "SemanticHit",
    "SemanticSearchResult",
    "SemanticSearchUnavailableError",
    "delete_embedding",
    "drop_embeddings_triggers",
    "embeddings_table_exists",
    "embeddings_table_name",
    "ensure_embeddings_table",
    "get_active_embedding_model",
    "is_embedded",
    "mark_embedding_suppressed",
    "record_embedding",
    "register_embedding_profile",
    "replace_embedding",
    "run_hybrid_search",
    "run_semantic_search",
    "search_similar",
]
