# ADR-009: Per-message ingest mechanism

**Status:** Accepted
**Date:** 2026-05-13
**Confidence:** 85%
**Supersedes:** —
**Superseded by:** —

## Context

LCM's `ingest()` / `ingestBatch()` (TS `src/engine.ts:5899–6134`) must observe every new message that lands in the Hermes conversation. In OpenClaw, the host runtime explicitly called `engine.ingest({ message })` at every message-append site. Hermes has no such call. The `ContextEngine` ABC (`agent/context_engine.py`) exposes lifecycle methods (`update_from_response`, `should_compress`, `compress`, `on_session_start`, `on_session_end`, `on_session_reset`) but **no per-message ingest hook**.

The constraint forcing a choice: LCM's correctness invariant requires 100% coverage of new messages within the lifetime of a session, including:

- Pure user turns
- Assistant final-response turns
- Tool-only turns where the loop exits before a final response (Ctrl-C, max-iterations, or no-final-response paths)

Available mechanisms (cited from `docs/porting-guides/engine.md` lines 95–101 and `docs/reference/hermes-hooks.md`):

- **A.** Upstream ABC patch — add `engine.ingest(message)` + ~25 call-site additions in `run_agent.py`.
- **B.** Register `post_llm_call` hook and diff `conversation_history` against `last_seen_message_idx[session_id]` each turn.
- **C.** Hook `ContextEngine.handle_tool_call(name, args, **kwargs)` where `kwargs["messages"]` is the live message list (`run_agent.py:11249`) — diff on each tool dispatch.
- **D.** Poll `session.db` from an asyncio task.

## Options considered

### Option A: Upstream ABC patch — add `engine.ingest(message)`

- **Description:** Propose a Hermes upstream PR adding `ContextEngine.ingest(message)` ABC method and 25 call-site additions in `run_agent.py` at every message-append point.
- **Pros:** Cleanest contract, zero diffing logic, deterministic per-message firing.
- **Cons:** High coupling cost, large blast radius, requires upstream merge before v1 can ship. Blocks the entire port on a non-trivial Hermes-core review.
- **Evidence:** `docs/porting-guides/engine.md:96–98` ("High coupling cost, high blast radius").

### Option B: `post_llm_call` hook + diff-on-each-turn

- **Description:** Register `post_llm_call`. The hook receives `conversation_history=list(messages)` (a copy at `run_agent.py:12038`/`15410`). Track `last_seen_message_idx[session_id]` in engine state; on each fire, slice `conversation_history[last_seen_message_idx:]` and ingest each new entry. Update the index.
- **Pros:** Zero Hermes core changes. Idempotent (the dedup guard makes a double-call harmless). Receives the FULL post-tool-loop conversation_history (so multi-tool turns produce one batched ingest).
- **Cons:** `post_llm_call` is gated `if final_response and not interrupted` (`run_agent.py:15407`) — does NOT fire when the user Ctrl-Cs mid-turn or when the loop exits without a final response.
- **Evidence:** `docs/reference/hermes-hooks.md:92` ("Fires **once per user turn** at the end of `run_conversation`, AFTER `transform_llm_output`, only when `final_response` is set AND `not interrupted`. **This is where LCM `ingest()` lives.**"). `docs/spike-results/002-hermes-pre-llm-call.md:39` confirms `conversation_history=list(messages)` is the kwarg shape.

### Option C: Combine B with `handle_tool_call` diff

- **Description:** Use Option B as the primary path. Additionally, in `ContextEngine.handle_tool_call(name, args, **kwargs)` — invoked at `run_agent.py:11249` for every LCM-owned tool — read `kwargs["messages"]` and run the same diff routine before dispatching the tool. Belt-and-suspenders: tool-only turns get covered.
- **Pros:** Covers the `post_llm_call`-misses gap. Idempotent under dedup.
- **Cons:** Only fires when an LCM tool is called — does not cover tool-only turns that use exclusively non-LCM tools. Adds complexity for a narrow gap (most production turns end with a final assistant response anyway).
- **Evidence:** `agent/context_engine.py` line ~159 (handle_tool_call kwargs include `messages`). `docs/porting-guides/engine.md:99`.

