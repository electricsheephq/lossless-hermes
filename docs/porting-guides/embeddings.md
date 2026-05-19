# Porting Guide: Embedding Pipeline

**Source LOC:** ~2,734 across `src/voyage/` (616) + `src/embeddings/` (2,102) + `src/concurrency/` (461; ~238 loop + 215 lock + ~144 types)
**Python target LOC:** ~2,500
**Confidence target:** 95% (pending Spike 001 = PASS, Spike 004 = PASS)
**Estimated effort:** 24–32 hours
**Epic:** 05-embeddings
**Source branch:** `lossless-claw@pr-613`
**Source path root:** `/Volumes/LEXAR/Claude/lossless-claw/`

---

## Architecture summary

LCM's embedding subsystem is a four-layer stack:

1. **Voyage HTTP client** (`src/voyage/client.ts`, 616 LOC) — async `fetch` wrapper around `POST /v1/embeddings` and `POST /v1/rerank`. Carries the hard-won retry/backoff rules from 11 review waves (429-with-lock-budget gate, PII suppression on error bodies, dim-mismatch sentinels, `truncation: false` lossless invariant). Pure I/O — no DB, no state.
2. **sqlite-vec store** (`src/embeddings/store.ts`, 609 LOC) — owns the per-model `lcm_embeddings_<slug>` `vec0` virtual tables, the polymorphic `(embedded_id, embedded_kind)` mapping (summary / entity / theme), the suppression-cascade and delete-cascade triggers, and the BigInt-binding quirks for `node:sqlite` (Python's `sqlite3` doesn't need this dance — see "Per-model virtual table shape" below).
3. **Async backfill worker** (`src/embeddings/backfill.ts`, 637 LOC) — single-tick cron entrypoint. SELECTs unembedded leaves (`summaries.kind='leaf' AND suppressed_at IS NULL AND token_count BETWEEN 1 AND 27_000 AND NOT EXISTS (... lcm_embedding_meta ...)`), packs them into token-budgeted batches (≤ 80K tokens/batch), POSTs to Voyage outside any DB transaction, then writes vec0 + meta in a SAVEPOINT-per-row transaction. Single-flight via `lcm_worker_lock` (90s TTL, 30s heartbeat). Re-checks lock ownership after every Voyage call — if the lock was stolen mid-call, writes are skipped.
4. **Retrieval surfaces** (`src/embeddings/semantic-search.ts` 419 LOC + `src/embeddings/hybrid-search.ts` 437 LOC) — `runSemanticSearch` embeds a query (with `input_type='query'` for asymmetric retrieval), runs KNN against the active model's vec0 table (over-fetching 10× when filters are present), JOINs back to `summaries` with defense-in-depth suppression filtering. `runHybridSearch` runs FTS + semantic in parallel, dedupes the union by `summary_id`, packs into the 600K-token rerank budget, calls Voyage rerank-2.5, falls back to reciprocal-rank-fusion (RRF) on rerank failure. The TS baseline measured a +52.5pp lift over FTS-only on Eva's 31-query paraphrastic eval; this figure is **not yet reproduced in the Python port** — the hybrid benchmark arm requires a live `VOYAGE_API_KEY` (see `docs/benchmarks/voyage-recall-2026-q2.md`).

Concurrency contract (`src/concurrency/model.ts`):
- **No LLM/network call inside any SQLite write transaction** (load-bearing — gateway hot path would stall on Voyage latency).
- Gateway `busy_timeout = 30s`; worker `busy_timeout = 5s` — gateway always wins contention.
- `WORKER_LOCK_TTL_MS = 90s`; `WORKER_HEARTBEAT_MS = 30s` (3× cadence).
- `GATEWAY_FALLBACK_SOAK_MS = 300s` — gateway only takes over a worker job after both lock expiry AND 5min of heartbeat silence.

Python port: `httpx.AsyncClient` for Voyage; stdlib `sqlite3` + `sqlite-vec` PyPI package; `asyncio.create_task` + `asyncio.sleep` for the worker-loop dispatcher; same SQL verbatim for the lock table.

---

## File mapping

| TS path | Python path | LOC est |
|---|---|---:|
| `src/voyage/client.ts` | `src/lossless_hermes/voyage/client.py` | 650 |
| `src/embeddings/store.ts` | `src/lossless_hermes/embeddings/store.py` | 600 |
| `src/embeddings/backfill.ts` | `src/lossless_hermes/embeddings/backfill.py` | 650 |
| `src/embeddings/semantic-search.ts` | `src/lossless_hermes/embeddings/semantic_search.py` | 420 |
| `src/embeddings/hybrid-search.ts` | `src/lossless_hermes/embeddings/hybrid_search.py` | 440 |
| `src/concurrency/worker-loop.ts` | `src/lossless_hermes/concurrency/worker_loop.py` | 250 |
| `src/concurrency/worker-lock.ts` | `src/lossless_hermes/concurrency/worker_lock.py` | 220 |
| `src/concurrency/model.ts` | `src/lossless_hermes/concurrency/model.py` | 150 |

Tests (port fixture-for-fixture, ~2,759 TS LOC total → ~2,500 Python LOC):

| TS test | Python test | TS LOC |
|---|---|---:|
| `test/voyage-client.test.ts` | `tests/voyage/test_client.py` | 561 |
| `test/embeddings-store.test.ts` | `tests/embeddings/test_store.py` | 525 |
| `test/embeddings-backfill.test.ts` | `tests/embeddings/test_backfill.py` | 474 |
| `test/semantic-search.test.ts` | `tests/embeddings/test_semantic_search.py` | 355 |
| `test/hybrid-search.test.ts` | `tests/embeddings/test_hybrid_search.py` | 313 |
| `test/worker-loop.test.ts` | `tests/concurrency/test_worker_loop.py` | 261 |
| `test/worker-lock.test.ts` | `tests/concurrency/test_worker_lock.py` | 150 |
| `test/lcm-worker-lock.test.ts` | `tests/concurrency/test_lcm_worker_lock.py` | 120 |

---

## Voyage client — exhaustive spec

Source: `/Volumes/LEXAR/Claude/lossless-claw/src/voyage/client.ts:1-616`. Spike 004 (`docs/spike-results/004-voyage-python-client.md`) confirmed branch-for-branch portability into `httpx.AsyncClient`.

### Endpoints

| URL | Method | Source |
|---|---|---|
| `https://api.voyageai.com/v1/embeddings` | POST | `client.ts:94` (`VOYAGE_API_BASE`), `client.ts:238` (`url = ${baseUrl}/embeddings`) |
| `https://api.voyageai.com/v1/rerank` | POST | `client.ts:329` |

### Models

Defined in `client.ts:96-103`:

- **Embedding (`VoyageEmbeddingModel`):** `voyage-4-large` (default for LCM; 1024 dim default, supports 256/512/1024/2048 via `output_dimension`), `voyage-3`, `voyage-3-large`, `voyage-3-lite`, `voyage-code-3`
- **Reranker (`VoyageRerankerModel`):** `rerank-2.5` (default), `rerank-2`, `rerank-2-lite`
- **Input type (`VoyageInputType`):** `"query"` (retrieval queries), `"document"` (stored items), `null` (Voyage default — field omitted from body)

Spike-004 docs also list these as available on the Voyage API (not necessarily wired in TS): `voyage-4`, `voyage-4-lite`, `voyage-3.5`, `voyage-3.5-lite`, `voyage-finance-2`, `voyage-law-2`. Port should accept arbitrary `model: str` since Voyage's catalog evolves; only the dim-checked code paths care about the specific model.

### Constants (preserve verbatim)

| TS constant | Value | Reason |
|---|---:|---|
| `MAX_TOKENS_PER_EMBED_BATCH` | `80_000` | Voyage server cap is 120K; Voyage tokenizer counts ~9.5% higher than our `summaries.token_count`. 80K × 1.10 = 88K << 120K — safe margin |
| `MAX_TOKENS_PER_EMBED_DOC` | `27_000` | `voyage-4-large` per-doc cap is 32K; 27K × 1.095 ≈ 29.6K. Wave-1 finding #3: 30K was at the edge and observed 400s in production |
| `MAX_TOKENS_PER_RERANK_CALL` | `600_000` | `rerank-2.5` per-call total cap |
| `DEFAULT_MAX_RETRIES` | `3` | 4 attempts total (initial + 3 retries) |
| `BACKOFF_BASE_MS` | `500` | Exponential schedule: 500, 1000, 2000, 4000, … |
| `BACKOFF_CAP_MS` | `25_000` | Wave-1 fix: was 30s. 30s × 2 attempts = 90s = `WORKER_LOCK_TTL_MS`. 25s leaves 5s margin |
| `DEFAULT_TIMEOUT_MS` | `60_000` | Per-attempt request timeout |
| `RETRY_AFTER_HARD_CAP_MS` | `5 * 60 * 1000` | Soft cap when parsing 429 `Retry-After` (no realistic Voyage value exceeds 5min) |
| `LOCK_BUDGET_AWARE_RETRY_MS` | `60_000` | ~2/3 of `WORKER_LOCK_TTL_MS=90s`. If 429 `Retry-After` exceeds this, throw immediately instead of waiting |

