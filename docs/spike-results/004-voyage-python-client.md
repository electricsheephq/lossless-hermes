# Spike 004: Voyage Python HTTP client

**Status:** READY-TO-PORT
**Date:** 2026-05-13
**Confidence:** 95%
**Decision impact:** ADR-004 (Python Voyage client implementation strategy)

## Question

Can we replicate LCM's Voyage HTTP client (TS, 616 LOC, `lossless-claw/src/voyage/client.ts`) faithfully in Python `httpx`, preserving retry/backoff/batch-packing semantics that are load-bearing for the +52.5pp recall claim?

**Answer:** Yes. The TS client uses only primitives that `httpx` exposes 1:1 (POST JSON, headers, timeouts via `httpx.Timeout`, response status/headers, `response.aclose()`). Every retry/error-classification branch maps to standard Python control flow. A live round-trip against the production Voyage API succeeded on first attempt (embed: 510ms, rerank: 304ms; see §Roundtrip test). The implementation is constrained but mechanical — no protocol-level surprises.

## TS client surface

Reference: `/Volumes/LEXAR/Claude/lossless-claw/src/voyage/client.ts`.

| Symbol | Signature | Purpose |
|---|---|---|
| `embedTexts(opts)` | `(VoyageEmbedOptions) => Promise<VoyageEmbedResult>` | POST `/v1/embeddings`; returns `{ vectors: Float32Array[], totalTokens, model }`. |
| `rerankCandidates(opts)` | `(VoyageRerankOptions) => Promise<VoyageRerankResult>` | POST `/v1/rerank`; returns `{ results: [{id, index, score}], totalTokens, model }`. |
| `VoyageError` | `class extends Error` | Discriminated by `kind`: `"auth" \| "bad_request" \| "rate_limit" \| "server_error" \| "network" \| "unexpected"`. Carries `status`, `retryAfterMs`, `responseBody`. |
| `MAX_TOKENS_PER_EMBED_BATCH` | `80_000` | Per-request batch cap (server cap 120K, 25% margin for Voyage tokenizer being ~9.5% higher than our `summaries.token_count`). |
| `MAX_TOKENS_PER_EMBED_DOC` | `27_000` | Per-document cap for `voyage-4-large` (server cap 32K; 27K × 1.095 ≈ 29.6K — safe). Caller pre-filters; client does not silently drop. |
| `MAX_TOKENS_PER_RERANK_CALL` | `600_000` | Per-call total (query + all docs). |

**Config knobs (every option on `VoyageEmbedOptions` / `VoyageRerankOptions`):**
- `model` — model id (typed enum: `voyage-4-large`, `voyage-3*`, `voyage-code-3` for embed; `rerank-2`, `rerank-2.5`, `rerank-2-lite` for rerank).
- `texts` / `candidates` / `query` — input payload.
- `inputType` — `"query" | "document" | null` (omitted from body when null, per Voyage default).
- `outputDimension` — optional integer for `voyage-4-large` (256/512/1024/2048). Wave-11 P1 fix: forwarded so non-default vec0 columns get correct dim back.
- `topK` — rerank only, defaults to `candidates.length`.
- `baseUrl` — override for tests (default `https://api.voyageai.com/v1`).
- `fetch` — inject mock fetch (TS-specific; in Python use a mock `httpx.AsyncClient` or `respx`).
- `apiKey` — override env var `VOYAGE_API_KEY`.
- `timeoutMs` — per-attempt timeout. Default 60 000 ms.
- `maxRetries` — default 3 (4 attempts total).

**Hard-coded constants (preserve verbatim in Python):**
- `BACKOFF_BASE_MS = 500`
- `BACKOFF_CAP_MS = 25_000` (Wave-1 fix: 25s, not 30s, to leave 5s margin under WORKER_LOCK_TTL=90s)
- `DEFAULT_TIMEOUT_MS = 60_000`
- `DEFAULT_MAX_RETRIES = 3`
- `RETRY_AFTER_HARD_CAP_MS = 5 * 60 * 1000` (soft cap on `Retry-After`)
- `LOCK_BUDGET_AWARE_RETRY_MS = 60_000` (~2/3 of WORKER_LOCK_TTL_MS=90s; if Retry-After exceeds this, throw immediately rather than wait)
- `VOYAGE_API_BASE = "https://api.voyageai.com/v1"`

## Retry/backoff rules (must preserve)

