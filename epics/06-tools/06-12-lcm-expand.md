---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-06] tools: port lcm_expand primitive (sub-agent only)'
labels: 'port, tool'
---

## Source (TypeScript)
- File: `src/tools/lcm-expand-tool.ts`
- Lines: 1–455
- Function(s)/class(es): `createLcmExpandTool` factory, schema (lines 24–66), main-agent refusal (line 165), `resolveDelegatedExpansionGrantId` + `wrapWithAuth` plumbing, the two entry shapes (`summaryIds` direct vs `query` grep-then-expand), `ExpansionOrchestrator.expand(...)` call.
- **NOT INCLUDED in this issue:** `lcm-expand-tool.delegation.ts` (580 LOC) — only used by `lcm_expand_query` which is **deferred per [ADR-012](../../docs/adr/012-subagent-defer.md)**.

## Target (Python)
- File: `src/lossless_hermes/tools/expand.py`
- Estimated LOC: ~420 LOC.

## Dependencies
- Depends on: #06-02, #06-04, #06-05, #06-06 (recursion guard for depth resolution), Epic 01 retrieval (`retrieval.grep` for the query-entry path), `src/lossless_hermes/expansion.py` (the `ExpansionOrchestrator` itself — port lives in `expansion.py`, not here).
- Depends on: A definition of `is_subagent_session_key(session_key: str) -> bool` — likely lands in `session_patterns.py` per [ADR-024](../../docs/adr/024-project-layout.md). See "Open question" below.
- Blocks: nothing in v0.1.0 (since `lcm_expand_query` is deferred); blocks v2 expand-query work.

## Acceptance criteria
- [ ] `LCM_EXPAND_SCHEMA` dict — **description string verbatim** from `lcm-expand-tool.ts:134–142` (tools.md lines 208–210). The SUB-AGENT ONLY prose + main-agent fallback hints (use `lcm_describe` or `lcm_expand_query`) are load-bearing. **Note:** the description still mentions `lcm_expand_query` even though that tool isn't registered in v0.1.0 — keep the prose verbatim; the model won't try to call an unregistered tool.
- [ ] **Main-agent refusal** (TS line 165): if `not is_subagent_session_key(session_key)` → return:
  ```python
  json.dumps({"error": "lcm_expand is only available in sub-agent sessions..."})
  ```
  Pin the exact error prose against the TS source.
- [ ] **Delegated grant lookup:** sub-agent session resolves the delegated expansion grant via `resolve_delegated_expansion_grant_id(session_key)`; wrap the orchestrator with `wrap_with_auth(orchestrator, runtime_auth_manager)`. The grant ledger lives in `expansion_auth.py` (per ADR-024 project layout).
- [ ] **Conversation scope** (#06-05) → if no conversation resolved, error out.
- [ ] **Two entry shapes:**
  - `summaryIds` (validated, non-empty after dedup) → call `run_expand({summaryIds, conversationId, maxDepth, tokenCap, includeMessages})` directly.
  - `query` → grep first (`retrieval.grep({query, mode: "full_text"})`), take the top summary IDs from results, then expand.
  - At least one of `summaryIds` / `query` must be present (runtime validation per the schema's empty `required` array).
- [ ] `ExpansionOrchestrator.expand(...)` walks the DAG breadth-first under the token cap; with `includeMessages`, hydrates leaf messages.
- [ ] Output is a compact text payload + `citedIds` array for the sub-agent to cite back.
- [ ] **NOT wrapped in `runWithTokenGate`** — `lcm_expand` is in `TOKEN_GATE_TOOLS` bypass set per tools.md line 638. The grant ledger does its own (sub-agent-scoped) budget gating.
- [ ] **Failure modes:**
  - Main-agent invocation → structured error.
  - Delegated session with no grant → `{"error": "Delegated expansion requires a valid grant..."}`.
  - Grant budget exhausted mid-expansion → `ExpansionOrchestrator` truncates; output flagged `truncated: true`.
- [ ] PR description cites the LCM commit SHA being ported + ADR-012 (rationale for why `lcm_expand_query` is NOT being ported alongside).

## Open question
- **`is_subagent_session_key(session_key)` predicate.** In TS, `deps.isSubagentSessionKey(sessionKey)` returns true when the session is a delegated child of a parent. Hermes's session-key model is different (`agent:profile:session`). **Need to define the equivalent before porting `lcm_expand`.** Recommend: add to `session_patterns.py`, look for the `subagent:` prefix (or whatever Hermes uses for delegated runs). Open task in tools.md "Remaining 5% risk" #2.

## Tests
- Mirror `lcm-expand-tool.test.ts` 1:1 in `tests/tools/test_lcm_expand.py` (~496 TS LOC → ~390 pytest LOC):
  - Main-agent session key → refusal error (pin error prose).
  - Sub-agent session, no grant → refusal error.
  - Sub-agent session, valid grant → `summaryIds` direct entry path.
  - Sub-agent session, valid grant → `query` grep-then-expand entry path.
  - Conversation scope errors propagate.
  - `maxDepth` cap respected.
  - `tokenCap` cap respected; truncation flag set.
  - `includeMessages=True` hydrates leaf messages.

## Estimated effort
**10 hours** — 5h port (the orchestrator interface + grant ledger plumbing are the heaviest bits), 5h tests (the sub-agent-session vs main-agent fixture is involved).

## Confidence
**85%** — depends on `is_subagent_session_key` semantics which are still unresolved (tools.md flags this in "Remaining 5% risk"). 10% on the predicate decision; 5% on the orchestrator port (depends on `expansion.py` being ready).

## References
- [`docs/porting-guides/tools.md`](../../docs/porting-guides/tools.md) "lcm_expand" section (lines 202–256).
- [ADR-012](../../docs/adr/012-subagent-defer.md) — explains why `lcm_expand` ships but `lcm_expand_query` does not.
- TS test fixture: `test/lcm-expand-tool.test.ts` (496 LOC).