### Retry policy (load-bearing — port branch-for-branch from `postWithRetry` at `client.ts:421-558`)

1. **Max attempts:** `1 + maxRetries` (default 4).
2. **Backoff schedule:** `BACKOFF_BASE_MS * 2 ** attempt`, capped at `BACKOFF_CAP_MS`. So waits before retries 1/2/3 are 500/1000/2000 ms.
3. **Per-attempt timeout:** 60 s default. Wraps full request via `AbortController` in TS → `httpx.Timeout(total=timeout_s, connect=10.0)` in Python.
4. **401/403 → `VoyageError(kind="auth")`, no retry** (`client.ts:467-478`). Caller stops, surfaces to operator.
5. **400 → `VoyageError(kind="bad_request")`, no retry** (`client.ts:480-496`). `summarizeBody` applied to both message AND `responseBody` (Wave-4 finding: raw body leaks input echoes to Sentry).
6. **429 with `Retry-After ≤ LOCK_BUDGET_AWARE_RETRY_MS`:** sleep server hint, retry (`client.ts:497-526`).
7. **429 with `Retry-After > LOCK_BUDGET_AWARE_RETRY_MS`:** throw immediately so caller releases worker lock. Honoring 120s `Retry-After` would burn the 90s lock TTL (Wave-2 fix F1).
8. **429 without `Retry-After`:** exponential backoff.
9. **5xx → retry with exponential backoff** (`client.ts:528-542`). After `maxRetries`: `VoyageError(kind="server_error")`.
10. **Network error (fetch throws):** `client.ts:446-455`. Retry with backoff. After exhausted: `VoyageError(kind="network")`.
11. **Other 4xx (not 400/401/403/429):** `VoyageError(kind="bad_request")`, no retry (`client.ts:545-551`).
12. **Connection timeout:** 10s (Python addition — TS uses single timeout). Read timeout: 60s.

### Batch packing (caller-enforced; client just passes through)

- **Embed batch (caller):** `sum(token_count) ≤ MAX_TOKENS_PER_EMBED_BATCH (80K)`. See `packBatches` in `backfill.ts:531-546` — greedy bin-pack, no re-sorting.
- **Embed per-doc:** `token_count ≤ MAX_TOKENS_PER_EMBED_DOC (27K)`. Voyage 400s otherwise; client surfaces 400 without retry. Caller (backfill) MUST pre-filter; backfill records over-cap docs in `BackfillResult.skippedOverCap` and never sends them.
- **Rerank total:** `query_tokens + sum(doc_tokens) ≤ MAX_TOKENS_PER_RERANK_CALL (600K)`. See `hybrid-search.ts:329-360` — packs candidates with 85% safety margin (`Math.floor(MAX * 0.85) = 510K`), skips individually-oversized candidates (Wave-11 fix), surfaces `rerankPackTruncated` on truncation.
- **Token counting:** caller's responsibility. LCM uses `summaries.token_count` stored at write time (heuristic, ~9.5% lower than Voyage's tokenizer). Python port should accept caller-provided counts; do not re-implement the heuristic in the HTTP client.
- **Single-doc-exceeds-cap behavior:** TS client does NOT split or truncate. Caller (backfill) records `skippedOverCap`; agent tool surfaces via `/lcm health`. Lossless contract: splitting changes the semantic unit.

### Error body PII suppression (`client.ts:560-576`)

Apply to EVERY non-2xx body before attaching to `VoyageError.responseBody` or error messages (Waves 4 + 7 fixes — Sentry captures full exception; raw body leaks too):

```python
def summarize_body(body: str) -> str:
    if '"input"' in body or '"texts"' in body or '"documents"' in body:
        return "input echoed in error body — suppressed for privacy"
    return body[:200]

def safe_read_body(resp: httpx.Response) -> str:
    try:
        text = resp.text
        return text[:800] + "…(truncated)" if len(text) > 800 else text
    except Exception:
        return ""
```

`safeReadBody` clips to 800 chars BEFORE `summarizeBody`, which then clips to 200 (or full suppression string).

### API key resolution (`client.ts:410-419`)

Order: explicit `apiKey` opt > `process.env.VOYAGE_API_KEY`. Empty key → `VoyageError(kind="auth", "voyage_auth: VOYAGE_API_KEY is empty (set env or pass `apiKey` option)")`.

Note: source comment at `client.ts:128-129` mentions production loads from `~/.openclaw/credentials/voyage-api-key`. Hermes will need its own credential resolution — see "Open architecture decisions" below.

### Response parsing

