---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-07] synthesis: port dispatch.ts (817 LOC, 3 pass kinds + best-of-N)'
labels: 'port, epic-07, wave-4, wave-5, wave-7, wave-8'
---

## Source (TypeScript)

- File: `src/synthesis/dispatch.ts`
- Lines: 817 LOC
- Function(s)/class(es): `SynthesisDispatcher` class (`synthesize(req)`, `_run_single`, `_run_verify_fidelity`, `_run_best_of_n_yearly`, `_pick_model`, `_render_prompt`, `_render_verify_prompt`, `_render_judge_prompt`, `_parse_judge_output`, `_truncate_for_audit`), `SynthesizeRequest` / `SynthesizeResult` / `BestOfNDetail` / `SynthesisDispatchError` types, `PASS_STRATEGY_BY_TIER` / `DEFAULT_MODEL_BY_TIER` / `HARD_CAP_BEST_OF_N` constants, `LlmCall` Protocol + `LlmCallArgs` / `LlmCallResult` dataclasses

## Target (Python)

- File: `src/lossless_hermes/synthesis/dispatch.py`
- Estimated LOC: ~900

## What this issue covers

The tier-dispatched synthesis orchestrator. `SynthesisDispatcher.synthesize(req)` is the single entrypoint. Three pass-strategy branches:

- **`single`** (daily / weekly / custom / filtered) — one LLM call, one audit row
- **`single + verify_fidelity`** (monthly) — two sequential LLM calls; second checks the first for hallucination; result surfaces `hallucination_flagged: bool`
- **`best_of_n_judge`** (yearly) — N candidates in parallel via `asyncio.gather(*, return_exceptions=True)` + 1 judge call picking the winner; N defaults to 3, hard-capped at 5; `BestOfNDetail` surfaces `requested`, `capped`, `selected_index`

Behavioral parity checklist (13 items from `synthesis.md` §"Behavioral parity checklist"; every one is a regression test under `tests/synthesis/`):

1. **`missing_target` validates BEFORE the LLM call** — `target_summary_id IS NULL AND target_cache_id IS NULL` raises `SynthesisDispatchError("missing_target")`. Group D adversarial Gap 1.
2. **`force_model` without `model_override` falls back to TIER default**, not the prompt's `model_recommendation`. Wave-4 Auditor #5 P1.
3. **Best-of-N hard cap = 5.** `req.best_of_n=10` clamps; `BestOfNDetail.requested=10, capped=True`. Wave-5 P2.
4. **All best-of-N candidates + judge share ONE `pass_session_id`.** Don't suffix `_cand{i}`. Group D adversarial Gap 2.
5. **Verify-fidelity regex:** `^OK\b` at start-of-string OR start-of-line passes; `^UNSUPPORTED:` / `^HALLUCINATION:` at start-of-line fail. BOTH conditions checked. Wave-4 Auditor #5 P0 — a previous relaxation matched `"UNSUPPORTED: X\nOK on rest"` and CLEARED the flag.
6. **Judge output parser:** prefer `Winner: N` capture group; fall back to "scan backwards for last digit"; out-of-range raises `SynthesisDispatchError("judge_failure")` with N in the message. Final.review.3 Loop 4 Bug 4.3.
7. **`asyncio.gather(*, return_exceptions=True)`** (the Python equivalent of `Promise.allSettled`) for yearly candidates. Single-candidate survivor → SKIP the judge (judge over N=1 is a foot-gun); populate full `SynthesizeResult` from that single candidate. Wave-7 P1.1/P1.2 + Wave-8 P1 CRITICAL.
8. **Audit insert wrapped in try/except.** FK/CHECK violations raise `SynthesisDispatchError("audit_insert_failure")` BEFORE the LLM is called (no LLM spend on a corrupt audit row). Group D adversarial Gap 4.
9. **Verify-prompt placeholder aliases:** both `{{source_text}}` AND `{{source_leaves}}` substitute; both `{{candidate_summary}}` AND `{{draft}}` substitute. Final.review.3 Loop 4 Bug 4.2.
10. **Empty-string `tier_label` normalizes to NULL** in both `get_active_prompt` AND `register_prompt`. Group D adversarial Gap 3.
11. **`session_key` fallback chain (4 sources):** targetSummary.session_key → input.sessionKey → resolved conversationIds[0].session_key → `"agent:main:main"`. Wave-7 Auditor #6 P0.
12. **`pass_input_truncated` and `pass_output` truncated to 8000 chars** with `"…(truncated)"` marker. Full inputs are not retained.
13. **Audit `status='started'` insert BEFORE LLM call**, UPDATE to `completed`/`failed` after. dispatch.ts:402. Forensic record survives crash between call and ack.

