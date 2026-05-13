---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-04] compaction: port compactFullSweep + leafPass'
labels: 'port'
---

## Source (TypeScript)
- File: `lossless-claw/src/compaction.ts` (pr-613 `1f07fbd`)
- Lines:
  - `compactFullSweep`: 626–774 (~149 LOC) — hard-trigger sweep (phase-1 leaves, phase-2 condensed)
  - `compactLeaf`: 481–492 (~12 LOC) — soft-trigger single-pass wrapper
  - `leafPass`: 1492–1607 (~116 LOC) — the actual leaf-summary creation
  - `selectOldestLeafChunk`: 1005–1057 (~53 LOC) — chunk-selection helper
  - Supporting: `annotateMediaContent` 1457–1485, `extractMeaningfulMessageText` 313–331, `formatTimestamp` 125–150
- Function(s)/class(es): `CompactionEngine.compactFullSweep`, `CompactionEngine.compactLeaf`, `CompactionEngine._leafPass`, `CompactionEngine._selectOldestLeafChunk`

## Target (Python)
- File: `src/lossless_hermes/compaction.py`
- Estimated LOC: ~380 (across all listed methods + helpers)
- Methods on `CompactionEngine`:
  - `compact_full_sweep(conversation_id, token_budget, summarize: SummarizeFn, hard_trigger=False, summary_model=None) → CompactionResult`
  - `compact_leaf(conversation_id, token_budget, summarize, summary_model=None) → CompactionResult`
  - `_leaf_pass(conversation_id, summarize, summary_model=None) → dict | None` (private)
  - `_select_oldest_leaf_chunk(conversation_id, override=None) → dict | None`
  - `_annotate_media_content(content) → str`
  - `_extract_meaningful_message_text(content) → str`
  - `_format_timestamp(dt, timezone) → str`

## Algorithm — leaf-pass

Per `docs/porting-guides/assembler-compaction.md` §"Leaf-pass algorithm":

**Goal:** collapse one chunk of contiguous raw messages outside the fresh tail into one **leaf summary**.

