---
patch_id: 001
adr: 010
status: superseded-by-7464
pr_url: https://github.com/NousResearch/hermes-agent/pull/24949
last_checked: 2026-05-14
fallback: N/A — upstream PR #7464 (merged 2026-04-11) made `compress(messages, current_tokens) -> list` the substitution seam; preflight at run_agent.py:11965-12016 calls it pre-API. Our `should_compress` returns True every turn (already wired at engine/compact.py:184) and `compress` returns the assembled list. No "fallback" — this IS the path.
blocks_issues: []
verification: docs/upstream/001a-preassemble-vs-7464-investigation.md
---

> ⚠️ **STATUS: superseded.** PR #7464 (merged 2026-04-11) provides the substitution seam this patch was specified to add. See [`001a-preassemble-vs-7464-investigation.md`](./001a-preassemble-vs-7464-investigation.md) for the verification trail and [issue #60](https://github.com/electricsheephq/lossless-hermes/issues/60) for the cleanup work (ADR-031 supersession + dead-code removal in `engine/assemble.py`). PR #24949 should be closed-as-obsolete on the upstream repo.

# Upstream patch 001 — `ContextEngine.preassemble()`

## Summary

Add an additive, non-breaking ABC method `preassemble(messages, budget) -> list[Message]` to `agent/context_engine.py:ContextEngine` (default no-op pass-through), with a single call site in `run_agent.py` immediately before each model API request.

## Rationale

[Spike 002](../spike-results/002-hermes-pre-llm-call.md) found that Hermes's `pre_llm_call` hook is **append-only** (returns concatenated to the in-flight user message, not a message-list rewrite). LCM's v4.1 "always-on assembly substitution" — replacing evicted raw turns with summary stubs every turn, not just on overflow — needs a per-turn rewrite hook that doesn't exist in Hermes today.

[ADR-010](../adr/010-always-on-assembly.md) documents the chosen path: file an upstream PR adding `preassemble()` to the ABC. ~30 LOC change, additive, default no-op, no behavior change for any existing context engine.

## Why this is acceptable upstream

- **Additive only.** Default no-op. Existing `ContextCompressor` and any other engines are unaffected.
- **Symmetric with existing surface.** `ContextEngine` already has `compress()` and `should_compress()`; `preassemble()` fits the same shape.
- **Precedent.** Hermes issue [#22929](https://github.com/NousResearch/hermes-agent/issues/22929) (filed 2026-05-10) "Wire on_pre_compress into the context compression pipeline for MCP servers" indicates maintainers are actively shipping compression-pipeline extensibility.
- **Low blast radius.** One method add to ABC, one call site in `run_agent.py`, no schema changes, no runtime contract changes.

## Proposed API

```python
# agent/context_engine.py
class ContextEngine(ABC):
    # ... existing methods ...

    def preassemble(
        self,
        messages: list[dict[str, Any]],
        budget_tokens: int | None = None,
    ) -> list[dict[str, Any]]:
        """Per-turn rewrite hook called immediately before each model API request.

        Engines may return a substituted message list (e.g., replace evicted raw
        turns with summary stubs while preserving the assistant/tool-call
        pairing invariant). Default no-op: return messages unchanged.

        Called every turn, regardless of `should_compress()`. Engines that
        only run on overflow should override `compress()` instead.

        Args:
            messages: full in-memory message list at the call boundary
            budget_tokens: target token budget (if known); engines may use
                this to decide how aggressively to substitute

        Returns:
            new message list (may be `messages` unchanged for no-op engines)
        """
        return messages
```

## Call site in `run_agent.py`

```python
# In the API-call pipeline, between pre_llm_call hook and the API request:
messages_for_api = self.context_engine.preassemble(
    messages_for_api,
    budget_tokens=self.context_engine.threshold_tokens,
)
```

## Fallback if rejected

[ADR-010](../adr/010-always-on-assembly.md) Option A: force `should_compress()` to return `True` every turn; do substitution inside `compress()`. Breaks session lineage (rotates SQLite session ID per turn, breaks memory-provider continuity, trips "compressed N times" warnings). Ship gated by `experimental.always_on_via_compress` config flag with explicit non-production-ready warning. Track follow-up as v0.2.0 issue.

## Transition log

- **2026-05-13 — drafted.** Captured here during Wave 0c.
- **2026-05-13 — filed.** PR: https://github.com/NousResearch/hermes-agent/pull/24949
  - +110 / -0 across 3 files (`agent/context_engine.py`, `run_agent.py`, `tests/agent/test_context_engine.py`)
  - 22/22 tests pass (3 new + 19 existing)
  - GitNexus impact analysis: LOW risk, 4 direct callers, 0 affected processes
  - Branch: `100yenadmin/hermes-agent:feat/context-engine-preassemble`
- _(future)_ under_review / accepted / rejected — check weekly via `gh pr view 24949 --repo NousResearch/hermes-agent`
