---
name: Port issue
about: Belt-and-suspenders ingest from `handle_tool_call` kwargs for tool-only turns
title: '[epic-03] ingest: read messages= kwarg in handle_tool_call for tool-only turns'
labels: 'port'
---

## Source (TypeScript)
- File: `src/engine.ts` (`pr-613` HEAD `1f07fbd`)
- Lines: 5899–6134 (same `ingestSingle` / `ingestBatch` body — this issue re-uses the path, with a different entry seam)
- Function(s)/class(es): N/A on TS side (this is a Hermes-specific seam — OpenClaw called `engine.ingest` from explicit call sites).

## Target (Python)
- File: `src/lossless_hermes/engine/ingest.py` — extend `_IngestMixin` with the `handle_tool_call` entry path.
- Estimated LOC: ~50 (a thin wrapper around `_ingest_batch` plus the kwargs extraction).

## Background

Per **ADR-009** §"Option C: Combine B with `handle_tool_call` diff":

> Use Option B as the primary path. Additionally, in `ContextEngine.handle_tool_call(name, args, **kwargs)` — invoked at `run_agent.py:11249` for every LCM-owned tool — read `kwargs["messages"]` and run the same diff routine before dispatching the tool. Belt-and-suspenders: tool-only turns get covered.

The `post_llm_call` hook is gated `if final_response and not interrupted` (`run_agent.py:15407`) — it does NOT fire when:

- The loop exits without a final response (max iterations, tool error short-circuit).
- The user Ctrl-Cs mid-turn.
- The turn is a pure-tool-call turn that the user terminates before final assistant.

Catching these via `handle_tool_call` covers the gap **when the turn calls an LCM tool**. Pure-tool-call turns that use only non-LCM tools and don't reach a final response still fall through to the next successful turn's diff (idempotent under the dedup guard).

## Implementation

In `LCMEngine.handle_tool_call` (already defined on the engine shell per Epic 02 / `docs/porting-guides/engine.md` §"Python class skeleton"):

```python
def handle_tool_call(self, name: str, args: dict, **kwargs) -> str:
    # Belt-and-suspenders ingest (ADR-009 Option C). Fires only for LCM tools
    # (lcm_compact, lcm_grep, lcm_describe, lcm_expand, ...); does NOT participate
    # in the general pre_tool_call / post_tool_call hook chain.
    messages = kwargs.get("messages")
    session_id = kwargs.get("session_id") or kwargs.get("sender_id")
    if messages and session_id:
        # Synchronous wrapper — handle_tool_call is sync per Hermes ABC.
        asyncio.run(self._ingest_from_kwargs(session_id, messages))
    # ... existing per-tool dispatch ...
```

`_ingest_from_kwargs` is the same diff routine as `_on_post_llm_call`:

```python
async def _ingest_from_kwargs(self, session_id: str, messages: list[dict]) -> None:
    async with self._session_locks[session_id]:
        last_idx = self._last_seen_message_idx.get(session_id, 0)
        new_messages = messages[last_idx:]
        if not new_messages:
            return
        ingested = await self._ingest_batch(session_id, new_messages)
        if ingested > 0:
            self._last_seen_message_idx[session_id] = len(messages)
```

The two paths share the dedup guard (`identity_hash` UNIQUE constraint, Wave-4 P0 fix). Double-fire from both hooks on the same conversation is harmless.

## Caveats from ADR-009 §"Open questions"

- **Coverage of tool-only turns that call no LCM tool.** Option C only fires when an LCM tool is invoked. A turn that calls only built-in Hermes tools and exits without a final response misses BOTH B and C. In practice this happens primarily on Ctrl-C; the next successful turn re-picks-up. **If empirical coverage falls below 99% during Phase 2 testing, escalate to Option A (upstream patch — ADR-015 patch #3).**
- **`ContextEngine.handle_tool_call` is BYPASSED by `pre_tool_call`/`post_tool_call` hooks** (separate dispatch branch at `run_agent.py:11249`, per `docs/reference/hermes-hooks.md:179`). The kwargs path is well-defined but isolated. Not a leak risk; flagged because reviewers may expect general-hook-chain participation.

## Dependencies
- Depends on: #03-02 (re-uses `_ingest_batch` body and the dedup transaction shape).
- Blocks: nothing else strictly — the path is additive.

## Acceptance criteria

- [ ] `handle_tool_call` reads `kwargs["messages"]` defensively (returns gracefully if missing — never raises on the tool-dispatch hot path).
- [ ] The diff routine is byte-identical to `_on_post_llm_call`'s diff (idempotent; same `identity_hash` dedup).
- [ ] A test calls an `lcm_*` tool on a session with N new un-ingested messages — assert all N land in the DB.
- [ ] A test fires `post_llm_call` for the same conversation **after** `handle_tool_call` already ingested — assert no duplicate rows (`identity_hash` UNIQUE works).
- [ ] A test fires `handle_tool_call` for a non-LCM tool name — assert ingest does NOT fire (we never see that path; the Hermes router only invokes LCM tools through this).
- [ ] Sync wrapper uses `asyncio.run` or equivalent without breaking on already-running event loop (Hermes `handle_tool_call` is sync — see ADR-010 risk §2 about sync/async bridge).
- [ ] All TS tests for ingest replay / dedup pass against the new entry seam (`tests/test_engine_ingest.py` adds parametrize over `(hook_source: "post_llm_call" | "handle_tool_call")`).
- [ ] `pytest tests/test_engine_ingest.py` passes locally + on GitHub CI.
- [ ] No new mypy errors.
- [ ] PR description cites the LCM commit SHA being ported.

## Tests

- Tool-only turn ending in Ctrl-C (simulate by NOT firing `post_llm_call`; assert ingest still ran via `handle_tool_call`).
- Both hooks fire for the same conversation — no duplicate rows.
- `handle_tool_call` with no `messages` kwarg — no-op.
- `handle_tool_call` with `messages=None` — no-op.
- Concurrent `handle_tool_call` + `post_llm_call` on the same session — per-session lock serializes, no race.

## Estimated effort
**4 hours**.

## Confidence
**80%**. Sync→async bridge inside the sync `handle_tool_call` ABC method is the residual risk (ADR-010 risk §2). If `asyncio.run` inside Hermes's call shape causes "loop already running" errors, fall back to `asyncio.get_event_loop().run_until_complete` or schedule via `asyncio.run_coroutine_threadsafe`. Verify with a smoke test before declaring done.
