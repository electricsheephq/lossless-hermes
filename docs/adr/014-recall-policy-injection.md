# ADR-014: Recall-policy injection point

**Status:** Accepted
**Date:** 2026-05-13
**Confidence:** 85%
**Supersedes:** —
**Superseded by:** —

## Context

OpenClaw LCM injects a ~3 KB `LOSSLESS_RECALL_POLICY_PROMPT` at the start of every conversation. The policy text instructs the agent on how to use `lcm_*` tools — when to call `lcm_grep`, when to call `lcm_expand`, how to interpret summary markers, etc. (TS: `src/plugin/index.ts:LOSSLESS_RECALL_POLICY_PROMPT`).

In OpenClaw, the injection mechanism is `before_prompt_build` returning `{ prependSystemContext: LOSSLESS_RECALL_POLICY_PROMPT }`. **OpenClaw prepends the text to the system prompt.**

Hermes deliberately diverges:

- `pre_llm_call` is the equivalent hook. Plugin return values (dicts with `context` key, or plain strings) are **appended to the user message at API-call time** (`run_agent.py:12266–12277`).
- The system prompt is reserved for Hermes (`docs/reference/hermes-hooks.md:91` and `docs/spike-results/002-hermes-pre-llm-call.md:39`).
- The reason this divergence exists: **preserve Anthropic prompt cache**. The system prompt + tools form the cache prefix. Mutating the system prompt every turn invalidates the cache and balloons latency / cost.

Plugin-glue.md (`docs/porting-guides/plugin-glue.md:367–370`) explicitly flags this as a CRITICAL DIVERGENCE: "OpenClaw prepends to SYSTEM; Hermes injects to USER (see hermes_cli/plugins.py:invoke_hook docstring — 'Context is ALWAYS injected into the user message, never the system prompt' to preserve prompt cache). The policy text needs minor rewording so it still reads correctly as a user-message preamble."

The constraint forcing a choice: where should the recall-policy text live in the prompt structure, and what's the cache-vs-correctness trade-off?

## Options considered

### Option A: Inject into the USER message via `pre_llm_call` (Hermes-conventional)

- **Description:** Register `pre_llm_call`. Return `{"context": LOSSLESS_RECALL_POLICY_PROMPT}` or the plain string. Hermes appends it to the current turn's user message at API call time. System prompt remains stable and cache-able.
- **Pros:** Preserves prompt cache. Idiomatic to Hermes. Spike 002 confirmed the injection mechanics (line 39: "All non-None returns are concatenated with `\n\n` and **appended to the current turn's user-message content**"). Zero upstream changes.
- **Cons:** Tiny model-side semantic difference — the policy text appears as user-message content, not system-prompt content. Some models weight these slightly differently. Adds ~3 KB to every user turn (vs. once in the cached system prompt). The user-message position also means the policy is visible to the user (e.g., in /messages dump), which is mostly cosmetic but worth noting.
- **Evidence:** `docs/porting-guides/plugin-glue.md:367–370`; `docs/spike-results/002-hermes-pre-llm-call.md:38–39`; `docs/reference/hermes-hooks.md:91`.

### Option B: Propose `pre_system_prompt_build` upstream hook

- **Description:** Open a Hermes upstream PR adding a hook that allows plugins to prepend to the system prompt at session start (NOT every turn — only once, at the boundary where the cache prefix can rebuild without per-turn cost).
- **Pros:** Behavioral parity with OpenClaw. Policy text lives in system prompt position.
- **Cons:** Requires upstream Hermes PR. Adds a NEW hook to `VALID_HOOKS`. Hermes maintainer may push back — the system-prompt-is-read-only invariant is intentional. Even if accepted, mutating the cache prefix at session start invalidates the cache for THAT session — not as bad as every turn but still real cost. Unclear whether Hermes's cache implementation tracks "stable post-init system prompt" or "fully-stable system prompt" — first session turn may always pay the cache cost. Net cache savings unclear.
- **Evidence:** `docs/porting-guides/plugin-glue.md:686–687` (mentions but does not recommend); `docs/spike-results/002-hermes-pre-llm-call.md:21` (confirms no such hook exists).

### Option C: Don't inject anywhere — rely on tool descriptions

