---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-05] embeddings: port semantic-search.ts → embeddings/semantic_search.py'
labels: 'port, embeddings, retrieval, semantic'
---

## Source (TypeScript)
- File: `lossless-claw/src/embeddings/semantic-search.ts`
- Lines: 419
- Function(s)/class(es): `runSemanticSearch` (193-411 — entrypoint), `getActiveEmbeddingModel` (helper for profile lookup; lives in this file in TS), `SemanticSearchUnavailableError`, cosine-similarity bands constants (122-128), `SemanticHit` shape.

## Target (Python)
- File: `src/lossless_hermes/embeddings/semantic_search.py`
- Estimated LOC: ~420

## Dependencies
- Depends on: #05-01 (Voyage client for query embedding), #05-03 (vec0 store `search_similar`), Epic 01 (summaries table for the JOIN).
- Blocks: #05-09 (hybrid search composes the semantic arm via `runSemanticSearch`).

## Acceptance criteria

- [ ] **`run_semantic_search(conn, *, query, k=20, voyage, model_name=None, query_vector=None, input_type="query", exclude_suppressed=True, **filters) -> SemanticSearchResult`** (port of `semantic-search.ts:193-411`).
- [ ] **Pipeline (verbatim from TS, lines 193-411):**
  1. **Validate environment** (lines 193-208):
     - `vec0_version(conn)` is not None.
     - `get_active_embedding_model(conn) -> EmbeddingProfile | None` returns a registered profile.
     - `embeddings_table_exists(conn, profile.model_name)`.
     - Otherwise → raise `SemanticSearchUnavailableError` (caller can catch and degrade to FTS-only — see #05-09 + #05-10).
     - `query.strip()` is non-empty (raise `ValueError` on empty).
  2. **Embed query** (lines 211-246):
     - If `query_vector is not None`, skip the Voyage call (test path / hybrid arm reuse).
     - Otherwise: `await voyage.embed([query], model=voyage_model, input_type="query", output_dimension=profile.dim)`. The `output_dimension=profile.dim` is **Wave-11 fix** — without it, vec0 columns with non-default dim would receive mismatched-length vectors.
     - Track `voyage_tokens_consumed` from `EmbedResult.total_tokens`.
  3. **Over-fetch on filtered KNN** (lines 257-269): if any filter is active (`since`, `before`, `conversation_ids`, `session_keys`, `summary_kinds`), request `k_request = min(500, max(k, k * 10))` from vec0. Reason (documented in source comments): KNN doesn't know about post-filter; without over-fetch, top-K globally may all live OUTSIDE the filter window → 0 results despite hundreds of matches.
  4. **KNN search** (line 270-276): call `store.search_similar(conn, profile.model_name, query_vector, k=k_request, exclude_suppressed=exclude_suppressed, embedded_kind=["summary"])`. Returns `(embedded_id, embedded_kind, distance)` rows.
  5. **JOIN back to summaries** (lines 320-380, dynamic WHERE):
     - `excludeSuppressed`: `AND s.suppressed_at IS NULL` (defense-in-depth — vec0 metadata might race with the trigger).
     - `session_keys`: `AND s.session_key IN (...)`.
     - `conversation_ids`: `AND s.conversation_id IN (...)`.
     - `since` / `before`: `AND COALESCE(s.latest_at, s.created_at) >= ?` / `<= ?` (Wave-1 finding — semantic and FTS arms had divergent time semantics; `COALESCE(latest_at, created_at)` aligns them).
     - `summary_kinds`: `AND s.kind IN (...)`.
  6. **Trim to user's k** (lines 388-411) AFTER filtering. Compute `cosine_similarity = max(-1, min(1, 1 - (distance ** 2) / 2))` from L2 distance (Voyage embeddings are L2-unit-normalized; this is the algebraic identity).
- [ ] **Cosine similarity bands** (constants from `semantic-search.ts:122-128`):
  ```python
  COSINE_BAND_HIGH = 0.65    # L2 ~0.84
  COSINE_BAND_MEDIUM = 0.50  # L2 ~1.00
  COSINE_BAND_LOW = 0.35     # L2 ~1.14
  # below COSINE_BAND_LOW → "noise"
  ```
  Each `SemanticHit` exposes `cosine_similarity: float` and `band: Literal["high", "medium", "low", "noise"]`.
- [ ] **`SemanticHit` dataclass** with: `summary_id`, `content`, `distance` (raw L2), `cosine_similarity`, `band`, `session_key`, `conversation_id`, `created_at`, `latest_at`, `kind`, `token_count`.
- [ ] **`SemanticSearchResult` dataclass** with: `hits: list[SemanticHit]`, `voyage_tokens_consumed: int`, `model: str`.
- [ ] **`SemanticSearchUnavailableError`** (Exception subclass) — thrown when vec0 unavailable / no active profile / dim mismatch. Caller (hybrid search, `lcm_grep`) catches and degrades to FTS-only.
- [ ] **`get_active_embedding_model(conn) -> EmbeddingProfile | None`**: query `lcm_embedding_profile WHERE active = 1 ORDER BY registered_at DESC LIMIT 1`. Used by both `runSemanticSearch` and `/lcm health`.
- [ ] **§0 invariant:** the `voyage.embed` call is OUTSIDE any DB transaction. Add `assert_no_open_tx(conn)` before the await.
- [ ] **`VoyageError` propagation:** `kind="auth"` re-thrown (operator surfaces "set VOYAGE_API_KEY"); other kinds let the hybrid layer decide degradation policy (re-throw here, catch in `runHybridSearch`).
- [ ] `mypy --strict` and `ty check` pass.
- [ ] All 355 LOC of `test/semantic-search.test.ts` ported to `tests/embeddings/test_semantic_search.py`.

## Tests (`tests/embeddings/test_semantic_search.py`)

Cases from `test/semantic-search.test.ts` (355 LOC):

- `get_active_embedding_model`: null when none; most-recent active wins; excludes archived.
- `run_semantic_search` raises `SemanticSearchUnavailableError` when vec0 unavailable.
- Raises `SemanticSearchUnavailableError` when no profile registered.
- Raises on dim mismatch (mock profile dim=1024 but vec0 table is 512).
- Raises `ValueError` on empty query.
- Returns ranked hits joined with summary content.
- Excludes suppressed by default; includes on `exclude_suppressed=False`.
- `session_keys` filter applies (return only matching session).
- `conversation_ids` filter applies.
- `since` / `before` filters apply via `COALESCE(latest_at, created_at)`.
- `summary_kinds` filter applies.
- Calls Voyage when `query_vector=None`; `voyage_tokens_consumed` reflects the response.
- Skips Voyage when `query_vector` is supplied; `voyage_tokens_consumed=0`.
- **Over-fetch:** filtered query with `k=5` requests `k=50` from vec0; filtered survivors aren't crowded out (stage a corpus where the top-50 globally include only 5 in the filter window).
- `cosine_similarity` exposed on each hit; bands populated correctly for known distances.
- Wave-11 dim alignment: when `profile.dim=512`, the Voyage call passes `output_dimension=512`.

## Estimated effort
4 hours

## Confidence
95% — the porting guide §"Semantic search" (lines 1073-1097) gives the full pipeline. The cosine-from-L2 identity is mathematically exact for unit vectors. Residual 5%:
- The over-fetch heuristic (10× user-k capped at 500) is empirically tuned in TS — no analytical justification. Port verbatim; revisit if Eva's eval shows recall regressions.
- `COALESCE(latest_at, created_at)` matters when `latest_at` is unset on legacy rows — Epic 01's migration should backfill, but the COALESCE is defense-in-depth.

## Files to read before starting
- `docs/porting-guides/embeddings.md` §"Semantic search" (lines 1073-1097)
- TS source: `lossless-claw/src/embeddings/semantic-search.ts` (entire — 419 LOC)
- TS tests: `lossless-claw/test/semantic-search.test.ts` (entire — 355 LOC)
