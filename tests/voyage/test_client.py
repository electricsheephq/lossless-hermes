"""Port of ``lossless-claw/test/voyage-client.test.ts`` (561 LOC, 24 fixtures).

Source pin: ``lossless-claw@1f07fbd`` (branch ``pr-613``).

Every fixture below mirrors the TS test of the same name (or section). The
TS suite uses an injected mock ``fetch``; we use ``respx`` (the canonical
httpx mock router — see ADR-019 + spike 004) which intercepts the
``httpx.AsyncClient`` we hand to :class:`VoyageClient`. ``respx`` is pinned
in ``pyproject.toml`` ``[dev]`` extra.

Test inventory (numbering matches spike 004 §"Test fixtures for Phase 2"):

1.  Happy path embed (2 inputs, 200 ok)
2.  Happy path rerank (defensive sort + ``top_k`` defaults to ``len(candidates)``)
3.  ``input_type=None`` omits the field
4.  Out-of-order data response — client re-orders by ``index``
5.  Empty input list → no HTTP call (embed)
6.  Empty candidates → no HTTP call (rerank)
7.  ``output_dimension`` round-trip
8.  400 → ``VoyageError(kind="bad_request")`` with PII suppression
9.  401 → ``VoyageError(kind="auth")``, no retry
10. 403 → ``VoyageError(kind="auth")``, no retry
11. 429 with ``Retry-After: 30`` (seconds) → retry once after wait
12. 429 with ``Retry-After: <HTTP-date>`` 10s in future → waits, retries
13. 429 with ``Retry-After: 120`` (> LOCK_BUDGET_AWARE_RETRY_MS) → immediate throw (Wave-2)
14. 429 without ``Retry-After`` → exponential backoff
15. 500 → retry up to maxRetries → throw ``server_error``
16. Network error (``httpx.ConnectError``) → retry → throw ``network``
17. Per-attempt timeout (``TimeoutException``) → ``network``
18. Dim mismatch within batch → ``unexpected``
19. Bad ``index`` in response → ``unexpected``
20. Missing ``data`` array → ``unexpected``
21. Missing ``relevance_score`` in rerank item → ``unexpected``
22. ``_summarize_body`` suppresses when body contains ``"input"`` substring
23. ``_summarize_body`` clips to 200 chars when not suppressed
24. ``_safe_read_body`` clips to 800 chars with ``…(truncated)`` suffix

Plus:
* ``constants`` group — verifies all 7 hard-coded constants verbatim
* ``api_key_resolution`` — env var fallback, empty rejection, ``aclose``
* Wave-1 backoff cap verification (BACKOFF_CAP_MS == 25_000)
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from email.utils import format_datetime

import httpx
import pytest
import respx

from lossless_hermes.voyage import (
    BACKOFF_CAP_MS,
    DEFAULT_MAX_RETRIES,
    LOCK_BUDGET_AWARE_RETRY_MS,
    MAX_TOKENS_PER_EMBED_BATCH,
    MAX_TOKENS_PER_EMBED_DOC,
    MAX_TOKENS_PER_RERANK_CALL,
    RETRY_AFTER_HARD_CAP_MS,
    EmbedResult,
    RerankResult,
    VoyageClient,
    VoyageError,
)
from lossless_hermes.voyage.client import (
    BACKOFF_BASE_MS,
    DEFAULT_TIMEOUT_S,
    _backoff_ms,
    _parse_retry_after_ms,
    _safe_read_body,
    _summarize_body,
)

# pytest-asyncio is in ``auto`` mode (see ``pyproject.toml`` ``asyncio_mode``),
# so async tests are picked up implicitly — no ``pytestmark`` decoration needed.


# ---------------------------------------------------------------------------
# Constants — fixtures (28)-(30) from spec; verifies the 7 hard-coded values.
# ---------------------------------------------------------------------------


class TestConstants:
    """All 7 constants match TS verbatim (``client.ts:61-93``)."""

    def test_max_tokens_per_embed_batch_is_80k(self) -> None:
        assert MAX_TOKENS_PER_EMBED_BATCH == 80_000

    def test_max_tokens_per_embed_doc_is_27k(self) -> None:
        # LCM Wave-1: 30K observed 400s in production at the edge; 27K × 1.095
        # tokenizer inflation ≈ 29.6K, comfortably under the 32K Voyage cap.
        assert MAX_TOKENS_PER_EMBED_DOC == 27_000

    def test_max_tokens_per_rerank_call_is_600k(self) -> None:
        assert MAX_TOKENS_PER_RERANK_CALL == 600_000

    def test_default_max_retries_is_3(self) -> None:
        assert DEFAULT_MAX_RETRIES == 3

    def test_backoff_base_ms_is_500(self) -> None:
        assert BACKOFF_BASE_MS == 500

    def test_backoff_cap_ms_is_25k(self) -> None:
        # LCM Wave-1 (2025-11-XX): 25s, not 30s, to leave 5s margin under
        # WORKER_LOCK_TTL_MS=90s. Drop to 25s so worst-case retry path is
        # 25s + 30s + 30s = 85s. Original: lossless-claw/src/voyage/client.ts:91.
        assert BACKOFF_CAP_MS == 25_000

    def test_default_timeout_is_60s(self) -> None:
        assert DEFAULT_TIMEOUT_S == 60.0

    def test_retry_after_hard_cap_is_5min(self) -> None:
        assert RETRY_AFTER_HARD_CAP_MS == 5 * 60 * 1000

    def test_lock_budget_aware_retry_is_60s(self) -> None:
        assert LOCK_BUDGET_AWARE_RETRY_MS == 60_000


# ---------------------------------------------------------------------------
# Fixture helpers — respx wiring, instant-sleep monkeypatch.
# ---------------------------------------------------------------------------


@pytest.fixture
def respx_router():
    """A ``respx.MockRouter`` for the Voyage host.

    We do NOT use ``respx.mock`` as a decorator because each test needs
    fine-grained control over response sequences.
    """
    with respx.mock(base_url="https://api.voyageai.com", assert_all_called=False) as router:
        yield router


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace ``asyncio.sleep`` with a no-op that records the sleep durations.

    Returns a list that accumulates the requested sleep durations (seconds) so
    tests can assert on retry timing without actually waiting.
    """
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return sleeps


