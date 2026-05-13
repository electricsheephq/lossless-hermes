"""Voyage AI HTTP client — port of ``lossless-claw/src/voyage/client.ts``.

Source pin: ``lossless-claw@1f07fbd`` (branch ``pr-613``).

Design constraints baked in here (carried verbatim from the TS source — see
the TS docstring at ``client.ts:1-54`` for the original rationale):

1. **Per-batch token budget cap.** Voyage server cap is 120K; we batch at
   :data:`MAX_TOKENS_PER_EMBED_BATCH` = 80K. Voyage's tokenizer counts ~9.5%
   higher than our stored ``summaries.token_count`` (Phase-A spike finding),
   so 80K × 1.10 = 88K << 120K — safe margin.

2. **Rate-limit budget visible to caller.** 429 responses carry ``Retry-After``
   (seconds or HTTP-date). The Wave-2 fix below clamps lock-budget-aware: if
   the server hint exceeds :data:`LOCK_BUDGET_AWARE_RETRY_MS` (60s) we throw
   immediately rather than hold a worker lock through a long sleep.

3. **Truncation policy explicit.** ``truncation: false`` is sent verbatim on
   every embed and rerank — lossless is a hard requirement (a silently-clipped
   embedding is worse than no embedding because the vector doesn't signal it
   was clipped).

4. **Async-native, mockable client.** Production uses the long-lived
   ``httpx.AsyncClient`` constructed in ``__init__``; tests inject a custom
   client (typically via ``respx``) or call ``aclose()`` after each test to
   keep the runtime connection pool clean.

5. **No retries on non-429 4xx.** 4xx means caller bug (bad input / over-cap
   document / bad auth). Retrying just spends quota. We retry only on 429,
   5xx, and network errors.

6. **Bounded retries.** :data:`DEFAULT_MAX_RETRIES` = 3 attempts (so 4 total
   tries: initial + 3 retries). Exponential backoff base 500ms, doubled each
   attempt, capped at :data:`BACKOFF_CAP_MS` = 25s (Wave-1 fix). Worst-case
   wall time before giving up: 25 + 25 + 25 + 25 = ~100s with the cap hit.

7. **No PII in error messages.** Voyage echoes input back in some 400 / 429
   / 5xx responses; :func:`_summarize_body` redacts those bodies before they
   attach to :class:`VoyageError` (Wave-4 + Wave-7 fixes — Sentry/log capture
   sees the full exception object, so raw body in ``response_body`` would leak
   even when the message string is clean).

Per ADR-019, the HTTP layer is ``httpx[socks]==0.28.1`` (matches Hermes host
pin). Per ADR-029, the Wave-1 / Wave-2 / Wave-4 / Wave-7 / Wave-11 fixes carry
inline ``# LCM Wave-N (YYYY-MM-DD): ...`` provenance comments anchored to the
TS source file:line.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Final, Literal, cast

import httpx

# ---------------------------------------------------------------------------
# Constants — all 7 hard-coded values mirror ``client.ts:60-93`` verbatim.
# ---------------------------------------------------------------------------

VOYAGE_API_BASE: Final[str] = "https://api.voyageai.com/v1"
"""Voyage v1 API base URL (``client.ts:94``)."""

MAX_TOKENS_PER_EMBED_BATCH: Final[int] = 80_000
"""Per-request batch cap. Voyage server cap is 120K; we leave a 25% margin
because Voyage's tokenizer counts ~9.5% higher than our ``summaries.token_count``.
80K × 1.10 = 88K << 120K — safe (``client.ts:61``).
"""

MAX_TOKENS_PER_EMBED_DOC: Final[int] = 27_000
"""Per-document cap for ``voyage-4-large``. Server per-doc cap is 32K; 27K ×
1.095 (tokenizer inflation) ≈ 29.6K — safe.