**Embeddings (`client.ts:266-313`):**
1. `json.data` must be array of length `texts.length`. Otherwise `VoyageError(kind="unexpected")`.
2. Each `data[i]` has `embedding: number[]` (must match other items' dim) and `index: number` (0 ≤ index < len). Otherwise `unexpected`.
3. **Re-order by `index` field** — Voyage may return out-of-order in pathological cases.
4. Return `{ vectors: Float32Array[] in input order, totalTokens: usage.total_tokens, model: response.model }`.

**Rerank (`client.ts:349-385`):**
1. `json.data` must be array. Each item: `index: number` (valid range), `relevance_score: number`. Otherwise `unexpected`.
2. Join `id` from original `candidates[item.index]`.
3. **Defensive sort by `score` descending** (Voyage docs say they sort, but defend).
4. Return `{ results, totalTokens, model }`.

### Python implementation sketch

```python
# src/lossless_hermes/voyage/client.py
from __future__ import annotations
import asyncio
import json
import os
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
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
    "auth", "bad_request", "rate_limit", "server_error", "network", "unexpected",
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
    vectors: list[list[float]]  # ordered as input texts
    total_tokens: int
    model: str


@dataclass(frozen=True)
class RerankItem:
    id: str        # caller-supplied opaque id, joined back
    index: int     # original position in candidates
    score: float


@dataclass(frozen=True)
class RerankResult:
    results: list[RerankItem]  # sorted score desc
    total_tokens: int
    model: str


class VoyageClient:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = VOYAGE_API_BASE,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = (api_key or os.environ.get("VOYAGE_API_KEY", "")).strip()
        if not self._api_key:
            raise VoyageError(
                "auth",
                "voyage_auth: VOYAGE_API_KEY is empty (set env or pass `api_key`)",
            )
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._max_retries = max_retries
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=10.0),
        )

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
        if output_dimension and output_dimension > 0:
            body["output_dimension"] = output_dimension
        resp = await self._post_with_retry("/embeddings", body, input_count=len(texts))
        return self._parse_embed_response(resp, expected=len(texts), model_hint=model)

    async def rerank(
        self,
        query: str,
        candidates: list[tuple[str, str]],   # [(id, text), ...]
        *,
        model: str = "rerank-2.5",
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

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- internals ----

    async def _post_with_retry(
        self, path: str, body: dict, *, input_count: int,
    ) -> httpx.Response:
        url = f"{self._base_url}{path}"
        last_err: VoyageError | None = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._client.post(
                    url,
                    json=body,
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
                raise VoyageError(
                    "auth", f"voyage_auth: {status} (check VOYAGE_API_KEY)",
                    status=status, response_body=_summarize_body(body_text),
                )
            if status == 400:
                suppressed = _summarize_body(body_text)
                raise VoyageError(
                    "bad_request",
                    f"voyage_400: bad request on {input_count} inputs ({suppressed})",
                    status=status, response_body=suppressed,
                )
            if status == 429:
                retry_after = _parse_retry_after_ms(resp.headers.get("Retry-After"))
                last_err = VoyageError(
                    "rate_limit",
                    f"voyage_429: rate limited (attempt {attempt + 1}/{self._max_retries + 1})",
                    status=status, retry_after_ms=retry_after,
                    response_body=_summarize_body(body_text),
                )
                if (
                    attempt < self._max_retries
                    and (retry_after or 0) <= LOCK_BUDGET_AWARE_RETRY_MS
                ):
                    await asyncio.sleep((retry_after or _backoff_ms(attempt)) / 1000)
                    continue
                raise last_err
            if 500 <= status < 600:
                last_err = VoyageError(
                    "server_error",
                    f"voyage_5xx: {status} (attempt {attempt + 1}/{self._max_retries + 1})",
                    status=status, response_body=_summarize_body(body_text),
                )
                if attempt < self._max_retries:
                    await asyncio.sleep(_backoff_ms(attempt) / 1000)
                    continue
                raise last_err
            # Some other 4xx
            raise VoyageError(
                "bad_request",
                f"voyage_4xx: {status} {_summarize_body(body_text)}",
                status=status, response_body=_summarize_body(body_text),
            )
        raise last_err or VoyageError("unexpected", "voyage_unexpected: postWithRetry exited loop")


def _backoff_ms(attempt: int) -> int:
    return min(BACKOFF_BASE_MS * 2 ** attempt, BACKOFF_CAP_MS)


def _safe_read_body(resp: httpx.Response) -> str:
    try:
        text = resp.text
        return text[:800] + "…(truncated)" if len(text) > 800 else text
    except Exception:
        return ""


def _summarize_body(body: str) -> str:
    if '"input"' in body or '"texts"' in body or '"documents"' in body:
        return "input echoed in error body — suppressed for privacy"
    return body[:200]


def _parse_retry_after_ms(header: str | None) -> int | None:
    """Parse Retry-After (seconds or HTTP-date). Soft-capped at 5min."""
    if not header:
        return None
    try:
        as_num = float(header)
        if as_num >= 0:
            return min(int(as_num * 1000), RETRY_AFTER_HARD_CAP_MS)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(header)
        import datetime as dtmod
        delta_ms = int((dt - dtmod.datetime.now(dt.tzinfo)).total_seconds() * 1000)
        return min(delta_ms, RETRY_AFTER_HARD_CAP_MS) if delta_ms > 0 else None
    except (TypeError, ValueError):
        return None
```

**Response parsing** (omitted above for brevity — port `client.ts:266-313` for `_parse_embed_response` and `client.ts:349-385` for `_parse_rerank_response`, including the index-re-order and defensive sort).

### Open question: Float32 precision parity

TS uses `Float32Array.from(...)` which silently downcasts JSON `number` (IEEE 754 double) to single-precision. Python `float` is double-precision. For byte-identical vec0 INSERTs to a `float32` vec0 column, cast at the storage boundary via `numpy.array(vec, dtype=np.float32).tobytes()` or use `sqlite_vec.serialize_float32(vec)` (per Spike 001).

---

## sqlite-vec store

Source: `/Volumes/LEXAR/Claude/lossless-claw/src/embeddings/store.ts:1-610`. Spike 001 (`docs/spike-results/001-sqlite-vec-python.md`) = PASS — `sqlite-vec==0.1.9` works on Homebrew Python 3.12+ via stdlib `sqlite3.enable_load_extension(True) + sqlite_vec.load(conn)`.

### Per-model virtual table shape

`ensureEmbeddingsTable(db, modelName, dim)` at `store.ts:206-253` creates:

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS lcm_embeddings_<slug> USING vec0(
    embedding float[<DIM>],
    +embedded_id text,      -- AUXILIARY (prefix `+`): stored uncompressed, not WHERE-filterable
    embedded_kind text,     -- METADATA: WHERE-filterable inside MATCH (summary/entity/theme)
    suppressed integer      -- METADATA: WHERE-filterable pre-pass (0/1)
);
```

Column class choice is load-bearing (`store.ts:172-180` comments):
- `embedding` — partition key, stores the vector. Distance metric is L2 by default (NO cosine modifier in `USING vec0(...)`). For unit-normalized Voyage vectors, `L² = 2·(1 - cos)` (monotone).
- `+embedded_id` — auxiliary (`+` prefix). Stored alongside the vector, returned in KNN results. NOT filterable in `MATCH` queries. `summaries.summary_id` / `lcm_entities.entity_id` / `lcm_themes.theme_id`.
- `embedded_kind` — metadata. Filterable: `WHERE embedded_kind IN ('summary')` inside MATCH. Required because polymorphic over summary/entity/theme.
- `suppressed` — metadata. Pre-filter so suppressed rows never surface in KNN: `WHERE suppressed = 0`.

**vec0 gotcha:** UPDATE on partition-key columns corrupts vec0 (v4.1.1 finding). Only UPDATE on METADATA columns is safe (`markEmbeddingSuppressed` at `store.ts:487-501` uses this). For id/kind changes, use DELETE + INSERT.

**Python sqlite3 quirk avoided:** TS `node:sqlite` requires `BigInt` literals for INTEGER metadata cols (`store.ts:399: const suppressedBig = suppressed ? 1n : 0n;`). Python's `sqlite3` round-trips int natively — pass `1` or `0` directly. No BigInt dance.

### Sidecar tables (defined in LCM migrations, not in this module — port the migration separately)

- **`lcm_embedding_meta`** — `(embedded_id, embedded_kind, embedding_model, embedded_at, source_token_count, archived)`. "Is this thing embedded?" lookup that doesn't need to load the vector. Sidecar to vec0 for `NOT EXISTS` pre-filter in backfill SELECT.
- **`lcm_embedding_profile`** — `(model_name PK, dim, active, registered_at, archive_after)`. One row per registered model; `active=1` rows are queryable. Profile dim is immutable — `registerEmbeddingProfile` at `store.ts:294-351` throws on dim-mismatch. Slug uniqueness enforced (two model names that sluggify identically → throw to prevent vec0 table-name collision).
- **`lcm_worker_lock`** — `(job_kind PK, worker_id, acquired_at, expires_at, last_heartbeat_at, job_session_key, job_metadata)`. See "Worker lock" section.

### Slug normalization

`embeddingsTableName(modelName)` at `store.ts:58-72`:

```python
MODEL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

def embeddings_table_name(model_name: str) -> str:
    if not MODEL_NAME_PATTERN.match(model_name):
        raise ValueError(f"invalid model name: {model_name!r}")
    slug = re.sub(r"[^a-z0-9]", "", model_name.lower())
    if not slug:
        raise ValueError(f"model name {model_name!r} sluggifies to empty")
    return f"lcm_embeddings_{slug}"
```

`voyage-4-large` → `lcm_embeddings_voyage4large`. Used in CREATE TABLE / CREATE TRIGGER (no bind params allowed in DDL) — defense-in-depth against table-name injection.

### AFTER UPDATE + AFTER DELETE triggers (per-model)

`ensureEmbeddingsTable` also creates two triggers (`store.ts:231-252`):

```sql
CREATE TRIGGER IF NOT EXISTS lcm_embed_suppress_<slug>
    AFTER UPDATE OF suppressed_at ON summaries
    WHEN (NEW.suppressed_at IS NULL) != (OLD.suppressed_at IS NULL)
    BEGIN
        UPDATE lcm_embeddings_<slug>
            SET suppressed = CASE WHEN NEW.suppressed_at IS NULL THEN 0 ELSE 1 END
            WHERE embedded_id = NEW.summary_id AND embedded_kind = 'summary';
    END;

CREATE TRIGGER IF NOT EXISTS lcm_embed_delete_<slug>
    AFTER DELETE ON summaries
    BEGIN
        DELETE FROM lcm_embeddings_<slug>
            WHERE embedded_id = OLD.summary_id AND embedded_kind = 'summary';
    END;
```

**Why triggers and not FK CASCADE:** vec0 corrupts under foreign-key constraints (v4.1.1 finding). Triggers are the only safe path. Per-model because vec0 SQL doesn't support dynamic table-name resolution inside triggers.

### Load pattern (Python, Spike 001 = PASS)

```python
import sqlite3
import sqlite_vec

def open_lcm_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)  # tighten attack surface
    return conn
```

**Vector binding (preferred — 2.3× faster than JSON per Spike 001):**

```python
vec_bytes = sqlite_vec.serialize_float32(vector)  # bytes, len = 4 * dim
conn.execute(
    f"INSERT INTO {tbl} (embedding, embedded_id, embedded_kind, suppressed) "
    "VALUES (?, ?, ?, ?)",
    (vec_bytes, embedded_id, embedded_kind, 1 if suppressed else 0),
)
```

JSON-string binding also works (`json.dumps(list(vector))`) and is what the TS port does (`store.ts:396`). For maximum perf, use bytes; for parity-debug-ability with the TS port, use JSON. Recommend bytes — perf gap matters for backfill throughput.

**Gotchas (Spike 001):**
- Apple system `/usr/bin/python3` has NO `enable_load_extension` — fails with `AttributeError`. Hard-require Homebrew / pyenv / uv-managed Python.
- `pysqlite3-binary` is Linux-only — don't pin in `pyproject.toml` as a hard dep.
- Fallback driver: `apsw==3.53.1.0` (cross-platform). Different API (`enableloadextension` no underscore) — isolate the connection-open behind a function.

### Module candidate-path search (likely unneeded in Python)

TS has `candidateVec0Paths()` at `store.ts:83-100` that searches `~/.openclaw/extensions/node_modules/sqlite-vec-<platform>-<arch>/vec0.<ext>` because LCM bundles the binary with the plugin install.

Python's `sqlite_vec.load(conn)` finds its own bundled extension via the `sqlite-vec` PyPI package — no candidate-path search needed. Port can drop this complexity; `tryLoadSqliteVec` collapses to:

```python
def try_load_sqlite_vec(conn: sqlite3.Connection, *, silent: bool = False) -> bool:
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except (AttributeError, sqlite3.OperationalError) as e:
        if not silent:
            logging.warning(f"[embeddings.store] failed to load sqlite-vec: {e}")
        return False
