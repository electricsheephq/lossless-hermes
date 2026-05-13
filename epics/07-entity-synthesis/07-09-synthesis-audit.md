---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-07] synthesis: port audit row writes + retention sweep (lcm_synthesis_audit)'
labels: 'port, epic-07'
---

## Source (TypeScript)

- File: `src/synthesis/dispatch.ts` (audit-write subsection ~lines 380–520)
- File: `src/operator/health.ts` (relevant: orphan-`started`-row sweep, 1h cutoff)
- Lines: ~150 LOC combined
- Function(s)/class(es): `_insert_audit_started(conn, audit_id, ctx, llm_args) → None`, `_update_audit_completed(conn, audit_id, result) → None`, `_update_audit_failed(conn, audit_id, err: str) → None`, `sweep_orphan_audit_starts(conn, *, cutoff_minutes: int = 60) → int`, age-out for `completed`/`failed` rows older than 30 days

## Target (Python)

- File: `src/lossless_hermes/synthesis/audit.py` (new file; the three private methods on `SynthesisDispatcher` from 07-05 delegate here)
- Estimated LOC: ~180

## What this issue covers

The forensic audit trail for every LLM call dispatched by 07-05. Schema (created in Epic 01-06):

```sql
CREATE TABLE lcm_synthesis_audit (
  audit_id              TEXT NOT NULL PRIMARY KEY,
  pass_session_id       TEXT NOT NULL,
  target_summary_id     TEXT REFERENCES summaries(summary_id) ON DELETE CASCADE,
  target_cache_id       TEXT REFERENCES lcm_synthesis_cache(cache_id) ON DELETE CASCADE,
  prompt_id             TEXT NOT NULL REFERENCES lcm_prompt_registry(prompt_id) ON DELETE RESTRICT,
  pass_kind             TEXT NOT NULL,
  pass_input_truncated  TEXT NOT NULL,
  pass_output           TEXT,
  status                TEXT NOT NULL DEFAULT 'started'
                         CHECK (status IN ('started', 'completed', 'failed')),
  model_used            TEXT NOT NULL,
  latency_ms            INTEGER,
  cost_usd_cents        INTEGER,
  last_error            TEXT,
  ran_at                TEXT NOT NULL DEFAULT (datetime('now')),
  CHECK (target_summary_id IS NOT NULL OR target_cache_id IS NOT NULL)
);
```

Behavioral contract:

- **Insert-before-call pattern** (dispatch.ts:402): `INSERT status='started'` BEFORE calling the LLM; UPDATE to `completed`/`failed` AFTER. Guarantees a forensic record survives a process crash between LLM call and ack — operators can later sweep orphan `started` rows older than 1h.
- **Try/except wraps the started-insert** so FK/CHECK violations surface as `SynthesisDispatchError("audit_insert_failure")` BEFORE the LLM is called (no spend on a corrupt audit row). Group D adversarial Gap 4.
- **`pass_session_id` shared across all passes** of one logical synthesis attempt (monthly: `[single, verify_fidelity]`; yearly: `[3× single, 1× judge]`). NOT suffixed with `_cand{i}`. Group D adversarial Gap 2.
- **Truncation:** `pass_input_truncated` and `pass_output` truncated to 8000 chars with `"…(truncated)"` marker. Full inputs are not retained.
- **`hallucination_flag` is NOT a column** — derived per-call from the `verify_fidelity` output and surfaced on `SynthesizeResult` (not stored in the audit row). Wave-4 Auditor #5 P0 regex applies in 07-05.

Retention sweep (called by Epic 06 `/lcm health` or doctor-ops):

- **`sweep_orphan_audit_starts(conn, *, cutoff_minutes=60) → int`** — DELETE rows where `status='started' AND ran_at < datetime('now', '-{cutoff_minutes} minutes')`. These are the crashed/abandoned starts. Uses the partial index `WHERE status = 'started'` (created in Epic 01-06).
- **Age-out for terminal rows:** DELETE where `status IN ('completed', 'failed') AND ran_at < datetime('now', '-30 days')`. Uses the partial index `WHERE status IN ('completed', 'failed')`. Operator-tunable via env: `LCM_AUDIT_RETENTION_DAYS` default 30.

