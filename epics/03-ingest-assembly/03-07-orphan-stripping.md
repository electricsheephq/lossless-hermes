---
name: Port issue
about: Port `filterNonFreshAssistantToolCalls` and `sanitizeToolUseResultPairing`
title: '[epic-03] assembler: port orphan-tool-call stripping + final repair pass'
labels: 'port'
---

## Source (TypeScript)
- File: `src/assembler.ts` (`pr-613` HEAD `1f07fbd`)
  - Lines: 687–778 (`filterNonFreshAssistantToolCalls`), called at 1244 inside `assemble`.
- File: `src/transcript-repair.ts`
  - Lines: full file. Function: `sanitizeToolUseResultPairing`. Called from `assemble` at 1301.
- Function(s)/class(es): `filterNonFreshAssistantToolCalls`, `sanitizeToolUseResultPairing`.

## Target (Python)
- `src/lossless_hermes/assembler.py` — `_filter_non_fresh_assistant_tool_calls` (static method).
- `src/lossless_hermes/transcript_repair.py` — `sanitize_tool_use_result_pairing` (public module function).
- Estimated LOC: ~250 across both files (orphan stripping ~150 + transcript repair ~100; the TS counterparts are similar sizes).

## Background

Two distinct repair passes, both load-bearing:

### 1. `filter_non_fresh_assistant_tool_calls` (assembler.ts:687–778)

After budget-walk, the selected message list may contain assistant turns whose `tool_use` / `toolCall` / `function_call` blocks reference a `tool_call_id` whose matching `tool_result` was evicted. Anthropic-compatible APIs reject such "orphan" tool-call blocks. This pass strips them.

**Algorithm** (from `docs/porting-guides/assembler-compaction.md` §"Step-by-step" step 11):