```

### Function-by-function port checklist

| TS function | Source lines | Python equivalent | Notes |
|---|---:|---|---|
| `embeddingsTableName` | 58–72 | `embeddings_table_name` | Same regex; same slug rule |
| `candidateVec0Paths` | 83–100 | Drop (use `sqlite_vec.load`) | |
| `tryLoadSqliteVec` | 122–146 | `try_load_sqlite_vec` | Simplified per above |
| `vec0Version` | 152–159 | `vec0_version` | `SELECT vec_version()`; return None on failure |
| `ensureEmbeddingsTable` | 206–253 | `ensure_embeddings_table` | Same SQL verbatim |
| `dropEmbeddingsTriggers` | 263–275 | `drop_embeddings_triggers` | Same |
| `registerEmbeddingProfile` | 294–351 | `register_embedding_profile` | Same slug-collision check |
| `recordEmbedding` | 366–443 | `record_embedding` | SAVEPOINT with crypto-random suffix; DELETE-before-INSERT (Wave-4 dup guard) |
| `replaceEmbedding` | 450–459 | `replace_embedding` | DELETE + recordEmbedding |
| `deleteEmbedding` | 465–477 | `delete_embedding` | DELETE from both vec0 + meta |
| `markEmbeddingSuppressed` | 487–501 | `mark_embedding_suppressed` | UPDATE metadata col (safe for vec0) |
| `searchSimilar` | 542–580 | `search_similar` | `WHERE embedding MATCH ? AND k = ?`; bind vector as JSON or bytes; filter on metadata cols inside MATCH |
| `embeddingsTableExists` | 586–592 | `embeddings_table_exists` | `sqlite_master` lookup |
| `isEmbedded` | 598–609 | `is_embedded` | Meta lookup |

---

## Worker loop

Source: `/Volumes/LEXAR/Claude/lossless-claw/src/concurrency/worker-loop.ts:1-238`.

### TS pattern (setInterval cooperative dispatch)

`WorkerLoop` class manages multiple background jobs in one Node process. Each job has its own `intervalMs` cadence. Key behaviors (`worker-loop.ts:122-158`):

1. `start()` is idempotent — returns false on already-running.
2. Bumps a `generationId` on each start; scheduled timers check `this.generationId !== myGeneration` to skip leftover ticks from old loops (defense against `stop()`-then-`start()`).
3. For each job: `setInterval(..., job.intervalMs)`. On each tick:
   - Skip if `!this.running` or generation mismatch.
   - Skip if previous tick for this job is still `inFlight` (no queueing — drop overlaps).
   - Wrap `job.run(this.db)` in async IIFE that catches all errors, calls `onJobComplete({kind, durationMs, result?, error?})`.
   - Track promise in `this.inFlight: Map<WorkerJobKind, Promise<void>>` so `stop()` can wait gracefully.
4. `stop({ gracefulTimeoutMs = 30s })`:
   - Set `running = false`; `clearInterval` all timers.
   - `Promise.race([Promise.all(inFlightPromises), timeoutPromise])`. Return `true` if all in-flight finished cleanly, `false` on timeout.
5. `runOnce(kind)` — invoke a specific job immediately outside the schedule (for tests, leaf-write nudges, `/lcm worker tick` CLI).

Jobs MUST acquire their own `lcm_worker_lock` for cross-process single-flight. Errors thrown by `job.run` are captured (not propagated) — a bad tick doesn't crash the loop.

### Python pattern (asyncio)

`setInterval` doesn't have a direct asyncio equivalent. Use one `asyncio.create_task` per job that loops with `asyncio.sleep(interval_s)`. Hermes already does this pattern (`tools/mcp_tool.py:_schedule_tools_refresh`, `plugins/platforms/google_chat/adapter.py:_run_supervisor`).

```python
# src/lossless_hermes/concurrency/worker_loop.py
from __future__ import annotations
import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from .model import WorkerJobKind

logger = logging.getLogger(__name__)


@dataclass
class WorkerJob:
    kind: WorkerJobKind
    interval_s: float
    run: Callable[[], Awaitable[object]]  # Python: connection captured by caller, not passed


@dataclass
class JobCompleteInfo:
    kind: WorkerJobKind
    duration_ms: float
    result: object | None = None
    error: BaseException | None = None


class WorkerLoop:
    def __init__(
        self,
        jobs: list[WorkerJob],
        *,
        on_job_complete: Callable[[JobCompleteInfo], None] | None = None,
    ) -> None:
        self._jobs = jobs
        self._on_job_complete = on_job_complete
        self._tasks: list[asyncio.Task] = []
        self._in_flight: dict[WorkerJobKind, asyncio.Task] = {}
        self._running = False
        self._generation = 0
        self._validate_jobs()

    def _validate_jobs(self) -> None:
        seen: set[WorkerJobKind] = set()
        for job in self._jobs:
            if job.kind in seen:
                raise ValueError(f"[worker-loop] duplicate job kind: {job.kind}")
            seen.add(job.kind)
            if not job.interval_s or job.interval_s <= 0:
                raise ValueError(
                    f"[worker-loop] job {job.kind} has invalid interval_s {job.interval_s}"
                )

    def start(self) -> bool:
        if self._running:
            return False
        self._running = True
        self._generation += 1
        gen = self._generation
        for job in self._jobs:
            task = asyncio.create_task(self._run_job(job, gen), name=f"worker-{job.kind}")
            self._tasks.append(task)
        return True

    async def _run_job(self, job: WorkerJob, my_gen: int) -> None:
        while self._running and self._generation == my_gen:
            # Skip if a previous tick for this kind is still in flight
            existing = self._in_flight.get(job.kind)
            if existing is not None and not existing.done():
                await asyncio.sleep(job.interval_s)
                continue
            started = time.monotonic()
            tick_task = asyncio.create_task(self._invoke_once(job, started))
            self._in_flight[job.kind] = tick_task
            await asyncio.sleep(job.interval_s)

    async def _invoke_once(self, job: WorkerJob, started: float) -> None:
        try:
            result = await job.run()
            self._dispatch_complete(
                JobCompleteInfo(
                    kind=job.kind,
                    duration_ms=(time.monotonic() - started) * 1000,
                    result=result,
                )
            )
        except BaseException as e:  # noqa: BLE001 (intentional — see TS comments)
            self._dispatch_complete(
                JobCompleteInfo(
                    kind=job.kind,
                    duration_ms=(time.monotonic() - started) * 1000,
                    error=e,
                )
            )
            # Do NOT re-raise — the loop continues even if a single tick fails

    def _dispatch_complete(self, info: JobCompleteInfo) -> None:
        if self._on_job_complete is None:
            return
        try:
            self._on_job_complete(info)
        except BaseException:
            logger.exception("[worker-loop] on_job_complete raised")

    async def stop(self, *, graceful_timeout_s: float = 30.0) -> bool:
        if not self._running:
            return True
        self._running = False
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        in_flight_tasks = [t for t in self._in_flight.values() if not t.done()]
        if not in_flight_tasks:
            return True
        try:
            await asyncio.wait_for(
                asyncio.gather(*in_flight_tasks, return_exceptions=True),
                timeout=graceful_timeout_s,
            )
            return True
        except asyncio.TimeoutError:
            return False

    async def run_once(self, kind: WorkerJobKind) -> object:
        job = next((j for j in self._jobs if j.kind == kind), None)
        if not job:
            raise ValueError(f"[worker-loop] no job kind: {kind}")
        existing = self._in_flight.get(kind)
        if existing is not None and not existing.done():
            raise RuntimeError(f"[worker-loop] job {kind} is already in flight")
        started = time.monotonic()
        task = asyncio.create_task(job.run())
        self._in_flight[kind] = task
        try:
            result = await task
            self._dispatch_complete(
                JobCompleteInfo(kind=kind, duration_ms=(time.monotonic() - started) * 1000, result=result)
            )
            return result
        except BaseException as e:
            self._dispatch_complete(
                JobCompleteInfo(kind=kind, duration_ms=(time.monotonic() - started) * 1000, error=e)
            )
            raise

    def is_running(self) -> bool:
        return self._running

    def in_flight_count(self) -> int:
        return sum(1 for t in self._in_flight.values() if not t.done())
