---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-04] compaction: port evaluate() + evaluateLeafTrigger()'
labels: 'port'
---

## Source (TypeScript)
- File: `lossless-claw/src/compaction.ts` (pr-613 `1f07fbd`)
- Lines: 408–459 (~52 LOC; both evaluators)
- Function(s)/class(es):
  - `CompactionEngine.evaluate(conversationId, tokenBudget, observedTokenCount?) → CompactionDecision`
  - `CompactionEngine.evaluateLeafTrigger(conversationId, leafChunkTokensOverride?) → { shouldCompact, reason, rawTokensOutsideTail, threshold }`

## Target (Python)
- File: `src/lossless_hermes/compaction.py`
- Estimated LOC: ~70 (Python slightly more verbose for the dataclass shapes)
- Class: `CompactionEngine`
- Methods: `evaluate()`, `evaluate_leaf_trigger()`
- Dataclass: `CompactionDecision(should_compact: bool, reason: str, current_tokens: int, threshold: int)`

## Algorithm

Per `docs/porting-guides/assembler-compaction.md` §"Trigger evaluation":

**`evaluate()` — context-level threshold trigger:**
1. `live_tokens = summary_store.get_context_token_count(conversation_id, max_ordinal_exclusive=None)` (stored running total)
2. `current_tokens = max(stored_tokens, live_tokens, observed_token_count or 0)` — defensive max over stored + observed; live takes the lead when ingest just landed but telemetry hasn't refreshed.
3. `threshold = floor(context_threshold * token_budget)` (default `context_threshold = 0.75`)
4. Return `CompactionDecision(should_compact=current_tokens > threshold, reason="threshold" if exceeded else "none", current_tokens, threshold)`

**`evaluate_leaf_trigger()` — soft incremental trigger:**
1. Resolve `fresh_tail_ordinal` from current context items (use compaction engine's own helper, NOT the assembler's — compaction has its own walk that doesn't apply token caps).
2. Sum raw-message tokens for items with `ordinal < fresh_tail_ordinal`.
3. Threshold = `leaf_chunk_tokens_override or config.leaf_chunk_tokens` (default 20_000).
4. Return `{ should_compact: raw_tokens_outside_tail >= threshold, reason: "leaf-trigger" if exceeded else "below-leaf-trigger", raw_tokens_outside_tail, threshold }`.

## Reference: assembler-compaction.md walkthrough

The walkthrough explicitly distinguishes the two triggers (§"Trigger evaluation"):

> Two distinct triggers:
> 1. `evaluate(conversationId, tokenBudget, observedTokenCount?)` — context-level. ...
> 2. `evaluateLeafTrigger(conversationId, leafChunkTokensOverride?)` — soft incremental trigger.

Both are called from `engine/compact.py` (`_CompactMixin.evaluate_incremental_compaction` per engine.md lines 261–263). The leaf trigger is the cheap maintenance signal; the context evaluator is the hard "we're going to overflow" signal.

## Dependencies
- Depends on: Epic 02 issue "engine state + stores wired" (need `summary_store` handle); Epic 01 "summary_store.get_context_token_count" method
- Blocks: Issue 04-02 (leaf pass needs the trigger evaluator), Issue 04-04 (anti-thrashing tests need both evaluators), Issue 04-08 (telemetry decision logging cites these)

## Acceptance criteria
- [ ] `CompactionDecision` dataclass matches TS shape: `should_compact`, `reason`, `current_tokens`, `threshold`
- [ ] `evaluate()` returns `should_compact=True` iff `current_tokens > threshold` (strict `>`, NOT `>=` — matches TS line 433)
- [ ] `evaluate()` uses `max(stored, live, observed)` — observed token count overrides stored if provided and larger
- [ ] `threshold = floor(context_threshold * token_budget)` (Python `int()` truncates toward zero same as TS `Math.floor` for positive values)
- [ ] `evaluate_leaf_trigger()` sums tokens strictly *outside* the fresh tail (`ordinal < fresh_tail_ordinal`)
- [ ] `evaluate_leaf_trigger()` uses `>=` (NOT strict `>`) for the trigger — soft trigger fires AT the boundary
- [ ] Both methods are sync (`def`, not `async def`) per ADR-017
- [ ] All TS unit tests in `test/compaction-maintenance-store.test.ts` (the "evaluate" describe block) have ported pytest equivalents
- [ ] PR description cites the LCM commit SHA being ported (`1f07fbd` from pr-613)

## Tests

Port from `test/compaction-maintenance-store.test.ts`:
- `evaluate returns shouldCompact=false when under threshold` — observe 5k tokens vs 10k budget × 0.75 = 7500 threshold → false.
- `evaluate returns shouldCompact=true when over threshold` — 8k tokens vs 7500 threshold → true.
- `evaluate uses max(stored, live, observed)` — stored=5k, observed=8k, threshold=7500 → true (observed wins).
- `evaluate respects custom contextThreshold` — config.context_threshold=0.5 → threshold=5000.
- `evaluate_leaf_trigger fires at leaf_chunk_tokens` — rawOutsideTail=20000, default 20k → fires with `>=`.
- `evaluate_leaf_trigger override` — pass `leaf_chunk_tokens_override=10000` → fires earlier.
- `evaluate_leaf_trigger excludes fresh tail` — messages inside the fresh-tail ordinal contribute 0 to the sum.

## Estimated effort
4–6 hours

## Confidence
95% — pure arithmetic + dataclass plumbing. The only subtle point is the `max(stored, live, observed)` defensive ordering, which the porting guide documents explicitly.
