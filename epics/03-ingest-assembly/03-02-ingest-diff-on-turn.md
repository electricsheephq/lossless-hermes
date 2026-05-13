---
name: Port issue
about: `post_llm_call` hook — diff conversation_history against last_seen_message_idx and ingest new rows
title: '[epic-03] ingest: implement post_llm_call diff-on-turn hook'
labels: 'port'
---

## Source (TypeScript)
- File: `src/engine.ts` (`pr-613` HEAD `1f07fbd`)
- Lines: 5899–6134 (`ingestSingle` private body 5899–6064, public `ingest` 6066–6090, `ingestBatch` 6092–6134); `afterTurn` 6220–6646 (the per-turn driver to map to `_on_post_llm_call`)
- Function(s)/class(es): `ingestSingle`, `ingestBatch`, `afterTurn` (ingest portion only — compaction decision logic is Epic 04)

## Target (Python)
- File: `src/lossless_hermes/engine/ingest.py` (`_IngestMixin`)
- Estimated LOC: ~600 (ingest body) + ~50 (`_on_post_llm_call` hook entry)

## Background

Per **ADR-009** (status: Accepted, 85%), the chosen ingest mechanism is:

- **Option B (primary):** Register `post_llm_call`. The hook receives `conversation_history=list(messages)` (a shallow copy at `run_agent.py:12038` / `15410`). Diff `conversation_history[last_seen_message_idx[session_id]:]` and ingest each new entry via `_ingest_batch`. Update `last_seen_message_idx[session_id]` to `len(conversation_history)` after a successful batch.
- **Option C (safety net, separate issue 03-03):** Also run the same diff inside `handle_tool_call` to cover tool-only turns that exit before `post_llm_call` fires (gate at `run_agent.py:15407` — `final_response and not interrupted`).

This issue ports Option B only.

## Required state on the engine shell class

The `__init__` from Epic 02 must declare:

```python
self._last_seen_message_idx: dict[str, int] = {}
self._session_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
```

`_on_post_llm_call` acquires `self._session_locks[session_id]` before diffing — the SAME lock used by `compact()` and `assemble()` so ingest serializes correctly with compaction (per per-session-queue invariant from `docs/porting-guides/engine.md` §"Per-session async queue pattern").

## Hook handler shape

```python
async def _on_post_llm_call(
    self,
    session_id: str,
    user_message: str,
    assistant_response: str,
    conversation_history: list[dict],
    model: str,
    platform: str,
    **kwargs,
) -> None:
    """Maps to engine.ts:6220–6646 (afterTurn — ingest portion only).

    Compaction decision lives in Epic 04; this hook only ingests.
    """
    async with self._session_locks[session_id]:
        last_idx = self._last_seen_message_idx.get(session_id, 0)
        new_messages = conversation_history[last_idx:]
        if not new_messages:
            return
        ingested = await self._ingest_batch(session_id, new_messages)
        if ingested > 0:
            self._last_seen_message_idx[session_id] = len(conversation_history)
```

Registered via `PluginContext.register_hook("post_llm_call", self._on_post_llm_call)` in the plugin entry point (Epic 02 territory; cite here for completeness).

## `_ingest_single` / `_ingest_batch` port

Body of TS `ingestSingle` (5899–6064):

- Skip when `isHeartbeat`, ignored-session, stateless-session, or non-persistable role.
- Skip assistant messages with `stopReason=error|aborted` and empty content.
- Run media-interception pipeline (`interceptInlineImagesInToolMessage`, `interceptNativeUserImageBlocks`, `interceptInlineImages`, `interceptLargeFiles`, `interceptLargeToolResults`, `interceptLargeRawPayload`). **For v0.1.0, port the simplest of these (text-only externalization)**; large-file/binary externalization can ship as a follow-up if blocking. Track the deferral in a TODO with the LCM line range.
- One atomic SQLite transaction wrapping `getMaxSeq → createMessage → createMessageParts → appendContextMessage`. The Wave-4 P0 fix is load-bearing: **without `BEGIN IMMEDIATE` you get orphan rows on partial failure or `UNIQUE` conflicts on concurrent ingest race.**
- `_ingest_batch` loops `_ingest_single` under one queue acquisition.

State mutation: appends to `messages`, `message_parts`, `context_items`.

## Dropped in port (per `docs/porting-guides/engine.md`)

- `lastFullReadFileState`, `recentBootstrapImportsByConversation`, `oversizedAutoRotateCheckpointByQueueKey` — all JSONL-specific.
- The `bootstrapMaxTokens` trimming path — no JSONL bootstrap on Hermes.

## Dependencies
- Depends on: #03-01 (token estimator — `_ingest_single` populates `token_count` via `estimate_tokens(content)` per ADR-021 risk §3), Epic 01 (storage — `ConversationStore.createMessage`, `createMessageParts`; `SummaryStore.appendContextMessage`), Epic 02 (engine shell + `hermes_bridge.py` hook registration seam).
- Blocks: #03-09 (always-on substitution depends on ingest having written rows for the current turn before assembly fires), Epic 04 (compaction reads the DAG that ingest writes).

## Acceptance criteria

- [ ] `_on_post_llm_call` is registered via `PluginContext.register_hook("post_llm_call", ...)` (verified by a test that mounts the plugin and asserts the hook is present in `VALID_HOOKS` lookup).
- [ ] Diff-on-each-turn correctly ingests `conversation_history[last_idx:]` and updates `_last_seen_message_idx`.
- [ ] Re-running the hook with the same `conversation_history` is a **no-op** (idempotent via `identity_hash` UNIQUE constraint per ADR-009).
- [ ] Atomic transaction wraps the three-write sequence (`getMaxSeq → createMessage → createMessageParts → appendContextMessage`) — a deliberate failure injected after `createMessage` leaves no orphan rows.
- [ ] `on_session_reset` clears `_last_seen_message_idx[session_id]` (per ADR-009 §"State invariant").
- [ ] Heartbeat / ignored-session / stateless-session skip rules match TS.
- [ ] Assistant messages with `stopReason=error|aborted` and empty content are dropped (regression: prevents retry pollution loop).
- [ ] Function signatures match `docs/porting-guides/engine.md` §"Python class skeleton" + §"ingest(params) / ingestBatch(params)".
- [ ] All TS unit tests in `test/engine.test.ts` covering ingest paths (look for the `// ── ingest` section banner) have ported pytest equivalents under `tests/test_engine_ingest.py`. Include `bootstrap-flood-regression.test.ts` adapted for the diff-on-turn shape.
- [ ] `pytest tests/test_engine_ingest.py` passes locally + on GitHub CI.
- [ ] No new mypy errors.
- [ ] PR description cites the LCM commit SHA being ported.

## Tests

- Single-turn user-only message ingest.
- Single-turn user + assistant (final response).
- Multi-message turn (5+ tool calls between user and final assistant).
- Replay same `conversation_history` twice — second call is a no-op (idempotent).
- Concurrent ingest on two different sessions (verify per-session lock is the right granularity).
- Ingest after `on_session_reset` — `_last_seen_message_idx` cleared, full conversation re-ingested.
- Heartbeat skip.
- Failed-assistant-with-empty-content skip.
- Transaction atomicity (inject failure after `createMessage`; assert no orphan).

## Estimated effort
**8 hours**.

## Confidence
**85%**. The mechanism is settled (ADR-009 + spike 002). Residual risk: coverage of pure-tool-call turns that exit before `post_llm_call` — addressed by #03-03 as the belt-and-suspenders. Atomic-transaction port is mechanical given Epic 01's `ConversationStore.withTransaction` shape.