Cost accounting:

- **`cost_usd_cents`** stored as INTEGER. The LLM-call adapter (Epic 04 + 07-05's `LlmCall`) computes cents from token counts × per-model rates. Adapter holds the rate table; this module trusts the adapter's `LlmCallResult.cost_cents`.
- **Open Decision per `synthesis.md` §"LLM cost-accounting drift":** consider storing `prompt_tokens` + `completion_tokens` as separate INTEGER columns to make rate-table updates recomputable on demand. Not in v0.1 schema; flag for ADR follow-up if cost analytics get hot.

## Dependencies

- Depends on: 07-05 (`SynthesisDispatcher` calls these helpers), 07-08 (`prompt_id` FK), Epic 01-06 (`lcm_synthesis_audit` table + indexes)
- Blocks: 07-05 acceptance gate (parity-checklist item 8 — audit insert wrapped in try/except)

## Acceptance criteria

- [ ] `insert_audit_started` runs `INSERT status='started'` BEFORE the LLM call; raises `SynthesisDispatchError("audit_insert_failure")` on any IntegrityError
- [ ] `update_audit_completed` writes `status='completed'`, `pass_output`, `latency_ms`, `cost_usd_cents`, `model_used` (in case the adapter changed it from the requested model)
- [ ] `update_audit_failed` writes `status='failed'`, `last_error` (truncated to 500 chars), `latency_ms` if available
- [ ] All passes of one logical synthesis share ONE `pass_session_id` (Group D Gap 2)
- [ ] `pass_input_truncated` and `pass_output` truncated to 8000 chars with `"…(truncated)"` marker
- [ ] CHECK constraint enforced: exactly one of `target_summary_id` / `target_cache_id` must be non-NULL on every row (the schema enforces; the helper should pass both nullable but error if BOTH are NULL)
- [ ] `sweep_orphan_audit_starts(conn, cutoff_minutes=60)` deletes `started` rows older than the cutoff; returns count
- [ ] Age-out DELETE for terminal rows uses the partial index `WHERE status IN ('completed', 'failed')`
- [ ] `LCM_AUDIT_RETENTION_DAYS` env override (default 30); document in ADR-023 config delivery
- [ ] `audit_id` is `aud_<6 hex chars from secrets.token_hex(3)>`
- [ ] `pytest tests/synthesis/test_audit.py` passes
- [ ] No new mypy errors with strict mode

## Tests to port

| Source | LOC | Cases |
|---|---:|---|
| `test/synthesis-dispatch.test.ts` (audit subset) | ~80 | (1) `started` row exists during LLM call; (2) UPDATE to `completed` post-success; (3) UPDATE to `failed` on LLM error; (4) FK violation on bad prompt_id raises `audit_insert_failure` BEFORE LLM call (Group D Gap 4); (5) `pass_session_id` shared across monthly's 2 passes; (6) `pass_session_id` shared across yearly's 4 passes (Group D Gap 2); (7) truncation marker present when input > 8000 chars |
| `test/operator-health-audit.test.ts` (relevant subset) | ~50 | (8) orphan `started` rows older than 1h deleted; (9) 30-day age-out for terminal rows; (10) custom `LCM_AUDIT_RETENTION_DAYS=7` honored |

## Estimated effort

**3–4 hours.** Three thin INSERT/UPDATE wrappers and two DELETE sweeps. Most cost is the integration test that exercises the full insert → call → update lifecycle and asserts the row is in the expected state at each step.

## Confidence

**92%.** Residual risk:

- **PII in `pass_input_truncated`/`pass_output`.** Per `synthesis.md` Open Decisions §3, 8000 chars is plenty to leak names, emails, secrets. ADR follow-up may add `LCM_AUDIT_LOG_BODIES=0|1` env (default 0 in prod, 1 in dev) for opt-in body logging. Out of scope for this issue; document in ADR open question.
- **Cost-table drift.** Stored `cost_usd_cents` is wrong if Anthropic updates per-token rates. Mitigation per `synthesis.md` §"Remaining 5% risk" item 1: future schema addition of `prompt_tokens` + `completion_tokens` columns. Flag in this issue's ADR follow-up.