# LCM Wave-1 (2025-11-XX): previous 30K value was at the edge and observed
# 400s in production on 28-30K stored-token leaves. Caller pre-filters; the
# client does NOT silently drop oversized docs (lossless contract — splitting
# changes the semantic unit being embedded).
# Original: lossless-claw/src/voyage/client.ts:77.
"""

MAX_TOKENS_PER_RERANK_CALL: Final[int] = 600_000
"""Per-call total budget (query + all documents) for ``rerank-2.5``
(``client.ts:83``).
"""

DEFAULT_MAX_RETRIES: Final[int] = 3
"""4 total attempts (initial + 3 retries) — ``client.ts:85``."""

BACKOFF_BASE_MS: Final[int] = 500
"""Exponential backoff base: 500 / 1000 / 2000 / 4000 ms (``client.ts:86``)."""

# LCM Wave-1 (2025-11-XX): per-attempt backoff cap. Previously 30s; combined
# with the 30s per-attempt timeout × 2 attempts that equals 90s, which equals
# WORKER_LOCK_TTL_MS exactly. Dropping the cap to 25s leaves a 5s margin under
# the lock TTL so a runaway 429-storm cannot starve other workers waiting on
# the same row-uniqueness lock.
# Original: lossless-claw/src/voyage/client.ts:91.
BACKOFF_CAP_MS: Final[int] = 25_000

DEFAULT_TIMEOUT_S: Final[float] = 60.0
"""Per-attempt request timeout in seconds (``client.ts:92``: ``DEFAULT_TIMEOUT_MS = 60_000``)."""

RETRY_AFTER_HARD_CAP_MS: Final[int] = 5 * 60 * 1000
"""Soft cap when parsing ``Retry-After``. No realistic Voyage value exceeds
5 minutes (``client.ts:592``).
"""

LOCK_BUDGET_AWARE_RETRY_MS: Final[int] = 60_000
"""~2/3 of ``WORKER_LOCK_TTL_MS=90s``. If ``Retry-After`` exceeds this, we
throw immediately rather than wait, so the caller can release its worker
lock cleanly. See Wave-2 fix in :meth:`VoyageClient._post_with_retry`.
"""

# ---------------------------------------------------------------------------
# Error taxonomy.
# ---------------------------------------------------------------------------

VoyageErrorKind = Literal[
    "auth",
    "bad_request",
    "rate_limit",
    "server_error",
    "network",
    "unexpected",
]
"""Discriminator on :class:`VoyageError`. Caller dispatches recovery on this:

- ``auth``: 401/403. Stop, surface to operator. Don't retry.
- ``bad_request``: 400 or other non-retryable 4xx. Caller bug — likely over-cap
  document or malformed input. Caller may suppress the offender and continue.
- ``rate_limit``: 429 (either after exhausted retries OR with a server-supplied
  ``Retry-After`` that exceeds the lock-budget — see Wave-2 fix).
  ``retry_after_ms`` carries the server hint; caller (backfill cron) parks
  until it elapses.
- ``server_error``: 5xx after exhausted retries. Requeue and try later.
- ``network``: ``httpx.TimeoutException`` / ``httpx.NetworkError`` (connection
  refused, DNS failure, read timeout). Same treatment as ``server_error``.
- ``unexpected``: malformed Voyage response (missing ``data``, wrong shape,
  dimension mismatch within batch). Bug in Voyage or in this client; surface
  to operator.
