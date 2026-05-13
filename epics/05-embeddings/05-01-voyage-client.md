---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-05] voyage: port client.ts → voyage/client.py'
labels: 'port, embeddings, voyage'
---

## Source (TypeScript)
- File: `lossless-claw/src/voyage/client.ts`
- Lines: 616 (full file)
- Function(s)/class(es): `embedTexts`, `rerankCandidates`, `postWithRetry` (lines 421-558 — load-bearing retry loop), `parseRetryAfterMs`, `summarizeBody`, `safeReadBody`, `VoyageError` class, plus the 7 hard-coded constants block (lines 80-95).

The retry loop in `postWithRetry` is the most load-bearing block in this epic — it encodes hard-won lessons from 11 LCM review waves. Port branch-for-branch.

## Target (Python)
- File: `src/lossless_hermes/voyage/client.py`
- Estimated LOC: ~650 (matches TS LOC after async/dataclass overhead)

## Dependencies
- Depends on: Epic 00 (`pyproject.toml` pin `httpx[socks]==0.28.1` per ADR-019, `pydantic==2.12.5` for typed response shapes if used; `tenacity==9.1.4` available but NOT used in the inner retry loop — see ADR-019 §Rationale).
- Blocks: #05-02 (credentials resolver), #05-07 (backfill), #05-08 (semantic search), #05-09 (hybrid search).

## Acceptance criteria

- [ ] All 24 fixtures from `test/voyage-client.test.ts` (561 LOC) ported to `tests/voyage/test_client.py` via `respx` (httpx mock router). Fixture-for-fixture; same assertions.
- [ ] `VoyageClient.embed(texts, *, model, input_type, output_dimension)` returns `EmbedResult(vectors, total_tokens, model)`. Empty `texts` returns immediately without HTTP call. `input_type=None` omits the field from the body. `output_dimension=None` omits the field; `output_dimension=512` forwards verbatim (Wave-11 finding — required for non-default vec0 dim alignment).
- [ ] `VoyageClient.rerank(query, candidates, *, model, top_k)` returns `RerankResult(results, total_tokens, model)`. `top_k=None` defaults to `len(candidates)`. Empty candidates returns immediately without HTTP call. Results joined back to caller-supplied `id`s by `index` field. Defensive sort by score descending (per `client.ts:380-400`).
- [ ] **All 7 constants match TS verbatim:** `MAX_TOKENS_PER_EMBED_BATCH=80_000`, `MAX_TOKENS_PER_EMBED_DOC=27_000`, `MAX_TOKENS_PER_RERANK_CALL=600_000`, `DEFAULT_MAX_RETRIES=3`, `BACKOFF_BASE_MS=500`, `BACKOFF_CAP_MS=25_000`, `DEFAULT_TIMEOUT_MS=60_000` (or `_S=60.0`), `RETRY_AFTER_HARD_CAP_MS=5*60*1000`, `LOCK_BUDGET_AWARE_RETRY_MS=60_000`.
- [ ] **Retry loop (`_post_with_retry`)** branch-for-branch matches `postWithRetry` at `client.ts:421-558`:
  - 1 initial attempt + 3 retries = 4 attempts default.
  - Exponential backoff `min(BACKOFF_BASE_MS * 2 ** attempt, BACKOFF_CAP_MS)`. Waits before retries 1/2/3 are 500/1000/2000 ms.
  - Per-attempt timeout via `httpx.Timeout(total=timeout_s, connect=10.0)`.
  - 401/403 → `VoyageError(kind="auth")`, no retry.
  - 400 → `VoyageError(kind="bad_request")`, no retry, `summarize_body` applied.
  - 429 with `Retry-After ≤ LOCK_BUDGET_AWARE_RETRY_MS=60s` → sleep server hint, retry.
  - **Wave-2 fix:** 429 with `Retry-After > LOCK_BUDGET_AWARE_RETRY_MS` → throw immediately so caller releases worker lock (carry inline `# LCM Wave-2 (2025-12-XX): ...` comment per ADR-029).
  - 429 without `Retry-After` → exponential backoff.
  - 5xx → retry with exponential backoff. Exhausted → `VoyageError(kind="server_error")`.
  - `httpx.TimeoutException` / `httpx.NetworkError` → retry, exhausted → `VoyageError(kind="network")`.
  - Other 4xx (not 400/401/403/429) → `VoyageError(kind="bad_request")`, no retry.