- **Description:** Remove the policy prompt entirely. The 8 LCM tools have well-documented descriptions in their schemas — the model learns when to use them from the schema, not from a top-of-prompt block.
- **Pros:** Zero injection cost. Smallest surface.
- **Cons:** OpenClaw operators have tuned the policy prompt over multiple LCM versions. Tool descriptions don't carry the same cross-tool orchestration guidance (e.g., "Always try lcm_grep BEFORE lcm_describe for unknown terms"). Quality regression vs. OpenClaw.
- **Evidence:** No formal evidence; this is the strawman option.

## Decision

Chosen: **Option A — inject into USER message via `pre_llm_call`**

Register a `pre_llm_call` hook on `PluginContext`. The hook returns `{"context": LOSSLESS_RECALL_POLICY_PROMPT}` and Hermes appends the policy text to the current turn's user message.

Reword the policy text slightly so it reads naturally as a user-message preamble rather than a system instruction. (E.g., shift from "You MUST use lcm_grep..." to "When working with this conversation, the lcm_grep tool..." — preserve all semantic content, just shift voice.)

## Rationale

Plugin-glue.md (`docs/porting-guides/plugin-glue.md:367–370`) explicitly flagged this as a prompt-cache regression risk if injected into the system prompt. The Hermes convention "Context is ALWAYS injected into the user message, never the system prompt" exists specifically to preserve prompt cache — this is a deliberate Hermes design choice, not an oversight.

Spike 002 (`docs/spike-results/002-hermes-pre-llm-call.md:38–39`) confirms `pre_llm_call` is the documented mechanism for context injection and that its return value is concatenated into the current turn's user message. The contract is stable.

The cache preservation is load-bearing. Anthropic prompt cache TTL is 5 minutes; a stable system prompt across turns saves substantial latency and cost (~3-5x for cache hits per Anthropic docs). Mutating the system prompt every turn flushes the cache, every turn — at ~3 KB of policy text plus all of Hermes's own system prompt + tool schemas, the per-turn cache miss is substantial.

The user-position injection costs per-turn duplication of ~3 KB (since each turn carries the policy preamble in its user-message content). With caching turned ON for user-side content (which Anthropic supports — `cache_control` blocks), this can be cache-aware too. Net: similar caching properties to system-prompt position, with the architectural benefit of NOT mutating system prompt.

## Consequences

- **Policy text is appended to the user message** on every turn (not the system prompt). Visible in `/messages` dump as user-message content.
- **Cache preserved.** System prompt + tools remain stable across turns. The user-message cache prefix (if enabled by Hermes config) carries the policy text.
- **Slight model-side voice difference.** Policy appears as user-said-this rather than system-said-this. Models generally follow both fine, but the placement is observable.
- **Reword required.** The policy text needs minor voice adjustment to read as a user preamble. This is a one-time edit during port.
- **Verification metric.** Phase 2 should benchmark cache-hit rate before/after the lossless-hermes plugin is enabled, to confirm the assumption that user-message injection doesn't tank cache. Run with both Anthropic caching enabled and disabled; compare turn latency.
- **No upstream changes.** Zero risk of upstream rejection. v1 ships without external dependencies on Hermes core.
- **`pre_llm_call` is hooked alongside `post_llm_call`** (ADR-009 ingest hook). Both fire per-turn; both share the engine instance.

## Open questions / 5% uncertainty

- **Cache-hit measurement.** Engine.md (`docs/porting-guides/engine.md:559–562`) flags this for verification: "Spike 002 must measure cache-hit rate under always-on assembly on a real Hermes session before we commit to the contract." Phase 2 instrumentation: run with/without the lossless-hermes hook, measure cache hits via the `usage.cache_read_tokens` field in Anthropic responses (which Hermes may or may not forward — see ADR-015 patch #4).
- **User-position cache-control.** Whether Hermes attaches `cache_control` to user messages is itself a Hermes-side choice. If not, per-turn duplication of the policy text is a real (small) cost. Investigate during cache benchmarking.
- **Token cost over a long session.** 3 KB × N turns vs. 3 KB × 1 (system prompt) — over 200 turns, the user-injection path adds substantial token volume. Mitigation: Anthropic cache hits make this near-free. If cache hit rate proves low, revisit Option B.
- **Reword scope.** The reword must preserve all behavioral guidance for the model. Risk of unintended semantic drift. Mitigation: side-by-side review with original prompt; eval-suite regression test if available.
- **Future Hermes change.** If a future Hermes version routes `pre_llm_call` returns to the system prompt instead, this ADR's premise breaks. Mitigation: lock the contract via a test that verifies the policy text appears in user-message content (not system).
