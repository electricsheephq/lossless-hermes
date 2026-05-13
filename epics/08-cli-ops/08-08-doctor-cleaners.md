---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-08] cli-ops: port lcm-doctor-cleaners.ts (DB-wide row deletion)'
labels: 'port, epic-08-cli-ops'
---

## Source (TypeScript)

- File: `src/plugin/lcm-doctor-cleaners.ts`
- Lines: 641 LOC
- Function(s)/class(es): `getDoctorCleanerFilters()`, `getDoctorCleanerFilterIds()`, `scanDoctorCleaners(db, filterIds?)`, `applyDoctorCleaners(db, options)`, `getDoctorCleanerApplyUnavailableReason(databasePath)`, `normalizeFirstMessagePreview`, the three `CleanerDefinition` records (lines 71–96)

## Target (Python)

- File: `src/lossless_hermes/doctor/cleaners.py`
- Estimated LOC: ~700

## What this issue covers

The DB-wide row-deletion path of `/lcm doctor clean` and `/lcm doctor clean apply` — bulk-deletes whole conversations matching predefined predicates (archived sub-agents, cron sessions, null-key sub-agent context). Per doctor-ops.md §"Cleaners — full inventory" lines 170–188, there are exactly **three** cleaner definitions:

| ID | Detects | Cascades through |
|---|---|---|
| `archived_subagents` | `conversations.active = 0 AND session_key LIKE 'agent:main:subagent:%'` | `summary_messages`, `summary_parents`, `context_items` (3 ref types), `messages_fts`, `summaries_fts`, `summaries_fts_cjk`, `conversations` |
| `cron_sessions` | `session_key LIKE 'agent:main:cron:%'` (no active filter) | same |
| `null_subagent_context` | `session_key IS NULL AND active = 0 AND archived_at IS NOT NULL AND <first message preview> LIKE '[Subagent Context]%'` | same (requires `needsFirstMessage` join) |

### Scan-time surface (`scanDoctorCleaners`)

Returns per-filter conversation+message counts plus top-3 example conversations (sorted by `message_count DESC, created_at DESC, conversation_id DESC`). First-message preview is normalized (whitespace collapsed, 256-char prefix, then trimmed to 120 chars with "..." ellipsis if longer) — same normalizer as 08-05's reconcile-list. Scan + apply use the same predicate SQL, so the dry-run count equals the apply count.

### Apply-time guards (from doctor-ops.md §"Apply-time guards" lines 179–183):

1. **Backup is mandatory.** Call `getDoctorCleanerApplyUnavailableReason(databasePath)` first. If the DB is in-memory (no file path), returns `{"kind": "unavailable", "reason": "Cleaner apply requires a file-backed SQLite database..."}`. On success, write a backup via `write_lcm_database_backup` (08-09) to a path under `build_lcm_database_backup_path(databasePath, "doctor-cleaners")` BEFORE the `BEGIN IMMEDIATE`.
2. **Temp-table staging** (4 or 5 temp tables): `doctor_cleaner_candidate_conversations`, `doctor_cleaner_first_messages` (only if any selected cleaner has `needs_first_message`), `doctor_cleaner_conversation_ids`, `doctor_cleaner_summary_ids`, `doctor_cleaner_message_ids`. Always DROP in `finally`.
3. **FTS branches are best-effort.** `has_table(db, "messages_fts" | "summaries_fts" | "summaries_fts_cjk")` gates each. Never assume an FTS table exists.
4. **Vacuum only fires if `vacuum: True` AND `deleted_conversations > 0`** (so a no-op apply is cheap).
5. Optional `PRAGMA wal_checkpoint(TRUNCATE)` after VACUUM.

### Return shape (`DoctorCleanerApplyResult`):

```python
class DoctorCleanerApplyResult(BaseModel):
    kind: Literal["applied", "unavailable"]
    filter_ids: list[DoctorCleanerId] = []
    deleted_conversations: int = 0
    deleted_messages: int = 0
    vacuumed: bool = False
    backup_path: str = ""
    reason: str | None = None  # only set when kind="unavailable"
```

## Dependencies

- Depends on: #08-06 (doctor contract), #08-09 (DB backup primitive — `write_lcm_database_backup` is called BEFORE the BEGIN IMMEDIATE), Epic 01 (`has_table` helper, `conversations`/`summaries`/`context_items` schema).
- Blocks: nothing.

## Acceptance criteria

- [ ] `get_doctor_cleaner_filters() -> list[CleanerDefinition]` returns the three cleaners in the same order as the TS source (archived_subagents, cron_sessions, null_subagent_context).
- [ ] `scan_doctor_cleaners(db, filter_ids=None) -> DoctorCleanerScan` returns per-filter counts + top-3 examples + totals.
- [ ] First-message preview normalization is shared with 08-05's reconcile-list (one helper, two consumers).
- [ ] Example ordering: `message_count DESC, created_at DESC, conversation_id DESC` (deterministic).
- [ ] `apply_doctor_cleaners(db, options)` refuses (returns `{"kind": "unavailable"}`) on in-memory DBs.
- [ ] Backup is written BEFORE the BEGIN IMMEDIATE (verified by file-system timestamp ordering in a fixture).
- [ ] Temp tables are always dropped in `finally` (even on raise).
- [ ] FTS branches use `has_table` guard; missing FTS tables don't error.
- [ ] `vacuum=True` + `deleted_conversations=0` is a no-op (VACUUM doesn't fire).
- [ ] `vacuum=True` + `deleted_conversations>0` runs VACUUM + `PRAGMA wal_checkpoint(TRUNCATE)`.
- [ ] All three cleaner predicates match the TS SQL 1:1 (validated by `EXPLAIN QUERY PLAN` snapshot test).
- [ ] `null_subagent_context` join correctly reads the earliest message via window function (`ROW_NUMBER() OVER (PARTITION BY conversation_id ORDER BY seq ASC)`).
- [ ] All TS test cases in `test/v41-data-cleanup.test.ts` have ported pytest equivalents in `tests/v41/test_data_cleanup.py`.
- [ ] **New test:** `tests/doctor/test_cleaners.py::test_backup_before_begin_immediate` — file timestamp invariant.
- [ ] **New test:** `tests/doctor/test_cleaners.py::test_temp_tables_dropped_on_raise` — inject a fault mid-apply, assert temp tables are gone.
- [ ] **New test:** `tests/doctor/test_cleaners.py::test_missing_fts_table_best_effort` — drop `summaries_fts_cjk` from fixture, apply still succeeds.
- [ ] **New test:** `tests/doctor/test_cleaners.py::test_scan_equals_apply_count` — dry-run count matches apply count exactly.
- [ ] Function signatures match the spec in [docs/porting-guides/doctor-ops.md](../../docs/porting-guides/doctor-ops.md) §"Doctor contract API (canonical)" lines 150–157.
- [ ] `pytest tests/doctor/test_cleaners.py tests/v41/test_data_cleanup.py` passes.
- [ ] No new mypy errors (`mypy --strict src/lossless_hermes/doctor/cleaners.py`).
- [ ] PR description cites LCM commit `1f07fbd` (pr-613 head).

## Estimated effort

**8 hours.**

## Confidence

**90%** — the three cleaner predicates are SQL-level translation; the temp-table staging is mechanical. The 10% risk is in the `null_subagent_context` window-function query (correctness on conversations with messages out of seq order) — covered by a dedicated fixture test.