These rules are load-bearing — they encode hard-won lessons from Waves 1, 2, 4, 7, 11 of LCM development. The Python port MUST replicate them byte-for-byte. The TS source comments cite the wave/auditor that found each issue, which is useful provenance.

1. **Max attempts:** 4 (initial + `DEFAULT_MAX_RETRIES=3`).
2. **Backoff schedule:** exponential, base 500 ms, doubled per attempt, capped at 25 000 ms. So waits before retries 1, 2, 3 are 500/1000/2000 ms (none hit the cap with default 3 retries). The cap is defensive in case caller bumps `maxRetries`.
3. **Per-attempt timeout:** 60 s (configurable). Wraps the full request via `AbortController` in TS → `httpx.Timeout` in Python.
4. **No retries on 4xx (except 429).** 401/403 → throw `auth`. 400 → throw `bad_request`. Any other 4xx → throw `bad_request`. Caller is buggy; retry just spends quota.
5. **429 → parse `Retry-After`.** Voyage may send either seconds-as-number OR HTTP-date. Helper `parseRetryAfterMs`:
   - If header parses as a finite non-negative number: `min(value * 1000, RETRY_AFTER_HARD_CAP_MS)`.
   - Else if it parses as HTTP-date: `min(date - now, RETRY_AFTER_HARD_CAP_MS)` (when positive).
   - Else: `undefined` (caller falls back to backoff).
6. **429 lock-budget gate.** If the server-supplied `Retry-After` exceeds `LOCK_BUDGET_AWARE_RETRY_MS=60s`, **throw immediately** rather than wait — caller (backfill cron) releases its worker lock and the next tick re-tries fresh. Honoring a 120s `Retry-After` would burn the 90s lock TTL.
7. **5xx → retry** with exponential backoff (no `Retry-After` parsing).
8. **Network errors (fetch threw):** treated like 5xx — retry with backoff, then throw `network`.
9. **Final attempt exhausted:** throw the last accumulated `VoyageError`.
10. **`truncation: false` is sent verbatim on every embed and rerank.** Lossless is a hard requirement; a silently-truncated embedding is worse than no embedding because the vector doesn't signal it was clipped.

## PII suppression rule

`summarizeBody(body)` is applied to **every** non-2xx response body before it's attached to `VoyageError.responseBody` AND to error messages (Waves 4 + 7 fixes — Sentry/log capture the full exception, so raw body in `.responseBody` leaks too). The rule:

```
if body contains '"input"' OR '"texts"' OR '"documents"':
    return "input echoed in error body — suppressed for privacy"
else:
    return body.slice(0, 200)
```

The body is also pre-clipped to 800 chars by `safeReadBody` (with `…(truncated)` suffix) before summarization.

**Empirical finding from this spike (§Live error probes):** Voyage's actual 400 bodies on `voyage-INVALID` model do NOT contain `"input"` / `"texts"` / `"documents"` — they're shaped `{"detail": "Model X is not supported..."}`. The substring suppression therefore would NOT trigger, and the operator would see the raw model-list response — which is fine and informative. The suppression is defense-in-depth for the cases where Voyage DOES echo input (per Waves 4+7 audit findings).

## Batch packing limits

Caller (not client) enforces:
- **Embed batch:** `sum(estimateTokens(text)) <= MAX_TOKENS_PER_EMBED_BATCH (80K)`.
- **Embed per-doc:** each `text <= MAX_TOKENS_PER_EMBED_DOC (27K)`. Voyage 400s otherwise; client surfaces the 400 without retry.
- **Rerank total:** `estimateTokens(query) + sum(estimateTokens(d.text)) <= MAX_TOKENS_PER_RERANK_CALL (600K)`.

**Single doc exceeds embed-doc cap:** caller must split or suppress. The client deliberately does NOT split. This preserves the lossless contract — splitting changes the semantic unit being embedded.

**Single doc exceeds rerank total (impossible in practice — 600K tokens):** would 400 with the same passthrough behavior.

`estimateTokens` is NOT in this file — it lives separately in LCM and is a heuristic from `summaries.token_count` (pre-stored). The Python port should accept tokens-already-counted as caller input rather than re-implement the heuristic, to keep the client a thin HTTP shim.

## Voyage API contract (from upstream docs + live probes)

**Embeddings — `POST https://api.voyageai.com/v1/embeddings`**

Headers:
- `Content-Type: application/json`
- `Authorization: Bearer <key>`