"""


class VoyageError(Exception):
    """Raised for any Voyage HTTP error or malformed response.

    See :data:`VoyageErrorKind` for the discrimination + caller dispatch.

    Per ADR-029 Wave-4 + Wave-7 fix: ``response_body`` is always run through
    :func:`_summarize_body` before reaching this constructor — so PII leaks
    through Sentry/log capture of the exception object are defended-in-depth.
    """

    __slots__ = ("kind", "status", "retry_after_ms", "response_body")

    def __init__(
        self,
        kind: VoyageErrorKind,
        message: str,
        *,
        status: int | None = None,
        retry_after_ms: int | None = None,
        response_body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.kind: VoyageErrorKind = kind
        self.status: int | None = status
        self.retry_after_ms: int | None = retry_after_ms
        self.response_body: str | None = response_body


# ---------------------------------------------------------------------------
# Result dataclasses.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EmbedResult:
    """Result of :meth:`VoyageClient.embed`. Vectors are in the same order as
    the input ``texts`` (re-ordered by the ``index`` field if Voyage returns
    out-of-order)."""

    vectors: list[list[float]]
    """Embedding vectors in input order. Each is a list[float] from JSON —
    cast to ``numpy.float32`` at the vec0 storage boundary if Float32 parity
    with the TS client is required (see ADR-019 + ``client.ts:297``
    ``Float32Array.from``).
    """
    total_tokens: int
    """Voyage server-reported token count for this batch."""
    model: str
    """Voyage model id echoed back."""


@dataclass(frozen=True, slots=True)
class RerankItem:
    """One rerank result. ``id`` is the caller-supplied opaque id, joined back
    from ``candidates[item.index]``."""

    id: str
    index: int
    score: float


@dataclass(frozen=True, slots=True)
class RerankResult:
    """Result of :meth:`VoyageClient.rerank`. ``results`` is sorted by
    ``score`` descending (defensive — Voyage docs say they sort, but we sort
    again per ``client.ts:378``)."""

    results: list[RerankItem]
    total_tokens: int
    model: str


# ---------------------------------------------------------------------------
# Client.
# ---------------------------------------------------------------------------


class VoyageClient:
    """Async HTTP client for the Voyage embeddings + reranker endpoints.

    Lifecycle: instantiate once per process; share across worker tasks; call
    :meth:`aclose` at shutdown. The underlying :class:`httpx.AsyncClient` is
    pinned with explicit connection-pool limits (``max_keepalive_connections=20``,
    ``max_connections=100``) per ADR-019 §Consequences to avoid implicit-default
    drift across httpx point releases.
    """

    __slots__ = (
        "_api_key",
        "_base_url",
        "_timeout_s",
        "_max_retries",
        "_client",
        "_owns_client",
    )

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = VOYAGE_API_BASE,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        # API key resolution mirrors ``client.ts:410-419``: explicit opt >
        # ``VOYAGE_API_KEY`` env. Three-tier file/config resolver (#05-02) is
        # upstream of this constructor — caller passes a resolved key in.
        resolved = (
            api_key if api_key is not None else os.environ.get("VOYAGE_API_KEY", "")
        ).strip()
        if not resolved:
            raise VoyageError(
                "auth",
                "voyage_auth: VOYAGE_API_KEY is empty (set env or pass `api_key`)",
            )
        self._api_key: str = resolved
        self._base_url: str = base_url.rstrip("/")
        self._timeout_s: float = timeout_s
        self._max_retries: int = max_retries
        if client is None:
            # Explicit pool pins per ADR-019 §Consequences: defaults pinned
            # to avoid implicit-default drift across httpx 0.27 → 0.30.
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(timeout_s, connect=10.0),
                limits=httpx.Limits(
                    max_keepalive_connections=20,
                    max_connections=100,
                ),
            )
            self._owns_client: bool = True
        else:
            self._client = client
            self._owns_client = False

    async def aclose(self) -> None:
        """Close the underlying ``httpx.AsyncClient`` if we own it."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "VoyageClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    # -------------------------------------------------------------------
    # Embeddings
    # -------------------------------------------------------------------

    async def embed(
        self,
        texts: list[str],
        *,
        model: str,
        input_type: Literal["query", "document"] | None = "document",
        output_dimension: int | None = None,
    ) -> EmbedResult:
        """POST ``/v1/embeddings``. Returns vectors in input order.

        ``texts`` empty -> returns immediately without HTTP call.
        ``input_type=None`` -> field omitted from body (Voyage default).
        ``output_dimension`` -> forwarded verbatim when > 0 (Wave-11 fix).
        ``truncation: false`` is sent verbatim — lossless invariant.

        See ``client.ts:230-314``.
        """
        if not texts:
            return EmbedResult(vectors=[], total_tokens=0, model=model)

        body: dict[str, Any] = {
            "model": model,
            "input": texts,
            "truncation": False,
        }
        if input_type is not None:
            body["input_type"] = input_type
        # LCM Wave-11 (2026-04-XX): forward output_dimension to Voyage so
        # non-default-dim profiles (256/512/2048) actually get those dims
        # back. Without this, Voyage returns its default (1024) and the
        # vec0 INSERT fails with dim mismatch on the per-model table.
        # Original: lossless-claw/src/voyage/client.ts:252.
        if output_dimension is not None and output_dimension > 0:
            body["output_dimension"] = output_dimension

        resp = await self._post_with_retry(
            "/embeddings",
            body,
            input_count=len(texts),
        )
        return self._parse_embed_response(resp, expected=len(texts), model_hint=model)

    # -------------------------------------------------------------------
    # Rerank
    # -------------------------------------------------------------------

    async def rerank(
        self,
        query: str,
        candidates: list[tuple[str, str]],
        *,
        model: str = "rerank-2.5",
        top_k: int | None = None,
    ) -> RerankResult:
        """POST ``/v1/rerank``. ``candidates`` is a list of ``(id, text)`` pairs.

        ``candidates`` empty -> returns immediately without HTTP call.
        ``top_k=None`` -> defaults to ``len(candidates)``.
        Returns results sorted by score descending (defensive — ``client.ts:378``).

        See ``client.ts:319-385``.
        """
        if not candidates:
            return RerankResult(results=[], total_tokens=0, model=model)

        body: dict[str, Any] = {
            "model": model,
            "query": query,
            "documents": [t for _, t in candidates],
            "top_k": top_k if top_k is not None else len(candidates),
            "truncation": False,
        }
        resp = await self._post_with_retry(
            "/rerank",
            body,
            input_count=len(candidates),
        )
        return self._parse_rerank_response(resp, candidates, model_hint=model)

    # -------------------------------------------------------------------
    # Internals — retry loop (load-bearing — port branch-for-branch from
    # postWithRetry at client.ts:421-558).
    # -------------------------------------------------------------------

    async def _post_with_retry(
        self,
        path: str,
        body: dict[str, Any],
        *,
        input_count: int,
    ) -> httpx.Response:
        url = f"{self._base_url}{path}"
        last_err: VoyageError | None = None

        for attempt in range(self._max_retries + 1):
            # Per-attempt request. The ``httpx.Timeout`` set on the client
            # (or the per-request override below for test reproducibility)
            # supplies the abort semantics of TS ``AbortController +
            # setTimeout``.
            try:
                resp = await self._client.post(
                    url,
                    content=json.dumps(body),
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self._api_key}",
                    },
                )
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                last_err = VoyageError(
                    "network",
                    f"voyage_network: {e} (attempt {attempt + 1}/{self._max_retries + 1})",
                )
                if attempt < self._max_retries:
                    await asyncio.sleep(_backoff_ms(attempt) / 1000)
                    continue
                raise last_err

            if 200 <= resp.status_code < 300:
                return resp

            status = resp.status_code
            body_text = _safe_read_body(resp)

            if status in (401, 403):
                # LCM Wave-7 (2026-02-XX): route response_body through
                # summarize_body for parity with the 400 path (defense-in-depth
                # against Sentry/log capture of the raw body).
                # Original: lossless-claw/src/voyage/client.ts:472.
                suppressed = _summarize_body(body_text)
                raise VoyageError(
                    "auth",
                    f"voyage_auth: {status} (check VOYAGE_API_KEY)",
                    status=status,
                    response_body=suppressed,
                )

            if status == 400:
                # LCM Wave-4 (2026-01-XX): pass the SAME suppressed body to
                # both the error message AND response_body. Upstream Sentry
                # captures the full exception object, so raw body_text on
                # response_body leaks input echoes even when the message string
                # is clean.
                # Original: lossless-claw/src/voyage/client.ts:488.
                suppressed = _summarize_body(body_text)
                raise VoyageError(
                    "bad_request",
                    f"voyage_400: bad request on {input_count} inputs ({suppressed})",
                    status=status,
                    response_body=suppressed,
                )

            if status == 429:
                retry_after = _parse_retry_after_ms(resp.headers.get("Retry-After"))
                last_err = VoyageError(
                    "rate_limit",
                    f"voyage_429: rate limited (attempt {attempt + 1}/{self._max_retries + 1})",
                    status=status,
                    retry_after_ms=retry_after,
                    response_body=_summarize_body(body_text),
                )
                # LCM Wave-2 (2025-12-XX): if the server-supplied Retry-After
                # exceeds LOCK_BUDGET_AWARE_RETRY_MS (60s), throw immediately
                # rather than sleeping. Honoring a 120s Retry-After would burn
                # the 90s worker-lock TTL — caller (backfill cron) releases the
                # lock cleanly and the autostart's next interval picks up where
                # we left off, much better than sleeping past the lock TTL.
                # Original: lossless-claw/src/voyage/client.ts:507-526.
                if (
                    attempt < self._max_retries
                    and (retry_after if retry_after is not None else 0)
                    <= LOCK_BUDGET_AWARE_RETRY_MS
                ):
                    # Honor server hint if present; else exponential backoff.
                    wait_ms = retry_after if retry_after is not None else _backoff_ms(attempt)
                    await asyncio.sleep(wait_ms / 1000)
                    continue
                # Either (a) we exhausted retries, OR (b) server told us to wait
                # longer than our lock-aware budget. Throw so caller can release
                # the lock and the next tick retries fresh.
                raise last_err

            if 500 <= status < 600:
                # LCM Wave-7 (2026-02-XX): summarize 5xx body too for parity
                # with the 400 path (Sentry capture defense-in-depth).
                # Original: lossless-claw/src/voyage/client.ts:530.
                last_err = VoyageError(
                    "server_error",
                    f"voyage_5xx: {status} (attempt {attempt + 1}/{self._max_retries + 1})",
                    status=status,
                    response_body=_summarize_body(body_text),
                )
                if attempt < self._max_retries:
                    await asyncio.sleep(_backoff_ms(attempt) / 1000)
                    continue
                raise last_err

            # Some other 4xx — treat as bad_request, no retry.
            # LCM Wave-7 (2026-02-XX): summarize_body on response_body too.
            # Original: lossless-claw/src/voyage/client.ts:545.
            suppressed = _summarize_body(body_text)
            raise VoyageError(
                "bad_request",
                f"voyage_4xx: {status} {suppressed}",
                status=status,
                response_body=suppressed,
            )

        # Defensive: every branch in the loop above either returns or raises
        # on the final attempt. If somehow we exit cleanly (loop ran 0 times
        # because max_retries was negative — invalid but tolerated), surface
        # the accumulated error or an unexpected.
        raise last_err or VoyageError(
            "unexpected",
            "voyage_unexpected: _post_with_retry exited loop without response",
        )

    # -------------------------------------------------------------------
    # Response parsers — port branch-for-branch from ``client.ts:266-385``.
    # -------------------------------------------------------------------

    def _parse_embed_response(
        self,
        resp: httpx.Response,
        *,
        expected: int,
        model_hint: str,
    ) -> EmbedResult:
        try:
            payload = cast(dict[str, Any], resp.json())
        except (ValueError, json.JSONDecodeError) as e:
            raise VoyageError(
                "unexpected",
                f"voyage_unexpected: embeddings response was not valid JSON ({e})",
                status=resp.status_code,
            ) from e

        data = payload.get("data")
        if not isinstance(data, list) or len(data) != expected:
            got_desc = f"data[{len(data)}]" if isinstance(data, list) else "no data array"
            raise VoyageError(
                "unexpected",
                f"voyage_unexpected: embeddings response shape — expected "
                f"data[{expected}], got {got_desc}",
                status=resp.status_code,
            )

        first_item = data[0] if data else {}
        first_emb = first_item.get("embedding") if isinstance(first_item, dict) else None
        dims = len(first_emb) if isinstance(first_emb, list) else 0
        vectors: list[list[float] | None] = cast("list[list[float] | None]", [None] * expected)
        # Voyage may return data out-of-order in pathological cases; index by
        # the ``index`` field. Same loop as ``client.ts:280-298``.
        for item in data:
            if not isinstance(item, dict):
                raise VoyageError(
                    "unexpected",
                    f"voyage_unexpected: embedding item is not an object (got {type(item).__name__})",
                    status=resp.status_code,
                )
            emb = item.get("embedding")
            if not isinstance(emb, list) or len(emb) != dims:
                got_desc = f"length {len(emb)}" if isinstance(emb, list) else "non-array"
                raise VoyageError(
                    "unexpected",
                    f"voyage_unexpected: dimension mismatch in batch (expected {dims}, got {got_desc})",
                    status=resp.status_code,
                )
            idx = item.get("index")
            if not isinstance(idx, int) or isinstance(idx, bool) or idx < 0 or idx >= expected:
                raise VoyageError(
                    "unexpected",
                    f"voyage_unexpected: bad index {idx!r} (batch size {expected})",
                    status=resp.status_code,
                )
            # Cast to list[float] — JSON ints/floats are already numbers, but
            # ensure every element is numeric (defensive against pathological
            # mixed-type arrays).
            vectors[idx] = [float(x) for x in emb]

        for i, v in enumerate(vectors):
            if v is None:
                raise VoyageError(
                    "unexpected",
                    f"voyage_unexpected: missing embedding for index {i}",
                    status=resp.status_code,
                )

        usage = payload.get("usage") or {}
        total_tokens_raw = usage.get("total_tokens") if isinstance(usage, dict) else None
        total_tokens = total_tokens_raw if isinstance(total_tokens_raw, int) else 0
        model = payload.get("model")
        model_str = model if isinstance(model, str) else model_hint

        # Narrowed by the None-check loop above; type-narrowing for ty.
        return EmbedResult(
            vectors=cast(list[list[float]], vectors),
            total_tokens=total_tokens,
            model=model_str,
        )

    def _parse_rerank_response(
        self,
        resp: httpx.Response,
        candidates: list[tuple[str, str]],
        *,
        model_hint: str,
    ) -> RerankResult:
        try:
            payload = cast(dict[str, Any], resp.json())
        except (ValueError, json.JSONDecodeError) as e:
            raise VoyageError(
                "unexpected",
                f"voyage_unexpected: rerank response was not valid JSON ({e})",
                status=resp.status_code,
            ) from e

        data = payload.get("data")
        if not isinstance(data, list):
            raise VoyageError(
                "unexpected",
                "voyage_unexpected: rerank response missing data array",
                status=resp.status_code,
            )

        items: list[RerankItem] = []
        n = len(candidates)
        for item in data:
            if not isinstance(item, dict):
                raise VoyageError(
                    "unexpected",
                    "voyage_unexpected: rerank item is not an object",
                    status=resp.status_code,
                )
            idx = item.get("index")
            score = item.get("relevance_score")
            # Reject bools that would otherwise pass `isinstance(x, int)` due
            # to Python's bool-is-int inheritance.
            if (
                not isinstance(idx, int)
                or isinstance(idx, bool)
                or idx < 0
                or idx >= n
                or not isinstance(score, (int, float))
                or isinstance(score, bool)
            ):
                raise VoyageError(
                    "unexpected",
                    f"voyage_unexpected: bad rerank item (index={idx!r}, score={score!r})",
                    status=resp.status_code,
                )
            items.append(RerankItem(id=candidates[idx][0], index=idx, score=float(score)))

        # Voyage docs say they return sorted descending; sort defensively per
        # ``client.ts:378``.
        items.sort(key=lambda x: x.score, reverse=True)

        usage = payload.get("usage") or {}
        total_tokens_raw = usage.get("total_tokens") if isinstance(usage, dict) else None
        total_tokens = total_tokens_raw if isinstance(total_tokens_raw, int) else 0
        model = payload.get("model")
        model_str = model if isinstance(model, str) else model_hint

        return RerankResult(results=items, total_tokens=total_tokens, model=model_str)


