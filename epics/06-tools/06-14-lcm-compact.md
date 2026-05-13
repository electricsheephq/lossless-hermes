---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-06] tools: port lcm_compact tool + engine-side gates'
labels: 'port, tool'
---

## Source (TypeScript)
- File: `src/tools/lcm-compact-tool.ts`
- Lines: 1–378
- Function(s)/class(es): `createLcmCompactTool` factory, schema (lines 117–126), 8-stage gate (operator-disabled, engine-unavailable, no-session, engine-gate-state, per-window cap, success), `checkAndIncrementCounter` (in-memory per-window Map), `mapEngineReason`.

## Target (Python)
- File: `src/lossless_hermes/tools/compact.py`
- Estimated LOC: ~350 LOC.

## Dependencies
- Depends on: #06-02, #06-04, **Epic 04 compaction** (`LCMEngine.compact()` + `LCMEngine.get_agent_compaction_gate_state()` + `LCMEngine.note_successful_compact()`).
- Blocks: Epic 09 eval (compact is exercised in long-conversation recall tests).

## Acceptance criteria
- [ ] `LCM_COMPACT_SCHEMA` dict — **description string verbatim** from `lcm-compact-tool.ts:233–240` (tools.md lines 500–501). The "DOES blocking work — typical 5-30s" + cache-deferral-bypass prose is operator-tuning-critical model copy; do not paraphrase.
- [ ] Schema parameters: `reserveFraction` ∈ `[0.5, 1.0]`, default 0.5. (No other params — single-knob tool.)
- [ ] **8-stage gate** per tools.md "Behavior":
  1. **Operator opt-in:** `cfg.agentCompactionToolEnabled` must be `True`; else `{ok: False, reason: "operator-disabled"}`.
  2. **Engine availability:** `get_lcm()` must resolve; else `{reason: "engine-unavailable"}`.
  3. **Session key required:** else `{reason: "no-session"}`.
  4. **Engine-side gate** (`lcm.get_agent_compaction_gate_state(...)`): checks `reserveFraction` floor, migration health, etc. If `should_refuse` → `{reason: gate.refusal_reason, contextRatio: gate.context_ratio}`.
  5. **Per-window cap** (`check_and_increment_counter`): in-memory `dict[session_key, {count, first_at}]`. Max 2 calls per 5-min window. **Wave-12 fix:** gate-refusals are FREE (don't burn the cap). Inline comment:
     ```python
     # LCM Wave-12: gate-refusals don't burn the per-window cap.
     # If we counted refusals, an agent that ran into the floor would be locked out
     # for 5 minutes even when the floor was the right answer.
     # Original: lossless-claw/src/tools/lcm-compact-tool.ts.
     ```
     NOT durable across plugin restart (in-memory only — match TS).
  6. **Call `lcm.compact({sessionId, sessionKey, sessionFile, tokenBudget, currentTokenCount, force: False})`** — blocking, no timeout. Honors engine-side cache-hot + threshold gates.
  7. **On success:** `note_successful_compact(session_key)` clears the token-state cache so the next wrapped tool sees fresh ground truth.
     - Inline **Wave-12 W2A1 P0** comment:
       ```python
       # LCM Wave-12 W2A1 P0: clear the token-state cache on successful compact
       # so the next wrapped tool's gate computes fresh ground truth.
       # Without this clear, a compact→refuse loop forms (cached high ratio
       # survives the compact, the next gated tool refuses, and so on).
       # Original: lossless-claw/src/tools/lcm-compact-tool.ts.
       ```
  8. **Map engine reason** via `map_engine_reason` → tool-facing enum: `compacted | noop | auth-failure | session-excluded | no-conversation | missing-budget | partial-compact | unknown`.
- [ ] **NOT wrapped in `runWithTokenGate`** — `lcm_compact` is in the bypass set per tools.md line 638 (status response is only ~150 chars; estimator returns 150 tokens).
- [ ] **Failure modes:**
  - All 8 gate states above map to structured `{ok, compacted, reason, note, contextRatio?, retryAfterIso?}` — agent-readable.
  - Engine throws → `{ok: False, reason: "exception", note: error.message}`.
- [ ] PR description cites the LCM commit SHA being ported + the Wave-12 rows of ADR-029.

## Tests
- Mirror `v41-lcm-compact-tool.test.ts` 1:1 in `tests/tools/test_lcm_compact.py` (~333 TS LOC → ~270 pytest LOC):
  - Each of the 8 gate states → corresponding `reason` value.
  - Per-window cap: 2 calls in 5min succeed, 3rd refused with `reason: "rate-limited"`, 6 minutes later succeeds again.
  - **Wave-12 gate-refusal exemption:** seed a state that gate-refuses; call 3 times within 5min; assert NONE of them counted against the cap (because gate refused before the cap check). Call a 4th time with a now-passable state; assert it succeeds (cap still at 0).
  - **Wave-12 W2A1 P0 regression:** compact succeeds; assert `note_successful_compact` cleared the token-state cache for the session.
  - `reserveFraction` floor honored (passing 0.8 with a context at 0.7 → refused with `reason: "below-floor"`).
  - Engine throws → `{ok: False, reason: "exception"}` (don't propagate the exception to the caller).

## Estimated effort
**6 hours** — 3h port (gate sequence is straightforward), 3h tests (the 8-state matrix + cap + Wave-12 regressions).

## Confidence
**95%** — well-specified; gate states + reason enum are documented. 5% risk on the per-window cap's `dict[session_key, ...]` thread-safety (Python may face concurrent dispatch where TS was single-threaded — wrap in `threading.Lock`).

## References
- [`docs/porting-guides/tools.md`](../../docs/porting-guides/tools.md) "lcm_compact" section (lines 494–534).
- [ADR-029](../../docs/adr/029-wave-fix-provenance.md) — Wave-12 rows + inline comment format.
- TS test fixture: `test/v41-lcm-compact-tool.test.ts` (333 LOC).
