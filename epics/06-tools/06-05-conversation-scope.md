---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-06] tools: port lcm-conversation-scope.ts'
labels: 'port'
---

## Source (TypeScript)
- File: `src/tools/lcm-conversation-scope.ts`
- Lines: ~162 LOC (full file)
- Function(s)/class(es): `parseIsoTimestampParam(params, key)`, `resolveLcmConversationScope({lcm, params, sessionId, sessionKey, deps})`.

## Target (Python)
- File: `src/lossless_hermes/tools/conversation_scope.py`
- Estimated LOC: ~140 LOC.

## Dependencies
- Depends on: Epic 01 conversation store (`getConversationBySessionKey`, `getConversationFamilyIds`), #06-04 (uses `read_string_param`).
- Blocks: every tool that accepts `conversationId` / `allConversations` / `since` / `before` (i.e. every tool except `lcm_compact`).

## Acceptance criteria
- [ ] `parse_iso_timestamp_param(params: dict, key: str) -> datetime | None`:
  - Returns `None` when key absent or empty string.
  - Raises `ValueError` (or returns a tool-error dict — match TS shape) on invalid ISO timestamps.
  - Parses RFC 3339 / ISO 8601 with `datetime.fromisoformat()` (Python 3.11+ handles `Z` suffix; older needs explicit handling).
- [ ] `resolve_lcm_conversation_scope(*, lcm, params, session_id, session_key, deps) -> ConversationScope` — resolution order MUST match TS lines 92–161:
  1. Explicit `params["conversationId"]` (number) → `{conversation_id, conversation_ids: [it], all_conversations: False}`.
  2. `params.get("allConversations") is True` → `{all_conversations: True}`.
  3. `session_key` → `conversation_store.get_conversation_by_session_key(session_key)` + `get_conversation_family_ids(...)` for session-family scoping.
  4. Fall through to `session_id` lookup via the store.
  5. No match → `{all_conversations: False, conversation_id: None}`.
- [ ] Family expansion SQL: `SELECT conversation_id FROM conversations WHERE root_conversation_id = ?` (tools.md line 559).
- [ ] Return type is a dataclass or `TypedDict` with fields `conversation_id: int | None`, `conversation_ids: list[int] | None`, `all_conversations: bool` — match the TS surface so call sites don't change shape.
- [ ] Per [ADR-017](../../docs/adr/017-sync-vs-async-db.md): sync method, no `async def`.
- [ ] PR description cites the LCM commit SHA being ported.

## Tests
- `tests/tools/test_conversation_scope.py`:
  - Each branch of the resolution-priority order has a dedicated test.
  - `since > before` produces a structured error (caller's concern, but the timestamp parser is shared).
  - Invalid ISO timestamp surfaces a clear error.
  - Family expansion: seed a parent + 2 child conversations, assert `conversation_ids` lists all 3.

## Estimated effort
**4 hours** — 1.5h port, 2.5h tests (resolution priority has 5 branches × happy/sad each).

## Confidence
**95%** — well-specified in tools.md. 5% risk on the session-key-glob-vs-exact-match semantics (`sessionPatterns.py` is a separate module; verify the wiring here doesn't accidentally pattern-match when exact-match was intended).

## References
- [`docs/porting-guides/tools.md`](../../docs/porting-guides/tools.md) lines 546–560 ("lcm-conversation-scope.ts").
