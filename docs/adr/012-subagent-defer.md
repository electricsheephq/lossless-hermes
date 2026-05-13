# ADR-012: Subagent prepareSubagentSpawn — drop or defer

**Status:** Accepted
**Date:** 2026-05-13
**Confidence:** 80%
**Supersedes:** —
**Superseded by:** —

## Context

OpenClaw LCM exposes two subagent-lifecycle methods on the `ContextEngine` interface (TS `src/engine.ts:7245–7341`):

- `prepareSubagentSpawn({ parentSessionKey, childSessionKey, ttlMs? }) → SubagentSpawnPreparation | undefined` — sets up an in-process **delegated expansion grant** so the child agent can call `lcm_expand` / `lcm_grep` against the parent's conversation under a token-cap and max-depth restriction.
- `onSubagentEnded({ childSessionKey, reason: "deleted"|"completed"|"swept"|"released" }) → void` — tears the grant down.

The grant manager (`expansion-auth.ts`) is the backbone of LCM's subagent context-sharing. Without it, a child agent calling `lcm_expand` finds no parent conversation and either fails or returns empty.

Hermes's subagent model is different. The relevant tool is `delegate_task` (TS subagent equivalent: TASK tool):

- **Hermes:** `delegate_task` is a **model-callable tool** (`tools/delegate_tool.py`). The model decides to spawn a child; Hermes does NOT expose a plugin-initiated spawn API.
- **OpenClaw:** plugins could initiate spawns programmatically.

LCM's `lcm_expand_query` tool — the convenience wrapper that auto-dispatches a sub-agent to run a focused expansion — was specifically designed against OpenClaw's plugin-initiated spawn API. Porting it to Hermes requires redesigning it as a `delegate_task`-callable tool, which means:

