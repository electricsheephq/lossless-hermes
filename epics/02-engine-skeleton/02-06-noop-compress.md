---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-02] engine: implement no-op compress(messages, current_tokens, focus_topic) pass-through'
labels: 'port, epic-02'
---

## Source (TypeScript)
- File: `src/engine.ts`
- Lines: 7185–7243 (`compact` public surface — full body lands in Epic 04)
- Function(s)/class(es): `compact` (Epic 02 stubs the surface only; `executeCompactionCore` body comes in Epic 04)

## Target (Python)
- File: `src/lossless_hermes/engine/__init__.py` (ABC override)
- Estimated LOC: ~30 (vs. ~600 for the full Epic-04 version)

## Summary

Implement `ContextEngine.compress(messages, current_tokens, focus_topic) → List[Dict]` as a **no-op pass-through** that returns `messages` unchanged. Per `docs/reference/hermes-hooks.md` line 52, this is called from `run_agent.py:10264-10268` when `should_compress()` returns True.

The real compaction algorithm — `executeCompactionCore`, `compactUntilUnder`, circuit-breaker integration, deferred-debt drain, cache-aware deferral — is **Epic 04**. This epic ships the surface so Hermes can integrate against it, and a passing roundtrip test that proves the message list isn't corrupted.

This is also the safe shipping path for Epic 02 because:
1. Real compaction depends on `compaction.py` (CompactionEngine) which depends on `summarize.py` (LLM client wiring) — both Epic 04.
2. The no-op is contract-correct: returns a valid OpenAI-format message sequence (the same one passed in).
3. Tests for Epic 04 will replace this with the real body in-place.

## Implementation

```python
# src/lossless_hermes/engine/__init__.py

def compress(
    self,
    messages: list[dict],
    current_tokens: int | None = None,
    focus_topic: str | None = None,
) -> list[dict]:
    """No-op pass-through for Epic 02.

    Real implementation lands in Epic 04 — port of engine.ts:7185-7243 (compact)
    plus engine.ts:3344-3528 (executeCompactionCore).

    Per hermes-hooks.md line 52: called from run_agent.py:10264 when
    should_compress() returns True. Must return a valid OpenAI-format
    message sequence.

    Engines that don't support focus_topic fall back via the TypeError-recovery
    at run_agent.py:10265-10268. We accept the kwarg without using it for now.
    """
    if not messages:
        return messages

    logger.debug(
        "[lcm] compress called (no-op): %d messages, current_tokens=%s, focus_topic=%s",
        len(messages), current_tokens, focus_topic,
    )

    # Epic 04: route through _CompactMixin.compress_async / executeCompactionCore.
    # For Epic 02, return the list unchanged. Increment the compression_count
    # so run_agent.py's display stays consistent (per hermes-hooks.md "Required
    # class-level state" — compression_count is read at run_agent.py:10377).
    self.compression_count += 1

    return messages
```

## Dependencies
- Depends on: 02-01 (class shell), 02-02 (`compression_count` field)
- Blocks: Epic 04 (replaces the body with the real compaction algorithm). Does NOT block other Epic 02 issues.

## Acceptance criteria
- [ ] `engine.compress([{"role":"user","content":"hi"}])` returns the exact same list (identity or equal-by-value)
- [ ] `engine.compress([], current_tokens=0)` returns `[]`
- [ ] `engine.compress(messages, focus_topic="quantum")` accepts the kwarg without error
- [ ] After `compress` returns, `engine.compression_count` is incremented by 1
- [ ] Roundtrip test: pass a complex tool-call message list with `{"role":"assistant","tool_calls":[...]}` blocks; assert the return is structurally identical
- [ ] `pytest tests/test_engine_compress_noop.py` passes

## Tests
- `tests/test_engine_compress_noop.py::test_passthrough_simple` — single user message; assert identical return
- `tests/test_engine_compress_noop.py::test_passthrough_empty` — empty list; assert empty return
- `tests/test_engine_compress_noop.py::test_passthrough_tool_calls` — multi-message list with tool_calls + tool_results; assert structurally identical (use `==` on the lists)
- `tests/test_engine_compress_noop.py::test_compression_count_increments` — call N times; assert `compression_count == N`
- `tests/test_engine_compress_noop.py::test_focus_topic_accepted` — pass `focus_topic="x"`; assert no exception
- `tests/test_engine_compress_noop.py::test_current_tokens_accepted` — pass `current_tokens=12345`; assert no exception
- `tests/test_engine_compress_noop.py::test_roundtrip_preserves_message_shape` — pass a real Hermes-shaped message list (fixture); assert byte-level equality on `json.dumps(result) == json.dumps(input)`

## Estimated effort
4 hours

## Confidence
95% — trivial implementation. The only minor decision is incrementing `compression_count` even on no-op (yes, because Hermes reads it for display and a "called but did nothing" still counts). Epic 04 may revisit if real compactions should increment a separate counter from no-op skipped ones.
