---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-06] tools: implement TOOL_DISPATCH table + handle_tool_call'
labels: 'port, engine'
---

## Source (TypeScript)
- File: `src/engine.ts` — `ContextEngine.get_tool_schemas()` and `ContextEngine.handle_tool_call(name, args, messages=, **kwargs)` registration site. Also `src/plugin/shared-init.ts` which wires the tool factories.
- Lines: tool registration is scattered across `engine.ts` around the LCM tool wiring block.
- Function(s)/class(es): `getToolSchemas()`, `handleToolCall()`, `_context_engine_tool_names` set used by the call site at `run_agent.py:11249`.

## Target (Python)
- File: `src/lossless_hermes/engine/__init__.py` — `LCMEngine.get_tool_schemas()` + `LCMEngine.handle_tool_call()`. Per [ADR-024](../../docs/adr/024-project-layout.md) the engine class shell lives here.
- Estimated LOC: ~60 LOC (dispatch dict + 2 method bodies + token-gate set + error path).

## Dependencies
- Depends on: #06-01 (schema conventions), Epic 02 issue establishing the `LCMEngine` class shell, [ADR-017](../../docs/adr/017-sync-vs-async-db.md) (sync DB → sync dispatch).
- Blocks: every per-tool handler (06-07 through 06-14).

## Acceptance criteria
- [ ] `TOOL_DISPATCH: dict[str, Callable]` defined at module level in `engine/__init__.py` with 8 entries (or 7 in v0.1.0 — `lcm_expand_query` is NOT registered per [ADR-012](../../docs/adr/012-subagent-defer.md)).
- [ ] `TOKEN_GATE_TOOLS: set[str]` defined at module level — every tool EXCEPT `lcm_expand` and `lcm_compact` (tools.md lines 636–639).
- [ ] `get_tool_schemas()` returns a static list of dicts; ordering is stable (tests rely on it).
- [ ] `handle_tool_call(name, args, **kwargs) -> str`:
  - Returns `json.dumps({"error": f"Unknown LCM tool: {name}"})` for unknown names.
  - Resolves `session_key` from kwargs or `self._current_session_key`.
  - Resolves `runtime_ctx` via `self.get_runtime_context(session_key)` (current_token_count + token_budget for the gate).
  - Dispatches through middleware for tools in `TOKEN_GATE_TOOLS`; bypasses middleware otherwise.
  - Returns a JSON string in every code path (no exceptions surfaced to the caller; tool errors are payload-encoded).
- [ ] Per ADR-017, the method is `def`, not `async def`. Inner-async paths (Voyage, sub-agents) are bridged via the engine's background event loop, not exposed at this seam.
- [ ] Functional test: register a stub handler that echoes its args; call `handle_tool_call("stub", {"foo": 1})`; assert the returned string parses to `{"foo": 1}`.
- [ ] Functional test: call `handle_tool_call("does_not_exist", {})`; assert returned string is `{"error": "Unknown LCM tool: does_not_exist"}`.
- [ ] PR description cites the LCM commit SHA being ported.

## Tests
- `tests/engine/test_tool_dispatch.py` — covers schema listing, dispatch table happy path, unknown-tool error, token-gate-set membership predicate.

## Estimated effort
**3 hours** — straightforward translation; the bulk of the time is wiring `get_runtime_context()` to read the in-memory token-state cache that Epic 02 also delivers.

## Confidence
**95%** — pattern is well-specified in tools.md lines 622–688. 5% risk on the `get_runtime_context` plumbing (ADR-TOOLS-05 in tools.md flags this as an open question, but the answer — wire to `ContextEngine.update_from_response`'s usage hook — is concrete enough to implement).

## References
- [`docs/porting-guides/tools.md`](../../docs/porting-guides/tools.md) lines 622–690.
- [ADR-012](../../docs/adr/012-subagent-defer.md) — why `lcm_expand_query` is omitted in v0.1.0.
- [ADR-017](../../docs/adr/017-sync-vs-async-db.md) — why the method is sync.