1. **Select chunk** — `_select_oldest_leaf_chunk`:
   - Walk context items oldest → newest, skipping non-message items until the first raw message.
   - Once started, stop on any non-message item OR when adding the next message would push chunk tokens over `leaf_chunk_tokens` (always include at least one message).
   - Stop AT (don't include) any item with `ordinal >= fresh_tail_ordinal`.
2. **Resolve prior leaf summary context** — last up-to-2 summary items before the chunk's start ordinal, joined by `\n\n`. Becomes `options.previous_summary` for the summarizer (iterative continuity).
3. **Fetch full messages** for each chunk item. Annotate media: `_annotate_media_content` replaces media-only messages with `"[Media attachment]"`; media-mostly messages keep their text + `" [with media attachment]"` suffix.
4. **Concatenate** — each message becomes `[YYYY-MM-DD HH:mm TZ]\n<text>` (`_format_timestamp`). Reasoning/thinking blocks are stripped via `_extract_meaningful_message_text`. Empty messages filtered.
5. **Extract file ids** for the summary's `file_ids` index — `extract_file_ids_from_content` from `large_files.py`.
6. **Summarize** via `_summarize_with_escalation` (issue 04-06) with `target_tokens = config.leaf_target_tokens`, `options.is_condensed = False`.
7. **On success**, persist atomically in a transaction:
   - `insert_summary({summary_id: "sum_" + sha256(content+now).hexdigest()[:16], kind: "leaf", depth: 0, content, token_count, file_ids, earliest_at, latest_at, descendant_count: 0, descendant_token_count: 0, source_message_token_count, model})`
   - `link_summary_to_messages(summary_id, message_ids)` — DAG edges
   - `replace_context_range_with_summary({conversation_id, start_ordinal, end_ordinal, summary_id})` — atomic swap
8. **Invalidate context cache** so subsequent passes see the new state.
9. **Return** `{summary_id, level, content, removed_tokens, added_tokens}`.

**Auth-failure short-circuit:** if `_summarize_with_escalation` raises `LcmProviderAuthError` (or its sentinel), `_leaf_pass` returns `None`. Caller treats it as a non-compacting skip and sets `auth_failure: True` on the `CompactionResult`. This avoids persisting fallback summaries during transient provider outages.

## Algorithm — compactFullSweep

Per the porting guide §"Entry points":

- Phase 1: loop `_leaf_pass` until no progress, `max_rounds` exceeded, or `evaluate()` says we're under threshold.
- Phase 2: loop `_condensed_pass` (issue 04-03) under the same conditions.
- Per-pass progress check (Wave-12, see issue 04-04): break if `pass_tokens_after >= pass_tokens_before` OR `pass_tokens_after >= previous_tokens`.

Wraps the whole call in `_with_context_cache(conversation_id)` (per-conversation refcounted cache, lines 362–403 in TS — port verbatim).

## Reference

`assembler-compaction.md` walkthrough lines 252–272. Note the explicit invariant in step 9: `removed_tokens` is sum of source `resolve_message_token_count` (NOT what the DB will report — `token_count` column may be 0 for some rows; this fed into the running-delta optimization but is bounded). See Remaining-5%-risk §3 — populate `token_count` at insert time (Epic 01/03 contract) to make this concern vanish.

## Wave-N fixes to preserve

Per ADR-029, add inline comments at:

- **Wave-12 (per-pass progress check)** at the break condition inside `compact_full_sweep` phase loops:
  ```python
  # LCM Wave-12 (2026-04-22): per-pass progress guard prevents thrashing when
  # the summarizer returns near-input-size output. Break if pass made no progress
  # against either the immediate or running floor.
  # Original: lossless-claw/src/compaction.ts:705–712.
  ```
- **Auth-short-circuit return** at the `if summarize_result is None: return None` site in `_leaf_pass`:
  ```python
  # LCM auth-short-circuit: avoid persisting fallback-truncation summaries
  # during transient provider outages — preserves DAG integrity.
  # Original: lossless-claw/src/compaction.ts:1571 (early-return on null).
  ```

## Dependencies
- Depends on: Issue 04-01 (evaluate); Epic 01 (summary_store, conversation_store, replace_context_range_with_summary, link_summary_to_messages, insert_summary)
- Depends on: Issue 04-06 (summarize_with_escalation) — leaf_pass calls this for the actual LLM call
- Blocks: Issue 04-03 (condensation pass uses the same chunk-selection patterns), Issue 04-04 (anti-thrashing tests target both passes)

## Acceptance criteria
- [ ] `_select_oldest_leaf_chunk` produces the same chunk set as TS for the same context_items input (port the existing TS test fixtures)
- [ ] Chunk-selection terminates on any non-message item (NOT just summaries) — guards future item types
- [ ] Chunk always includes ≥1 message even if it alone exceeds `leaf_chunk_tokens`
- [ ] Chunk stops STRICTLY BEFORE `fresh_tail_ordinal` (uses `<`, not `<=`)
- [ ] Prior-summary context is exactly the last 2 summary items before chunk start, joined `\n\n`
- [ ] Media annotation: pure-media messages become `"[Media attachment]"`; mixed get `" [with media attachment]"` suffix
- [ ] Reasoning/thinking blocks are stripped from message text before concatenation
- [ ] Summary insert uses `"sum_" + sha256(content + str(now_ms)).hexdigest()[:16]` ID format
- [ ] `link_summary_to_messages` is called with the message IDs in chunk order
- [ ] `replace_context_range_with_summary` is called inside the same transaction as `insert_summary`
- [ ] Context cache invalidated after each successful `replace_context_range_with_summary`
- [ ] Auth-failure path returns `None` (NOT raising) — caller handles
- [ ] Per-pass progress check (Wave-12 inline comment present)
- [ ] All TS unit tests in `test/compaction-maintenance-store.test.ts` (the "leafPass" + "compactFullSweep" describe blocks) ported
- [ ] PR description cites LCM commit SHA `1f07fbd`

## Tests

Port from `test/compaction-maintenance-store.test.ts` and `test/regression-2026-03-17.test.ts`:

- `_select_oldest_leaf_chunk includes exactly contiguous raw messages`
- `_select_oldest_leaf_chunk terminates on summary item` (mid-chunk summary stops walk)
- `_select_oldest_leaf_chunk respects token cap` (chunk size ≤ `leaf_chunk_tokens` unless single message exceeds)
- `_select_oldest_leaf_chunk stops at fresh_tail_ordinal` (strict <)
- `_leaf_pass persists summary with correct DAG edges` (message_ids → summary_id link rows present)
- `_leaf_pass invalidates context cache on success`
- `_leaf_pass returns None on auth failure` (mock summarizer raises `LcmProviderAuthError`)
- `_leaf_pass strips reasoning blocks from message text`
- `_leaf_pass media-only message becomes [Media attachment]`
- `compact_full_sweep stops when evaluate() reports under threshold`
- `compact_full_sweep stops after max_rounds`
- `compact_full_sweep per-pass progress break` (mock summarizer returns input-size output → break)

## Estimated effort
10–14 hours

## Confidence
90% — algorithm is well-documented but has many small invariants (strict-vs-non-strict ordinals, the always-include-one-message rule, the auth-failure short-circuit). The TS test suite is the safety net.