```

**Differences from TS:**
- Python passes the DB connection via closure in `job.run` (not as a parameter) — async sqlite calls in Python often use a per-job connection or thread-pool wrapper. Cleaner than passing a stale handle.
- TS's `setInterval` is replaced by per-job loop tasks. Same semantics: skip overlapping ticks; isolate exceptions.
- `runOnce` semantics preserved.

---

## Worker lock (cross-process)

Source: `/Volumes/LEXAR/Claude/lossless-claw/src/concurrency/worker-lock.ts:1-216`.

### Table schema (port the migration separately)

```sql
CREATE TABLE lcm_worker_lock (
    job_kind TEXT PRIMARY KEY,         -- one row per kind; PK uniqueness IS the lock
    worker_id TEXT NOT NULL,           -- "<role>-<pid>-<startMs>-<nonce>"
    acquired_at TEXT NOT NULL,         -- ISO-8601
    expires_at TEXT NOT NULL,          -- ISO-8601; comparison is lexicographic-safe
    last_heartbeat_at TEXT NOT NULL,
    job_session_key TEXT,              -- informational scope (e.g., per-session)
    job_metadata TEXT                  -- diagnostic tag
);
```

### Constants (from `concurrency/model.ts:55-87`)

| Constant | Value | Reason |
|---|---:|---|
| `GATEWAY_BUSY_TIMEOUT_MS` | `30_000` | Gateway always wins contention |
| `WORKER_BUSY_TIMEOUT_MS` | `5_000` | Worker yields to gateway |
| `WORKER_HEARTBEAT_MS` | `30_000` | Heartbeat every 30s |
| `WORKER_LOCK_TTL_MS` | `90_000` | 3× heartbeat cadence (one missed heartbeat OK) |
| `GATEWAY_FALLBACK_SOAK_MS` | `300_000` | Gateway takeover requires lock expiry + 5min heartbeat silence |

### Job kinds (`model.ts:95-104`)

```python
WORKER_JOB_KINDS = (
    "condensation",
    "extraction",
    "embedding-backfill",
    "profile-rebuild",
    "theme-consolidation",
    "eval",
)
```

### TTL + heartbeat scheme

1. **`acquire_lock(db, job_kind, worker_id, *, ttl_ms=90_000, job_session_key=None, job_metadata=None) -> bool`** (`worker-lock.ts:69-109`)
   - GC step: `DELETE FROM lcm_worker_lock WHERE job_kind = ? AND expires_at <= datetime('now')` — lazily clears stale locks. `<=` so `ttl=0` is immediately reclaimable.
   - `INSERT OR IGNORE INTO lcm_worker_lock (...) VALUES (?, ?, datetime('now'), datetime('now', '+<ttl_s> seconds'), datetime('now'), ?, ?)` — atomic; `changes > 0` means we got it.
   - Race: another process acquires between DELETE and INSERT → second writer's INSERT no-ops (PK uniqueness). Worst case: caller is told false when they could have had the lock; never silently double-acquires.

2. **`heartbeat_lock(db, job_kind, worker_id, *, ttl_ms=90_000) -> bool`** (`worker-lock.ts:139-168`)
   - `UPDATE lcm_worker_lock SET last_heartbeat_at = datetime('now'), expires_at = datetime('now', '+<ttl_s> seconds') WHERE job_kind = ? AND worker_id = ? AND expires_at > datetime('now')`
   - **Critical Wave-1 fix:** the `expires_at > now` predicate. Without it, an already-expired lock could be silently re-extended even after another worker GC'd + acquired. Returns false → caller MUST abort.

3. **`release_lock(db, job_kind, worker_id) -> bool`** (`worker-lock.ts:121-130`)
   - `DELETE FROM lcm_worker_lock WHERE job_kind = ? AND worker_id = ?`

4. **`lock_info(db, job_kind) -> LockInfo | None`** (`worker-lock.ts:174-202`)
   - `SELECT * FROM lcm_worker_lock WHERE job_kind = ?` — used by `/lcm health` and tests.

5. **`generate_worker_id(role) -> str`** (`worker-lock.ts:210-215`) — `"<role>-<pid>-<startMs>-<6_hex_chars>"`. Python: `f"{role}-{os.getpid()}-{int(time.time()*1000)}-{secrets.token_hex(3)}"`.

### Python port

The SQL ports straight (SQLite TEXT comparison on ISO-8601 is lexicographic-safe). Use `asyncio.create_task` for the heartbeat task:

```python
# src/lossless_hermes/concurrency/worker_lock.py
import os, secrets, sqlite3, time
from dataclasses import dataclass
from .model import WORKER_LOCK_TTL_MS, WorkerJobKind


@dataclass(frozen=True)
class LockInfo:
    job_kind: str
    worker_id: str
    acquired_at: str
    expires_at: str
    last_heartbeat_at: str
    job_session_key: str | None
    job_metadata: str | None


def acquire_lock(
    db: sqlite3.Connection,
    job_kind: WorkerJobKind,
    *,
    worker_id: str,
    ttl_ms: int = WORKER_LOCK_TTL_MS,
    job_session_key: str | None = None,
    job_metadata: str | None = None,
) -> bool:
    if not worker_id.strip():
        raise ValueError("[worker-lock] worker_id is required")
    # Lazy GC of stale locks
    db.execute(
        "DELETE FROM lcm_worker_lock "
        "WHERE job_kind = ? AND expires_at <= datetime('now')",
        (job_kind,),
    )
    ttl_s = round(ttl_ms / 1000)
    cur = db.execute(
        "INSERT OR IGNORE INTO lcm_worker_lock "
        "(job_kind, worker_id, acquired_at, expires_at, last_heartbeat_at, "
        " job_session_key, job_metadata) "
        "VALUES (?, ?, datetime('now'), "
        "        datetime('now', '+' || ? || ' seconds'), "
        "        datetime('now'), ?, ?)",
        (job_kind, worker_id, ttl_s, job_session_key, job_metadata),
    )
    db.commit()
    return cur.rowcount > 0


def heartbeat_lock(
    db: sqlite3.Connection,
    job_kind: WorkerJobKind,
    worker_id: str,
    ttl_ms: int = WORKER_LOCK_TTL_MS,
) -> bool:
    ttl_s = round(ttl_ms / 1000)
    cur = db.execute(
        "UPDATE lcm_worker_lock "
        "SET last_heartbeat_at = datetime('now'), "
        "    expires_at = datetime('now', '+' || ? || ' seconds') "
        "WHERE job_kind = ? AND worker_id = ? AND expires_at > datetime('now')",
        (ttl_s, job_kind, worker_id),
    )
    db.commit()
    return cur.rowcount > 0


def release_lock(db: sqlite3.Connection, job_kind: WorkerJobKind, worker_id: str) -> bool:
    cur = db.execute(
        "DELETE FROM lcm_worker_lock WHERE job_kind = ? AND worker_id = ?",
        (job_kind, worker_id),
    )
    db.commit()
    return cur.rowcount > 0


def generate_worker_id(role: str) -> str:
    return f"{role}-{os.getpid()}-{int(time.time() * 1000)}-{secrets.token_hex(3)}"
