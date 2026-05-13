---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-02] engine: implement should_compress(prompt_tokens) with anti-thrashing back-off'
labels: 'port, epic-02'
---

## Source (TypeScript)
- File: `src/engine.ts`
- Lines:
  - `evaluateIncrementalCompaction` (2824–3002) — full decision logic
  - `isUnderCriticalBudgetPressure` (~3260)
  - `cacheAwareCompaction` config + telemetry gates throughout
- Function(s)/class(es): `evaluateIncrementalCompaction`, `should_compress` shim glue

## Target (Python)
- File: `src/lossless_hermes/engine/__init__.py` (ABC override)
- Estimated LOC: ~60 (vs. ~180 in TS, full state machine; Epic 02 just ships the threshold gate + back-off; Epic 04 grows it into the full state machine)

## Summary

Implement `ContextEngine.should_compress(prompt_tokens: int = None) → bool`. Per `docs/reference/hermes-hooks.md` line 51, called from `run_agent.py:14841` after each turn's API call. When `True`, fires `compress(messages, current_tokens=..., focus_topic=...)`.

For Epic 02, ship the **conventional threshold gate**: `prompt_tokens >= self.threshold_tokens`. Add **anti-thrashing back-off**: if a recent `compress()` call returned the same message-list shape (no actual compaction happened — Epic 04 will distinguish), back off for N turns to avoid hot-loop thrashing.

The full `evaluateIncrementalCompaction` state machine (cache state, retention, observations, leaf-trigger evaluator) is **Epic 04**. This issue lays the surface and a simple back-off counter.

**Note:** ADR-010 (always-on assembly) recommends `should_compress() → False` always for LCM IF the upstream `preassemble` hook lands (ADR-015 patch #1). For Epic 02, since `preassemble` is not yet wired, `should_compress` uses the conventional threshold path. Epic 03 / 04 will revisit once spike 002 results land.

## Implementation

```python
# src/lossless_hermes/engine/__init__.py

# State field (add to _init_state_fields in issue 02-02)
# self._compress_backoff_counter: int = 0
# self._compress_backoff_floor: int = 0  # how many more turns to back off

def should_compress(self, prompt_tokens: int | None = None) -> bool:
    """Per hermes-hooks.md: called from run_agent.py:14841 after each API call.
    Returns True → fires _compress_context which calls self.compress(...).

    Maps to engine.ts:evaluateIncrementalCompaction (2824–3002) — the full
    state machine lands in Epic 04. Epic 02 ships the threshold gate + a
    simple back-off counter.

    ADR-010 Note: once preassemble lands (upstream patch #1), should_compress
    becomes False for LCM (compaction runs via the always-on hook + deferred
    debt queue, not the threshold gate). For Epic 02 we use the conventional
    threshold gate.
    """
    observed = prompt_tokens if prompt_tokens is not None else self.last_prompt_tokens

    # Anti-thrashing back-off — don't fire compress() on consecutive turns
    # when the previous compress was a no-op (Epic 02: every compress is a
    # no-op pass-through; back-off keeps the loop sane).
    if self._compress_backoff_floor > 0:
        self._compress_backoff_floor -= 1
        return False

    # Conventional threshold gate
    if self.threshold_tokens > 0 and observed >= self.threshold_tokens:
        # Engage back-off for next BACKOFF_TURNS turns. Epic 04 replaces this
        # with the leaf-trigger + budget-trigger decision logic, which
        # already has its own anti-thrashing semantics (cache state, activity
        # band, etc.).
        self._compress_backoff_floor = BACKOFF_TURNS_AFTER_COMPRESS
        return True

    return False
```

Constants (`src/lossless_hermes/engine/__init__.py`):

```python
# Wave-3 P1 fix in TS source — prevent compress thrashing when the algorithm
# can't bring tokens below threshold in one pass.
BACKOFF_TURNS_AFTER_COMPRESS = 3  # tunable; matches TS default
```

## Dependencies
- Depends on: 02-04 (`threshold_tokens` is set by `update_from_response`), 02-02 (`_compress_backoff_floor` state field — add to the init list in 02-02)
- Blocks: 02-06 (the no-op `compress` body that this triggers); Epic 04 (replaces this with full state machine)

## Acceptance criteria
- [ ] `should_compress(prompt_tokens=100000)` returns `True` when `threshold_tokens=80000`
- [ ] `should_compress(prompt_tokens=50000)` returns `False` when `threshold_tokens=80000`
- [ ] `should_compress()` (no arg) falls back to `self.last_prompt_tokens`
- [ ] After `should_compress` returns `True`, the next `BACKOFF_TURNS_AFTER_COMPRESS` calls return `False` even at over-threshold prompt_tokens
- [ ] `should_compress` with `threshold_tokens == 0` (never set) returns `False` (no compression before `update_model` fires)
- [ ] `pytest tests/test_engine_should_compress.py` passes

## Tests
- `tests/test_engine_should_compress.py::test_threshold_trigger` — set threshold=80k, call with 100k, assert True
- `tests/test_engine_should_compress.py::test_under_threshold` — assert False
- `tests/test_engine_should_compress.py::test_default_uses_last_prompt_tokens` — set `engine.last_prompt_tokens=100k`, threshold=80k, call `should_compress()` no-arg, assert True
- `tests/test_engine_should_compress.py::test_backoff_after_trigger` — call once returning True, then 3 calls all return False even at over-threshold tokens; 4th call returns True
- `tests/test_engine_should_compress.py::test_no_threshold_no_compress` — threshold=0, call with 100k, assert False (avoid never-was-set bug)
- `tests/test_engine_should_compress.py::test_lcm_always_on_future_path` — placeholder test marked `@pytest.mark.skip(reason="ADR-010 always-on assembly — Epic 03/04")` documenting the future behavior

## Estimated effort
6 hours

## Confidence
90% — the threshold gate is trivial. The back-off counter design is a minor decision (3 turns default, tunable in Epic 04). The ADR-010 always-on path is explicitly deferred to Epic 03/04 with a skip-marked test as the contract reminder.