- [ ] **Wave-1 fix:** `BACKOFF_CAP_MS=25_000` (not 30_000). Carry inline `# LCM Wave-1 (2025-11-XX): 25s cap leaves 5s margin under WORKER_LOCK_TTL_MS=90s` per ADR-029.
- [ ] **PII suppression (Waves 4+7):** `summarize_body(body)` returns `"input echoed in error body — suppressed for privacy"` when body contains `'"input"'`, `'"texts"'`, or `'"documents"'`; otherwise `body[:200]`. Applied to **every** non-2xx body before attaching to `VoyageError.response_body` AND to error messages. `safe_read_body(resp)` clips to 800 chars with `…(truncated)` suffix before `summarize_body` runs.
- [ ] **`parse_retry_after_ms(header)`:** numeric-seconds form → `min(value * 1000, RETRY_AFTER_HARD_CAP_MS)`; HTTP-date form via `email.utils.parsedate_to_datetime` → `min(delta_ms, RETRY_AFTER_HARD_CAP_MS)` if positive; unparseable → `None` (caller falls back to backoff). Match TS `parseRetryAfterMs` behavior.
- [ ] **Response parsing (embed, `client.ts:266-313`):** `json["data"]` must be a list of `len(texts)`. Each item has `embedding: list[float]` (matching dim) and `index: int` in `[0, len)`. **Re-order by `index` field** before returning (TS does this; we must too). Dim mismatch within batch → `VoyageError(kind="unexpected")`. Missing/malformed → `unexpected`.
- [ ] **Response parsing (rerank, `client.ts:349-385`):** `json["data"]` is a list; each item has `index: int` (valid range) and `relevance_score: float`. Join `id` from `candidates[item.index]`. Defensive sort by score descending. Missing fields → `unexpected`.
- [ ] **`truncation: false`** is sent verbatim in every embed and rerank body (lossless invariant — `client.ts:240, :337`).
- [ ] **API key resolution in the client constructor** uses only `api_key` opt-or-`os.environ["VOYAGE_API_KEY"]`. Empty → `VoyageError(kind="auth")`. The three-tier file/config resolver (#05-02) is upstream of the constructor — pass a resolved key in.
- [ ] **httpx.AsyncClient construction** uses `httpx.Timeout(timeout_s, connect=10.0)` and explicit connection-pool pinning (`max_keepalive_connections=20, max_connections=100`) per ADR-019 §Consequences.
- [ ] `VoyageClient.aclose()` closes the underlying httpx client. Tests verify no leaked connections after a happy-path embed + rerank.
- [ ] `mypy --strict src/lossless_hermes/voyage/client.py` and `ty check src/lossless_hermes/voyage/client.py` both pass with zero errors.
- [ ] PR description cites `lossless-claw@pr-613 HEAD` SHA for `src/voyage/client.ts`.

## Tests (`tests/voyage/test_client.py`)

Port these 24 fixtures from `test/voyage-client.test.ts` (numbering matches spike 004 §"Test fixtures for Phase 2"):

1. Happy path embed (2 inputs, 200 ok, body has `truncation: false`, `Authorization: Bearer ...`).
2. Happy path rerank (defensive sort, top_k defaults to `len(candidates)`).
3. `input_type=None` omits the field.
4. Out-of-order data response (server returns `index: 1` before `index: 0`) — client re-orders.
5. Empty input list → no HTTP call.
6. Empty candidates → no HTTP call (rerank).
7. `output_dimension=512` round-trip — body contains the field; `None` omits it.
8. 400 → `VoyageError(kind="bad_request")` with `summarize_body` applied.
9. 401 → `VoyageError(kind="auth")`, no retry.
10. 403 → `VoyageError(kind="auth")`, no retry.
11. 429 with `Retry-After: 30` (seconds) → retry once after 30s wait, then succeed.
12. 429 with `Retry-After: <HTTP-date>` 10s in future → waits 10s, retries.
13. 429 with `Retry-After: 120` (> LOCK_BUDGET_AWARE_RETRY_MS) → throws immediately (Wave-2).
14. 429 without `Retry-After` → exponential backoff.
15. 500 → retry up to maxRetries → throw `server_error`.
16. Network error (`httpx.ConnectError`) → retry with backoff → throw `network`.
17. Per-attempt timeout (`TimeoutException`) → maps to `network`.
18. Dim mismatch within batch → `unexpected`.
19. Bad `index` in response → `unexpected`.
20. Missing `data` array → `unexpected`.
21. Missing `relevance_score` in rerank item → `unexpected`.
22. `summarize_body` suppresses when body contains `"input"` substring.
23. `summarize_body` clips to 200 chars when not suppressed.
24. `safe_read_body` clips to 800 chars with `…(truncated)` suffix before suppression runs.

Plus the gated integration test (`tests/integration/test_voyage_live.py`) — promote `/tmp/voyage-spike/roundtrip.py` from spike 004. Runs only on nightly workflow with `VOYAGE_API_KEY` secret. Asserts dim=1024, L2 norm ≈ 1.0 ± 0.001, embed p99 < 5s, rerank p99 < 3s.

## Estimated effort
8–10 hours

## Confidence
95% — spike 004 (`docs/spike-results/004-voyage-python-client.md`) verified the full implementation sketch against live Voyage. Every TS primitive has a 1:1 `httpx` equivalent per spike 004 §"Mapping table". Residual 5%:
- Float32 precision parity (TS `Float32Array.from` silently downcasts; Python `float` is double) — handled at the vec0 storage boundary, not in the client (see #05-03).
- Real 429 body shape not live-probed (would burn quota) — synthetic fixture from TS suffices for v0.1; capture real-staging 429 as a fixture post-launch.

## Files to read before starting
- `docs/porting-guides/embeddings.md` §"Voyage client — exhaustive spec" (lines 60-411)
- `docs/spike-results/004-voyage-python-client.md` (entire — 402 LOC; the implementation sketch and mapping table are the literal port skeleton)
- `docs/adr/019-http-client.md` (`httpx[socks]==0.28.1` pin, retry loop hand-rolled not via tenacity)
- `docs/adr/029-wave-fix-provenance.md` (inline `# LCM Wave-N` comment format + table row for Wave-1 and Wave-2 fixes that land in this file)
- TS source: `lossless-claw/src/voyage/client.ts` (entire — 616 LOC; primary reference for the branch-for-branch port)
- TS tests: `lossless-claw/test/voyage-client.test.ts` (entire — 561 LOC; fixture inventory)
