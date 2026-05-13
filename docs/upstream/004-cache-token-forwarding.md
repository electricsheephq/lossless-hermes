---
patch_id: 004
adr: 015
status: drafted
pr_url: null
last_checked: 2026-05-13
fallback: LCM's cache-aware compaction degrades gracefully — `cacheContextUnknownLogged` path disables deferral when cache state is unknown
blocks_issues: []
---

# Upstream patch 004 — cache-token forwarding in `update_from_response(usage)`

## Summary

Extend `update_from_response(usage)` to forward `cache_read_tokens` and `cache_write_tokens` keys when the provider's response exposes them (Anthropic and OpenAI both do today). ~5 LOC in `run_agent.py` where the `usage` dict is built before being passed to the context engine.

## Rationale

LCM v4.1's compaction has a cache-aware deferral path: when the model's prompt cache is hot (high `cache_read_tokens`), LCM defers compaction to preserve the cache hit. When cold or unknown, it compacts normally. Hermes currently does not forward these fields to the context engine — LCM has a graceful-degrade path (`cacheContextUnknownLogged`) but the optimization is disabled.

## Proposed change

In `run_agent.py` where the `usage` dict is constructed for `update_from_response()`:

```python
# When provider response carries Anthropic-style cache fields:
usage = {
    "prompt_tokens": response.usage.input_tokens,
    "completion_tokens": response.usage.output_tokens,
    "total_tokens": ...,
    # NEW:
    "cache_read_tokens": getattr(response.usage, "cache_read_input_tokens", 0),
    "cache_write_tokens": getattr(response.usage, "cache_creation_input_tokens", 0),
}
self.context_engine.update_from_response(usage)
```

(Similar shape for OpenAI's `prompt_tokens_details.cached_tokens`.)

## Why this is acceptable upstream

- **Forward-only.** Existing engines that don't use the keys ignore them. `ContextCompressor` reads only `prompt_tokens` and `total_tokens`.
- **Already in provider responses.** Both Anthropic and OpenAI emit these; Hermes just isn't propagating.
- **Low blast radius.** No new methods, no signature changes — just additional keys on an existing dict.

## Fallback if rejected

LCM's `cache_aware_deferral` is disabled when fields are absent (existing graceful-degrade path). No data loss, no incorrect behavior — just a missed optimization opportunity.

## Transition log

- **2026-05-13 — drafted.**
- _(future)_ filed → under_review → accepted/rejected
