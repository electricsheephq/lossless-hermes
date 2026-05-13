---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-01] storage: port db/migration.ts core schema (11 tables + 42 indexes) → db/migration.py'
labels: 'port, epic-01-storage'
---

## Source (TypeScript)

- File: `src/db/migration.ts`
- Lines: subset of ~2,037 LOC covering the **11 always-on core tables + 42 indexes** (the FTS5 virtual tables go in #01-05; the 13 v4.1 additions go in #01-06; the 3 versioned backfills go in #01-15).
- Function(s)/class(es): `runLcmMigrations(db, opts?)` (top-level orchestrator — partial scope here), the per-table `ensure*Table()` helpers for `conversations`, `messages`, `message_parts`, `summaries`, `summary_messages`, `summary_parents`, `context_items`, `large_files`, `conversation_bootstrap_state`, `conversation_compaction_telemetry`, `conversation_compaction_maintenance`, `lcm_migration_state`. Plus `ensureMessagePartsTable()` belt-and-suspenders (storage.md §2.1 last note) and the `lcm_embedding_meta_cleanup_summary` AFTER-DELETE trigger.

## Target (Python)

- File: `src/lossless_hermes/db/migration.py` (partial — this issue lands the largest section; #01-05 / #01-06 / #01-15 land the rest)
- Estimated LOC for this issue: ~1,100 of the ~2,200 target

## What this issue covers

**The largest single issue in Epic 01.** All 11 always-on tables + 42 indexes + 1 trigger, applied inside a `BEGIN EXCLUSIVE` wrapping the entire ladder (per ADR-026 open-question §2 concurrent-migration mitigation).

### Tables in scope (per storage.md §2.1)

1. **`conversations`** — 9 columns; UNIQUE partial index on `(session_key)` where `session_key IS NOT NULL AND active = 1`. Plus 3 other indexes (`session_key_active_created`, `session_id_active_created`, `session_key_v41`). Drop the obsolete global UNIQUE `conversations_session_key_idx` if found.
2. **`messages`** — 9 columns; UNIQUE `(conversation_id, seq)`; 3 indexes including the partial `messages_suppressed_idx WHERE suppressed_at IS NOT NULL`. CHECK constraint on `role IN ('system','user','assistant','tool')`.
3. **`message_parts`** — 25 sparse columns; CHECK `part_type IN (12 values)`; UNIQUE `(message_id, ordinal)`; 2 indexes. Belt-and-suspenders: re-run the CREATE outside the bulk block (storage.md §2.1) — guards against `node:sqlite` multi-statement-abort residue. Python's `executescript` raises on failure (no silent partial-success), so the guard is defensive but cheap and we port it.
4. **`summaries`** — 19 columns including v3.1/v4.1 additions (`session_key`, `suppressed_at`, `entity_index`, `contains_suppressed_leaves`, `suppress_reason`, `superseded_by`, `leaf_summarizer_cap_was`); 5 indexes including 3 partial.
5. **`summary_messages`** — leaf→message edge table. PK `(summary_id, message_id)`. Two CREATE INDEX calls for `summary_messages_message_idx` (one in bulk + one after — idempotent per IF NOT EXISTS).
6. **`summary_parents`** — condensed→parent edge. Identical shape; index `summary_parents_parent_summary_idx`.
7. **`context_items`** — assembled-prompt ordering. PK `(conversation_id, ordinal)`; CHECK exactly-one-of-message_id-or-summary_id.
8. **`large_files`** — sidecar metadata for `<file>` blocks stored on disk. Index `large_files_conv_idx`.
9. **`conversation_bootstrap_state`** — per-conversation file-anchor offsets. Index `bootstrap_state_path_idx`.
10. **`conversation_compaction_telemetry`** — cache-state machine. 18 columns; CHECK `cache_state IN ('hot','cold','unknown')`; CHECK `last_activity_band IN ('low','medium','high')`; index `compaction_telemetry_state_idx`.
11. **`conversation_compaction_maintenance`** — single-row deferred-compaction debt. 10 columns.
12. **`lcm_migration_state`** — versioned-backfill ledger. PK `(step_name, algorithm_version)`. (Created in this issue; populated by #01-15.)

### Trigger in scope

- **`lcm_embedding_meta_cleanup_summary`** (per storage.md §2.8) — AFTER DELETE ON summaries → cleans polymorphic `lcm_embedding_meta` rows. The trigger references `lcm_embedding_meta` which is created in #01-06; ordering inside `run_lcm_migrations()` must create the table first.

### Out of scope for this issue

- FTS5 virtual tables (#01-05).
- All 13 v4.1 tables (#01-06).
- The 3 versioned backfills (#01-15) — table creation here, backfill logic there.
- Synthesis prompt seeding — `migration.py` accepts `seed_default_prompts: Callable[[Connection], dict] | None = None` and skips when `None` (per ADR-005 option A).

### Implementation notes

- Every CREATE uses `IF NOT EXISTS`.
- Every CREATE INDEX uses `IF NOT EXISTS`.
- ALTER TABLE for adding columns (e.g. `bootstrapped_at` on `conversations`, `model` on `summaries`, the 7 v4.1 columns on `summaries`) uses a `PRAGMA table_info` probe to skip if already present. This is the structural-state half of ADR-026.
- Wrap the entire ladder in `BEGIN EXCLUSIVE` so two concurrent migrations don't race (per `tests-and-config.md` line 494 — the "savepoint retry" test).
- Use SQL string constants at module top level (per storage.md §1 note: "SQL strings, which dominate `migration.py`, translate 1:1"). Don't build SQL dynamically except for the structural-probe ALTERs.

### Schema-diff invariant

The Python-generated schema must be byte-equivalent (modulo formatting whitespace) to the TS-generated reference DB. Per `docs/reference/lcm-source-map.md` open-question #2: a separate validation script diffs the two schemas; drift fails CI.

## Dependencies

- Depends on: #01-01 (DB connection), #01-03 (features probe — for the eventual FTS5 branch in #01-05).
- Blocks: #01-05 (FTS5 tables — they reference these tables' columns), #01-06 (v4.1 tables — they FK into these), #01-15 (versioned backfills run after these tables exist), #01-08 / #01-09 (stores need the schema).

## Acceptance criteria

- [ ] All 11 tables + 42 indexes + 1 trigger created on a fresh DB via `run_lcm_migrations(conn)`.
- [ ] `run_lcm_migrations(conn)` twice in a row is a no-op on the second call (idempotency invariant per ADR-026).
- [ ] On a DB with a legacy `conversations_session_key_idx` index present, the migration drops it and creates the new partial UNIQUE replacement.
- [ ] CHECK constraints enforced: inserting `role = 'invalid'` into `messages` raises `IntegrityError`; inserting `cache_state = 'lukewarm'` into `conversation_compaction_telemetry` raises `IntegrityError`.
- [ ] FK cascade verified: deleting a conversation cascades to messages, summaries, context_items, large_files, bootstrap_state, compaction_{telemetry,maintenance}.
- [ ] FK RESTRICT verified: deleting a message that is referenced by `summary_messages` raises `IntegrityError` (RESTRICT).
- [ ] Belt-and-suspenders `ensure_message_parts_table()` re-runs outside the bulk block and is idempotent.
- [ ] The 16-case TS test set in `test/migration.test.ts` has ported pytest equivalents in `tests/test_migration.py` (storage.md §8 row 2 — depth backfill, identity hashes, FTS recreate, session_key uniqueness flip, message_parts belt-and-suspenders, tool_call_id backfill, idempotency, savepoint retry). Note: depth backfill / tool_call_id backfill are in #01-15; FTS recreate is in #01-05; this issue covers idempotency, savepoint retry, session_key uniqueness flip, message_parts belt-and-suspenders, and the structural-state probes (~10 of the 16 cases).
- [ ] The 6-case TS test set in `test/v41-data-cleanup.test.ts` (session_key NULL → `legacy:conv_<id>` backfill, audit row, summary session_key fill from conv) is partially in scope here (the conversations-side; the audit table is in #01-06).
- [ ] **Schema-diff CI check** lands as a separate script under `scripts/check_schema_drift.py` that compares the Python-generated `sqlite_master` content to a TS-generated reference DB. Zero diff outside formatting.
- [ ] `pytest tests/test_migration.py` passes.
- [ ] `mypy --strict src/lossless_hermes/db/migration.py` passes.
- [ ] PR description cites LCM commit `1f07fbd` and lists the 11 tables + 42 indexes ported.

## Estimated effort

**30–40 hours.** This is the single largest issue in Epic 01 — 1,100 LOC of mechanical DDL plus the structural-probe ALTERs plus the test-port + schema-diff harness.

## Confidence

**90%** — schema is fully specified in storage.md §2.1 and §Appendix B (the 42-index inventory). Residual risk: (a) the schema-diff script is new infrastructure (no spike for it), (b) FK CASCADE/RESTRICT semantics under Python sqlite3 are PRAGMA-gated (already handled in #01-01, but verify), (c) the `executescript` vs `execute`+`commit` boundary on the bulk DDL block needs care to satisfy the `BEGIN EXCLUSIVE` requirement.
