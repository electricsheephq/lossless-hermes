---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-06] tools: port lcm_describe tool (766 LOC)'
labels: 'port, tool'
---

## Source (TypeScript)
- File: `src/tools/lcm-describe-tool.ts`
- Lines: 1–766
- Function(s)/class(es): `createLcmDescribeTool` factory, schema definition (lines 61–116), summary path (lines 219–650), file path (lines 651+), delegated-grant ledger lookup, line-by-line truncation.

## Target (Python)
- File: `src/lossless_hermes/tools/describe.py`
- Estimated LOC: ~700 LOC.

## Dependencies
- Depends on: #06-02 (dispatch), #06-03 (token gate — this is THE highest blow-up-risk tool per `needs-compact-gate.ts:27`), #06-04 (`tool_result`, param helpers), #06-05 (conversation scope), Epic 01 retrieval surface (`retrieval.describe(id)`).
- Blocks: Epic 09 (eval recall tests cite `lcm_describe`).

## Acceptance criteria
- [ ] `LCM_DESCRIBE_SCHEMA` dict at module top — **description string verbatim** from `lcm-describe-tool.ts:146–155` (tools.md lines 138–139). Includes the `sum_xxx` / `file_xxx` discrimination prose. Caps: `expandChildrenLimit ≤ 50` default 20; `expandMessagesLimit ≤ 50` default 20; `expandMessagesOffset ≥ 0`.
- [ ] Handler signature: `def handle_lcm_describe(args, *, db, retrieval, expansion_auth_manager, deps, session_key, runtime_ctx, **_) -> str` — middleware wraps the call with `runWithTokenGate`.
- [ ] **Resolve conversation scope first.** If no scope can be resolved → `tool_result({"error": "No LCM conversation found..."})`.
- [ ] **`retrieval.describe(id)`** returns `{type: "summary", summary: {...}}` | `{type: "file", file: {...}}` | `None`.
- [ ] **Summary path:**
  - Emit `LCM_SUMMARY <id>` header.
  - Meta line: `kind`, `depth`, `tok counts`, `range`, `created`.
  - Parents + children lists.
  - `manifest` block walking the subtree with per-node `cost[s=,m=]` and `budget[s=in/over,m=in/over]` flags computed against `resolved_token_cap`.
  - If `expandChildren=True`: fetch first-hop children's full content with suppression filter; expose raw count when suppression hides some.
  - If `expandMessages=True` AND target is a leaf: fetch source messages with `expandMessagesOffset` pagination.
- [ ] **File path:** emit file metadata + content with the same line-by-line truncation policy.
- [ ] **Delegated grant enforcement:** when running in a sub-agent session with a delegated grant, look up `remainingTokenBudget`; if base summary tokens exceed remaining, redact content and surface `budget exhausted`. Charge the grant ledger AFTER successful emit. (In v0.1.0 this path is reachable only via `lcm_expand` — which is sub-agent-only — not via `lcm_expand_query` which is deferred. Grant manager must still be wired.)
- [ ] **Failure modes** match tools.md "Failure modes" subsection:
  - ID not found → `{"error": "Not found: <id>", "hint": "Check the ID format..."}`.
  - Found-but-outside-scope → `{"error": "Not found in this session scope: <id>", "hint": "Use allConversations=true..."}`.
  - Delegated session with no grant → behaves as non-delegated.
  - Output > `MAX_RESULT_CHARS` → `truncate_lines_to_cap` appends the truncation notice (regex pinned by Wave-12 N3).
- [ ] **Token-gate estimator** is `350 + 5*250 + 3200 = 4800` base + `k*2000` for `expandChildren` + `k*600` for `expandMessages`, capped at `HARD_CAP_TOKENS`. Codified in `estimate_result_tokens` in #06-03.
- [ ] PR description cites the LCM commit SHA being ported.

## Tests
- Mirror `lcm-describe-expand-flags.test.ts` 1:1 in `tests/tools/test_lcm_describe.py` (~415 TS LOC → ~350 pytest LOC):
  - `expandChildren` happy path.
  - `expandChildren` with suppression filter (raw count exposed).
  - `expandMessages` on leaf with `expandMessagesOffset` pagination.
  - `expandMessages` on non-leaf → no message expansion.
  - Delegated-grant redaction when remaining budget < base summary tokens.
  - `not found` and `not in scope` error shapes.
  - Output exceeds `MAX_RESULT_CHARS` → truncation notice appended; regex `truncated at ~\d+ tokens to protect agent context` matches.
  - File-path branch with the same truncation semantics.

## Estimated effort
**14 hours** — 7h port (766 LOC of careful behavior), 7h tests (the suppression + delegated-grant cases are subtle).

## Confidence
**90%** — DB-only tool, well-specified. 10% risk on the manifest-walking subtree cost computation (depth-bounded traversal with budget flags is fiddly to get right — port the TS algorithm structure verbatim and lean on the test suite).

## References
- [`docs/porting-guides/tools.md`](../../docs/porting-guides/tools.md) "lcm_describe" section (lines 132–198).
- TS test fixture: `test/lcm-describe-expand-flags.test.ts` (415 LOC).