def _client(
    timeout_s: float = DEFAULT_TIMEOUT_S, max_retries: int = DEFAULT_MAX_RETRIES
) -> VoyageClient:
    """Make a client. ``respx`` intercepts inside its router context."""
    return VoyageClient(
        api_key="test-key",
        base_url="https://api.voyageai.com/v1",
        timeout_s=timeout_s,
        max_retries=max_retries,
    )


# ---------------------------------------------------------------------------
# Fixture 1 — Happy path embed.
# ---------------------------------------------------------------------------


class TestEmbedHappyPath:
    async def test_embed_two_inputs_returns_vectors_and_calls_endpoint(
        self,
        respx_router: respx.MockRouter,
    ) -> None:
        route = respx_router.post("/v1/embeddings").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {"embedding": [0.1, 0.2, 0.3], "index": 0, "object": "embedding"},
                        {"embedding": [0.4, 0.5, 0.6], "index": 1, "object": "embedding"},
                    ],
                    "model": "voyage-4-large",
                    "usage": {"total_tokens": 42},
                },
            )
        )

        client = _client()
        try:
            result = await client.embed(
                ["hello", "world"],
                model="voyage-4-large",
                input_type="document",
            )
        finally:
            await client.aclose()

        assert isinstance(result, EmbedResult)
        assert len(result.vectors) == 2
        assert result.vectors[0] == [0.1, 0.2, 0.3]
        assert result.vectors[1] == [0.4, 0.5, 0.6]
        assert result.total_tokens == 42
        assert result.model == "voyage-4-large"

        assert route.called
        req = route.calls[0].request
        assert req.method == "POST"
        assert req.headers["Authorization"] == "Bearer test-key"
        assert req.headers["Content-Type"] == "application/json"
        body = json.loads(req.content)
        assert body == {
            "model": "voyage-4-large",
            "input": ["hello", "world"],
            "truncation": False,
            "input_type": "document",
        }

    async def test_truncation_false_always_sent(
        self,
        respx_router: respx.MockRouter,
    ) -> None:
        """Lossless invariant — ``client.ts:243``."""
        route = respx_router.post("/v1/embeddings").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [{"embedding": [0.0], "index": 0}],
                    "usage": {"total_tokens": 1},
                    "model": "voyage-4-large",
                },
            )
        )
        client = _client()
        try:
            await client.embed(["x"], model="voyage-4-large", input_type="document")
        finally:
            await client.aclose()
        body = json.loads(route.calls[0].request.content)
        assert body["truncation"] is False


# ---------------------------------------------------------------------------
# Fixture 2 — Happy path rerank + defensive sort + top_k default.
# ---------------------------------------------------------------------------


class TestRerankHappyPath:
    async def test_rerank_posts_to_rerank_endpoint_joins_ids_sorts_descending(
        self,
        respx_router: respx.MockRouter,
    ) -> None:
        route = respx_router.post("/v1/rerank").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 1, "relevance_score": 0.9},
                        {"index": 0, "relevance_score": 0.4},
                    ],
                    "model": "rerank-2.5",
                    "usage": {"total_tokens": 100},
                },
            )
        )

        client = _client()
        try:
            result = await client.rerank(
                "what is foo?",
                [("leaf_a", "doc A about foo"), ("leaf_b", "doc B about bar")],
                model="rerank-2.5",
                top_k=2,
            )
        finally:
            await client.aclose()

        assert isinstance(result, RerankResult)
        assert len(result.results) == 2
        # Sorted descending by score
        assert result.results[0].id == "leaf_b"
        assert result.results[0].index == 1
        assert result.results[0].score == pytest.approx(0.9)
        assert result.results[1].id == "leaf_a"
        assert result.results[1].index == 0
        assert result.results[1].score == pytest.approx(0.4)
        assert result.total_tokens == 100
        assert result.model == "rerank-2.5"

        body = json.loads(route.calls[0].request.content)
        assert body["model"] == "rerank-2.5"
        assert body["query"] == "what is foo?"
        assert body["documents"] == ["doc A about foo", "doc B about bar"]
        assert body["top_k"] == 2
        assert body["truncation"] is False

    async def test_top_k_defaults_to_len_candidates(
        self,
        respx_router: respx.MockRouter,
    ) -> None:
        route = respx_router.post("/v1/rerank").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [{"index": 0, "relevance_score": 0.5}],
                    "model": "rerank-2.5",
                    "usage": {"total_tokens": 1},
                },
            )
        )
        client = _client()
        try:
            await client.rerank("q", [("x", "y")], model="rerank-2.5")
        finally:
            await client.aclose()
        body = json.loads(route.calls[0].request.content)
        assert body["top_k"] == 1

    async def test_rerank_defensive_sort_even_if_server_returns_unsorted(
        self,
        respx_router: respx.MockRouter,
    ) -> None:
        respx_router.post("/v1/rerank").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        # Server intentionally returns out of score order
                        {"index": 0, "relevance_score": 0.1},
                        {"index": 1, "relevance_score": 0.9},
                        {"index": 2, "relevance_score": 0.5},
                    ],
                    "model": "rerank-2.5",
                    "usage": {"total_tokens": 3},
                },
            )
        )
        client = _client()
        try:
            result = await client.rerank(
                "q",
                [("a", "doc a"), ("b", "doc b"), ("c", "doc c")],
                model="rerank-2.5",
            )
        finally:
            await client.aclose()
        scores = [r.score for r in result.results]
        assert scores == sorted(scores, reverse=True)
        assert result.results[0].id == "b"  # highest score (0.9)


