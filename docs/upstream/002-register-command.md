---
patch_id: 002
adr: 015
status: drafted
pr_url: null
last_checked: 2026-05-13
fallback: Ship lossless-hermes as an entry-point plugin (NOT directory-mode); full PluginContext is wired by that path
blocks_issues: []
---

# Upstream patch 002 — `_EngineCollector.register_command` forwarding

## Summary

Add `register_command` (and `register_hook`, `register_cli_command`, `register_memory_provider`) forwarding to the stub `_EngineCollector` at `plugins/context_engine/__init__.py:208–219` so that directory-mode context engine plugins can register slash commands and hooks (today these are silently no-op'd).

## Rationale

Directory-mode loading is a documented Hermes plugin pattern but the `_EngineCollector` only forwards `register_context_engine` — every other plugin-context method is a silent stub. Operator-author confusion: a directory-mode plugin that calls `ctx.register_command(...)` looks like it succeeded but nothing happens.

We don't need this patch for lossless-hermes itself — [ADR-001](../adr/001-plugin-distribution-model.md) chose the entry-point distribution model precisely because it gets the full `PluginContext`. But adding the forwarding is a 15-LOC quality-of-life improvement for all future directory-mode plugins.

## Proposed change

```python
# plugins/context_engine/__init__.py
class _EngineCollector:
    """..."""
    def __init__(self, plugin_manager: PluginManager):
        self._plugin_manager = plugin_manager

    def register_context_engine(self, engine):
        # ... existing impl ...

    # NEW: forward to the real plugin manager
    def register_command(self, name, handler, args_hint=None, **kwargs):
        return self._plugin_manager.register_command(name, handler, args_hint=args_hint, **kwargs)

    def register_hook(self, hook_name, handler):
        return self._plugin_manager.register_hook(hook_name, handler)

    def register_cli_command(self, name, handler):
        return self._plugin_manager.register_cli_command(name, handler)

    def register_tool(self, name, toolset, schema, handler, **kwargs):
        return self._plugin_manager.register_tool(name, toolset, schema, handler, **kwargs)
```

(Verify the exact signatures of the underlying `PluginManager.register_*` methods before filing.)

## Why this is acceptable upstream

- **Pure quality-of-life fix.** No behavior change for existing plugins; new behavior for any directory-mode plugin that uses these methods.
- **Trivial size.** ~15 LOC.
- **Documented confusion.** [Spike 002](../spike-results/002-hermes-pre-llm-call.md) and [Hermes hooks reference](../reference/hermes-hooks.md) both call this out as a stumble for plugin authors.

## Fallback if rejected

Not blocking. Lossless-hermes is distributed as an entry-point plugin per [ADR-001](../adr/001-plugin-distribution-model.md), which uses the full `PluginContext` directly. The directory-mode stub bug remains for other plugin authors but doesn't affect us.

## Transition log

- **2026-05-13 — drafted.**
- _(future)_ filed → under_review → accepted/rejected
