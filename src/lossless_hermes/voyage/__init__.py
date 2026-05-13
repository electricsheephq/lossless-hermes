"""Voyage AI HTTP client (port of ``lossless-claw/src/voyage/client.ts``).

Exposes the embedding + reranker entrypoints used by the embedding store
and hybrid-search subsystems. The retry/backoff loop is hand-rolled per
ADR-019 (`httpx` for HTTP, no `tenacity` in the hot path) and carries
inline ``# LCM Wave-N`` provenance comments per ADR-029 for the load-bearing
scar-tissue fixes (Wave-1 backoff cap, Wave-2 lock-budget-aware Retry-After,
Wave-4 + Wave-7 PII suppression, Wave-11 ``output_dimension`` forwarding).

The Float32 storage boundary is the *caller's* responsibility — the client
returns ``list[float]`` from JSON. Cast to ``numpy.float32`` (or use
``sqlite_vec.serialize_float32``) at the vec0 INSERT site to preserve the
TS ``Float32Array`` precision behavior.
"""

from __future__ import annotations

from lossless_hermes.voyage.client import (
    BACKOFF_BASE_MS,
    BACKOFF_CAP_MS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_S,
    LOCK_BUDGET_AWARE_RETRY_MS,
    MAX_TOKENS_PER_EMBED_BATCH,
    MAX_TOKENS_PER_EMBED_DOC,
    MAX_TOKENS_PER_RERANK_CALL,
    RETRY_AFTER_HARD_CAP_MS,
    VOYAGE_API_BASE,
    EmbedResult,
    RerankItem,
    RerankResult,
    VoyageClient,
    VoyageError,
    VoyageErrorKind,
)

__all__ = [
    "BACKOFF_BASE_MS",
    "BACKOFF_CAP_MS",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_TIMEOUT_S",
    "EmbedResult",
    "LOCK_BUDGET_AWARE_RETRY_MS",
    "MAX_TOKENS_PER_EMBED_BATCH",
    "MAX_TOKENS_PER_EMBED_DOC",
    "MAX_TOKENS_PER_RERANK_CALL",
    "RETRY_AFTER_HARD_CAP_MS",
    "RerankItem",
    "RerankResult",
    "VOYAGE_API_BASE",
    "VoyageClient",
    "VoyageError",
    "VoyageErrorKind",
]