```

**Python sqlite3 gotcha:** stdlib `sqlite3.Connection` is in autocommit-off mode by default (PEP-249) — INSERTs/UPDATEs need an explicit `db.commit()`. Node's `node:sqlite` autocommits. Don't forget the `commit()` calls.

---

## Hybrid search pipeline

Source: `/Volumes/LEXAR/Claude/lossless-claw/src/embeddings/hybrid-search.ts:1-437`.

### Pipeline steps

1. **Parallel arms** (`hybrid-search.ts:200-252`):
   - `ftsSearch(query, limit=kFts=50)` — caller-injected (Python: `await asyncio.gather(fts_search(...), semantic_search(...))`).
   - `runSemanticSearch(query, k=kSemantic=50, inputType='query')` — embeds query, runs KNN, JOINs back to summaries.
2. **Dedupe by `summary_id`** (`hybrid-search.ts:259-298`): for each FTS hit, create a `HybridHit` with `ftsRank=i`; for each semantic hit, either merge into the FTS hit (sets `fromSemantic=true`, fills `semanticDistance`) or create a new entry.
3. **Rerank packing** (`hybrid-search.ts:326-360`, **load-bearing Wave-10/11 fixes**):
   - Budget = `floor(MAX_TOKENS_PER_RERANK_CALL * 0.85)` = 510K.
   - Initial cumulative = `ceil(len(query) / 4)` (token estimate).
   - For each candidate: skip if `tokenCount > budget` (Wave-11: don't break on first oversized; continue packing). Stop when `cumulative + candTokens > budget` (truncated).
   - Surface `rerankPackTruncated` and `rerankPackedCount` so caller can warn.
4. **Voyage rerank-2.5** (`hybrid-search.ts:362-373`): POST `/v1/rerank` with packed candidates. `topK = min(topN, packed.length)`.
5. **Rerank scoring** (`hybrid-search.ts:380-400`): map rerank results back to `HybridHit` by id; emit sorted by rerank score.
6. **RRF fallback** (`hybrid-search.ts:415-428`): if rerank skipped (auth-error re-thrown; other errors fall through) or `rerank=false`:
   - `score = (1 / (60 + ftsRank)) + (1 / (60 + semIdx))` where `semIdx` is recovered by searching `semResult.hits` for the summaryId.
   - Sort descending; take top `topN`.

### Degraded-result surfaces (TS contract — preserve in Python)

| Field | True when | Caller action |
|---|---|---|
| `degradedToFtsOnly` | vec0 unavailable OR semantic Voyage non-auth error | Operator warning; results still useful |
| `degradedSkippedRerank` | Rerank Voyage non-auth error OR rerank=false OR packed-empty | RRF used; slightly lower precision |
| `rerankPackTruncated` | Rerank input was packed to fit 600K budget | Operator warning; tail candidates excluded from rerank but available in `candidateCount` for backstop |
| `rerankPackedCount` | Always set when rerank ran | Diagnostic |

**Auth errors re-thrown:** `VoyageError(kind="auth")` in either arm propagates out (operator surfaces "set VOYAGE_API_KEY"); silent degradation would hide misconfigured deploys.

### Python skeleton

```python
# src/lossless_hermes/embeddings/hybrid_search.py
import asyncio, math
from dataclasses import dataclass
from .semantic_search import (
    runSemanticSearch, SemanticSearchUnavailableError, SemanticHit,
)
from ..voyage.client import (
    VoyageClient, VoyageError, MAX_TOKENS_PER_RERANK_CALL,
)


async def run_hybrid_search(
    conn,
    *,
    query: str,
    fts_search,           # async fn(**filters) -> list[FtsHit]
    voyage: VoyageClient,
    k_fts: int = 50,
    k_semantic: int = 50,
    top_n: int = 20,
    rerank: bool = True,
    reranker_model: str = "rerank-2.5",
    **filters,
) -> "HybridSearchResult":
    query = query.strip()
    if not query:
        raise ValueError("[hybrid-search] query is required")

    async def _semantic():
        try:
            return await runSemanticSearch(
                conn, query=query, k=k_semantic, voyage=voyage, **filters,
            )
        except SemanticSearchUnavailableError:
            return None
        except VoyageError as e:
            if e.kind == "auth":
                raise
            return None  # graceful degrade

    fts_hits, sem_result = await asyncio.gather(
        fts_search(query=query, limit=k_fts, **filters),
        _semantic(),
    )
    degraded_to_fts_only = sem_result is None

    merged = {}
    for i, f in enumerate(fts_hits):
        merged[f.summary_id] = HybridHit(
            summary_id=f.summary_id,
            ..., from_fts=True, from_semantic=False,
            semantic_distance=None, fts_rank=i,
        )
    if sem_result is not None:
        for s in sem_result.hits:
            if s.summary_id in merged:
                merged[s.summary_id].from_semantic = True
                merged[s.summary_id].semantic_distance = s.distance
            else:
                merged[s.summary_id] = HybridHit(
                    summary_id=s.summary_id, ..., from_fts=False, from_semantic=True,
                    semantic_distance=s.distance, fts_rank=None,
                )

    candidates = list(merged.values())
    if not candidates:
        return HybridSearchResult(hits=[], candidate_count=0, ...)

    rerank_packed, rerank_pack_truncated, voyage_tokens = candidates, False, 0
    degraded_skipped_rerank = False
    if rerank:
        budget = math.floor(MAX_TOKENS_PER_RERANK_CALL * 0.85)
        query_est = math.ceil(len(query) / 4)
        cumulative = query_est
        packed = []
        for c in candidates:
            cand_tokens = c.token_count or math.ceil(len(c.content) / 4)
            if cand_tokens > budget:
                rerank_pack_truncated = True
                continue  # Wave-11: skip individually-oversized
            if cumulative + cand_tokens > budget:
                rerank_pack_truncated = True
                break
            packed.append(c)
            cumulative += cand_tokens
        rerank_packed = packed

    if rerank and rerank_packed:
        try:
            resp = await voyage.rerank(
                query,
                [(c.summary_id, c.content) for c in rerank_packed],
                model=reranker_model,
                top_k=min(top_n, len(rerank_packed)),
            )
            voyage_tokens += resp.total_tokens
            by_id = {c.summary_id: c for c in rerank_packed}
            final = [
                replace(by_id[r.id], score=r.score)
                for r in resp.results if r.id in by_id
            ]
            return HybridSearchResult(
                hits=final,
                candidate_count=len(candidates),
                voyage_tokens_consumed=voyage_tokens,
                degraded_to_fts_only=degraded_to_fts_only,
                degraded_skipped_rerank=False,
                rerank_pack_truncated=rerank_pack_truncated,
                rerank_packed_count=len(rerank_packed),
                ...,
            )
        except VoyageError as e:
            if e.kind == "auth":
                raise
            degraded_skipped_rerank = True
    elif rerank and not rerank_packed:
        degraded_skipped_rerank = True

    # RRF fallback
    RRF_K = 60
    sem_idx_by_id = {h.summary_id: i for i, h in enumerate(sem_result.hits)} if sem_result else {}
    for c in candidates:
        score = 0.0
        if c.fts_rank is not None:
            score += 1 / (RRF_K + c.fts_rank)
        if c.from_semantic and c.summary_id in sem_idx_by_id:
            score += 1 / (RRF_K + sem_idx_by_id[c.summary_id])
        c.score = score
    candidates.sort(key=lambda c: c.score, reverse=True)
    return HybridSearchResult(
        hits=candidates[:top_n],
        candidate_count=len(candidates),
        voyage_tokens_consumed=voyage_tokens,
        degraded_to_fts_only=degraded_to_fts_only,
        degraded_skipped_rerank=degraded_skipped_rerank,
        ...,
    )