The LLM call itself is **injected** as `LlmCall: (args: LlmCallArgs) → Awaitable[LlmCallResult]` — dispatch is LLM-vendor-agnostic. The Hermes-side adapter lives in `synthesis/llm_adapter.py` (Epic 04 ports the base adapter; this issue wires it).

Inline `# LCM Wave-N` comments are mandatory at all 13 sites per ADR-029.

## Dependencies

- Depends on: 07-08 (prompt registry — `get_active_prompt` / `get_prompt_by_id`), 07-09 (audit row writes), 07-06 (cache write path that calls into this dispatcher), Epic 04 (LLM adapter shape — the `LlmCall` Protocol it consumes)
- Blocks: 07-06 (cache write path uses `SynthesisDispatcher.synthesize` as its hot subroutine), 07-07 (invalidation depends on the cache table this writes to)

## Acceptance criteria

- [ ] `SynthesisDispatcher(db, llm_call)` constructor stores both as instance state; no module-level singletons
- [ ] `synthesize(req)` validates `missing_target` BEFORE prompt lookup or LLM call
- [ ] `_pick_model(req, prompt)` honors precedence: `req.force_model and req.model_override` → `req.model_override`; `req.force_model` alone → `DEFAULT_MODEL_BY_TIER[req.tier]`; otherwise `prompt.model_recommendation or req.model_override or DEFAULT_MODEL_BY_TIER[req.tier]` (Wave-4 Auditor #5 P1)
- [ ] All three pass-kind branches dispatch to private `_run_single` / `_run_verify_fidelity` / `_run_best_of_n_yearly`
- [ ] `HARD_CAP_BEST_OF_N = 5`; `BestOfNDetail.capped = (req.best_of_n > 5)` and `.requested = req.best_of_n`
- [ ] One `pass_session_id` per logical synthesis (shared across all candidates + judge)
- [ ] `asyncio.gather(*, return_exceptions=True)` for yearly candidates; failed exceptions don't poison successful siblings
- [ ] Survivor-of-one path: if only one of N succeeds, skip judge and use that one
- [ ] Verify-fidelity passes regex: `re.match(r"^OK\b", out)` OR `re.search(r"^OK\b", out, re.M)` for OK; `re.search(r"^(UNSUPPORTED|HALLUCINATION):", out, re.M)` for FAIL
- [ ] Judge parser: try `re.search(r"Winner:\s*(\d+)", out)` first; fall back to "last digit scanning backwards"; raise `judge_failure` if out-of-range
- [ ] `_truncate_for_audit(s, 8000)` appends `"…(truncated)"` when `len(s) > 8000`
- [ ] Audit insert wrapped in try/except → `SynthesisDispatchError("audit_insert_failure")`
- [ ] `# LCM Wave-N (date): ...` comments at all 13 parity-checklist sites
- [ ] `pytest tests/synthesis/test_dispatch.py` passes
- [ ] No new mypy errors with strict mode

## Tests to port

| Source | LOC | Cases |
|---|---:|---|
| `test/synthesis-dispatch.test.ts` (and related v4.1 group-D / wave-4-5-7-8 regression files) | ~600 | All 13 parity-checklist items (one test per); plus single-pass happy path; verify-fidelity happy path; yearly best-of-N happy path; judge picks winner; survivor-of-one skips judge; audit row count matches pass count; force-model precedence matrix; session-key 4-step fallback |

## Estimated effort

**14–18 hours.** The hardest part is not the SQL or the pass-strategy plumbing — it's getting all 13 Wave-N regressions to pass simultaneously without papering over the failure modes they were each written for. Budget ~6 h on parity tests alone.

## Confidence

**82%.** Sub-95% items:

- **`LlmCall` adapter shape from Hermes side** — see `synthesis.md` "Hermes cross-reference: model selection". Hermes's existing client (likely `anthropic.AsyncAnthropic`) must be wrapped. Token counts → `cost_cents` calculation needs the adapter's per-model rate table. If Epic 04's adapter lands first, drop to 90%; if not, ship 07-05 against a deterministic mock and integrate later.
- **`asyncio.gather` exception semantics vs `Promise.allSettled`.** Python's `return_exceptions=True` returns the exception as a value, not a wrapper object. Differs subtly from `PromiseSettledResult{ status, value | reason }`. Caught by parity-checklist item 7's survivor-of-one test.
- **Extended-thinking adapter** — if `model_recommendation = "claude-opus-4"` + extended thinking is seeded for yearly judge (07-10 / Open Decision A), the adapter must set `thinking={"type": "enabled", "budget_tokens": ...}`. New surface; not in TS source.
