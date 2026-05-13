---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-05] embeddings: port hybrid-search.ts → embeddings/hybrid_search.py'
labels: 'port, embeddings, retrieval, hybrid, rerank'
---

## Source (TypeScript)
- File: `lossless-claw/src/embeddings/hybrid-search.ts`
- Lines: 437
- Function(s)/class(es): `runHybridSearch` (top-level entrypoint), parallel-arm dispatch (200-252), dedupe-by-summary_id (259-298), rerank-pack with Wave-10/11 fixes (326-360), Voyage rerank (362-373), rerank score mapping (380-400), RRF fallback (415-428), `HybridHit` + `HybridSearchResult` shapes.

## Target (Python)
- File: `src/lossless_hermes/embeddings/hybrid_search.py`
- Estimated LOC: ~440

## Dependencies
- Depends on: #05-01 (Voyage rerank), #05-08 (semantic search arm). FTS arm injected by caller (a callable; Epic 06's `lcm_grep` will provide it backed by Epic 01's FTS5 store).
- Blocks: nothing in Epic 05; Epic 06 `lcm_grep --mode hybrid` consumes this.

## Acceptance criteria

- [ ] **`run_hybrid_search(conn, *, query, fts_search, voyage, k_fts=50, k_semantic=50, top_n=20, rerank=True, reranker_model="rerank-2.5", **filters) -> HybridSearchResult`** (port of `hybrid-search.ts:200-428`):
  - `fts_search` is a caller-injected async callable `(query: str, *, limit: int, **filters) -> list[FtsHit]`. Decouples Epic 05 from Epic 06's FTS5 store.
  - `query.strip()` non-empty (raise `ValueError`).
- [ ] **Parallel arms** (port of `hybrid-search.ts:200-252`):
  ```python
  fts_hits, sem_result = await asyncio.gather(
      fts_search(query, limit=k_fts, **filters),
      _semantic_with_degrade(...),
  )
  ```
  - The semantic arm is wrapped: catches `SemanticSearchUnavailableError` → returns `None`; catches `VoyageError(kind != "auth")` → returns `None`; **re-raises `VoyageError(kind="auth")`** (operator-actionable).
  - `degraded_to_fts_only = sem_result is None`.
- [ ] **Dedupe by `summary_id`** (port of `hybrid-search.ts:259-298`):
  - For each FTS hit at index `i`: create `HybridHit(summary_id=..., from_fts=True, fts_rank=i, ...)`.
  - For each semantic hit: if its `summary_id` is in the FTS-keyed dict, merge (set `from_semantic=True`, fill `semantic_distance`); else create new entry with `from_fts=False, from_semantic=True, fts_rank=None`.
