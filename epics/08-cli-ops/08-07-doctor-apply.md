---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-08] cli-ops: port lcm-doctor-apply.ts (per-conversation summary repair)'
labels: 'port, epic-08-cli-ops'
---

## Source (TypeScript)

- File: `src/plugin/lcm-doctor-apply.ts`
- Lines: 541 LOC
- Function(s)/class(es): `applyScopedDoctorRepair(params): Promise<DoctorApplyResult>`, private helpers `_orderTargets`, `_buildLeafSourceText`, `_buildCondensedSourceText`, `_resolvePreviousSummary`, `_validateRewrite`

## Target (Python)

- File: `src/lossless_hermes/doctor/apply.py`
- Estimated LOC: ~580

## What this issue covers

The per-conversation summary-repair path of `/lcm doctor apply` — re-summarizes broken summaries (rows with fallback/truncated markers) by re-running the active summarizer. Mutates `summaries.content`, `summaries.token_count`, and the `summaries_fts` mirror (best-effort). Returns a structured `DoctorApplyResult` describing what was detected, repaired, unchanged, and skipped.

Owner-gated (per ADR-013 / doctor-ops.md §"Operator gate") — but as with all `/lcm` destructive subcommands, the gate is upstream of dispatch; this handler trusts authorization.

### Algorithm (from doctor-ops.md §"Doctor marker detection" lines 202–212):

1. **Order targets** — active leaves by `context_items.ordinal` ASC, then orphan leaves by `(depth, created_at, summary_id)`, then condensed in the same order. **Repair must happen leaves-first** because condensed re-summarization reads its leaf children's (possibly already-rewritten) content from the in-memory `overrides` map. This is load-bearing — wrong order = stale condensed summaries.

2. **For each target, build source text:**
   - **Leaf:** joins `summary_messages` → `messages`, concatenating `[timestamp]\ncontent` for each message in `sm.ordinal` order.
   - **Condensed:** joins `summary_parents` → `summaries` for each child (recursively re-using overrides for children just rewritten in this same pass).

3. **Resolve "previous summary" context** via three fallbacks (in order): `context_items` lookup → `summary_parents` lookup → `created_at` timestamp-neighbor lookup.

4. **Call the resolved summarizer.** Reject empty output or output that still contains a marker.

5. **Skip** a target with `reason: "rewritten content still contains a doctor marker"` rather than overwriting (avoids loops where the LLM keeps producing fallbacks).

6. **Write all rewrites in a single `withDatabaseTransaction(db, "BEGIN IMMEDIATE", ...)` block at the end.** Update `summaries.content`, `summaries.token_count`, and the `summaries_fts` mirror (best-effort — wrap in try/except).

### Return shape:

```python
class DoctorApplyResult(BaseModel):
    kind: Literal["applied", "unavailable"]
    detected: int = 0
    repaired: int = 0
    unchanged: int = 0
    skipped: list[dict[str, str]] = []  # {"summary_id": "...", "reason": "..."}
    repaired_summary_ids: list[str] = []
    reason: str | None = None  # only set when kind="unavailable"
```

Returns `{"kind": "unavailable", "reason": "..."}` when no summarizer can be resolved (e.g. no provider configured, all fallback chain options exhausted at startup).

### Summarizer seam (the LLM coupling)

Per doctor-ops.md "Remaining 5% risk" #2: this module pulls in `createLcmSummarizeFromLegacyParams` (a plugin-specific factory) plus `LcmDependencies` (a DI shape). The Python port consumes Epic 04's `LcmSummarizer` — the same class used by leaf/condensed compaction — so the prompt construction, provider resolution, fallback chain, and auth-failure detection are reused without duplication.

Doctor-apply's prompt construction differs from compaction's in one respect: it includes a **"repair context"** stanza (the previous-summary text resolved in step 3) so the LLM has surrounding context for accurate re-summarization. The leaf vs condensed prompts otherwise reuse Epic 04-05's verbatim templates.

## Dependencies

- Depends on: #08-06 (doctor contract — `DoctorTargetRecord`, `DoctorApplyResult`), Epic 04 (`LcmSummarizer` — the LLM seam this consumes), Epic 04-05 (prompt templates).
- Blocks: nothing — the doctor scan handler (#08-01 routing to `commands.doctor:run_scan`) reads `load_doctor_targets` from 08-06 directly; apply needs 08-06 + Epic 04.

## Acceptance criteria

- [ ] `apply_scoped_doctor_repair(*, db, config, conversation_id, deps=None, summarize=None, runtime_config=None) -> DoctorApplyResult` matches the TS signature 1:1.
- [ ] Target ordering is leaves-first, then condensed; within each kind, active items by `context_items.ordinal`, orphan items by `(depth, created_at, summary_id)`.
- [ ] In-memory `overrides` map carries rewritten leaf content into condensed source-text construction (verified by a 3-leaf + 1-condensed fixture where leaf rewrite changes the condensed input).
- [ ] Three-fallback "previous summary" resolution: `context_items` → `summary_parents` → `created_at` neighbor.
- [ ] Empty output from the summarizer causes `skipped` with `reason: "empty output"`.
- [ ] Output containing a marker (after re-running `detect_doctor_marker`) is skipped with `reason: "rewritten content still contains a doctor marker"`.
- [ ] All writes happen in one `BEGIN IMMEDIATE` at the end (not per-target).
- [ ] `summaries_fts` mirror update is best-effort (try/except `OperationalError`).
- [ ] Returns `{"kind": "unavailable", "reason": "..."}` when summarizer factory raises; never raises out of the function.
- [ ] No dedicated TS test (doctor-ops.md §"Test inventory" line 430 — coverage gap).
- [ ] **New test:** `tests/doctor/test_apply.py::test_leaves_first_then_condensed` (per Epic README "Verification gates" #6) — confirms ordering invariant.
- [ ] **New test:** `tests/doctor/test_apply.py::test_overrides_map_propagation` — rewritten leaf content reaches condensed re-summarization.
- [ ] **New test:** `tests/doctor/test_apply.py::test_marker_in_output_skipped` — LLM that keeps returning fallbacks is short-circuited.
- [ ] **New test:** `tests/doctor/test_apply.py::test_unavailable_when_no_summarizer` — summarizer factory raises → `{"kind": "unavailable"}`.
- [ ] **New test:** `tests/doctor/test_apply.py::test_atomic_write` — partial failure mid-loop rolls back ALL writes via `BEGIN IMMEDIATE`.
- [ ] Function signatures match the spec in [docs/porting-guides/doctor-ops.md](../../docs/porting-guides/doctor-ops.md) §"Doctor contract API (canonical)" lines 103–110.
- [ ] `pytest tests/doctor/test_apply.py` passes.
- [ ] No new mypy errors (`mypy --strict src/lossless_hermes/doctor/apply.py`).
- [ ] PR description cites LCM commit `1f07fbd` (pr-613 head).

## Estimated effort

**10 hours.**

## Confidence

**85%** — algorithm is well-specified, but doctor-ops.md "Remaining 5% risk" #2 calls out the `LcmDependencies` / summarizer factory coupling as the largest abstraction-boundary uncertainty. Mitigation: land Epic 04's `LcmSummarizer` first; this issue consumes it directly. Dedicated tests fill the TS coverage gap.
