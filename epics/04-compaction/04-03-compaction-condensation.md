---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-04] compaction: port condensation pass (depth N+1)'
labels: 'port'
---

## Source (TypeScript)
- File: `lossless-claw/src/compaction.ts` (pr-613 `1f07fbd`)
- Lines:
  - `condensedPass`: 1614–1751 (~138 LOC) — the actual condensation
  - `selectShallowestCondensationCandidate`: 1193–1222 (~30 LOC)
  - `selectOldestChunkAtDepth`: 1230–1282 (~53 LOC)
  - `resolveFanoutForDepth`: 1173–1181 (~9 LOC)
  - `resolveCondensedMinChunkTokens`: ~10 LOC (called inline)
- Function(s)/class(es): `CompactionEngine._condensedPass`, `_selectShallowestCondensationCandidate`, `_selectOldestChunkAtDepth`, `_resolveFanoutForDepth`

## Target (Python)
- File: `src/lossless_hermes/compaction.py`
- Estimated LOC: ~260 (across all listed methods)
- Methods on `CompactionEngine`:
  - `_condensed_pass(conversation_id, summarize, summary_model=None, hard_trigger=False) → dict | None`
  - `_select_shallowest_condensation_candidate(conversation_id, hard_trigger=False) → dict | None`
  - `_select_oldest_chunk_at_depth(conversation_id, target_depth, fresh_tail_override=None) → dict`
  - `_resolve_fanout_for_depth(depth, hard_trigger) → int`
  - `_resolve_condensed_min_chunk_tokens() → int`

## Algorithm

Per `docs/porting-guides/assembler-compaction.md` §"Condensation algorithm":

**Goal:** collapse N contiguous summaries at the **same depth** into one **condensed** summary at depth+1.

1. **Pick candidate depth** — `_select_shallowest_condensation_candidate`:
   - Call `summary_store.distinct_depths_in_context(conversation_id, max_ordinal_exclusive=fresh_tail_ordinal)` for the depth list.
   - For each depth from shallowest, compute `fanout = _resolve_fanout_for_depth(depth, hard_trigger)`:
     - `hard_trigger=True` → use `config.condensed_min_fanout_hard` (default 2) for all depths
     - `hard_trigger=False` AND `depth == 0` → use `config.leaf_min_fanout` (default 8)
     - `hard_trigger=False` AND `depth > 0` → use `config.condensed_min_fanout` (default 4)
   - Call `_select_oldest_chunk_at_depth(conversation_id, depth, fresh_tail_ordinal)`:
     - Walk items, terminate on any non-summary, depth mismatch, OR when adding the next summary would push chunk tokens over `leaf_chunk_tokens`.
   - **Skip this depth** if `chunk.items.length < fanout` OR `chunk.summary_tokens < _resolve_condensed_min_chunk_tokens()` (= `max(config.condensed_target_tokens, 0.1 * config.leaf_chunk_tokens)`).
   - Return the first depth that produces a valid chunk.

2. **Fetch summary records** for the chunk (each `chunk.items[i].summary_id` → full `SummaryRecord` via `summary_store.get_summary`).

3. **Concatenate** with date-range header `[<earliest> - <latest>]\n<content>` per summary, joined by `\n\n`. Date format = the same `_format_timestamp` used in leaf pass.

4. **Resolve prior summary context** (only at depth 0; lines 1648–1651): walk back up to 4 same-depth summaries, take the last 2. At depth > 0, `previous_summary` is None — depth-≥2 condensations don't carry prior-summary continuity (the prompt templates D2/D3+ also don't have a `<previous_context>` block, per the prompt-templates issue).

5. **Summarize** with `_summarize_with_escalation`, `target_tokens = config.condensed_target_tokens`, `options.is_condensed = True`, `options.depth = target_depth + 1`.

6. **Persist** in a transaction:
   - `insert_summary({summary_id: "sum_" + sha256(content + now_ms).hexdigest()[:16], kind: "condensed", depth: target_depth + 1, content, token_count, earliest_at, latest_at, descendant_count: sum(child.descendant_count) + len(chunk), descendant_token_count: sum(child.descendant_token_count) + sum(child.token_count), source_message_token_count, model})`
   - `link_summary_to_parents(summary_id, parent_summary_ids)` — DAG edges to the child summaries (NOT messages, unlike leaf pass)
   - `replace_context_range_with_summary` — atomic swap of the summary range with one new summary item

## Anti-thrashing interaction

