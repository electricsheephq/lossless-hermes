---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-04] compaction: port 3 anti-thrashing guards'
labels: 'port'
---

## Source (TypeScript)
- File: `lossless-claw/src/compaction.ts` (pr-613 `1f07fbd`)
- Lines:
  - **Guard 1 — per-pass progress** (in `compactFullSweep` phase-1 + phase-2): 705–712 + mirror in phase-2
  - **Guard 2 — `compactUntilUnder` bail-out**: 849
  - **Guard 3 — `summarizeWithEscalation` "didn't compress"**: 1411 + 1422 (in `summarize.ts` via `summarizeWithEscalation`)
- Function(s)/class(es): `CompactionEngine.compactFullSweep`, `CompactionEngine.compactUntilUnder`, `CompactionEngine._summarizeWithEscalation`

## Target (Python)
- File: `src/lossless_hermes/compaction.py`
- Estimated LOC: ~40 (the guards themselves are short; the bulk is the methods they live inside, ported in 04-02 and 04-06)
- This issue is **wiring + assertions** — it does NOT port the host methods (those land in 04-02 / 04-06), only the guard logic + tests.

## Algorithm — the 3 guards

Per `docs/porting-guides/assembler-compaction.md` §"Anti-thrashing logic":

> Three independent guards:
>
> 1. **Per-pass progress** (lines 705–712 in `compactFullSweep` phase-1; mirror in phase-2): `if (passTokensAfter >= passTokensBefore || passTokensAfter >= previousTokens) break;` — stop if a pass didn't make progress relative to either the immediate or the running floor.
> 2. **`compactUntilUnder`** (line 849): `if (!result.actionTaken || result.tokensAfter >= lastTokens) return success:false` — bail out instead of infinite-looping.
> 3. **`summarizeWithEscalation` "didn't compress" guard** (lines 1411 + 1422): if normal output ≥ input, retry aggressive; if aggressive also ≥ input, fall to deterministic.

### Guard 1: per-pass progress

Inside `compact_full_sweep` (both phase-1 and phase-2 loops):

```python
# LCM Wave-12 (2026-04-22): per-pass progress guard prevents thrashing when
# the summarizer returns near-input-size output. Break if pass made no progress
# against either the immediate or running floor.
# Original: lossless-claw/src/compaction.ts:705–712.
if pass_tokens_after >= pass_tokens_before or pass_tokens_after >= previous_tokens:
    break
```

`pass_tokens_before` = token count immediately before this pass started.
`pass_tokens_after` = token count after this pass succeeded.
`previous_tokens` = the running floor across the whole sweep (tracks the best result so far; only updates when a pass makes progress).

### Guard 2: `compact_until_under` bail-out

Inside the `compact_until_under` while loop:

```python
# Anti-thrashing: bail out if a single round made no progress.
# Original: lossless-claw/src/compaction.ts:849.
if not result.action_taken or result.tokens_after >= last_tokens:
    return {"success": False, "rounds": rounds_completed, ...}
```

`last_tokens` is the token count from the previous round (or initial count on round 1).

### Guard 3: summarize-escalation "didn't compress"

Lives inside `_summarize_with_escalation` (issue 04-06):

```python
# Anti-thrashing: if normal mode didn't compress, retry aggressive.
# If aggressive also didn't compress, fall to deterministic.
# Original: lossless-claw/src/summarize.ts:1411 + 1422.
if normal_output_tokens >= input_tokens:
    # ... retry with aggressive mode
if aggressive_output_tokens >= input_tokens:
    # ... fall to deterministic fallback
```

The exact code lives in issue 04-06 (summarize fallback chain). This issue is responsible for the **regression test** that proves the escalation triggers — see Tests below.

## Notes vs Hermes's cross-call guard

The porting guide explicitly notes:

> NOTE: LCM has NO equivalent of Hermes's `_ineffective_compression_count` (the "back off after 2 weak compressions" guard at `context_compressor.py:493`). LCM's anti-thrashing is per-pass progress checks; Hermes's is cross-call. Both are valid; pick one per the ADR below.