# ---------------------------------------------------------------------------
# Fixture 3 — input_type=None omits the field.
# ---------------------------------------------------------------------------


class TestInputTypeOmission:
    async def test_input_type_none_omits_field(
        self,
        respx_router: respx.MockRouter,
    ) -> None:
        route = respx_router.post("/v1/embeddings").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [{"embedding": [1, 2], "index": 0}],
                    "usage": {"total_tokens": 1},
                    "model": "voyage-4-large",
                },
            )
        )
        client = _client()
        try:
            await client.embed(["x"], model="voyage-4-large", input_type=None)
        finally:
            await client.aclose()
        body = json.loads(route.calls[0].request.content)
        assert "input_type" not in body


# ---------------------------------------------------------------------------
# Fixture 4 — Out-of-order data response re-ordered by index.
# ---------------------------------------------------------------------------


class TestOutOfOrderResponse:
    async def test_response_reordered_by_index_field(
        self,
        respx_router: respx.MockRouter,
    ) -> None:
        respx_router.post("/v1/embeddings").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        # Voyage might (in theory) return out of order
                        {"embedding": [0.9, 0.9], "index": 1},
                        {"embedding": [0.1, 0.1], "index": 0},
                    ],
                    "usage": {"total_tokens": 2},
                    "model": "voyage-4-large",
                },
            )
        )
        client = _client()
        try:
            result = await client.embed(
                ["first", "second"],
                model="voyage-4-large",
                input_type="query",
            )
        finally:
            await client.aclose()
        assert result.vectors[0] == [0.1, 0.1]
        assert result.vectors[1] == [0.9, 0.9]


# ---------------------------------------------------------------------------
# Fixtures 5/6 — Empty input lists, no HTTP call.
# ---------------------------------------------------------------------------


class TestEmptyInputs:
    async def test_embed_empty_list_returns_empty_no_http_call(
        self,
        respx_router: respx.MockRouter,
    ) -> None:
        route = respx_router.post("/v1/embeddings")
        # No mock set → respx asserts no call was made.
        client = _client()
        try:
            result = await client.embed([], model="voyage-4-large", input_type="query")
        finally:
            await client.aclose()
        assert result.vectors == []
        assert result.total_tokens == 0
        assert result.model == "voyage-4-large"
        assert not route.called

    async def test_rerank_empty_candidates_returns_empty_no_http_call(
        self,
        respx_router: respx.MockRouter,
    ) -> None:
        route = respx_router.post("/v1/rerank")
        client = _client()
        try:
            result = await client.rerank("q", [], model="rerank-2.5")
        finally:
            await client.aclose()
        assert result.results == []
        assert result.total_tokens == 0
        assert result.model == "rerank-2.5"
        assert not route.called


# ---------------------------------------------------------------------------
# Fixture 7 — output_dimension round-trip (Wave-11 fix).
# ---------------------------------------------------------------------------


class TestOutputDimension:
    async def test_output_dimension_forwarded_when_set(
        self,
        respx_router: respx.MockRouter,
    ) -> None:
        # LCM Wave-11: forward output_dimension so non-default-dim vec0
        # profiles get those dims back (client.ts:252).
        route = respx_router.post("/v1/embeddings").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [{"embedding": list(range(512)), "index": 0}],
                    "usage": {"total_tokens": 1},
                    "model": "voyage-4-large",
                },
            )
        )
        client = _client()
        try:
            await client.embed(
                ["x"],
                model="voyage-4-large",
                input_type="document",
                output_dimension=512,
            )
        finally:
            await client.aclose()
        body = json.loads(route.calls[0].request.content)
        assert body["output_dimension"] == 512

    async def test_output_dimension_omitted_when_none(
        self,
        respx_router: respx.MockRouter,
    ) -> None:
        route = respx_router.post("/v1/embeddings").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [{"embedding": [0.0], "index": 0}],
                    "usage": {"total_tokens": 1},
                    "model": "voyage-4-large",
                },
            )
        )
        client = _client()
        try:
            await client.embed(["x"], model="voyage-4-large", input_type="document")
        finally:
            await client.aclose()
        body = json.loads(route.calls[0].request.content)
        assert "output_dimension" not in body

    async def test_output_dimension_zero_omitted(
        self,
        respx_router: respx.MockRouter,
    ) -> None:
        """Mirrors the TS guard at ``client.ts:252`` (``opts.outputDimension > 0``)."""
        route = respx_router.post("/v1/embeddings").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [{"embedding": [0.0], "index": 0}],
                    "usage": {"total_tokens": 1},
                    "model": "voyage-4-large",
                },
            )
        )
        client = _client()
        try:
            await client.embed(
                ["x"],
                model="voyage-4-large",
                input_type="document",
                output_dimension=0,
            )
        finally:
            await client.aclose()
        body = json.loads(route.calls[0].request.content)
        assert "output_dimension" not in body