1. Index *selected* tool_result ordinals by id: `selected_tool_result_ords_by_id: dict[str, list[int]]`.
2. For each assistant message in selected:
   - For each tool-call block in its content:
     - Find some selected `tool_result` with the same id at an ordinal **strictly greater** than the assistant message.
     - If yes → **keep** the block.
     - If no AND `item.ordinal < orphan_stripping_ordinal` → **strip** the block.
     - If no AND the id has **NO** resolved `tool_result` *anywhere* (empty/absent in `all_tool_result_ords_by_id`) → **keep** (cache-marginal protection: absence-of-evidence guards against transient eviction churn).
     - Else (resolved somewhere in `all_tool_result_ords_by_id` but not in selected window AND boundary protected) → **strip**.

   **TS canonical semantics** (per PR #50 / #49 review): TS `assembler.ts:747` uses `if (!(allToolResult.get(id)?.length)) return true` — the leading `!` means **absence/empty** → KEEP, **presence** → STRIP. Earlier spec drafts inverted this; PR #50 ships the TS-canonical reading with two regression tests (`test_orphan_above_boundary_with_resolved_anywhere_stripped` and `test_orphan_above_boundary_with_empty_resolved_list_kept`).
   - If all blocks stripped, **drop the whole assistant message**.

The `orphan_stripping_ordinal` parameter is a stable boundary that hot-cache callers can supply to prevent prefix churn across turns (per `LCMEngine._stable_orphan_stripping_ordinals_by_conversation` state on the shell class).

### 2. `sanitize_tool_use_result_pairing` (transcript-repair.ts)

The FINAL repair pass before `assemble()` returns. Called at `assembler.ts:1301`.

**Job:** pair orphaned `tool_result` blocks with synthesized `tool_use` blocks; drop still-unpaired turns. The assembler MUST return a message list where every `tool_result` has a preceding matching `tool_use` (Anthropic invariant — APIs reject unpaired `tool_result` blocks).

Read `src/transcript-repair.ts` directly (the file is small; this issue treats it as a black-box port from the TS source — same input, same output).

## Why this is split into one issue

Both passes operate on the post-budget-walk message list and both protect downstream API call shape. They share concepts (tool_call_id indexing, block-level filtering). Porting them together preserves the test fixtures' shape — many existing TS tests assert end-to-end behavior across both passes.

## Critical invariant for `sanitize_tool_use_result_pairing`

> The Python port of this function is a **prerequisite**, not a follow-up — assembler depends on it for the last line of `assemble`.
> — `docs/porting-guides/assembler-compaction.md` §"Remaining 5% risk" item 6.

## Helper functions on the engine shell (state contract from Epic 02)

```python
# Read by _filter_non_fresh_assistant_tool_calls inside _AssembleMixin.assemble().
self._stable_orphan_stripping_ordinals_by_conversation: dict[int, int] = {}
```

Cold-cache assemblies clear it (per `docs/porting-guides/engine.md` §"State owned by LcmContextEngine"); hot-cache assemblies re-use the stable boundary. The boundary is computed inside the assembler call, not on the engine shell — but it's READ via the shell map. Engine-level wiring is Epic 02 / #03-08.

## Dependencies
- Depends on: #03-04 (`ResolvedItem`), #03-05 (boundary semantics), #03-06 (budget-walked list is the input).
- Blocks: #03-08 (orchestration calls both passes).

## Acceptance criteria

### `_filter_non_fresh_assistant_tool_calls`

- [ ] Builds the selected-tool-result-ordinals index correctly (one list per tool_call_id, ordinals in ascending order).
- [ ] For each assistant message, evaluates each tool-call block independently — partial-strip works (some blocks kept, some dropped within one message).
- [ ] When ALL tool-call blocks of an assistant message are stripped AND the message has no other content, the whole message is dropped from the output.
- [ ] When ALL tool-call blocks are stripped but the message has surviving text content, the text survives (assistant message stays with text only).
- [ ] Orphan-stripping ordinal threshold: items with `ordinal >= orphan_stripping_ordinal` are NEVER stripped (cache-marginal protection).
- [ ] If a tool_call_id has ANY resolved (not necessarily selected) tool_result, the block is KEPT (also cache-marginal protection — `all_tool_result_ords_by_id` is the fallback signal).
- [ ] Result message list preserves chronological order.
- [ ] Provider-agnostic: works on `tool_use`, `toolCall`, and `function_call` block types.

### `sanitize_tool_use_result_pairing`

- [ ] Black-box test: input that has every TS-source pairing scenario produces the same output as the TS implementation (verified via a fixture corpus exported from a TS-side dump).
- [ ] Final assembler output never contains a `tool_result` without a matching preceding `tool_use`.
- [ ] Synthesized `tool_use` blocks (when generated to pair a lone `tool_result`) carry a synthetic-marker comment in the TS source — preserve the marker (used by debug diagnostics).
- [ ] Empty input returns empty.

### Shared

- [ ] All TS unit tests covering both passes have ported pytest equivalents:
  - `test/assembler-blocks.test.ts` (orphan stripping scenarios)
  - `test/transcript-repair.test.ts` (sanitize_tool_use_result_pairing direct tests)
- [ ] `pytest tests/test_assembler_orphan_stripping.py tests/test_transcript_repair.py` passes locally + on GitHub CI.
- [ ] No new mypy errors.
- [ ] PR description cites the LCM commit SHA being ported.

## Tests

### Orphan stripping

- Tool-use block whose tool_result is in the selected window → KEPT.
- Tool-use block whose tool_result was evicted, `ordinal < orphan_stripping_ordinal` → STRIPPED.
- Tool-use block whose tool_result was evicted, `ordinal >= orphan_stripping_ordinal` → KEPT (cache-marginal).
- Tool-use block whose tool_result exists in *resolved* but not *selected*, ordinal < stripping → KEPT (cache-marginal fallback).
- Multi-block assistant message: 2 tool-uses, one with paired result and one orphan → mixed strip (only the orphan removed).
- All-orphan assistant message with no text → DROPPED from output.
- Provider variants: same test against `tool_use` (Anthropic), `function_call` (OpenAI), and `toolCall` (OpenClaw legacy).

### Transcript repair

- Orphan `tool_result` with no preceding `tool_use` → synthetic `tool_use` injected with the synthetic marker.
- Two `tool_result`s with the same id → keep one, drop the duplicate.
- Tool-use immediately followed by matching tool-result → unchanged.
- Long conversation with interleaved orphans → all paired/dropped consistently.

## Estimated effort
**10 hours**. Orphan stripping is intricate (multi-branch decision tree per block); transcript repair is straightforward port + parity-via-fixture.

## Confidence
**85%**. Orphan-stripping's cache-marginal fallbacks (the "any resolved tool_result somewhere" path) are subtle — the TS source comment explicitly notes this prevents prefix churn across hot-cache turns. Verify with a regression test that simulates two consecutive `assemble()` calls on a hot-cache session and asserts identical output for the overlapping prefix.
