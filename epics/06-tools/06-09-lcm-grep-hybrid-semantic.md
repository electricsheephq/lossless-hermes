---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-06] tools: port lcm_grep hybrid + semantic modes (Wave B)'
labels: 'port, tool, wave-b'
---

## Source (TypeScript)
- File: `src/tools/lcm-grep-tool.ts` (hybrid lines 474–760; semantic dispatch in lines 291–351), `src/embeddings/hybrid-search.ts`, `src/embeddings/semantic-search.ts`.
- Lines: ~300 LOC of hybrid path + ~150 LOC of semantic path + ~500 LOC across the two embeddings modules.
- Function(s)/class(es): hybrid path inside the grep dispatch + `runHybridSearch`, `runSemanticSearch`, `SemanticSearchUnavailableError`.

## Target (Python)
- File: extends `src/lossless_hermes/tools/grep.py` (delete the placeholder `not yet available` branches from #06-08) + adds `src/lossless_hermes/embeddings/hybrid_search.py` + `src/lossless_hermes/embeddings/semantic_search.py`.
- Estimated LOC: ~500 LOC (300 in grep.py for the hybrid path, 200 across embeddings/).

## Dependencies
- Depends on: **Epic 05 embeddings** (Voyage client, vec0 schema, embeddings backfill, hybrid_search.py). This issue cannot start until Epic 05 lands.
- Depends on: #06-08 (Wave A grep modes — this issue extends the same file).

## Acceptance criteria
- [ ] **`hybrid` mode** (TS lines 474–760):
  - `run_hybrid_search()` fans out two arms: FTS5 (via the same store path as `full_text`) and Voyage semantic vec0 KNN.
  - Over-fetches `max(50, limit * 3)` from each arm, capped 500.
  - RRF-fuses the union.
  - Voyage **rerank** scores the union with retry/timeout pinned: `voyage_max_retries=1`, `voyage_timeout_ms=15_000`. Wall-time budget for the agent hot path.
  - **Provenance tag per hit:** `[from FTS+semantic]` | `[from FTS only]` | `[from semantic only]`.
  - **Degrades gracefully:**
    - vec0 missing → `degraded_to_fts_only=True`.
    - Rerank fails → RRF-only with `degraded_skipped_rerank=True`.
- [ ] **`semantic` mode** (cheaper than hybrid; no rerank):
  - `run_semantic_search()` runs pure vec0 KNN over Voyage-embedded summaries.
  - Supports `summaryKinds: ["leaf", "condensed"]` filter (mode='semantic' / 'hybrid' only per the schema description).
- [ ] **`VOYAGE_API_KEY` missing** → `{"error": "...hybrid mode requires it. Use mode='full_text' for keyword-only search."}` (TS line 631 — operator-facing fallback hint is load-bearing).
- [ ] **vec0 not loaded** → semantic mode raises `SemanticSearchUnavailableError`; tool falls back to FTS-only or refuses (degraded flag set).
- [ ] **Wave-12 F5 invariant preserved** — both modes pass through the token gate middleware (estimators: hybrid `250 + limit*230`, semantic `350 + limit*215`).
- [ ] Delete the `not yet available` branches that #06-08 wrote — the regression test in #06-08 inverts to assert the new behavior.
- [ ] PR description cites the LCM commit SHA being ported.

## Tests
- Mirror `lcm-grep-tool-hybrid.test.ts` 1:1 in `tests/tools/test_lcm_grep_wave_b.py` (~419 TS LOC → ~330 pytest LOC):
  - Hybrid happy path: both arms return rows, RRF-fuse + rerank produces expected order, provenance tags correct.
  - Voyage 429 → retry once → succeed.
  - Voyage 429 → retry once → fail → `degraded_skipped_rerank=True` with RRF-only result.
  - vec0 absent → `degraded_to_fts_only=True`.
  - `summaryKinds=["leaf"]` filter restricts hits.
  - `summaryKinds=["condensed"]` likewise.
  - Semantic mode happy path with Voyage embed call.
  - `VOYAGE_API_KEY` missing → error with the fallback prose.

## Estimated effort
**10 hours** — 6h port (RRF-fusion + rerank + degradation flag plumbing), 4h tests (the degradation matrix is the work).

## Confidence
**85%** — depends on Epic 05's Voyage client surface being ready. Once that lands the port is mechanical, but cross-epic dependency adds risk.

## References
- [`docs/porting-guides/tools.md`](../../docs/porting-guides/tools.md) "lcm_grep" section, hybrid + semantic subsections.
- [`docs/porting-guides/embeddings.md`](../../docs/porting-guides/embeddings.md) for `runHybridSearch` / `runSemanticSearch` contracts.
- TS test fixture: `test/lcm-grep-tool-hybrid.test.ts` (419 LOC).
