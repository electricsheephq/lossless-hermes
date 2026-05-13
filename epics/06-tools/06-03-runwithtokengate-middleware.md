---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-06] tools: port runWithTokenGate as middleware (Wave-12 F5)'
labels: 'port, wave-fix'
---

## Source (TypeScript)
- File: `src/plugin/needs-compact-gate.ts` (~250 LOC), `src/plugin/result-budget.ts` (132 LOC), `src/plugin/token-state.ts`.
- Lines: `runWithTokenGate` + `estimateResultTokens` (lines 66–94) + `tapResultForTokenAccounting`.
- Function(s)/class(es): `runWithTokenGate`, `estimateResultTokens` (per-tool formulas), `tapResultForTokenAccounting`, `applyResultBudgetConfig`.

## Target (Python)
- File: `src/lossless_hermes/plugin/needs_compact_gate.py` + `src/lossless_hermes/plugin/result_budget.py` + `src/lossless_hermes/plugin/token_state.py`.
- Estimated LOC: ~300 LOC (Python ~15% denser; same logic).

## Dependencies
- Depends on: #06-02 (dispatch table — middleware lives in `handle_tool_call`), Epic 02 `get_runtime_context()` plumbing.
- Blocks: every gated tool (lcm_grep, lcm_describe, lcm_get_entity, lcm_search_entities, lcm_synthesize_around) — they can't ship without this gate.

## Acceptance criteria
- [ ] **Middleware, NOT decorator.** Per **Wave-12 F5** ([ADR-029](../../docs/adr/029-wave-fix-provenance.md) table row), wrap the handler call inside `LCMEngine.handle_tool_call` based on `TOKEN_GATE_TOOLS` membership. Decorator-time computation freezes gate state at plugin-init. The inline comment at the wrap site reads:
  ```python
  # LCM Wave-12 F5: runWithTokenGate is middleware-not-decorator so the
  # gate state is computed at invocation time, not at registration time.
  # Original: lossless-claw/src/plugin/needs-compact-gate.ts.
  ```
- [ ] `estimate_result_tokens(tool_name: str, params: dict) -> int` implements the per-tool formulas verbatim from tools.md lines 124–128 and the per-tool sections (each tool's "Token-budget gating" subsection has its exact estimator).
- [ ] Pre-call gate: if `(current_token_count + estimate) / token_budget > REFUSAL_THRESHOLD (0.92)`, return:
  ```python
  json.dumps({
      "ok": False,
      "needsCompact": True,
      "reason": "context-overflow-prevention",
      "projectedRatio": <float>,
      "suggested_actions": [...],
  })
  ```
  without invoking the handler.
- [ ] Post-call tap: `tap_result_for_token_accounting(session_key, tool_name, result_text)` updates the in-memory token-state cache (per-session). Idempotent across re-calls.
- [ ] `MAX_RESULT_CHARS` and `MAX_RESULT_TOKENS` constants live in `result_budget.py` with the `LCM_TOOL_RESULT_TOKEN_BUDGET` env override (floor 2000, default 10000 tokens) — operator-tunable per tools.md "MAX_RESULT_CHARS / MAX_RESULT_TOKENS / truncationNotice" section.
- [ ] `TRUNCATION_NOTICE_FORMAT` constant exposes the truncation prose ("truncated at ~N tokens to protect agent context") so the prose stays byte-identical across runtime + tests + descriptions. **Wave-12 N3 retro** pins this regex.
- [ ] Skip gating when `current_token_count` or `token_budget` is undefined (early-session calls before any `llm_output` hook has fired).
- [ ] All inline Wave-N comments in the agreed `# LCM Wave-N (YYYY-MM-DD): description` format per ADR-029.
- [ ] PR description cites the LCM commit SHA + the Wave-12 F5 row of ADR-029.

## Tests
- `tests/plugin/test_needs_compact_gate.py` — happy path, refusal at threshold, skip-when-no-budget, estimator per tool, tap-accumulates-state.
- `tests/plugin/test_result_budget.py` — env override resolution, floor enforcement, `applyResultBudgetConfig` raise-at-init.
- `tests/plugin/test_wave12_f5_middleware_not_decorator.py` — regression test: register a tool, mutate runtime context, call again; assert the gate reads the LATEST runtime context, not a snapshot taken at registration time.
- `tests/plugin/test_truncation_notice_format.py` — pins the truncation regex per Wave-12 N3.

## Estimated effort
**6 hours** — 3h for the gate + estimator (mechanical), 1h for env override + truncation prose, 2h for tests (the Wave-12 F5 regression test is the load-bearing one).

## Confidence
**95%** — per-tool estimator formulas are documented verbatim in tools.md. 5% risk on the threshold tuning (`0.92` is from TS; verify it's the right number for Hermes's token-counting precision).

## References
- [`docs/porting-guides/tools.md`](../../docs/porting-guides/tools.md) "runWithTokenGate / needs-compact-gate" section (lines 599–610) + per-tool estimator subsections.
- [ADR-029](../../docs/adr/029-wave-fix-provenance.md) — Wave-12 F5 row, inline comment format.
- Wave-12 N3 retro — `truncationNotice` regex pinning (tools.md "MAX_RESULT_CHARS" section).