- The model would call `lcm_expand_query` directly (not via plugin code).
- The grant management would need to fire on Hermes's `subagent_stop` hook (`tools/delegate_tool.py:2248`) rather than `onSubagentEnded`.
- The cross-agent state-sharing pattern (parent's `LCMEngine` instance, in-memory grant table) needs verification under Hermes's single-process multi-agent model.

The constraint forcing a choice: porting `prepareSubagentSpawn` + redesigning `lcm_expand_query` is significant work for a v1 release that already has 30%+ of port effort allocated to other tool surfaces (`docs/porting-guides/tools.md` notes this as the highest-risk single port).

## Options considered

### Option A: Full port — redesign `lcm_expand_query` against `delegate_task`

- **Description:** In v1, reimplement `prepareSubagentSpawn` using Hermes's `subagent_stop` hook plus a new in-engine grant table. Redesign `lcm_expand_query` as a `delegate_task`-callable wrapper.
- **Pros:** Behavioral parity with OpenClaw LCM. Main agents retain the convenience wrapper.
- **Cons:** Significant engineering work. High risk per tools.md (~30% of total tool-port effort). v1 timeline impact. The Hermes `delegate_task` model differs enough that the redesign isn't a translation, it's a re-architecture.
- **Evidence:** `docs/porting-guides/engine.md:194–197` (Option 2: DEFER); tools.md (referenced) flags this as highest risk.

### Option B: Drop entirely from v1 — never port

- **Description:** Don't ship `prepareSubagentSpawn`, `onSubagentEnded`, or `lcm_expand_query` in v1 or any subsequent release. Subagents in lossless-hermes have no LCM context-sharing, ever.
- **Pros:** Smallest port surface.
- **Cons:** Eliminates a real LCM win (subagent expansion against parent context). Closes the door on a feature OpenClaw users rely on.
- **Evidence:** Engine.md (`docs/porting-guides/engine.md:195`, Option 1: DROP).

### Option C: Defer to v2 — ship without subagent context sharing in v1

- **Description:** v1 ships without `prepareSubagentSpawn` / `onSubagentEnded` / `lcm_expand_query`. The `lcm_expand` tool still works (it's the primitive that `lcm_expand_query` wraps), but only via direct invocation from the main agent. Subagents can still call `lcm_grep` / `lcm_describe` if they receive a token grant out-of-band (this is OUT OF SCOPE for v1 — meaning subagents simply can't access parent context). v2 revisits with full redesign.
- **Pros:** Unblocks v1 ship. Preserves `lcm_expand` (the primitive) for the main agent. Buys time to validate the `delegate_task` redesign with real Hermes subagent usage patterns. Sub-agent-as-tool calling pattern can be added incrementally.
- **Cons:** Main agent loses the `lcm_expand_query` convenience wrapper. Documented gap from OpenClaw parity.
- **Evidence:** `docs/porting-guides/engine.md:195–197` ("Option 2 — DEFER: implement after lcm_expand ports. Hermes subagents call back to the parent's LCMEngine instance (single-process, shared via plugin registry).")

## Decision

Chosen: **Option C — defer to v2**

v1 ships without `prepareSubagentSpawn`, `onSubagentEnded`, or `lcm_expand_query`. The remaining 7 LCM tools (`lcm_grep`, `lcm_describe`, `lcm_expand`, `lcm_synthesize_around`, `lcm_get_entity`, `lcm_search_entities`, `lcm_compact`) all ship in v1. v2 revisits subagent context-sharing with a `delegate_task`-aligned redesign.

## Rationale

Engine.md (`docs/porting-guides/engine.md:194`) explicitly notes Hermes has `delegate_task` (model-callable) not plugin-initiated spawn. tools.md flags `lcm_expand_query` as the highest-risk single port (~30% of tool effort). Deferring this single subsystem unblocks the rest of the tool surface and lets v1 ship.

The `lcm_expand` primitive (which `lcm_expand_query` wraps) is independent of the spawn-lifecycle work. It can ship in v1 as a main-agent tool. Sub-agents that need expansion can be added in v2 once the `delegate_task` integration model is validated against real workloads.

Engine.md's `prepareSubagentSpawn` section (line 198): "**Confidence: 50%** — depends entirely on whether expansion tools ship in v1." We resolve this uncertainty by choosing: expansion tools DO ship in v1, but the subagent-spawn lifecycle does NOT. The two are decoupled.

## Consequences

- **`lcm_expand_query` ships in v2, not v1.** Main agents lose the convenience wrapper that auto-dispatches a sub-agent.
- **`lcm_expand` (primitive) ships in v1.** Main agents can still expand individual context items directly.
- **No `prepareSubagentSpawn` / `onSubagentEnded` methods on `LCMEngine`.** The ContextEngine ABC doesn't define these (they're LCM-specific), so omitting them in v1 is API-clean.
- **`expansion-auth.ts` and the runtime grant manager** do not port in v1. ~400 LOC drops.
- **Subagents called from a parent LCM-aware session lose access to LCM tools** unless explicitly granted. Document this gap in the v1 release notes.
- **`subagent_stop` hook** (`tools/delegate_tool.py:2248`) — LCM does NOT register this in v1. v2 will register it for grant teardown.
- **The `child_session_id` mapping** that OpenClaw maintained for delegated grants is not maintained. v2 must rebuild it.

## Open questions / 5% uncertainty

- **Are there any v1 use cases that hard-depend on subagent context-sharing?** Need to confirm with users migrating from OpenClaw. If yes, plan an accelerated v2.
- **Should v1 silently skip `lcm_expand_query` if the model calls it, or error?** Recommend: don't register the tool at all (cleaner — the model won't see it in the schema list).
- **Does Hermes's `delegate_task` already support enough state-passing for LCM v2 to work?** Engine.md notes ~30% of port effort for the redesign; that's an estimate, not a confirmed cost. Need a v2 spike before committing.
- **Other OpenClaw LCM features that depend on subagent context-sharing.** The autostart entity-extraction loop (`tryStartExtractionAutostart`) uses subagents to process queues. In Hermes this can run as a background asyncio task without subagent semantics — verify during entity-extraction port.