# ---------------------------------------------------------------------------
# Module-private helpers — ported from ``client.ts:560-616``.
# ---------------------------------------------------------------------------


def _backoff_ms(attempt: int) -> int:
    """Exponential: 500, 1000, 2000, 4000, ... capped at :data:`BACKOFF_CAP_MS`.

    See ``client.ts:608-612``.
    """
    return min(BACKOFF_BASE_MS * (2**attempt), BACKOFF_CAP_MS)


def _safe_read_body(resp: httpx.Response) -> str:
    """Read the response body as text and clip to 800 chars with a truncation
    suffix. Failures swallowed to "" — error reporting must not throw.

    Per ADR-019 §"PII suppression rule", the 800-char clip runs *before*
    :func:`_summarize_body` so the substring check at line 200 of an
    1800-char body still triggers suppression.

    See ``client.ts:560-567``.
    """
    try:
        text = resp.text
    except Exception:
        return ""
    if len(text) > 800:
        return text[:800] + "…(truncated)"
    return text


def _summarize_body(body: str) -> str:
    """PII suppression filter applied to every non-2xx body before attaching
    to :class:`VoyageError`. Per Waves 4 + 7 — Sentry / log capture sees the
    full exception object, so raw body in ``response_body`` leaks input even
    when the message string is clean.

    See ``client.ts:569-576``.
    """
    # LCM Wave-4 (2026-01-XX): substring check on JSON-quoted keys to detect
    # Voyage 400 responses that echo the caller's input back. Voyage's actual
    # 400 bodies on most failure modes do NOT contain "input"/"texts"/"documents"
    # (live-probed in spike 004), so this is defense-in-depth.
    # Original: lossless-claw/src/voyage/client.ts:572.
    if '"input"' in body or '"texts"' in body or '"documents"' in body:
        return "input echoed in error body — suppressed for privacy"
    return body[:200]


