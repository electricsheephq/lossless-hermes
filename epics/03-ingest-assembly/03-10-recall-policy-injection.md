---
name: Port issue
about: `pre_llm_call` hook returns LOSSLESS_RECALL_POLICY_PROMPT per ADR-014
title: '[epic-03] plugin: implement pre_llm_call recall-policy injection'
labels: 'port'
---

## Source (TypeScript)
- File: `src/plugin/index.ts` (`pr-613` HEAD `1f07fbd`)
- Lines: search for `LOSSLESS_RECALL_POLICY_PROMPT` constant + the `before_prompt_build` registration.
- Function(s)/class(es): the TS plugin's `before_prompt_build` callback returning `{prependSystemContext: LOSSLESS_RECALL_POLICY_PROMPT}`.

## Target (Python)
- File: `src/lossless_hermes/plugin/__init__.py` (entry-point register function) + the policy text content.
- File: `src/lossless_hermes/plugin/recall_policy.py` (the reworded prompt text — keep it as its own module so reviewers can diff against the TS original).
- Estimated LOC: ~50 in the plugin registration + ~3 KB of constant text in `recall_policy.py`.

## Background

Per **ADR-014** (status: Accepted, 85% confidence):

> Chosen: **Option A — inject into USER message via `pre_llm_call`**

Hermes routes `pre_llm_call` return values to the **user message** (not system prompt) to preserve Anthropic prompt cache. The TS-source policy text was written for system-prompt voice ("You MUST use lcm_grep..."); it needs minor rewording to read naturally as a user-message preamble ("When working with this conversation, the lcm_grep tool...").

## Implementation

### Plugin entry point

```python
# src/lossless_hermes/plugin/__init__.py
def register(ctx):
    """Plugin entry point per ADR-001.

    Registers:
      - The LCMEngine via ctx.register_context_engine(...)
      - The pre_llm_call hook for recall-policy injection (this issue)
      - The post_llm_call hook for ingest (issue 03-02)
      - The /lcm slash commands (Epic 08)
    """
    from lossless_hermes.engine import LCMEngine
    from lossless_hermes.plugin.recall_policy import LOSSLESS_RECALL_POLICY_PROMPT

    engine = LCMEngine(hermes_home=ctx.hermes_home, config=ctx.config)
    ctx.register_context_engine(engine)

    def _on_pre_llm_call(session_id, user_message, conversation_history, is_first_turn, model, platform, **kwargs):
        """Returns the LCM recall-policy text as user-message context.

        Per ADR-014 / spike 002: Hermes appends pre_llm_call return values to
        the current turn's user-message content at API-call time. Caches well
        when Hermes attaches `cache_control` to user messages.
        """
        return {"context": LOSSLESS_RECALL_POLICY_PROMPT}

    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("post_llm_call", engine._on_post_llm_call)
    # ... command + other-hook registrations ...
```

### Rewording the policy text

The TS source text is the ground truth for **what** the policy says. The Python port preserves every semantic instruction; only the voice shifts.

Each TS instruction is paired with a Python-port equivalent in `recall_policy.py`. Examples:

| TS (system voice) | Python (user-preamble voice) |
|---|---|
| `You MUST use lcm_grep BEFORE answering questions about prior conversation context.` | `Before answering questions about prior conversation context, use lcm_grep to search.` |
| `You SHOULD prefer lcm_describe over re-reading raw tool output when a file_xxx reference appears.` | `When a file_xxx reference appears, prefer lcm_describe over re-reading the raw tool output.` |
| `When you encounter a summary marker, you MUST NOT treat it as authoritative; call lcm_expand for the source.` | `Summary markers are NOT authoritative. Use lcm_expand to fetch the source when accuracy matters.` |

Apply the same transform across the full ~3 KB text. **Mandatory side-by-side review** during PR: post both versions in the PR description and have a reviewer scan for unintended semantic drift.

## Cache-hit measurement (Phase 2 follow-up)

ADR-014 §"Open questions" item 1:

> Phase 2 instrumentation: run with/without the lossless-hermes hook, measure cache hits via the `usage.cache_read_tokens` field in Anthropic responses (which Hermes may or may not forward — see ADR-015 patch #4).

This issue does NOT include the cache measurement — it ships the injection mechanism and the reworded text. Phase 2 adds the benchmark.

## Verification metric (per ADR-014 §"Consequences")

A regression test asserts that the policy text appears in the **user-message content** of the API call shape (not the system prompt). If a future Hermes change routes `pre_llm_call` returns to the system prompt, this test fails — signaling the ADR's premise is broken.

## Dependencies
- Depends on: Epic 02 (`hermes_bridge.py` with `register_hook` shape verified to forward `pre_llm_call` returns to the user-message content path).
- Blocks: nothing strictly — the policy is additive; the engine works without it (lower-quality output but no crash).

## Acceptance criteria

- [ ] `LOSSLESS_RECALL_POLICY_PROMPT` constant lives in `src/lossless_hermes/plugin/recall_policy.py`.
- [ ] Reworded text preserves every behavioral instruction from the TS source — verified by line-by-line side-by-side review in the PR description.
- [ ] `_on_pre_llm_call` returns `{"context": LOSSLESS_RECALL_POLICY_PROMPT}` (or the plain string — pick one and stay consistent).
- [ ] Hook is registered via `ctx.register_hook("pre_llm_call", ...)` and fires per turn.
- [ ] A test mounts the plugin, simulates a turn, and asserts the policy text appears in the API-call user-message content (NOT the system prompt). Mocks `pre_llm_call` invocation site to inspect the joined `_plugin_user_context`.
- [ ] Test asserts the policy text is NOT prepended to the system prompt (would break cache invariant per ADR-014).
- [ ] If a future Hermes ABC change moves `pre_llm_call` returns into the system prompt, the test fails. Document this as an intentional regression-detection invariant.
- [ ] Empty / disabled state: if the plugin is loaded but disabled via config, the hook is NOT registered (no policy injection).
- [ ] No regression in `pytest tests/test_plugin_registration.py` or any other plugin-level test.
- [ ] `pytest tests/test_recall_policy.py` passes locally + on GitHub CI.
- [ ] No new mypy errors.
- [ ] PR description includes side-by-side diff of TS source policy vs Python reworded text, with reviewer sign-off on semantic preservation.
- [ ] PR description cites the LCM commit SHA being ported.

## Tests

- Hook fires on every turn (verified by invocation count after N turns).
- Policy text appears in the user-message content at API-call site.
- Policy text does NOT appear in the system prompt.
- Disabled mode: hook not registered, no injection.
- First-turn vs subsequent-turn — both inject (per spike 002 line 38: "All non-None returns are concatenated").
- Multiple plugins returning context: ordering preserved, `\n\n` separator used.
- Token-cost telemetry: record the policy-text length so Phase 2 cache benchmark can compute incremental cost.

## Estimated effort
**6 hours**. The mechanism is trivial; the time goes into the rewording pass + side-by-side review.

## Confidence
**90%**. Mechanism is settled. Residual risk is the rewording pass introducing unintended semantic drift — mitigated by mandatory side-by-side review. Cache-hit measurement is deferred to Phase 2 per ADR-014, so this issue is not gated on a benchmark.