# ---------------------------------------------------------------------------
# Fixture 8 — 400 → bad_request with PII suppression.
# ---------------------------------------------------------------------------


class TestBadRequest:
    async def test_400_throws_bad_request_no_retry_suppresses_input_echo(
        self,
        respx_router: respx.MockRouter,
    ) -> None:
        # Voyage 400 sometimes echoes input — should NOT appear in error
        # message or response_body (Wave-4 fix).
        route = respx_router.post("/v1/embeddings").mock(
            return_value=httpx.Response(
                400,
                json={
                    "error": "input too long",
                    "input": "secret payload that should not leak",
                },
            )
        )
        client = _client(max_retries=3)
        try:
            with pytest.raises(VoyageError) as exc_info:
                await client.embed(["x"], model="voyage-4-large", input_type="document")
        finally:
            await client.aclose()

        err = exc_info.value
        assert err.kind == "bad_request"
        assert err.status == 400
        assert "secret payload" not in str(err)
        # Wave-4 fix: response_body MUST also be suppressed.
        assert err.response_body is not None
        assert "secret payload" not in err.response_body
        assert "input echoed in error body" in err.response_body
        # No retry — exactly one call.
        assert route.call_count == 1


# ---------------------------------------------------------------------------
# Fixtures 9/10 — 401/403 → auth, no retry.
# ---------------------------------------------------------------------------


class TestAuthErrors:
    async def test_401_throws_auth_no_retry(
        self,
        respx_router: respx.MockRouter,
    ) -> None:
        route = respx_router.post("/v1/embeddings").mock(
            return_value=httpx.Response(401, json={"detail": "Provided API key is invalid."})
        )
        client = _client(max_retries=3)
        try:
            with pytest.raises(VoyageError) as exc_info:
                await client.embed(["x"], model="voyage-4-large", input_type="document")
        finally:
            await client.aclose()
        assert exc_info.value.kind == "auth"
        assert exc_info.value.status == 401
        assert route.call_count == 1

    async def test_403_throws_auth_no_retry(
        self,
        respx_router: respx.MockRouter,
    ) -> None:
        route = respx_router.post("/v1/embeddings").mock(
            return_value=httpx.Response(403, json={"detail": "Forbidden."})
        )
        client = _client(max_retries=3)
        try:
            with pytest.raises(VoyageError) as exc_info:
                await client.embed(["x"], model="voyage-4-large", input_type="document")
        finally:
            await client.aclose()
        assert exc_info.value.kind == "auth"
        assert exc_info.value.status == 403
        assert route.call_count == 1


# ---------------------------------------------------------------------------
# Fixtures 11/12/13/14 — 429 / Retry-After behavior.
# ---------------------------------------------------------------------------