def _parse_retry_after_ms(header: str | None) -> int | None:
    """Parse a ``Retry-After`` header (numeric-seconds or HTTP-date form).

    - Numeric (``"30"``, ``"0.05"``) → ``min(value * 1000, RETRY_AFTER_HARD_CAP_MS)``.
    - HTTP-date (``"Wed, 21 Oct 2026 07:28:00 GMT"``) → ``min(delta_ms, cap)``
      if positive; ``None`` if in the past.
    - Unparseable / ``None`` → ``None`` (caller falls back to backoff).

    See ``client.ts:593-606``.

    Note: TS uses ``Date.parse`` which is permissive about formats; Python uses
    :func:`email.utils.parsedate_to_datetime` which is strict about RFC 5322
    / RFC 7231 date forms. Matching TS exactly requires accepting parse
    failures as "no header" — which we do here (the ``except`` returns None).
    """
    if not header:
        return None

    # First: numeric seconds (per HTTP spec, ``delta-seconds``).
    try:
        as_num = float(header)
        if as_num >= 0 and as_num == as_num and as_num != float("inf"):
            return min(int(as_num * 1000), RETRY_AFTER_HARD_CAP_MS)
    except ValueError:
        pass

    # Second: HTTP-date.
    try:
        parsed = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None

    # parsedate_to_datetime returns either naive (local time, no tz) or aware.
    # Normalize to aware UTC for the delta calculation.
    if parsed.tzinfo is None:
        # RFC 7231 §7.1.1.1 requires GMT; treat naive as UTC for safety.
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    now_aware = dt.datetime.now(parsed.tzinfo)
    delta_ms = int((parsed - now_aware).total_seconds() * 1000)
    if delta_ms <= 0:
        return None
    return min(delta_ms, RETRY_AFTER_HARD_CAP_MS)
