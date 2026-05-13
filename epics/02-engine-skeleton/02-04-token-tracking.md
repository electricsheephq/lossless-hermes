---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-02] engine: implement update_from_response(usage) token tracking'
labels: 'port, epic-02'
---

## Source (TypeScript)
- File: `src/engine.ts`
- Lines: ~2620–2710 (`updateCompactionTelemetry` + per-turn token recording in `afterTurn`)
- Function(s)/class(es): cache-aware compaction telemetry recording inside `afterTurn` (engine.ts:6473–6638)

## Target (Python)
- File: `src/lossless_hermes/engine/__init__.py` (the ABC override method)
- Estimated LOC: ~40

## Summary

Implement `ContextEngine.update_from_response(usage: Dict[str, Any]) → None`. Per `docs/reference/hermes-hooks.md` line 50, this is called from `run_agent.py:13046` after every successful LLM API call. The `usage` dict carries `prompt_tokens`, `completion_tokens`, `total_tokens` keys.

This issue does the **minimum required** ABC contract: update `last_prompt_tokens`, `last_completion_tokens`, `last_total_tokens`, recompute `threshold_tokens`. The downstream `compaction_telemetry_store` update (per ADR-015 patch #4 — `cache_read_tokens` / `cache_write_tokens` forwarding) is **noted as future work** for Epic 04 — this epic logs that the cache-aware fields are absent and proceeds with the conservative path.

## Implementation

```python
# src/lossless_hermes/engine/__init__.py

def update_from_response(self, usage: dict[str, Any]) -> None:
    """Per hermes-hooks.md: called from run_agent.py:13046 after every
    successful LLM API call. usage dict has prompt_tokens, completion_tokens,
    total_tokens keys (Hermes-normalized).

    Maps to the per-turn token-recording inside engine.ts:afterTurn
    (lines 6473–6638) — specifically the updateCompactionTelemetry path
    that feeds cache state to the deferral gate.

    Epic 02: just update last_*_tokens + threshold_tokens. The
    compaction_telemetry_store update lands in Epic 04 (depends on
    cache_read_tokens / cache_write_tokens forwarding — ADR-015 patch #4).
    """
    prompt_tokens = (
        usage.get("prompt_tokens")
        or usage.get("input_tokens")
        or 0
    )
    completion_tokens = (
        usage.get("completion_tokens")
        or usage.get("output_tokens")
        or 0
    )

    self.last_prompt_tokens = prompt_tokens
    self.last_completion_tokens = completion_tokens
    self.last_total_tokens = prompt_tokens + completion_tokens

    # threshold_tokens is the trigger for compress(). Recompute every
    # turn because context_length can change via update_model (e.g., model
    # switch at run_agent.py:2728).
    if self.context_length > 0:
        self.threshold_tokens = int(self.context_length * self.threshold_percent)

    # Future (Epic 04 + ADR-015 patch #4): forward cache_read_tokens and
    # cache_write_tokens to compaction_telemetry_store so the cache-aware
    # deferral gate has signal. Without these, the gate degrades to a
    # conservative policy (always-compact-when-over-threshold).
    cache_read = usage.get("cache_read_tokens")
    cache_write = usage.get("cache_write_tokens")
    if cache_read is None and cache_write is None:
        # Default Hermes today does NOT forward these. Log once per session
        # to flag the future-work gap; don't error.
        # (Dedupe via cache_context_unknown_logged — see issue 02-02.)
        pass
    else:
        # Epic 04: pass to compaction_telemetry_store.record(...)
        logger.debug(
            "[lcm] cache tokens received but not yet recorded: read=%s write=%s",
            cache_read, cache_write,
        )
```

## Dependencies
- Depends on: 02-01 (the class shell), 02-02 (state fields including `last_*_tokens`)
- Blocks: 02-05 (`should_compress` reads `threshold_tokens` set here)

## Acceptance criteria
- [ ] `engine.update_from_response({"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200})` sets `last_prompt_tokens=1000`, `last_completion_tokens=200`, `last_total_tokens=1200`
- [ ] `update_from_response({"input_tokens": 500, "output_tokens": 100})` (Anthropic-style keys) also works
- [ ] Calling with `context_length=200000` and `threshold_percent=0.75` produces `threshold_tokens=150000`
- [ ] After `update_model(model="claude-sonnet-4", context_length=200000)`, then `update_from_response({...})`, `threshold_tokens` is recomputed
- [ ] Empty `usage = {}` doesn't raise; all fields go to 0
- [ ] `cache_read_tokens` / `cache_write_tokens` are read (not yet acted on) — ADR-015 patch #4 future-work marker
- [ ] `pytest tests/test_engine_update_from_response.py` passes

## Tests
- `tests/test_engine_update_from_response.py::test_openai_style_keys` — pass `{"prompt_tokens": ..., "completion_tokens": ...}`; assert fields set
- `tests/test_engine_update_from_response.py::test_anthropic_style_keys` — pass `{"input_tokens": ..., "output_tokens": ...}`; assert fields set
- `tests/test_engine_update_from_response.py::test_threshold_recomputed` — set `context_length`, call `update_from_response`, assert `threshold_tokens == int(context_length * threshold_percent)`
- `tests/test_engine_update_from_response.py::test_empty_usage` — pass `{}`; assert no exception, fields go to 0
- `tests/test_engine_update_from_response.py::test_cache_tokens_noted` — pass `{"cache_read_tokens": 500, "cache_write_tokens": 100, ...}`; assert no exception (Epic 04 will validate the telemetry store gets the value)

## Estimated effort
4 hours

## Confidence
95% — direct ABC contract implementation. The cache-token-forwarding question is explicitly noted as future work (ADR-015 patch #4); this issue just receives the keys without acting on them.
