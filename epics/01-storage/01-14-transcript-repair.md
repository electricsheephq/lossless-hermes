---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-01] storage: port transcript-repair.ts → transcript_repair.py'
labels: 'port, epic-01-storage'
---

## Source (TypeScript)

- File: `src/transcript-repair.ts`
- Lines: **300 LOC** (storage.md §1 + lcm-source-map both say 300; task brief said 319. Use the porting-guide number: 300.)
- Function(s)/class(es): Tool-use ↔ tool-result pairing repair; sanitize OpenAI reasoning-block placement in assembled-context.

## Target (Python)

- File: `src/lossless_hermes/transcript_repair.py`
- Estimated LOC: ~360

## What this issue covers

Pure-function transcript repair used at assembly time. **No JSONL-rewrite path** — per `docs/reference/lcm-source-map.md` open-question #5 and task spec: the "drop JSONL-rewrite path" is a misread; the file has NO JSONL inside. **Keep verbatim shape, no shrink.**

### What it does (per storage.md §1 + lcm-source-map §"Misc")

Given an array of assembled messages that came out of context_items + freshTail, this module:

1. **Pairs `tool_use` with `tool_result`** — finds orphaned tool_use blocks (no matching tool_result) and orphaned tool_results (no matching tool_use), and either drops the orphans, synthesizes a placeholder tool_result with a clear error marker, or pairs adjacent compatible blocks. Behavior is provider-specific (Anthropic vs OpenAI):
   - **Anthropic:** tool_use blocks and tool_result blocks live in separate messages; pairing is by `tool_use_id`.
   - **OpenAI:** tool calls and tool results have different schema; the pairing logic translates between them.

2. **Sanitizes OpenAI reasoning placement** — OpenAI's `o1` / `o3` / `o4` reasoning blocks have specific placement rules in the message array. The module ensures reasoning content sits in the correct position (before content, not inside tool blocks).

### Pure-function interface

```python
@dataclass(frozen=True, slots=True)
class RepairResult:
    messages: list[dict]   # repaired message array
    dropped_count: int      # number of orphaned blocks dropped
    synthesized_count: int  # number of placeholder tool_results inserted
    repaired_count: int     # number of pairings adjusted


def repair_transcript(
    messages: Sequence[dict],
    *,
    provider: Literal["anthropic", "openai"],
) -> RepairResult: ...
```

No DB access, no I/O, no side effects. Pure data-in / data-out.

### What this is NOT

- It does **NOT** rewrite session JSONL files on disk. That entire surface (the openclaw `engine.ts` JSONL bootstrap path) drops on Hermes per lcm-source-map §"DROP list" and §"SIMPLIFY list".
- It does **NOT** touch the DB.

## Dependencies

- Depends on: #00-01 (scaffolding only — pure-function module).
- Blocks: Epic 02 (engine assemble calls `repair_transcript` on the assembled message array before returning to Hermes).
- **Parallel-portable** with #01-08, #01-09, #01-12 per storage.md §9 last paragraph.

## Acceptance criteria

- [ ] `repair_transcript(messages, provider="anthropic")` correctly pairs tool_use ↔ tool_result blocks by `tool_use_id`.
- [ ] `repair_transcript(messages, provider="openai")` translates OpenAI tool-call format and pairs correctly.
- [ ] Orphaned tool_use (no matching tool_result) → either dropped or paired with a synthesized placeholder tool_result containing a clear error marker (verify which is the TS behavior in source).
- [ ] Orphaned tool_result (no matching tool_use) → dropped.
- [ ] OpenAI reasoning blocks: a reasoning block embedded inside a tool block is hoisted out and placed in the correct array position.
- [ ] All **3 cases** from `test/transcript-repair.test.ts` (storage.md §8 row 21) ported to `tests/test_transcript_repair.py` — tool-use/tool-result pairing; reasoning placement.
- [ ] **Add parity test:** for a 10-message fixture with mixed Anthropic/OpenAI shapes, run the TS implementation (via subprocess to a small Node harness if Node is installed; skip otherwise) and the Python implementation; assert the repaired message arrays are structurally identical.
- [ ] **Counts test:** `RepairResult.dropped_count`, `synthesized_count`, `repaired_count` are correctly populated and the sum matches the structural diff between input and output messages.
- [ ] `pytest tests/test_transcript_repair.py` passes.
- [ ] `mypy --strict` passes.
- [ ] PR description cites LCM commit `1f07fbd`, `src/transcript-repair.ts`, and `docs/reference/lcm-source-map.md` open-question #5 (the "no JSONL" clarification).

## Estimated effort

**5 hours** — pure logic, well-bounded.

## Confidence

**95%** — pure function, 3 test cases, no DB or async surface. Residual risk: provider-specific shape edge cases (OpenAI reasoning blocks have evolved across model families) — the parity test catches drift.