Phase-2 of `compact_full_sweep` runs `_condensed_pass` repeatedly with the same per-pass progress break as phase-1 (issue 04-04). Hard-trigger sweeps relax fanout via `condensed_min_fanout_hard` to enable aggressive collapsing when context is genuinely overflowing.

## Reference

`assembler-compaction.md` walkthrough lines 274–290. The key invariants:

- Depth picking is **shallowest-first** — collapse leaves before condensed, condensed depth=1 before depth=2, etc.
- Chunk picking is **oldest-at-depth-first** — preserve recency by collapsing oldest summaries.
- Hard trigger relaxes fanout but does NOT change the min-chunk-tokens floor (still requires the chunk to be substantial enough to justify a condensation call).

## Wave-N fixes to preserve

Per ADR-029, add inline comment at:

- **Wave-12 per-pass progress check** (same as leaf pass, but for phase-2):
  ```python
  # LCM Wave-12 (2026-04-22): per-pass progress guard for condensation phase.
  # Mirror of the phase-1 guard; protects against summarizer returning
  # near-input-size output on a stack of summaries.
  # Original: lossless-claw/src/compaction.ts:705–712 (and its phase-2 mirror).
  ```

## Dependencies
- Depends on: Issue 04-02 (compact_full_sweep harness, _summarize_with_escalation, _format_timestamp)
- Depends on: Epic 01 (`summary_store.distinct_depths_in_context`, `link_summary_to_parents`, `insert_summary` with `kind: "condensed"`)
- Blocks: Issue 04-05 (prompt templates — needs depth parameter to dispatch D1/D2/D3+)

## Acceptance criteria
- [ ] `_resolve_fanout_for_depth` matches TS dispatch exactly (hard → 2; soft + depth 0 → 8; soft + depth > 0 → 4)
- [ ] `_select_shallowest_condensation_candidate` returns the FIRST valid depth (shallowest), not the deepest
- [ ] `_select_oldest_chunk_at_depth` terminates on depth mismatch (depth-1 summary in a depth-0 walk stops the walk)
- [ ] Skip threshold: `chunk.summary_tokens >= max(condensed_target_tokens, 0.1 * leaf_chunk_tokens)` (NOT below)
- [ ] Date-range header format: `[<earliest_iso> - <latest_iso>]\n<content>` (one space around dash)
- [ ] Prior-summary context: only at depth 0; walk back ≤4 candidates, take last 2 joined `\n\n`
- [ ] `is_condensed=True` and `depth=target_depth+1` passed to summarizer
- [ ] `insert_summary` uses `kind="condensed"` (NOT `"leaf"`)
- [ ] `descendant_count` = sum of child `descendant_count` + `len(chunk)` (each child contributes its own subtree count + 1 for itself)
- [ ] `descendant_token_count` = sum of child `descendant_token_count` + sum of child `token_count`
- [ ] `link_summary_to_parents` (NOT `link_summary_to_messages`) — the DAG edge is summary→summary
- [ ] Atomic transaction: insert + link + replace are in one `with conn:` block
- [ ] All TS unit tests for condensation in `test/compaction-maintenance-store.test.ts` ported
- [ ] PR description cites LCM commit SHA `1f07fbd`

## Tests

Port from `test/compaction-maintenance-store.test.ts`:

- `_resolve_fanout_for_depth dispatches by depth and hard flag` (4 cases: soft-depth-0, soft-depth>0, hard-depth-0, hard-depth>0)
- `_select_shallowest_condensation_candidate picks depth 0 first when leaves available`
- `_select_shallowest_condensation_candidate picks depth 1 when depth-0 chunk too small`
- `_select_oldest_chunk_at_depth terminates on depth mismatch`
- `_select_oldest_chunk_at_depth respects token cap (leaf_chunk_tokens)`
- `_condensed_pass min-chunk-tokens skip` (chunk with 1k summary tokens but condensed_target=900 → skip if also < 10% leaf_chunk)
- `_condensed_pass writes summary with kind=condensed and depth=N+1`
- `_condensed_pass descendant counts accumulate from children`
- `_condensed_pass prior-summary context only at depth 0`
- `_condensed_pass uses condensed_min_fanout_hard under hard trigger`
- `_condensed_pass DAG link is summary→parent_summaries, not summary→messages`

## Estimated effort
10–14 hours

## Confidence
90% — depth picking + fanout dispatch are the trickiest parts; both have explicit TS tests to port.