Per the porting guide §"ADR: Anti-thrashing semantics — LCM per-pass vs Hermes cross-call" the recommendation is **Option 3: port both**. This issue ports LCM's three. **Defer** Hermes's `_ineffective_compression_count` to a follow-up issue (recommended at engine-skeleton epic or post-port hardening) — it lives at the `compress()` entry point, not inside compaction internals.

If the team decides to port Hermes's guard now (Option 3 of the ADR), open a separate follow-up issue under Epic 02 to add `_ineffective_compression_count` tracking on `LCMEngine._on_post_llm_call`. **Not** in scope here.

## Wave-N fixes to preserve

Per ADR-029 (already in inline comments above):

- Guard 1 ↔ Wave-12 (per-pass progress).
- Guards 2 and 3 are not flagged Wave-N — but they are load-bearing for the same family of failures. Comment them as anti-thrashing intent without a Wave number.

## Dependencies
- Depends on: Issue 04-02 (`compact_full_sweep` and its loop body)
- Depends on: Issue 04-06 (`_summarize_with_escalation` body)
- Blocks: nothing strictly — this issue *encodes* invariants that the rest of the epic must preserve. It's a quality gate, not a sequence dependency.

## Acceptance criteria
- [ ] Guard 1 present in `compact_full_sweep` phase-1 loop with Wave-12 inline comment + TS line citation
- [ ] Guard 1 present in `compact_full_sweep` phase-2 loop (condensation) with the same inline comment pattern
- [ ] Guard 2 present in `compact_until_under` with TS line citation
- [ ] Guard 3 (`_summarize_with_escalation` escalation cascade) present in `summarize.py` per issue 04-06 spec
- [ ] All 3 guards have a passing regression test (see Tests below)
- [ ] `grep -rn "Wave-12" src/lossless_hermes/compaction.py` finds at least 2 hits (phase-1 + phase-2)
- [ ] PR description cites LCM commit SHA `1f07fbd` and notes which 3 guards are covered

## Tests

Three regression tests — each must FAIL without the guard and PASS with it:

### Test 1: per-pass progress guard

```python
async def test_compact_full_sweep_breaks_on_zero_progress():
    # Mock summarizer returns near-input-size output → no token reduction
    # Without guard 1: infinite loop or max_rounds exhaustion
    # With guard 1: break after pass-1, return after pass-2
    engine = make_engine_with_pseudo_summarizer(reduction_ratio=1.0)  # 100% = no compression
    result = await engine.compact_full_sweep(conv_id, token_budget=10_000)
    assert result.rounds_completed < engine.config.max_rounds  # broke early
```

### Test 2: `compact_until_under` bail-out

```python
async def test_compact_until_under_bails_on_no_action():
    # Setup: no eligible chunks → first call returns action_taken=False
    # Without guard 2: infinite loop
    # With guard 2: returns success=False on round 1
    engine = make_engine_with_empty_context(conv_id)
    result = await engine.compact_until_under(conv_id, target_tokens=1000)
    assert result["success"] is False
    assert result["rounds"] == 1
```

### Test 3: summarize-escalation didn't-compress

```python
async def test_summarize_escalation_falls_to_deterministic():
    # Mock LLM returns 2× input size for both normal AND aggressive
    # With guard 3: escalation cascade hits deterministic fallback
    # Without guard 3: returns the bloated aggressive output
    bloated_llm = MockLlm(output_ratio=2.0)
    summary = await summarizer.summarize("source text " * 100, options={})
    assert summary.startswith("[LCM fallback summary")  # deterministic marker present
```

(Test 3 lives in `tests/test_summarize.py` since the guard is inside `_summarize_with_escalation`.)

## Estimated effort
4–6 hours

## Confidence
95% — the guards are ~6 LOC each. Risk is concentrated in writing tests that genuinely fail without the guard (a flaky mock that "would have worked anyway" doesn't prove the guard).