class TestRateLimit429:
    async def test_429_no_retries_surfaces_retry_after(
        self,
        respx_router: respx.MockRouter,
    ) -> None:
        """Mirrors ``voyage-client.test.ts`` "throws VoyageError(rate_limit)
        on persistent 429" — maxRetries=0 surfaces immediately with the
        server-supplied ``retry_after_ms``.
        """
        respx_router.post("/v1/embeddings").mock(
            return_value=httpx.Response(
                429,
                json={"error": "slow down"},
                headers={"Retry-After": "2"},
            )
        )
        client = _client(max_retries=0)
        try:
            with pytest.raises(VoyageError) as exc_info:
                await client.embed(["x"], model="voyage-4-large", input_type="document")
        finally:
            await client.aclose()
        assert exc_info.value.kind == "rate_limit"
        assert exc_info.value.status == 429
        assert exc_info.value.retry_after_ms == 2000

    async def test_429_short_retry_after_honored_then_retries(
        self,
        respx_router: respx.MockRouter,
        no_sleep: list[float],
    ) -> None:
        """``Retry-After: 0.05`` (50ms) ≤ LOCK_BUDGET_AWARE_RETRY_MS → sleep
        then retry, second attempt returns 200.
        """
        respx_router.post("/v1/embeddings").mock(
            side_effect=[
                httpx.Response(
                    429,
                    json={"error": "slow down"},
                    headers={"Retry-After": "0.05"},
                ),
                httpx.Response(
                    200,
                    json={
                        "data": [{"embedding": [0.1, 0.2, 0.3], "index": 0}],
                        "usage": {"total_tokens": 5},
                        "model": "voyage-4-large",
                    },
                ),
            ]
        )
        client = _client(max_retries=1)
        try:
            result = await client.embed(["x"], model="voyage-4-large", input_type="document")
        finally:
            await client.aclose()
        assert result.total_tokens == 5
        # Slept the server-supplied 50ms exactly (parsed → 50ms).
        assert no_sleep == [pytest.approx(0.05)]

    async def test_429_retry_after_120s_immediate_throw_wave_2_fix(
        self,
        respx_router: respx.MockRouter,
        no_sleep: list[float],
    ) -> None:
        """LCM Wave-2: Retry-After > LOCK_BUDGET_AWARE_RETRY_MS throws
        immediately rather than sleeping past the worker-lock TTL.

        Mirrors the TS test "Retry-After > 60s threshold throws immediately
        (does NOT sleep)" at ``voyage-client.test.ts:251-281``.
        Original: lossless-claw/src/voyage/client.ts:515.
        """
        route = respx_router.post("/v1/embeddings").mock(
            return_value=httpx.Response(
                429,
                json={"error": "slow down"},
                headers={"Retry-After": "120"},  # 2 min > 60s threshold
            )
        )
        client = _client(max_retries=2)  # Allow retries — Wave-2 says ignore
        try:
            with pytest.raises(VoyageError) as exc_info:
                await client.embed(["x"], model="voyage-4-large", input_type="document")
        finally:
            await client.aclose()
        assert exc_info.value.kind == "rate_limit"
        assert exc_info.value.retry_after_ms == 120_000  # server value preserved
        assert route.call_count == 1  # ONE call, no retries
        assert no_sleep == []  # Did NOT sleep

    async def test_429_http_date_retry_after_10s_in_future_honored(
        self,
        respx_router: respx.MockRouter,
        no_sleep: list[float],
    ) -> None:
        """HTTP-date form of ``Retry-After``. 10s in future → sleep ~10s."""
        future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=10)
        retry_after_date = format_datetime(future, usegmt=True)
        respx_router.post("/v1/embeddings").mock(
            side_effect=[
                httpx.Response(
                    429,
                    json={"error": "slow down"},
                    headers={"Retry-After": retry_after_date},
                ),
                httpx.Response(
                    200,
                    json={
                        "data": [{"embedding": [0.0], "index": 0}],
                        "usage": {"total_tokens": 1},
                        "model": "voyage-4-large",
                    },
                ),
            ]
        )
        client = _client(max_retries=1)
        try:
            await client.embed(["x"], model="voyage-4-large", input_type="document")
        finally:
            await client.aclose()
        # Should have slept ~10s (allow 2s wiggle for parse-time delta).
        assert len(no_sleep) == 1
        assert 8.0 <= no_sleep[0] <= 11.0

    async def test_429_no_retry_after_falls_back_to_exponential_backoff(
        self,
        respx_router: respx.MockRouter,
        no_sleep: list[float],
    ) -> None:
        """``Retry-After`` missing → exponential backoff (500ms, 1000ms, ...)."""
        respx_router.post("/v1/embeddings").mock(
            return_value=httpx.Response(429, json={"error": "slow"})
        )
        client = _client(max_retries=2)
        try:
            with pytest.raises(VoyageError) as exc_info:
                await client.embed(["x"], model="voyage-4-large", input_type="document")
        finally:
            await client.aclose()
        assert exc_info.value.kind == "rate_limit"
        # Initial + 2 retries; slept between each (so 2 sleeps).
        # Backoff schedule 500ms, 1000ms.
        assert no_sleep == [pytest.approx(0.5), pytest.approx(1.0)]


# ---------------------------------------------------------------------------
# Fixture 15 — 5xx retry then exhaust.
# ---------------------------------------------------------------------------


class TestServerError5xx:
    async def test_5xx_retries_until_success(
        self,
        respx_router: respx.MockRouter,
        no_sleep: list[float],
    ) -> None:
        respx_router.post("/v1/embeddings").mock(
            side_effect=[
                httpx.Response(503, json={"error": "internal"}),
                httpx.Response(503, json={"error": "internal"}),
                httpx.Response(
                    200,
                    json={
                        "data": [{"embedding": [0.5], "index": 0}],
                        "usage": {"total_tokens": 1},
                        "model": "voyage-4-large",
                    },
                ),
            ]
        )
        client = _client(max_retries=3)
        try:
            result = await client.embed(["x"], model="voyage-4-large", input_type="document")
        finally:
            await client.aclose()
        assert len(result.vectors) == 1
        # Two retries before success.
        assert no_sleep == [pytest.approx(0.5), pytest.approx(1.0)]

    async def test_5xx_exhausted_throws_server_error(
        self,
        respx_router: respx.MockRouter,
        no_sleep: list[float],
    ) -> None:
        route = respx_router.post("/v1/embeddings").mock(
            return_value=httpx.Response(500, json={"error": "internal"})
        )
        client = _client(max_retries=1)
        try:
            with pytest.raises(VoyageError) as exc_info:
                await client.embed(["x"], model="voyage-4-large", input_type="document")
        finally:
            await client.aclose()
        assert exc_info.value.kind == "server_error"
        assert exc_info.value.status == 500
        assert route.call_count == 2  # initial + 1 retry