```

---

## Semantic search

Source: `/Volumes/LEXAR/Claude/lossless-claw/src/embeddings/semantic-search.ts:1-419`.

### Pipeline

1. **Validate environment** (`semantic-search.ts:193-208`): `vec0Version != None`, active model registered (`getActiveEmbeddingModel`), `embeddingsTableExists`. Throws `SemanticSearchUnavailableError` (caller can catch and degrade to FTS-only).
2. **Embed query** (`semantic-search.ts:211-246`): `embedTexts({texts: [query], inputType: "query", outputDimension: active.dim, ...})`. The `outputDimension` MUST match the active profile (Wave-11 fix).
   - OR: caller provides `queryVector` to skip the embed (used by hybrid path that may want to share a vector across semantic+rerank; also for tests).
3. **Over-fetch on filtered KNN** (`semantic-search.ts:257-269`): if any filter is active (`since`, `before`, `conversationIds`, `sessionKeys`, `summaryKinds`), request `k = min(500, max(userK, userK * 10))` from vec0. Reason: KNN doesn't know about post-filter; without over-fetch, top-K globally may all live OUTSIDE the filter window → 0 results despite hundreds of matches.
4. **KNN search** (`semantic-search.ts:270-276` → `store.ts:searchSimilar`): `SELECT embedded_id, embedded_kind, distance FROM lcm_embeddings_<slug> WHERE embedding MATCH ? AND k = ? AND suppressed = 0 AND embedded_kind IN (...) ORDER BY distance`.
5. **JOIN back to summaries** with filter clauses (`semantic-search.ts:320-380`):
   - Dynamic WHERE clauses for `excludeSuppressed` (defense-in-depth — vec0 metadata might race with the trigger), `sessionKeys`, `conversationIds`, `since`/`before` (via `COALESCE(s.latest_at, s.created_at)` — Wave-1 finding: semantic and FTS arms had divergent time semantics), `summaryKinds`.
6. **Trim to user's k** (`semantic-search.ts:388-411`) after filtering; compute `cosineSimilarity` from L2 distance:
   `cos = max(-1, min(1, 1 - (distance² / 2)))` (unit-vector assumption — Voyage embeddings are L2-normalized).

### Cosine similarity bands (from `semantic-search.ts:122-128`)

| Cosine | Band | Distance (L2) |
|---:|---|---:|
| ≥ 0.65 | high | ~0.84 |
| ≥ 0.5 | medium | ~1.00 |
| ≥ 0.35 | low | ~1.14 |
| < 0.35 | noise | > 1.14 |

---

## Backfill cron

Source: `/Volumes/LEXAR/Claude/lossless-claw/src/embeddings/backfill.ts:1-637`.

### Cadence

- Per-tick worker loop call. TS dispatcher invokes every ~10s (per `worker-loop.ts` comment at `:14` — actual interval set when wiring the job).
- `maxRequestsPerSecond = 0.5` default — one Voyage request every 2s. Worker lock single-flight makes RPS per-process accurate. (Voyage tier-1 limit is 300 RPM = 5 RPS — generous margin.)

### Per-tick limits

- **`perTickLimit`** default 200 documents. At 80K tokens/batch × 2s/batch = ~7–15 minutes/tick depending on doc length. Bump for first-run backfill (no contention); lower for steady state (yield to other work).
- **`minTokenCount`** default 1 (skip empty stubs).
- **`maxTokenCount`** default 27_000 (`MAX_TOKENS_PER_EMBED_DOC`).
- **`maxBatchTokens`** default 80_000.
- **`voyageMaxRetries`** default 1 (lower than client default 3 — caps worst-case batch wall-time below 90s lock TTL).
- **`voyageTimeoutMs`** default 30_000 (lower than client default 60s — same reason).

### Candidate SELECT (`backfill.ts:472-522`)

```sql
SELECT s.summary_id, s.content, s.token_count
  FROM summaries s
  WHERE s.suppressed_at IS NULL
    AND s.token_count BETWEEN ? AND ?    -- minTokenCount, maxTokenCount
    AND s.kind = 'leaf'
    AND s.summary_id NOT IN (...)        -- failed-this-tick blocklist (dynamic IN list)
    AND NOT EXISTS (
        SELECT 1 FROM lcm_embedding_meta m
          WHERE m.embedded_id = s.summary_id
            AND m.embedded_kind = ?
            AND m.embedding_model = ?
            AND m.archived = 0
    )
  ORDER BY s.summary_id DESC               -- newest-first (newer content queryable faster)
  LIMIT ?
```

### Tick algorithm (`backfill.ts:219-436`)

```
1. Validate: vec0 loaded; embeddings table exists for model
2. Acquire worker lock (skipLock for tests). If not acquired → return early
3. While processed < perTickLimit:
   a. SELECT next batch (cap 64 per SELECT)
   b. Partition: over-cap docs → result.skippedOverCap; queryable → packBatches
   c. For each batch:
      - Rate-limit pacing (asyncio.sleep(1/maxRps))
      - Heartbeat lock; if stolen → abort cleanly
      - Call Voyage embed (OUTSIDE any DB transaction)
        * Auth error → re-throw (fatal)
        * Other VoyageError → record per-doc skipped; continue
      - Heartbeat AGAIN post-embed (Wave-12 defense: 60s Retry-After + 30s timeout = 90s = TTL)
        * Stolen → abort writes for this batch, mark lock_stolen_mid_embed
      - writeBatch (per-row SAVEPOINT; vec0 + meta atomically per row):
        * BEGIN IMMEDIATE
        * for each (doc, vec): SAVEPOINT, recordEmbedding, RELEASE (or ROLLBACK TO on per-row error)
        * COMMIT (or ROLLBACK on tx-level error)
4. Finally: release lock (always — even on auth re-throw)
```

### Result shape (`backfill.ts:180-195`)

```python
@dataclass
class BackfillResult:
    embedded_count: int            # vec0+meta inserts succeeded
    skipped_over_cap: int          # docs > maxTokenCount (no quota spent)
    skipped: list[BackfillSkippedDoc]  # voyage_400, voyage_other, lock_stolen_mid_embed, over_cap
    per_tick_limit_reached: bool   # caller schedules next tick
    lock_not_acquired: bool        # caller skips this tick
    voyage_tokens_consumed: int    # from API usage.total_tokens
    duration_ms: int
