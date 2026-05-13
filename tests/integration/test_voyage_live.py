"""Live Voyage round-trip — gated on ``VOYAGE_API_KEY`` env var.

Promoted from spike 004's ``/tmp/voyage-spike/roundtrip.py`` (the
spike's live-verified pattern). Runs only on nightly CI; skipped
otherwise. Asserts:

* ``voyage-4-large`` returns ``dim=1024`` by default.
* Vectors are L2-unit-normalized (``‖v‖² ≈ 1.0 ± 1e-3``).
* Embed p99 latency < 5 s on a 3-input batch.
* Rerank p99 latency < 3 s on a 3-candidate set.
* Semantic ordering: the RAG-adjacent document ranks higher than an
  unrelated httpx-library document for the query "How does RAG work?".

See ``docs/spike-results/004-voyage-python-client.md`` §"Roundtrip test
result" for the live numbers from spike 004 (510ms embed, 304ms rerank
against production Voyage on first attempt).
"""

from __future__ import annotations

import math
import os
import time

import pytest

from lossless_hermes.voyage import VoyageClient

pytestmark = pytest.mark.live_voyage


@pytest.fixture
def voyage_api_key() -> str:
    key = os.environ.get("VOYAGE_API_KEY", "").strip()
    if not key:
        pytest.skip("VOYAGE_API_KEY not set — gated live test (Wave 6 contract)")
    return key


async def test_voyage_embed_returns_unit_normalized_1024_dim(voyage_api_key: str) -> None:
    client = VoyageClient(api_key=voyage_api_key)
    try:
        t0 = time.perf_counter()
        result = await client.embed(
            [
                "Voyage AI provides embedding models for retrieval-augmented generation.",
                "Python's httpx library supports both sync and async HTTP requests.",
                "Lossless context multiplication uses hierarchical recall.",
            ],
            model="voyage-4-large",
            input_type="document",
        )
        elapsed_s = time.perf_counter() - t0
    finally:
        await client.aclose()

    # Shape.
    assert len(result.vectors) == 3
    for v in result.vectors:
        assert len(v) == 1024
    # L2 unit-normalized (Voyage contract: cosine == dot product).
    for v in result.vectors:
        norm2 = sum(x * x for x in v)
        assert math.isclose(norm2, 1.0, abs_tol=1e-3), f"‖v‖² = {norm2}, expected ≈ 1.0"
    # Reasonable token count.
    assert result.total_tokens > 0
    # Latency budget (generous — spike measured 510ms; CI may be slower).
    assert elapsed_s < 5.0, f"embed took {elapsed_s:.2f}s, expected < 5.0s"


async def test_voyage_rerank_semantic_ordering(voyage_api_key: str) -> None:
    client = VoyageClient(api_key=voyage_api_key)
    try:
        t0 = time.perf_counter()
        result = await client.rerank(
            "How does RAG work?",
            [
                (
                    "doc_0",
                    "Voyage AI provides embedding models for retrieval-augmented generation.",
                ),
                ("doc_1", "Python's httpx library supports both sync and async HTTP requests."),
                ("doc_2", "Lossless context multiplication uses hierarchical recall."),
            ],
            model="rerank-2.5",
        )
        elapsed_s = time.perf_counter() - t0
    finally:
        await client.aclose()

    # Shape.
    assert len(result.results) == 3
    # Defensive sort — Voyage sorts but we re-sort.
    scores = [r.score for r in result.results]
    assert scores == sorted(scores, reverse=True)
    # Semantic ordering: doc_0 (RAG-adjacent) should outrank doc_1 (unrelated httpx).
    rank_by_id = {r.id: i for i, r in enumerate(result.results)}
    assert rank_by_id["doc_0"] < rank_by_id["doc_1"], (
        "RAG-adjacent doc_0 should rank higher than unrelated doc_1; got "
        f"doc_0 rank={rank_by_id['doc_0']}, doc_1 rank={rank_by_id['doc_1']}"
    )
    # Latency budget.
    assert elapsed_s < 3.0, f"rerank took {elapsed_s:.2f}s, expected < 3.0s"


async def test_voyage_output_dimension_round_trip(voyage_api_key: str) -> None:
    """Wave-11 fix verification: ``output_dimension=512`` returns a 512-dim
    vector, not the model default of 1024.
    """
    client = VoyageClient(api_key=voyage_api_key)
    try:
        result = await client.embed(
            ["hello world"],
            model="voyage-4-large",
            input_type="document",
            output_dimension=512,
        )
    finally:
        await client.aclose()

    assert len(result.vectors) == 1
    assert len(result.vectors[0]) == 512