### Option D: Background poll of `session.db`

- **Description:** asyncio task reads Hermes's `session.db` every N seconds.
- **Pros:** Decoupled from hook lifecycle.
- **Cons:** Highest latency, worst for stop-on-overflow guarantees, cross-DB consistency hazard (`session.db` vs `lcm.db` can disagree mid-write). Effectively a battery-draining substitute for a hook.
- **Evidence:** `docs/porting-guides/engine.md:100`.

## Decision

Chosen: **Option B (primary) + Option C (safety net)**

Register `post_llm_call` as the per-turn ingest hook. Diff `conversation_history[last_seen_message_idx[session_id]:]` and ingest each new message via `_ingest_batch`. Update `last_seen_message_idx[session_id]` to `len(conversation_history)` after a successful ingest.

Additionally, inside `ContextEngine.handle_tool_call(name, args, **kwargs)`, run the same diff routine using `kwargs["messages"]` before dispatching the LCM tool. This catches tool-only turns that call an LCM tool. The two paths share the dedup guard (Wave-4 P0 fix: atomic transaction + `identity_hash` UNIQUE constraint), so double-firing is a no-op.

## Rationale

Spike 002 (`docs/spike-results/002-hermes-pre-llm-call.md`) confirms `post_llm_call` delivers `conversation_history=list(messages)` at `run_agent.py:15410` — a full snapshot suitable for diff-based ingest. The shallow-copy is enough for read-only diffing. No upstream Hermes patch is required for the primary path.

Engine.md (`docs/porting-guides/engine.md:95–101`) explicitly recommends "B + C combo: B is the primary path (most turns end with a final assistant response). C is the safety net for pure-tool-call turns." Idempotency under the dedup guard makes the double-call cost negligible (a single DB lookup per duplicate hash).

This option drops ~1500 LOC of TS bootstrap/JSONL-replay code from the port (engine.md "What changes" sections under `bootstrap` and `ingest`), and the spike confirms the round-trip mechanics (003 `identity_hash` byte-identical).

## Consequences

- **Per-turn latency:** Slightly higher than per-append ingest. Each turn now does one batched ingest instead of N per-message ingests. For typical 1–5 messages per turn this is a wash; for tool-heavy turns (10–20 tool calls) the batch is more efficient.
- **Coverage gap for interrupted turns:** Ctrl-C mid-turn means `post_llm_call` does NOT fire (`run_agent.py:15407` gates on `final_response and not interrupted`). The next turn's `post_llm_call` will re-snapshot from the new `last_seen_message_idx` and pick up the interrupted-turn messages on the NEXT successful ingest. There may be a window during which the interrupted partial-turn is unreachable to LCM (no assemble can substitute it). Mitigation: also hook `on_session_end` (per-turn fire at `run_agent.py:15525`) to flush a final snapshot.
- **State invariant:** `last_seen_message_idx[session_id]` MUST be reset on `on_session_reset` (`/new` or `/reset`) and on session-replacement to avoid index drift. Engine owns this.
- **Identity hash invariant:** `identity_hash` is computed per message and is the UNIQUE constraint on `messages.identity_hash`. Round-trip byte-identical with the Node implementation (spike 003), so re-ingest is safe.

## Open questions / 5% uncertainty

- **Coverage of tool-only turns that call no LCM tool.** Option C only fires when an LCM tool is invoked. A turn that calls only built-in Hermes tools and exits without a final response would miss BOTH B and C. In practice this happens primarily on Ctrl-C; the next successful turn re-picks-up. If empirical coverage falls below 99%, escalate to Option A (upstream patch) — see ADR-015 patch #3.
- **Validation:** spike 001 in engine.md (`docs/porting-guides/engine.md:516`) recommended instrumenting both hooks for one session to confirm 100% coverage. Not yet run; do this during Phase 2 integration testing.
- **`ContextEngine.handle_tool_call` is BYPASSED by `pre_tool_call`/`post_tool_call` hooks** (separate dispatch branch at `run_agent.py:11249`, see `docs/reference/hermes-hooks.md:179`). The kwargs path is well-defined but isolated — not a leak risk, but worth flagging that Option C does NOT participate in the general tool-call hook chain.