# ---------------------------------------------------------------------------
# Fixtures 16/17 — Network errors + timeouts.
# ---------------------------------------------------------------------------


class TestNetworkErrors:
    async def test_connect_error_throws_network(
        self,
        respx_router: respx.MockRouter,
    ) -> None:
        respx_router.post("/v1/embeddings").mock(side_effect=httpx.ConnectError("ECONNREFUSED"))
        client = _client(max_retries=0)
        try:
            with pytest.raises(VoyageError) as exc_info:
                await client.embed(["x"], model="voyage-4-large", input_type="document")
        finally:
            await client.aclose()
        assert exc_info.value.kind == "network"
        assert "ECONNREFUSED" in str(exc_info.value)

    async def test_timeout_exception_maps_to_network(
        self,
        respx_router: respx.MockRouter,
    ) -> None:
        respx_router.post("/v1/embeddings").mock(side_effect=httpx.ReadTimeout("read timed out"))
        client = _client(max_retries=0)
        try:
            with pytest.raises(VoyageError) as exc_info:
                await client.embed(["x"], model="voyage-4-large", input_type="document")
        finally:
            await client.aclose()
        assert exc_info.value.kind == "network"

    async def test_network_retries_then_exhausts(
        self,
        respx_router: respx.MockRouter,
        no_sleep: list[float],
    ) -> None:
        respx_router.post("/v1/embeddings").mock(side_effect=httpx.ConnectError("DNS"))
        client = _client(max_retries=2)
        try:
            with pytest.raises(VoyageError) as exc_info:
                await client.embed(["x"], model="voyage-4-large", input_type="document")
        finally:
            await client.aclose()
        assert exc_info.value.kind == "network"
        # 2 sleeps (between the 3 attempts).
        assert no_sleep == [pytest.approx(0.5), pytest.approx(1.0)]


# ---------------------------------------------------------------------------
# Fixtures 18/19/20/21 — Malformed response handling (kind="unexpected").
# ---------------------------------------------------------------------------


class TestMalformedResponses:
    async def test_response_data_length_mismatch_throws_unexpected(
        self,
        respx_router: respx.MockRouter,
    ) -> None:
        respx_router.post("/v1/embeddings").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [{"embedding": [0.5], "index": 0}],  # sent 2, got 1
                    "usage": {"total_tokens": 1},
                    "model": "voyage-4-large",
                },
            )
        )
        client = _client()
        try:
            with pytest.raises(VoyageError) as exc_info:
                await client.embed(
                    ["a", "b"],
                    model="voyage-4-large",
                    input_type="document",
                )
        finally:
            await client.aclose()
        assert exc_info.value.kind == "unexpected"

    async def test_dim_mismatch_within_batch_throws_unexpected(
        self,
        respx_router: respx.MockRouter,
    ) -> None:
        respx_router.post("/v1/embeddings").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {"embedding": [0.1, 0.2, 0.3], "index": 0},
                        {"embedding": [0.4, 0.5], "index": 1},  # wrong dim
                    ],
                    "usage": {"total_tokens": 2},
                    "model": "voyage-4-large",
                },
            )
        )
        client = _client()
        try:
            with pytest.raises(VoyageError) as exc_info:
                await client.embed(
                    ["a", "b"],
                    model="voyage-4-large",
                    input_type="document",
                )
        finally:
            await client.aclose()
        assert exc_info.value.kind == "unexpected"
        assert "dimension mismatch" in str(exc_info.value)

    async def test_embed_bad_index_throws_unexpected(
        self,
        respx_router: respx.MockRouter,
    ) -> None:
        respx_router.post("/v1/embeddings").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {"embedding": [0.1, 0.2], "index": 99},  # out of [0,2)
                        {"embedding": [0.3, 0.4], "index": 0},
                    ],
                    "usage": {"total_tokens": 2},
                    "model": "voyage-4-large",
                },
            )
        )
        client = _client()
        try:
            with pytest.raises(VoyageError) as exc_info:
                await client.embed(
                    ["a", "b"],
                    model="voyage-4-large",
                    input_type="document",
                )
        finally:
            await client.aclose()
        assert exc_info.value.kind == "unexpected"

    async def test_missing_data_array_throws_unexpected(
        self,
        respx_router: respx.MockRouter,
    ) -> None:
        respx_router.post("/v1/embeddings").mock(return_value=httpx.Response(200, json={}))
        client = _client()
        try:
            with pytest.raises(VoyageError) as exc_info:
                await client.embed(["a"], model="voyage-4-large", input_type="document")
        finally:
            await client.aclose()
        assert exc_info.value.kind == "unexpected"

    async def test_rerank_missing_relevance_score_throws_unexpected(
        self,
        respx_router: respx.MockRouter,
    ) -> None:
        respx_router.post("/v1/rerank").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [{"index": 0}],  # missing relevance_score
                    "usage": {"total_tokens": 1},
                    "model": "rerank-2.5",
                },
            )
        )
        client = _client()
        try:
            with pytest.raises(VoyageError) as exc_info:
                await client.rerank("q", [("a", "b")], model="rerank-2.5")
        finally:
            await client.aclose()
        assert exc_info.value.kind == "unexpected"

    async def test_rerank_invalid_index_throws_unexpected(
        self,
        respx_router: respx.MockRouter,
    ) -> None:
        respx_router.post("/v1/rerank").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [{"index": 99, "relevance_score": 0.5}],
                    "usage": {"total_tokens": 1},
                    "model": "rerank-2.5",
                },
            )
        )
        client = _client()
        try:
            with pytest.raises(VoyageError) as exc_info:
                await client.rerank("q", [("a", "b")], model="rerank-2.5")
        finally:
            await client.aclose()
        assert exc_info.value.kind == "unexpected"


