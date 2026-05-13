---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-05] embeddings: graceful-degradation contract (4 flags)'
labels: 'port, embeddings, retrieval, degradation'
---

## Source (TypeScript)
- File: `lossless-claw/src/embeddings/hybrid-search.ts` (degraded flags surface, lines ~155-249 + `HybridSearchResult` shape), `lossless-claw/src/embeddings/semantic-search.ts` (`SemanticSearchUnavailableError` and the auth re-throw contract).
- Lines: contract spread across both files; ~80 LOC of contract-defining code total.
- Function(s)/class(es): the contract is enforced across multiple call sites. This issue implements the contract as a cross-cut consistency pass on top of #05-08 and #05-09.

## Target (Python)
- File: cross-cuts `src/lossless_hermes/embeddings/hybrid_search.py`, `src/lossless_hermes/embeddings/semantic_search.py`, and the shared types module (`src/lossless_hermes/embeddings/__init__.py` or `embeddings/types.py`). No new file; this is a verification + integration issue.
- Estimated LOC: ~30 LOC of plumbing on top of #05-08 and #05-09; the bulk is test coverage (~250 LOC of new test cases).

## Dependencies
- Depends on: #05-08 (semantic search), #05-09 (hybrid search). The flags live on `HybridSearchResult` shaped in #05-09, but verifying every code path that sets them is a separate audit pass.
- Blocks: Epic 06 `lcm_grep` (tool surfaces these flags as operator warnings in tool output).

## Acceptance criteria

The contract surfaces **four degraded-result flags** on `HybridSearchResult` (per the porting guide §"Degraded-result surfaces (TS contract — preserve in Python)" lines 918-928):

- [ ] **`degraded_to_fts_only: bool`** — `True` when:
  - `vec0` unavailable (`vec0_version` returns None, or `try_load_sqlite_vec` returned False at init).
  - `getActiveEmbeddingModel` returned None.
  - Semantic arm threw `VoyageError(kind != "auth")` and was caught.
  - Semantic arm threw `SemanticSearchUnavailableError` and was caught.
  - **Caller action:** operator warning; results still useful (FTS ranking via RRF over FTS rank alone).

- [ ] **`degraded_skipped_rerank: bool`** — `True` when:
  - Rerank call threw `VoyageError(kind != "auth")` and was caught.
  - Caller passed `rerank=False`.
  - `rerank_packed` list was empty after the pack loop (no candidates fit the budget — every candidate exceeded the per-doc budget, which is rare but possible).
  - **Caller action:** RRF used; slightly lower precision than reranked path.

- [ ] **`rerank_pack_truncated: bool`** — `True` when:
  - At least one candidate was excluded from the rerank input because of the 510K-token budget (per the pack loop in #05-09).
  - **Caller action:** operator warning; tail candidates excluded from rerank but remain in `candidate_count` for backstop visibility.

- [ ] **`rerank_packed_count: int`** — always set when rerank ran (the count of candidates actually sent to Voyage). 0 when rerank skipped.
  - **Caller action:** diagnostic only; surfaces in `/lcm health` output and in `lcm_grep` tool response (Epic 06).

- [ ] **Auth errors propagate (never silently degrade):** `VoyageError(kind="auth")` in EITHER arm (semantic embed or rerank) raises out of `runHybridSearch`. The wrapping degradation is per `kind != "auth"` only. Tests verify auth re-throws in:
  - Semantic arm with `query_vector=None` (embed call fails 401).
  - Rerank call fails 401 after a successful semantic arm.

- [ ] **Test fixture cube** (~250 LOC, mostly in `tests/embeddings/test_hybrid_search.py` + `tests/embeddings/test_degraded_modes.py` for the cross-cuts):

  | Scenario | `degraded_to_fts_only` | `degraded_skipped_rerank` | `rerank_pack_truncated` | `rerank_packed_count` |
  |---|---|---|---|---|
  | Happy path (vec0+rerank both work) | False | False | False | candidates fit |
  | vec0 not loaded | True | True (no candidates to rerank w/o semantic) | False | 0 |
  | Semantic Voyage 500 | True | True | False | 0 |
  | Semantic Voyage 401 | (re-raises) | — | — | — |
  | Rerank Voyage 500 | False | True (RRF used) | False | 0 |
  | Rerank Voyage 401 | (re-raises) | — | — | — |
  | rerank=False explicit | False | True | False | 0 |
  | 1000 candidates, total tokens > 510K | False | False | True | packed < 1000 |
  | 1 single candidate > 510K | False | False | True | 0 (then `degraded_skipped_rerank=True` because packed empty) |
  | Both arms empty | False | False | False | 0 |
  | Mixed: vec0 loaded but Voyage embed 500 + rerank not reached | True | True | False | 0 |
  | Mixed: semantic OK, rerank pack truncates but Voyage rerank 500 | False | True | True | 0 (RRF over the full candidate set, not just packed) |

- [ ] **Contract documentation:** the `HybridSearchResult` dataclass has a module-level docstring summarizing the four flags + caller actions. A `runHybridSearch` docstring section enumerates which scenarios trigger which flags.

- [ ] **Logger output:** each degradation path logs a single `INFO`-level line with the scenario (`"[hybrid] degraded to FTS-only: semantic arm Voyage 500"`). NOT `WARNING` (the system is functioning; this is informational). Operators observing high degradation rates can grep these.

- [ ] **`lcm_grep` integration hook (Epic 06 cross-ref):** the tool reads these flags and surfaces them as operator-visible warnings in the response. This issue does NOT touch Epic 06 — it just guarantees the flags are correct at the call site.

- [ ] `mypy --strict` and `ty check` pass.

## Tests (`tests/embeddings/test_degraded_modes.py`)

The 11 scenarios in the table above, each as a discrete test. Cases that exist in #05-09's test file (`test_hybrid_search.py`) can be cross-referenced rather than duplicated — this file focuses on the matrix and on the auth re-throw boundary.

Plus:
- Verify `INFO` log lines fire on each degradation path (use `caplog` fixture).
- Verify `HybridSearchResult` docstring lists all four flags (lint check or doctest).

## Estimated effort
2 hours

## Confidence
95% — the contract is fully enumerated in the porting guide §"Degraded-result surfaces" (lines 918-928). Most of the implementation lands in #05-08 and #05-09; this issue is the consistency audit + the matrix test. Residual 5%:
- The `rerank_packed empty → degraded_skipped_rerank=True` edge case (when the single candidate exceeds the budget) is implicit in the TS but not explicit. The porting guide §"Rerank packing" (lines 326-360) implies it; the test fixture cube above pins it.

## Files to read before starting
- `docs/porting-guides/embeddings.md` §"Degraded-result surfaces (TS contract — preserve in Python)" (lines 918-928)
- `docs/porting-guides/embeddings.md` §"ADR-?: Voyage outage degrade behavior" (lines 1215-1222)
- TS source: `lossless-claw/src/embeddings/hybrid-search.ts:155-249` (the auth re-throw + degradation boundary)
- TS tests: `lossless-claw/test/hybrid-search.test.ts:155-249` (TS-side test coverage of the contract)