- [ ] **Rerank pack (`hybrid-search.ts:326-360`, load-bearing Wave-10/11 fixes):**
  ```python
  budget = math.floor(MAX_TOKENS_PER_RERANK_CALL * 0.85)  # 510_000
  query_est = math.ceil(len(query) / 4)  # rough token estimate
  cumulative = query_est
  packed = []
  rerank_pack_truncated = False
  for c in candidates:
      cand_tokens = c.token_count or math.ceil(len(c.content) / 4)
      if cand_tokens > budget:
          rerank_pack_truncated = True
          continue  # Wave-11 fix: skip individually-oversized; DO NOT break
      if cumulative + cand_tokens > budget:
          rerank_pack_truncated = True
          break
      packed.append(c)
      cumulative += cand_tokens
  ```
  - **Wave-11 fix:** `continue` not `break` when a single doc exceeds budget — earlier TS code broke out of the loop, missing valid smaller candidates further down the list. Carry inline `# LCM Wave-11 (2026-03-XX): skip individually-oversized candidates; do not break (continue packing smaller ones)` per ADR-029 if the underlying Wave-11 fix is documented there. (Note: ADR-029's Wave-N table lists Wave-1, Wave-2, Wave-4, Wave-7, Wave-10, Wave-12, Wave-12-F5 explicitly — Wave-11 is not in the canonical table but the porting guide cites it. Add it to the ADR-029 table when this issue lands.)
  - **Wave-10 fix:** the 85% budget margin (510K instead of 600K). Pure cap on Voyage's 600K-token rerank limit would 400 on edge cases where the query-token-estimate drifts; the 85% leaves headroom.
- [ ] **Voyage rerank-2.5** (port of `hybrid-search.ts:362-373`):
  ```python
  resp = await voyage.rerank(
      query,
      [(c.summary_id, c.content) for c in rerank_packed],
      model=reranker_model,
      top_k=min(top_n, len(rerank_packed)),
  )
  voyage_tokens += resp.total_tokens
  ```
- [ ] **Rerank scoring** (port of `hybrid-search.ts:380-400`): map `resp.results` back to `HybridHit` by `id`; the final `hits` list is sorted by rerank score descending.
- [ ] **RRF (Reciprocal Rank Fusion) fallback** (port of `hybrid-search.ts:415-428`):
  ```python
  RRF_K = 60
  for c in candidates:
      score = 0.0
      if c.fts_rank is not None:
          score += 1 / (RRF_K + c.fts_rank)
      if c.from_semantic:
          sem_idx = sem_idx_by_id.get(c.summary_id)
          if sem_idx is not None:
              score += 1 / (RRF_K + sem_idx)
      c.score = score
  candidates.sort(key=lambda c: c.score, reverse=True)
  return HybridSearchResult(hits=candidates[:top_n], ..., degraded_skipped_rerank=True)
  ```
  - Triggered when: `rerank=False` OR rerank call failed with non-auth `VoyageError` OR `rerank_packed` is empty after packing.
  - Auth errors in the rerank arm are **re-thrown** (not RRF-fallback'd).
- [ ] **`HybridHit` dataclass:** `summary_id`, `content`, `token_count`, `from_fts: bool`, `from_semantic: bool`, `fts_rank: int | None`, `semantic_distance: float | None`, `cosine_similarity: float | None`, `score: float | None` (set by rerank or RRF), plus the joined `summaries` columns.
- [ ] **`HybridSearchResult` dataclass:**
  ```python
  @dataclass
  class HybridSearchResult:
      hits: list[HybridHit]                # top-N
      candidate_count: int                  # pre-rerank/RRF dedupe count
      voyage_tokens_consumed: int           # query embed + rerank tokens
      degraded_to_fts_only: bool            # semantic arm failed
      degraded_skipped_rerank: bool         # RRF used instead
      rerank_pack_truncated: bool           # ≥ 1 candidate dropped from rerank input
      rerank_packed_count: int              # how many made it into the rerank call
      model: str                            # active embedding model
      reranker_model: str | None            # None if rerank skipped
  ```
- [ ] **Empty corpus / both arms empty:** return `HybridSearchResult(hits=[], candidate_count=0, ...)` immediately without rerank call.
- [ ] `mypy --strict` and `ty check` pass.
- [ ] All 313 LOC of `test/hybrid-search.test.ts` ported to `tests/embeddings/test_hybrid_search.py` (most cases gated on `@pytest.mark.skipif(not VEC0_AVAILABLE)`).

## Tests (`tests/embeddings/test_hybrid_search.py`)

Cases from `test/hybrid-search.test.ts` (313 LOC):

- Merges FTS + semantic; reranks; returns top-N (verify hit ordering matches rerank score).
- Dedupes overlap: a `summary_id` present in both arms appears once with `from_fts=True AND from_semantic=True`.
- Vec0 not loaded → `degraded_to_fts_only=True`; FTS-only ranking (RRF over `fts_rank` alone).
- Rerank Voyage 500 → RRF fallback, `degraded_skipped_rerank=True`; result hits still populated.
- Rerank Voyage 401 → re-thrown (NOT fallback to RRF).
- Semantic Voyage 401 → re-thrown (auth surfaces).
- `rerank=False` → RRF mode (no Voyage rerank call; verify via mock call count).
- Empty query rejected (`ValueError`).
- Both arms empty → empty `HybridSearchResult` immediately.
- **Wave-10 budget:** stage candidates whose total tokens exceed 510K; verify packed count truncated and `rerank_pack_truncated=True`.
- **Wave-11 oversized-doc fix:** stage a candidate list `[doc_normal, doc_huge_individually_oversized, doc_normal]`; verify both normal docs are packed and only the huge one is dropped.
- RRF tie-breaking: a hit present in both arms at fts_rank=1 and sem_idx=1 scores higher than one at fts_rank=10 and no semantic match.
- `rerank_packed_count` matches the number of candidates actually sent to Voyage.

## Estimated effort
5 hours

## Confidence
90% — the algorithm is well-documented in the porting guide §"Hybrid search pipeline" (lines 897-1069). The Wave-10/11 fixes are explicit in the TS source. Residual 10%:
- RRF constant `K=60` is the standard literature value; LCM uses it without modification. Don't change.
- The 85% budget margin (510K) is a single hard-coded multiplier — easy to forget on rewrites. Carry an inline comment.
- Token-count heuristic in the pack loop (`math.ceil(len(content) / 4)` when `token_count` is missing) is a heuristic; matches TS but may diverge from Voyage's tokenizer by ~9.5% (per spike 004). Acceptable — overestimates push us to drop earlier rather than 400.

## Files to read before starting
- `docs/porting-guides/embeddings.md` §"Hybrid search pipeline" (lines 897-1069)
- `docs/adr/029-wave-fix-provenance.md` (add Wave-10/Wave-11 rows when this issue lands)
- TS source: `lossless-claw/src/embeddings/hybrid-search.ts` (entire — 437 LOC)
- TS tests: `lossless-claw/test/hybrid-search.test.ts` (entire — 313 LOC)