# ---------------------------------------------------------------------------
# Fixtures 22/23/24 — Body-summarization helpers.
# ---------------------------------------------------------------------------


class TestSummarizeBody:
    """Wave-4 + Wave-7 PII suppression rule (``client.ts:569-576``)."""

    def test_suppresses_when_input_key_present(self) -> None:
        body = '{"detail": "bad request", "input": "secret-text"}'
        assert _summarize_body(body) == "input echoed in error body — suppressed for privacy"

    def test_suppresses_when_texts_key_present(self) -> None:
        body = '{"texts": ["x"], "detail": "bad"}'
        assert _summarize_body(body) == "input echoed in error body — suppressed for privacy"

    def test_suppresses_when_documents_key_present(self) -> None:
        body = '{"documents": ["x"], "detail": "bad"}'
        assert _summarize_body(body) == "input echoed in error body — suppressed for privacy"

    def test_clips_to_200_chars_when_not_suppressed(self) -> None:
        body = "x" * 500
        assert _summarize_body(body) == "x" * 200

    def test_preserves_short_body_when_not_suppressed(self) -> None:
        body = '{"detail": "Model voyage-INVALID is not supported."}'
        assert _summarize_body(body) == body


class TestSafeReadBody:
    """``_safe_read_body`` clips to 800 chars with ``…(truncated)`` suffix
    (``client.ts:560-567``)."""

    def test_clips_body_over_800_chars_with_truncated_suffix(self) -> None:
        # We need an httpx.Response to drive this — build a fake one.
        long_body = "y" * 900
        resp = httpx.Response(200, content=long_body)
        out = _safe_read_body(resp)
        assert len(out) == 800 + len("…(truncated)")
        assert out.endswith("…(truncated)")
        assert out[:800] == "y" * 800

    def test_preserves_short_body(self) -> None:
        resp = httpx.Response(200, content="short body")
        assert _safe_read_body(resp) == "short body"


# ---------------------------------------------------------------------------
# API key resolution + aclose contract.
# ---------------------------------------------------------------------------


class TestApiKeyResolution:
    async def test_no_api_key_raises_voyage_error_auth(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
        with pytest.raises(VoyageError) as exc_info:
            VoyageClient()
        assert exc_info.value.kind == "auth"

    async def test_empty_api_key_raises_voyage_error_auth(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("VOYAGE_API_KEY", "   ")
        with pytest.raises(VoyageError) as exc_info:
            VoyageClient()
        assert exc_info.value.kind == "auth"

    async def test_uses_env_var_when_no_apikey_opt(
        self,
        respx_router: respx.MockRouter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("VOYAGE_API_KEY", "from-env-test")
        route = respx_router.post("/v1/embeddings").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [{"embedding": [0.0], "index": 0}],
                    "usage": {"total_tokens": 1},
                    "model": "voyage-4-large",
                },
            )
        )
        client = VoyageClient(base_url="https://api.voyageai.com/v1")
        try:
            await client.embed(["x"], model="voyage-4-large", input_type="document")
        finally:
            await client.aclose()
        assert route.calls[0].request.headers["Authorization"] == "Bearer from-env-test"


class TestClientLifecycle:
    """:meth:`VoyageClient.aclose` closes the underlying httpx client; tests
    verify no leaked connections after a happy-path embed + rerank.
    """

    async def test_aclose_closes_owned_client(
        self,
        respx_router: respx.MockRouter,
    ) -> None:
        respx_router.post("/v1/embeddings").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [{"embedding": [0.0], "index": 0}],
                    "usage": {"total_tokens": 1},
                    "model": "voyage-4-large",
                },
            )
        )
        respx_router.post("/v1/rerank").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [{"index": 0, "relevance_score": 0.5}],
                    "usage": {"total_tokens": 1},
                    "model": "rerank-2.5",
                },
            )
        )
        client = VoyageClient(
            api_key="k",
            base_url="https://api.voyageai.com/v1",
        )
        await client.embed(["x"], model="voyage-4-large", input_type="document")
        await client.rerank("q", [("a", "b")], model="rerank-2.5")
        await client.aclose()
        # After aclose, the underlying httpx client is closed.
        assert client._client.is_closed  # type: ignore[attr-defined]

    async def test_async_context_manager_closes_on_exit(
        self,
        respx_router: respx.MockRouter,
    ) -> None:
        respx_router.post("/v1/embeddings").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [{"embedding": [0.0], "index": 0}],
                    "usage": {"total_tokens": 1},
                    "model": "voyage-4-large",
                },
            )
        )
        async with VoyageClient(
            api_key="k",
            base_url="https://api.voyageai.com/v1",
        ) as client:
            await client.embed(["x"], model="voyage-4-large", input_type="document")
        assert client._client.is_closed  # type: ignore[attr-defined]

    async def test_does_not_close_injected_client(
        self,
        respx_router: respx.MockRouter,
    ) -> None:
        """When a caller injects an ``httpx.AsyncClient``, we do not own it
        and ``aclose`` must NOT close it.
        """
        respx_router.post("/v1/embeddings").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [{"embedding": [0.0], "index": 0}],
                    "usage": {"total_tokens": 1},
                    "model": "voyage-4-large",
                },
            )
        )
        outer = httpx.AsyncClient()
        try:
            client = VoyageClient(
                api_key="k",
                base_url="https://api.voyageai.com/v1",
                client=outer,
            )
            await client.embed(["x"], model="voyage-4-large", input_type="document")
            await client.aclose()
            assert not outer.is_closed
        finally:
            await outer.aclose()


