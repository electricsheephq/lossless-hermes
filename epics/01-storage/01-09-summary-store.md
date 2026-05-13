---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-01] storage: port store/summary-store.ts → store/summary.py'
labels: 'port, epic-01-storage'
---

## Source (TypeScript)

- File: `src/store/summary-store.ts`
- Lines: **1,668 LOC** (task brief said "1569" — the porting-guide and lcm-source-map both say 1,668; this is the v4.1 enlarged shape. Use 1,668.)
- Function(s)/class(es): `class SummaryStore` — full public API per storage.md §4.2 table (~30 methods plus CJK helpers).

## Target (Python)

- File: `src/lossless_hermes/store/summary.py`
- Estimated LOC: ~2,000

## What this issue covers

The summaries + context_items + large_files + bootstrap_state CRUD plus CJK-aware search dispatcher plus recursive subtree walks. **The largest non-migration store.** Per ADR-017 the store is **synchronous**.

### Public surface (per storage.md §4.2)

| Method | Notes |
|---|---|
| `__init__(conn, *, fts5_available, trigram_tokenizer_available)` | Two feature flags cached. |
| `insert_summary(input) -> SummaryRecord` | Insert leaf/condensed; updates `summaries_fts` (and `summaries_fts_cjk` if available). |
| `get_summary(summary_id)` | |
| `get_summaries_by_conversation(conv_id)` | |
| `link_summary_to_messages(summary_id, message_ids)` | summary_messages rows. |
| `link_summary_to_parents(summary_id, parent_ids)` | summary_parents rows. |
| `get_summary_messages(summary_id)` | |
| `get_conversation_max_summary_depth(conv_id)` | |
| `get_leaf_summary_links_for_message_ids(ids)` | Inverse lookup for search-hit expansion. |
| `list_transcript_gc_candidates(opts)` | Find messages safe to GC (covered by ≥1 leaf summary; not in context_items). |
| `get_summary_children(summary_id)` | |
| `get_summary_parents(summary_id)` | |
| `get_summary_subtree(summary_id)` | Recursive walk via `WITH RECURSIVE` CTE. |
| `get_context_items(conv_id)` | Assembled prompt ordering. |
| `get_distinct_depths_in_context(conv_id)` | |
| `prune_for_new_session(conv_id, retain_depth)` | Truncate context_items. |
| `append_context_message(conv_id, message_id)` | |
| `append_context_messages(conv_id, ids)` | Bulk. |
| `append_context_summary(conv_id, summary_id)` | |
| `replace_context_range_with_summary({conv_id, from_ordinal, to_ordinal, summary_id})` | Atomic; uses tx mutex. |
| `get_context_token_count(conv_id)` | Sum across rows joined to messages/summaries. |
| `search_summaries(input)` | FTS5 / FTS-CJK / LIKE / LIKE-CJK / regex dispatcher. |
| `insert_large_file(input) -> LargeFileRecord` | |
| `get_large_file(file_id)` | |
| `get_large_files_by_conversation(conv_id)` | |
| `get_conversation_bootstrap_state(conv_id)` | |
| `upsert_conversation_bootstrap_state(input)` | |
| `_extract_cjk_segments(text)` | CJK detection. |
| `_extract_latin_tokens(text)` | |
| `_split_cjk_chunks(text)` | |
| `_search_cjk_trigram(...)` | CJK path via `summaries_fts_cjk`. |
| `_search_like_cjk(...)` | LIKE fallback for CJK when trigram unavailable. |
| `with_transaction(fn)` | |

### CJK detection

Per storage.md §4.2 gotchas: `contains_cjk(text)` covers CJK Unified, Compat, Kana, Hangul. Python regex translates 1:1 from TS:

```python
_CJK_PATTERN = re.compile(
    r"[　-〿぀-ゟ゠-ヿ"
    r"㐀-䶿一-鿿가-힯"
    r"豈-﫿＀-￯]"
)
```

(Exact ranges per `src/store/summary-store.ts`. Use the same Unicode-block enumeration.)

### Recursive subtree walks

Use SQLite's `WITH RECURSIVE`:

```sql
WITH RECURSIVE
  walk(summary_id, depth) AS (
    SELECT summary_id, 0 FROM summaries WHERE summary_id = ?
    UNION ALL
    SELECT sp.summary_id, walk.depth + 1
    FROM summary_parents sp
    JOIN walk ON sp.parent_summary_id = walk.summary_id
  )
SELECT s.* FROM walk JOIN summaries s USING (summary_id) ORDER BY depth, summary_id
```

Port verbatim from TS.

### Externalized tool-output detection (storage.md §4.2 gotcha)

`search_full_text` detects externalized references via a regex so it doesn't surface `lcm_describe` boilerplate as relevant content. Port carefully — see `test/fts-fallback.test.ts:106` "ignores lcm_describe helper text".

## Dependencies

- Depends on: #01-01, #01-03, #01-04, #01-05, #01-06 (for the `lcm_*` tables that some queries scope to via partial indexes), #01-11 (helpers), #01-13 (transaction_mutex).
- Blocks: Epic 02 (engine assemble), Epic 03 (ingest assembly), Epic 04 (compaction).

## Acceptance criteria

- [ ] All ~30 public/private methods implemented per the table above.
- [ ] `insert_summary` writes to `summaries_fts` AND `summaries_fts_cjk` (when trigram available) in the same transaction.
- [ ] FK CASCADE verified: deleting a conversation cascades to summaries, summary_messages, summary_parents, context_items, large_files, bootstrap_state.
- [ ] FK RESTRICT verified: deleting a summary that is referenced by `context_items.summary_id` raises `IntegrityError`.
- [ ] `WITH RECURSIVE` subtree walks return correct depth-ordered results on a 4-level pyramid fixture (leaf → condensed-1 → condensed-2 → condensed-3).
- [ ] CJK detection regex matches Unicode block ranges per the TS source.
- [ ] CJK trigram path: a summary containing `"会議の議事録"` returns on `MATCH '議事録'` via `summaries_fts_cjk`. (Skipped when trigram unavailable.)
- [ ] CJK LIKE fallback path: same content + same query returns via `_search_like_cjk` when trigram is unavailable.
- [ ] Per `test/summary-store.test.ts` (storage.md §8 row 13) — 2 cases (shallow-tree helpers; LIKE fallback ordering) ported to `tests/test_summary_store.py`.
- [ ] Per `test/fts-fallback.test.ts` row 17: ported case "ignores lcm_describe helper text" passes — the regex correctly filters externalized-reference snippets.
- [ ] `replace_context_range_with_summary` is atomic — verified by a kill-mid-transaction test (insert a Python-level exception mid-call, assert no partial range replacement persists).
- [ ] Storage-only subset (~15 cases) from `test/lcm-integration.test.ts` covering summary lifecycle / context_items / bootstrap_state ported here.
- [ ] `pytest tests/test_summary_store.py tests/test_lcm_integration_storage.py::test_summary_*` passes.
- [ ] `mypy --strict` passes.
- [ ] PR description cites LCM commit `1f07fbd` and `src/store/summary-store.ts` (1,668 LOC).

## Estimated effort

**22–28 hours.** Bulk: method-by-method translation. Long tails: CJK paths (extract/split/segment), recursive subtree walks, context_items atomic replace, externalized-reference snippet filtering.

## Confidence

**92%** — TS source is well-structured. Residual risk: (a) CJK byte-offset edge cases in snippets (storage.md §4.1 gotcha — TS UTF-16 vs Python code-point); (b) recursive CTE correctness on deep pyramids; (c) externalized-reference snippet regex needs the spike-3-style fixture extended to summary-side content.