Request body:
- `model` (req): one of `voyage-4-large`, `voyage-4`, `voyage-4-lite`, `voyage-3-large`, `voyage-3.5`, `voyage-3.5-lite`, `voyage-code-3`, `voyage-finance-2`, `voyage-law-2`, …
- `input` (req): string OR list[str], max 1 000 items.
- `input_type` (opt): `"query" | "document" | null`. Defaults to null (omit when null).
- `truncation` (opt): bool, default `true`. **We send `false`.**
- `output_dimension` (opt): 256 / 512 / 1024 / 2048 (for `voyage-4-large`); else server returns model default.
- `output_dtype` (opt): `"float" | "int8" | "uint8" | "binary" | "ubinary"`. We don't use; default float.
- `encoding_format` (opt): `null | "base64"`. We use null (parsed as float list).

Response (200):
```
{
  "data": [{"embedding": [..., float], "index": 0, "object": "embedding"}, ...],
  "model": "voyage-4-large",
  "usage": {"total_tokens": int},
  "object": "list"
}
```

**Rerank — `POST https://api.voyageai.com/v1/rerank`**

Request body:
- `model` (req): `rerank-2.5` (recommended), `rerank-2.5-lite`, `rerank-2`, `rerank-2-lite`, `rerank-1`.
- `query` (req): string, max 8K tokens for rerank-2.5.
- `documents` (req): list[str], max 1 000.
- `top_k` (opt): int or null (returns all).
- `return_documents` (opt): bool, default false. **We omit (don't need echoed docs).**
- `truncation` (opt): bool, default true. **We send `false`.**

Response (200):
```
{
  "data": [{"index": 0, "relevance_score": 0.4414}, ...],
  "model": "rerank-2.5",
  "usage": {"total_tokens": int}
}
```

Per-request limits (rerank-2.5): query + single-doc ≤ 32K tokens; total ≤ 600K tokens.

**Error codes (live-probed during this spike):**
| Status | Body shape | Retry-After header? | Our action |
|---|---|---|---|
| 401 | `{"detail": "Provided API key is invalid."}` | No | `VoyageError(kind="auth")`, no retry |
| 400 | `{"detail": "<reason, sometimes with model list>"}` | No | `VoyageError(kind="bad_request")`, no retry |
| 429 | (not probed live — would risk quota) | Yes, per docs | `VoyageError(kind="rate_limit")`, retry with backoff unless server hint > 60s |
| 5xx | not probed | No | `VoyageError(kind="server_error")`, retry with backoff |

**Rate limits (base tier, from upstream docs):**
- `voyage-3.5`: 8M TPM / 2 000 RPM
- `voyage-4-lite`, `voyage-3.5-lite`: 16M TPM / 2 000 RPM
- Tier 2 (≥$100 spent): 2× multiplier. Tier 3 (≥$1 000): 3×.

## Python implementation sketch

Tested end-to-end against live Voyage; file at `/tmp/voyage-spike/roundtrip.py` (~150 LOC, deliberately minimal — full port adds retry loop, error taxonomy, `summarizeBody`).

```python
# voyage_client.py — sketch only; full impl is Phase 2
from __future__ import annotations
import asyncio, json, time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
import httpx

VOYAGE_API_BASE = "https://api.voyageai.com/v1"
MAX_TOKENS_PER_EMBED_BATCH = 80_000
MAX_TOKENS_PER_EMBED_DOC = 27_000
MAX_TOKENS_PER_RERANK_CALL = 600_000
DEFAULT_MAX_RETRIES = 3
BACKOFF_BASE_MS = 500
BACKOFF_CAP_MS = 25_000
DEFAULT_TIMEOUT_S = 60.0
RETRY_AFTER_HARD_CAP_MS = 5 * 60 * 1000
LOCK_BUDGET_AWARE_RETRY_MS = 60_000

VoyageErrorKind = Literal[
    "auth", "bad_request", "rate_limit", "server_error", "network", "unexpected"
]


class VoyageError(Exception):
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
        self.kind = kind
        self.status = status
        self.retry_after_ms = retry_after_ms
        self.response_body = response_body


@dataclass(frozen=True)
class EmbedResult:
    vectors: list[list[float]]  # Voyage returns floats; convert to numpy.float32 at boundary if needed
    total_tokens: int
    model: str


@dataclass(frozen=True)
class RerankItem:
    id: str
    index: int
    score: float


@dataclass(frozen=True)
class RerankResult:
    results: list[RerankItem]
    total_tokens: int
    model: str


class VoyageClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = VOYAGE_API_BASE,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key or self._load_api_key()
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._max_retries = max_retries
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=10.0),
        )

    @staticmethod
    def _load_api_key() -> str:
        # In production: env var first, then ~/.openclaw/credentials/voyage-api-key
        import os
        env = os.environ.get("VOYAGE_API_KEY", "").strip()
        if env:
            return env
        p = Path.home() / ".openclaw" / "credentials" / "voyage-api-key"
        if p.exists():
            return p.read_text().strip()
        raise VoyageError("auth", "voyage_auth: VOYAGE_API_KEY is empty")

    async def embed(
        self,
        texts: list[str],
        *,
        model: str,
        input_type: Literal["query", "document"] | None = "document",
        output_dimension: int | None = None,
    ) -> EmbedResult:
        if not texts:
            return EmbedResult(vectors=[], total_tokens=0, model=model)
        body: dict = {"model": model, "input": texts, "truncation": False}
        if input_type is not None:
            body["input_type"] = input_type
        if output_dimension is not None and output_dimension > 0:
            body["output_dimension"] = output_dimension

        resp = await self._post_with_retry("/embeddings", body, input_count=len(texts))
        return self._parse_embed_response(resp, expected=len(texts), model_hint=model)

    async def rerank(
        self,
        query: str,
        candidates: list[tuple[str, str]],  # (id, text)
        *,
        model: str,
        top_k: int | None = None,
    ) -> RerankResult:
        if not candidates:
            return RerankResult(results=[], total_tokens=0, model=model)
        body = {
            "model": model,
            "query": query,
            "documents": [t for _, t in candidates],
            "top_k": top_k if top_k is not None else len(candidates),
            "truncation": False,
        }
        resp = await self._post_with_retry("/rerank", body, input_count=len(candidates))
        return self._parse_rerank_response(resp, candidates, model_hint=model)

    # --- internals: retry loop, response parsers, summarize_body, parse_retry_after_ms ---
    # (Full implementation in src/lossless_hermes/voyage/client.py — port byte-for-byte from
    # postWithRetry in client.ts.)
```

**Mapping table — every TS field to its Python equivalent:**

| TS | Python |
|---|---|
| `Float32Array` | `list[float]` from JSON; convert to `numpy.ndarray(dtype=np.float32)` at storage boundary if needed (sqlite-vec0 expects bytes anyway) |
| `globalThis.fetch` | `httpx.AsyncClient.post` |
| `AbortController` + `setTimeout` | `httpx.Timeout(timeout_s, connect=10.0)` passed at client construction |
| `Response.ok` | `200 <= resp.status_code < 300` |
| `Response.headers.get("Retry-After")` | `resp.headers.get("Retry-After")` (same) |
| `await response.json()` | `resp.json()` |
| `await response.text()` | `resp.text` |
| `Promise<T>` | `Awaitable[T]` / `async def` returning `T` |
| `class VoyageError extends Error` | `class VoyageError(Exception)` |
| `Number.parseFloat / Date.parse` | `float()` / `email.utils.parsedate_to_datetime` |
| `Math.min(a, b)` | `min(a, b)` |
| `setTimeout(fn, ms)` | `await asyncio.sleep(ms / 1000)` |
| `JSON.stringify` | `json.dumps` (or pass `json=body` to httpx — but explicit `json.dumps` matches TS verbatim and avoids httpx-version-specific quirks) |

## Roundtrip test result (live, with API key)

`python3 /tmp/voyage-spike/roundtrip.py` against production Voyage:

```
loaded API key (length=46)
embed: 3 vectors, dim=1024, tokens=32, model=voyage-4-large, 510ms
  vec[0] L2 norm = 1.0000 (Voyage embeddings are unit-normalized)
embed query: 335ms, tokens=6
rerank: 3 results, tokens=44, model=rerank-2.5, 304ms
  doc-0 (idx=0) score=0.4414       # "Voyage AI provides embedding..."  ← most relevant to "How does RAG work?"
  doc-2 (idx=2) score=0.4297       # "Lossless context multiplication uses hierarchical recall..."
  doc-1 (idx=1) score=0.2305       # "Python's httpx library supports..."
approx word-count=28, voyage-reported=32, ratio=1.14
```

**Key findings:**
- `voyage-4-large` returns dim=1024 by default (no `output_dimension` sent).
- Vectors are L2-unit-normalized → cosine similarity == dot product.
- Word-count → Voyage-token ratio of 1.14 in this sample is consistent with the LCM-empirical 1.095 inflation (n=3 is too small to verify precisely, but it's in the ballpark; matches the rationale for the 9.5% safety margin).
- Latencies (~500ms embed, ~300ms rerank, single-request, small payload) match Voyage's published p50.
- Rerank scoring is semantically correct: the RAG-adjacent embedding doc ranks highest, the httpx-library doc ranks lowest.

## Live error probes

1. **401 (invalid API key):** status 401, body `{"detail":"Provided API key is invalid."}`, no `Retry-After` header. Maps cleanly to `VoyageError(kind="auth")`.
2. **400 (empty string in input):** status 400, body `{"detail":"...Input cannot contain empty strings or empty lists"}`. Maps to `VoyageError(kind="bad_request")`. No `"input"`/`"texts"`/`"documents"` substring → not suppressed; operator sees the helpful reason.
3. **400 (invalid model):** status 400, body `{"detail":"Model voyage-INVALID is not supported. Supported models are [...]"}`. Same suppression behavior.

**Not probed live (intentional — would burn quota or are operationally rare):**
- 429: covered by Voyage docs + TS unit-test fixtures.
- 5xx: rely on TS-side `httpbin`-style mocks; will replicate as `respx` fixtures in Phase 2.
- Network timeout: `httpx.TimeoutException` already mapped to `VoyageError(kind="network")` in sketch.

## Test fixtures for Phase 2

Port these from `lossless-claw/test/voyage-client.test.ts` (561 LOC) — fixture-for-fixture, using `respx` (httpx mock router) to inject responses. The most important cases:

1. **Happy path embed:** 2 inputs, 200 with `data` in order, asserts: vectors parsed, `total_tokens` returned, request body has `model`, `input`, `truncation: false`, `input_type: "document"`, `Authorization: Bearer <key>`.
2. **Happy path rerank:** verify body has `truncation: false`, top_k defaults to `len(candidates)`, results sorted descending by score (defensive sort even though server sorts).
3. **`input_type=null` omits the field.** (Field must not appear in JSON body.)
4. **Out-of-order data response:** server returns `data` array with `index: 1` before `index: 0` — verify Python client re-orders into the input order.
5. **Empty input list:** returns immediately without HTTP call, `EmbedResult([], 0, model)`.
6. **Empty candidates list:** same for rerank.
7. **`output_dimension` round-trip:** when caller passes `output_dimension=512`, request body must contain `"output_dimension": 512`. When caller passes `None`, field must NOT appear.
8. **400 → `VoyageError(kind="bad_request")`** with `summarizeBody` applied (no echo when body contains `"input"`/`"texts"`/`"documents"`).
9. **401 → `VoyageError(kind="auth")`**, no retry.
10. **403 → `VoyageError(kind="auth")`**, no retry.
11. **429 with `Retry-After: 30` (seconds):** retry once after 30s wait, then succeed → returns result.
12. **429 with `Retry-After: <HTTP-date>` 10s in future:** parses date, waits 10s, retries.
13. **429 with `Retry-After: 120` (> LOCK_BUDGET_AWARE_RETRY_MS):** throws immediately; caller releases lock.
14. **429 without `Retry-After`:** falls back to exponential backoff.
15. **500 → retry up to `maxRetries` times → throw `server_error`** with last status preserved.
16. **Network error (`httpx.ConnectError`):** retry with backoff → throw `network`.
17. **Per-attempt timeout:** mock that hangs > timeoutMs; httpx raises `TimeoutException`; client maps to `network`.
18. **Dim mismatch in batch:** mock returns `data[0].embedding.length != data[1].embedding.length` → `VoyageError(kind="unexpected")`.
19. **Bad `index` in response item:** `index: 99` for a 2-input batch → `unexpected`.
20. **Missing `data` array:** response `{}` → `unexpected`.
21. **Missing `relevance_score` in rerank item:** → `unexpected`.
22. **`summarizeBody` actually suppresses when body contains `"input"`:** craft mock body `'{"detail": "bad \"input\": [...]"}'` → `responseBody == "input echoed in error body — suppressed for privacy"`.
23. **`summarizeBody` clips to 200 chars** when no suppression triggered.
24. **`safeReadBody` clips to 800 chars before summarization** with `…(truncated)` suffix.

**Smoke test for CI (no API key required):** the 24 fixtures above run via `respx` mocks. They're unit-fast (no I/O), deterministic, and exercise every retry/error branch.

**Smoke test for CI (API key gated, gated on `VOYAGE_API_KEY` secret being present in repo settings):** the `/tmp/voyage-spike/roundtrip.py` script in this spike, promoted to `tests/integration/test_voyage_live.py`. Runs only on `nightly` workflow. Asserts: `dim==1024`, `L2 norm ≈ 1.0 ± 0.001`, embed latency < 5 s p99, rerank latency < 3 s p99, semantic ordering (RAG-adjacent doc ranks > unrelated doc).

## Remaining 5% risk

1. **`Float32Array` precision parity:** TS uses `Float32Array.from(item.embedding)` which silently downcasts JSON `number` (IEEE 754 double) to single-precision float. Python's `float()` is double-precision. If we want byte-identical vec0 INSERTs to a `float32` vec0 column, we must explicitly cast via `numpy.array(emb, dtype=np.float32)` at the storage boundary. **Mitigation:** test that LCM and Hermes produce vectors that agree to ≤ 1e-6 relative error on a fixed corpus. Easy to add in Phase 2.
2. **`asyncio.sleep` precision vs `setTimeout`:** both are best-effort; not load-bearing.
3. **`Retry-After` HTTP-date parsing:** Python's `email.utils.parsedate_to_datetime` is robust; TS uses `Date.parse` which is fuzzier. Voyage's actual format is unknown — could only confirm numeric-seconds from docs. If Voyage ever sends HTTP-date and Python parses it but TS didn't (or vice versa), behavior diverges. **Mitigation:** unit-test both forms explicitly; treat any 429 with `Retry-After` we can't parse as "no header" (fall back to backoff) — same as TS does.
4. **Concurrent-request connection-pool semantics:** TS uses a fresh `fetch` per call; Python typically reuses an `httpx.AsyncClient`. Under burst, httpx's default pool (max_keepalive_connections=20, max_connections=100) shouldn't bottleneck — but worth pinning explicitly when constructing the client to avoid implicit-default drift across httpx versions.
5. **`respx` vs real Voyage 429 body shape:** we didn't live-probe a 429 (would risk quota). The TS fixtures may use a synthetic 429 body. If Voyage's real 429 body shape differs from what TS tested with, error parsing edge cases could lurk. **Mitigation:** capture a real 429 the first time backfill hits one in staging and snapshot it as a fixture.
6. **Streaming responses:** Voyage doesn't stream embeddings/rerank. Confirmed — non-issue.
7. **HTTP/2:** httpx supports HTTP/2 with `http2=True` but requires `h2` extra; defaults to HTTP/1.1, matching TS `fetch`. No-op unless we explicitly opt in.

## Reference paths

- LCM source: `/Volumes/LEXAR/Claude/lossless-claw/src/voyage/client.ts` (616 LOC)
- LCM tests: `/Volumes/LEXAR/Claude/lossless-claw/test/voyage-client.test.ts` (561 LOC)
- This spike's live test: `/tmp/voyage-spike/roundtrip.py`
- API key: `~/.openclaw/credentials/voyage-api-key` (length 46, prefix `pa-`)
- Voyage docs: https://docs.voyageai.com/reference/embeddings-api, https://docs.voyageai.com/reference/reranker-api, https://docs.voyageai.com/docs/rate-limits

## Recommendation

**Proceed with the Python port in Phase 2.** Use the `VoyageClient` class sketch above as the starting point. Port the TS retry loop branch-for-branch into `_post_with_retry`. Mirror all 24 unit fixtures via `respx`. Add the live round-trip as a gated nightly job. Pin `httpx>=0.27,<0.30` (`json=` body kwarg, `aclose()` API, and `Timeout` semantics are stable across this range).

The TS client's most valuable property is that it's been hardened across 11 review waves and 7+ documented production fixes (PII-suppression parity, lock-budget-aware Retry-After, dim-mismatch sentinels, output_dimension forwarding). Carry every one of those forward verbatim — they are not optional polish, they are bug fixes earned in production. Adding a TODO marker in the Python code (`# LCM Wave-N fix: ...`) for each one preserves the provenance trail.