# ---------------------------------------------------------------------------
# Module-private helpers — direct unit tests for the small functions.
# ---------------------------------------------------------------------------


class TestParseRetryAfterMs:
    def test_returns_none_for_none(self) -> None:
        assert _parse_retry_after_ms(None) is None

    def test_returns_none_for_empty_string(self) -> None:
        assert _parse_retry_after_ms("") is None

    def test_integer_seconds(self) -> None:
        assert _parse_retry_after_ms("30") == 30_000

    def test_fractional_seconds(self) -> None:
        assert _parse_retry_after_ms("0.5") == 500

    def test_zero_seconds(self) -> None:
        assert _parse_retry_after_ms("0") == 0

    def test_clamped_at_5min(self) -> None:
        # 1 hour → clamped to 5min.
        assert _parse_retry_after_ms("3600") == RETRY_AFTER_HARD_CAP_MS

    def test_http_date_in_future(self) -> None:
        future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=30)
        out = _parse_retry_after_ms(format_datetime(future, usegmt=True))
        assert out is not None
        assert 28_000 <= out <= 31_000

    def test_http_date_in_past_returns_none(self) -> None:
        past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=30)
        assert _parse_retry_after_ms(format_datetime(past, usegmt=True)) is None

    def test_garbage_returns_none(self) -> None:
        assert _parse_retry_after_ms("not-a-date-and-not-a-number") is None

    def test_negative_number_falls_through_to_date_then_none(self) -> None:
        assert _parse_retry_after_ms("-5") is None


class TestBackoffMs:
    def test_exponential_schedule(self) -> None:
        assert _backoff_ms(0) == 500
        assert _backoff_ms(1) == 1_000
        assert _backoff_ms(2) == 2_000
        assert _backoff_ms(3) == 4_000
        assert _backoff_ms(4) == 8_000
        assert _backoff_ms(5) == 16_000

    def test_capped_at_25k(self) -> None:
        # 2^6 * 500 = 32K → capped at 25K (Wave-1 fix).
        assert _backoff_ms(6) == BACKOFF_CAP_MS == 25_000
        assert _backoff_ms(20) == BACKOFF_CAP_MS


# ---------------------------------------------------------------------------
# "Float32 parity smoke test" — verifies the embedding is parsed as a clean
# list[float] with the expected dim/usage shape.
# ---------------------------------------------------------------------------


class TestFloat32ParitySmoke:
    """The Float32 storage boundary is the caller's responsibility (cast to
    ``numpy.float32`` at vec0 INSERT). The client itself returns ``list[float]``
    from JSON — this smoke test asserts that.
    """

    async def test_embed_returns_unit_normalized_vector_shape(
        self,
        respx_router: respx.MockRouter,
    ) -> None:
        # Mock a 1024-dim unit-normalized vector (mirrors Voyage's contract:
        # vectors are L2-unit-normalized so cosine == dot product).
        import math

        n = 1024
        v = [1.0] * n
        # Manually normalize so sqrt(sum(x^2)) == 1.0.
        norm = math.sqrt(sum(x * x for x in v))
        v = [x / norm for x in v]
        respx_router.post("/v1/embeddings").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [{"embedding": v, "index": 0}],
                    "usage": {"total_tokens": 6},
                    "model": "voyage-4-large",
                },
            )
        )
        client = _client()
        try:
            result = await client.embed(
                ["how does RAG work?"],
                model="voyage-4-large",
                input_type="query",
            )
        finally:
            await client.aclose()
        assert len(result.vectors) == 1
        assert len(result.vectors[0]) == 1024
        # L2 norm ≈ 1.0 (unit-normalized).
        squared = sum(x * x for x in result.vectors[0])
        assert math.isclose(squared, 1.0, abs_tol=1e-9)


# ---------------------------------------------------------------------------
# Other 4xx (not 400/401/403/429) is treated as bad_request without retry.
# ---------------------------------------------------------------------------


class TestOther4xx:
    async def test_404_throws_bad_request_no_retry(
        self,
        respx_router: respx.MockRouter,
    ) -> None:
        route = respx_router.post("/v1/embeddings").mock(
            return_value=httpx.Response(404, json={"detail": "not found"})
        )
        client = _client(max_retries=3)
        try:
            with pytest.raises(VoyageError) as exc_info:
                await client.embed(["x"], model="voyage-4-large", input_type="document")
        finally:
            await client.aclose()
        assert exc_info.value.kind == "bad_request"
        assert exc_info.value.status == 404
        assert route.call_count == 1  # no retries