```

### `countPendingDocs` (`backfill.ts:439-468`)

Same SELECT shape with `COUNT(*)` — used by `/lcm health` to surface backlog.

---

## Open architecture decisions

These need ADRs before the port lands. Numbers are placeholders pending ADR allocation.

### ADR-?: HTTP client — `httpx` vs `aiohttp` vs `requests`

**Recommend `httpx`** (per Spike 004 = PASS):
- Async-first (`AsyncClient` is first-class, not retrofitted).
- Drop-in API parity with `fetch`: `httpx.Timeout(total, connect=)`, `resp.status_code`, `resp.headers.get`, `resp.json()`, `resp.text`.
- `respx` mock router exists for unit tests (port LCM's 24 fetch-mock fixtures via `respx`).
- Already used elsewhere in Hermes (`utils.py:300`, `hermes_constants.py:305`, `cli.py:659`).
- Pin: `httpx>=0.27,<0.30` (stable `json=` body kwarg, `aclose()`, Timeout semantics).

### ADR-?: Worker loop dispatcher — `asyncio.create_task` vs `apscheduler` vs cron

**Recommend `asyncio.create_task`** (per "Worker loop" section above):
- Matches TS pattern verbatim (one loop task per job; interior `await asyncio.sleep`).
- No external dep. apscheduler adds machinery we don't need (cron expressions, job stores, multi-process — LCM is single-process per gateway/worker).
- Hermes already uses this pattern in 5+ plugin adapters.

### ADR-?: Where does `VOYAGE_API_KEY` come from?

LCM resolution order (`client.ts:128-129`, `:411`):
1. Explicit `apiKey` option (tests, override).
2. `process.env.VOYAGE_API_KEY`.
3. (Documented only) `~/.openclaw/credentials/voyage-api-key`.

Hermes options:
- **(A) Env var only** — simplest, matches Hermes conventions (`.env.example` already has 700+ entries).
- **(B) Env var + `$HERMES_HOME/credentials/voyage-api-key`** — file-based fallback for ops that prefer not to expose env vars in process listings.
- **(C) Hermes keyring integration** — leverage existing OS keyring if present.

**Recommend (A) for v1**, layer in (B) post-launch. Document hard in `.env.example`.

### ADR-?: Voyage outage degrade behavior

When Voyage is down:
- **Semantic search:** `runSemanticSearch` throws `SemanticSearchUnavailableError` on vec0 issues; non-auth `VoyageError` in `runHybridSearch` degrades to FTS-only with `degradedToFtsOnly=true`. **Auth errors propagate** — operator sees actionable "set VOYAGE_API_KEY".
- **Rerank:** non-auth `VoyageError` falls through to RRF with `degradedSkippedRerank=true`. Auth propagates.
- **Backfill:** non-auth `VoyageError` records per-doc skipped, continues; auth re-throws, lock releases via finally. Next tick re-attempts (Voyage may have recovered).

This contract is well-tested in TS (see `hybrid-search.test.ts:155-249`). Preserve exactly.

### ADR-?: vec0 vector binding — JSON vs raw bytes

Per Spike 001, raw bytes (`sqlite_vec.serialize_float32`) is ~2.3× faster on insert than JSON. **Recommend bytes** for backfill hot path. JSON is fine for query path (KNN MATCH supports both forms).

### ADR-?: Python `float` (double) vs `Float32Array` (single) parity

For byte-identical vec0 INSERTs across LCM/Hermes on the same content, cast embeddings to `numpy.float32` at the storage boundary. Add a fixture test that LCM (TS) and Hermes (Python) produce vectors agreeing to ≤ 1e-6 relative error on a fixed corpus.

---

## Test inventory

Port these test files (~2,759 TS LOC → ~2,500 Python LOC). Source paths under `/Volumes/LEXAR/Claude/lossless-claw/test/`.

### `voyage-client.test.ts` (561 LOC)

**Constants:**
- `MAX_TOKENS_PER_EMBED_BATCH == 80_000`
- `MAX_TOKENS_PER_EMBED_DOC == 27_000`
- `MAX_TOKENS_PER_RERANK_CALL == 600_000`

**Embed happy path:**
- POST to `/embeddings` with `input_type`, parsed `Float32` vectors.
- Omits `input_type` when null (Voyage default).
- Re-orders out-of-order responses by `index` field.
- Empty input → no HTTP call.
- Sends `truncation: false` always.

**Embed error handling:**
- `VoyageError(auth)` on 401 — no retry.
- `VoyageError(bad_request)` on 400 — no retry, `summarizeBody` applied.
- `VoyageError(rate_limit)` on persistent 429 — exposes `Retry-After` in ms.
- 429 with `Retry-After > 60s` — throws immediately (Wave-2 LOCK_BUDGET fix).
- 429 with `Retry-After ≤ 60s` — sleeps server hint, retries.
- 5xx retries then succeeds.
- 5xx exhausts retries → `VoyageError(server_error)`.
- Network error (`fetch` throws) → `VoyageError(network)`.
- No API key → `VoyageError(auth)`.
- Response length mismatch → `unexpected`.
- Dim mismatch within batch → `unexpected`.

**Rerank:**
- POST to `/rerank` with `documents` + `topK`; joins ids back.
- `top_k` defaults to `candidates.length`.
- Empty candidates → no HTTP call.
- Invalid index in response → `unexpected`.

**Env:**
- Uses `process.env.VOYAGE_API_KEY` when no opt provided.

Port all 24 fixtures via `respx`.

### `embeddings-store.test.ts` (525 LOC)

- `embeddingsTableName` sluggification cases.
- `candidateVec0Paths` includes env var / plugin-local / openclaw dirs (Python: simpler — drop most of this).
- `tryLoadSqliteVec` returns false on missing.
- `vec0Version` returns None when not loaded.
- `registerEmbeddingProfile`: insert idempotent on same dim; throws on mismatch; rejects bad name; rejects bad dim.
- **`describe.skipIf(!VEC0_AVAILABLE)` block** (vec0-dependent):
  - Loads vec0 from dev path; `vec0Version` returns string.
  - `ensureEmbeddingsTable` creates virtual table; idempotent.
  - `recordEmbedding` inserts vec0 + meta; `isEmbedded` reflects.
  - `recordEmbedding` rejects wrong dim.
  - `recordEmbedding` requires registered profile.
  - `searchSimilar` finds nearest; excludes suppressed by default.
  - `searchSimilar` includes suppressed when `excludeSuppressed=false`.
  - `searchSimilar` filters by `embeddedKind`.
  - `markEmbeddingSuppressed` flips visibility.
  - `replaceEmbedding` removes prior + inserts new.
  - `deleteEmbedding` removes from both vec0 and meta.
  - Two different models → two independent vec0 tables.

### `embeddings-backfill.test.ts` (474 LOC)

- Embeds all pending leaves; result count matches; `isEmbedded` true after.
- Skips suppressed leaves (no Voyage call).
- Skips already-embedded leaves on subsequent ticks (idempotent).
- Over-cap leaves skipped + reported.
- `perTickLimit` caps work; `perTickLimitReached=true`.
- Voyage 400 records skipped doc; tick continues.
- Voyage 401 is fatal — re-thrown.
- Voyage 500 on first batch — marks skipped, continues other batches.
- Lock contention → `lockNotAcquired=true`.
- Releases lock on success.
- Releases lock on auth-re-throw too (try/finally).
- `packBatches` respects `maxBatchTokens`.
- `countPendingDocs` accurate.

### `semantic-search.test.ts` (355 LOC)

- `getActiveEmbeddingModel`: null when none; most-recent active wins; excludes archived.
- `runSemanticSearch`: throws when vec0 unavailable / no profile / dim mismatch / empty query.
- Returns ranked hits joined with summary content.
- Excludes suppressed by default; includes on `excludeSuppressed=false`.
- `sessionKeys`, `conversationIds`, `since`/`before`, `summaryKinds` filters work.
- Calls Voyage when `queryVector` not provided; `voyageTokensConsumed` reflects.
- Over-fetch on filtered KNN (Wave: filtered survivors aren't crowded out).
- `cosineSimilarity` exposed on each hit.

### `hybrid-search.test.ts` (313 LOC, mostly `skipIf(!VEC0_AVAILABLE)`)

- Merges FTS + semantic; reranks; returns top-N.
- Dedupes overlap.
- Vec0 not loaded → `degradedToFtsOnly=true`, FTS-only.
- Rerank Voyage 500 → RRF fallback, `degradedSkippedRerank=true`.
- Rerank Voyage 401 → re-thrown.
- `rerank=false` → RRF mode (no Voyage rerank call).
- Empty query rejected.
- Both arms empty → empty result.

### `worker-loop.test.ts` (261 LOC)

- `start()` returns true first, false on already-running.
- `stop()` before start is no-op.
- Schedules jobs at interval; two jobs don't interfere.
- Overlapping ticks skipped (not queued).
- Thrown error captured in `onJobComplete`; loop continues.
- Graceful `stop()` waits for in-flight.
- Returns false on graceful timeout.
- `runOnce` returns result.
- `runOnce` throws on unknown / already-in-flight.
- Rejects duplicate kinds / invalid intervals.

### `worker-lock.test.ts` + `lcm-worker-lock.test.ts` (150 + 120 LOC)

- First acquire OK; second from different worker blocked.
- Release frees lock.
- Release with wrong workerId no-ops.
- Acquire requires non-empty workerId.
- TTL=0 → immediately stale, GC'd on next acquire.
- Non-expired blocks.
- Heartbeat from holder extends `expires_at`.
- Heartbeat from non-holder fails.
- Heartbeat after stale GC + reacquire by different worker fails for old.
- `jobSessionKey`, `jobMetadata` round-trip via `lockInfo`.
- `lockInfo` null when none.
- `generateWorkerId` has role prefix + pid + nonce.
- Different `job_kind`s don't conflict.

### Integration tests (port to `tests/integration/`)

- Live Voyage round-trip — gated on `VOYAGE_API_KEY` secret in CI nightly workflow. Promote `/tmp/voyage-spike/roundtrip.py` from Spike 004. Assert dim=1024, L2 norm ≈ 1.0 ± 0.001, embed p99 < 5s, rerank p99 < 3s.

---

## Remaining 5% risk

1. **`Float32Array` precision parity** — TS silently single-precision; Python double. Mitigation: cast to `numpy.float32` at storage boundary; fixture test for ≤ 1e-6 relative error agreement (per Spike 004).
2. **`Retry-After` HTTP-date parsing** — Python `email.utils.parsedate_to_datetime` vs TS `Date.parse` may diverge on edge cases. Mitigation: explicit unit tests for both forms; treat unparseable as "no header" (TS behavior).
3. **`respx` vs real Voyage 429 body shape** — not live-probed (would burn quota). Mitigation: capture real 429 in staging, snapshot as fixture.
4. **Concurrent-request connection-pool semantics** — `httpx.AsyncClient` defaults (max_keepalive=20, max_connections=100) may bottleneck under burst. Mitigation: pin explicitly when constructing.
5. **No multi-connection WAL stress test for vec0** — Spike 001 covered single-conn; multi-writer + vec0 not exercised. Mitigation: follow-up spike before production if high-concurrency writes are anticipated.
6. **No persistence/upgrade test** — Spike 001 was in-memory. Validate vec0 schema survives `VACUUM`, `pragma user_version` bump, sqlite-vec upgrade before production (sub-spike).
7. **python.org Python installer not locally validated** — Spike 001 tested Homebrew only. Linux extension-loading also inferred (wheel availability). Mitigation: add `ubuntu-latest` to GH Actions matrix.
8. **`apsw` fallback ABI** — if stdlib path fails in some env, falling back to `apsw` is a rewrite (not drop-in: `enableloadextension` vs `enable_load_extension`, no PEP-249 cursor boilerplate). Isolate connection-open behind a function so swap stays cheap.
9. **Hermes credential resolution** — TS reads from `~/.openclaw/credentials/voyage-api-key`. Hermes has no equivalent. Resolve in ADR before launch.
10. **`asyncio.sleep` precision** — best-effort like `setTimeout`; not load-bearing for retry timing but worth noting under aggressive contention.

---

## Reference paths

- LCM source root: `/Volumes/LEXAR/Claude/lossless-claw/src/`
- LCM tests: `/Volumes/LEXAR/Claude/lossless-claw/test/`
- Spike 001 (sqlite-vec Python): `/Volumes/LEXAR/Claude/lossless-hermes/docs/spike-results/001-sqlite-vec-python.md`
- Spike 004 (Voyage Python client): `/Volumes/LEXAR/Claude/lossless-hermes/docs/spike-results/004-voyage-python-client.md`
- Voyage API docs: https://docs.voyageai.com/reference/embeddings-api, https://docs.voyageai.com/reference/reranker-api, https://docs.voyageai.com/docs/rate-limits
- LCM API key location (TS): `~/.openclaw/credentials/voyage-api-key`
