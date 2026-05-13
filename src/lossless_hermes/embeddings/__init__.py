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
* (Future) ``semantic_search.py`` — KNN retrieval surface that calls into
  :func:`store.search_similar` (issue 05-08).

The Voyage HTTP client lives in :mod:`lossless_hermes.voyage`, not here —
the embeddings store consumes vectors from any source and is intentionally
decoupled from the embedding provider.

See:

* ``docs/porting-guides/embeddings.md`` §"sqlite-vec store"
* ``docs/spike-results/001-sqlite-vec-python.md`` — extension loading PASS
* ``docs/adr/004-sqlite3-backend.md`` — stdlib primary, apsw fallback
"""

from __future__ import annotations

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
    "EmbeddedKind",
    "SearchHit",
    "SearchSimilarOptions",
    "delete_embedding",
    "drop_embeddings_triggers",
    "embeddings_table_exists",
    "embeddings_table_name",
    "ensure_embeddings_table",
    "is_embedded",
    "mark_embedding_suppressed",
    "record_embedding",
    "register_embedding_profile",
    "replace_embedding",
    "search_similar",
]
