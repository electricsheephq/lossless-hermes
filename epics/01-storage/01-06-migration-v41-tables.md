---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-01] storage: port v4.1 schema additions (13 tables) → db/migration.py'
labels: 'port, epic-01-storage'
---

## Source (TypeScript)

- File: `src/db/migration.ts`
- Lines: the v4.1 sections within ~2,037 LOC (covers storage.md §2.3, §2.4, §2.5, §2.6, §2.7).
- Function(s)/class(es): `ensureV41SupportTables(db)`, `ensureSynthesisLayer(db)`, `ensureEvalTables(db)`, `ensureEntityLayer(db)`, `ensureEmbeddingRegistry(db)`, `ensureLcmFeatureFlags(db)`. Plus the cache-recreate logic for `lcm_synthesis_cache` when an old narrow CHECK is detected (storage.md §2.4 — drops + recreates if old shape; `lcm_synthesis_audit` orphans deleted before DROP to prevent dangling refs).

## Target (Python)

- File: `src/lossless_hermes/db/migration.py` (additive — extends file from #01-04)
- Estimated LOC: ~700

## What this issue covers

**The 13 v4.1 tables that are always created (per storage.md §2.3 / §2.4 / §2.5 / §2.6 / §2.7).** All are owned by this epic at the DDL level; the data they hold is mostly populated by later epics.

### Tables in scope

1. **`lcm_feature_flags`** (storage.md §2.3) — `flag TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT datetime('now')`. Runtime-disable for optional features (e.g. semantic retrieval if vec0 fails to load).
2. **`lcm_worker_lock`** (storage.md §2.3) — cross-process job lock. PK `job_kind`, plus `worker_id`, `acquired_at`, `expires_at`, `last_heartbeat_at`, `job_session_key`, `job_metadata`. Used by Epic 05 (embeddings backfill) and Epic 07 (entity coreference).
3. **`lcm_extraction_queue`** (storage.md §2.3) — entity-coref + procedure-recheck queue. PK `queue_id`, FK `leaf_id → summaries(summary_id) CASCADE`, CHECK `kind IN ('entity','procedure-recheck')`, plus `queued_at`, `picked_at`, `worker_id`, `completed_at`, `attempts INT NOT NULL DEFAULT 0`, `last_error`. **Indexes:** `_pending_idx WHERE picked_at IS NULL`, `_dead_letter_idx WHERE attempts >= 5`.
4. **`lcm_session_key_audit`** (storage.md §2.3) — log of session_key re-keys for `/lcm undo-session-key-rekey`. PK `audit_id`, FK `conversation_id CASCADE`, `original_session_key`, `new_session_key NOT NULL`, `reason NOT NULL`, `applied_at`, `applied_by NOT NULL DEFAULT 'migration'`. **Index:** `_conv_idx ON (conversation_id, applied_at DESC)`.
5. **`lcm_prompt_registry`** (storage.md §2.4) — versioned prompts per `memory_type × tier × pass_kind`. CHECKs enforce 6 memory_types (`episodic-leaf`, `episodic-condensed`, `episodic-yearly`, `procedural-extract`, `entity-extract`, `theme-consolidation`) and 3 pass_kinds (`single`, `verify_fidelity`, `best_of_n_judge`). UNIQUE `(memory_type, tier_label, pass_kind, version)` + null-safe COALESCE UNIQUE INDEX `lcm_prompt_registry_uniq_lookup`. Partial index for active rows. **NOT seeded in this issue** — seeding belongs to Epic 07; this issue plumbs the `seed_default_prompts: Callable | None = None` parameter and verifies callers can pass `None` per ADR-005 option A.
6. **`lcm_synthesis_cache`** (storage.md §2.4) — rebuildable cache for `synthesize()`. UNIQUE on `(session_key, range_start, range_end, leaf_fingerprint, COALESCE(grep_filter, ''), tier_label, prompt_id)`. CHECK `tier_label IN ('year','yearly','monthly','weekly','daily','custom','filtered')`. **Migration drops + recreates** if old narrow CHECK detected — `lcm_synthesis_audit` orphans referencing `target_cache_id` are deleted before DROP. 3 indexes including `_status_building_idx WHERE status = 'building'`.
7. **`lcm_cache_leaf_refs`** (storage.md §2.4) — inverse index cache_id → leaves. PK `(cache_id, leaf_summary_id)` with both FKs CASCADE. **Index:** `_by_leaf_idx ON (leaf_summary_id)`.
8. **`lcm_synthesis_audit`** (storage.md §2.4) — per-pass synthesis log. `target_summary_id` and `target_cache_id` both NULLable FKs; CHECK `(target_summary_id IS NOT NULL OR target_cache_id IS NOT NULL)`. 6 indexes including 2 partial GC indexes.
9. **`lcm_eval_query_set`** (storage.md §2.5) — versioned query-set roots.
10. **`lcm_eval_query`** — individual queries. CHECK `stratum IN ('fts-easy','fts-medium','paraphrastic')`. Indexes `_stratum_idx`, `_must_not_regress_idx WHERE must_not_regress = 1`.
11. **`lcm_eval_run`** — `per_query_scores TEXT` (JSON), `judge_models TEXT` (JSON), CHECK `trigger IN ('manual','prompt-update','model-update','ci','nightly')`. Index `_recent_idx ON (query_set_id, ran_at DESC)`.
12. **`lcm_eval_drift`** — cumulative regression delta. Index `_recent_idx ON (query_set_id, computed_at DESC)`.
13. **`lcm_entity_type_registry`** (storage.md §2.6) — `type_name TEXT PK, first_seen_at, occurrence_count`. Freeform types (no CHECK enum per v4.1.1 §C).
14. **`lcm_entities`** — `entity_id TEXT PK`, `session_key NOT NULL`, `canonical_text NOT NULL`, etc. UNIQUE `(session_key, canonical_text COLLATE NOCASE)`. Index `_lookup_idx ON (session_key, entity_type, last_seen_at DESC)`.
15. **`lcm_entity_mentions`** — `mention_id PK`, FK `entity_id CASCADE`, FK `summary_id CASCADE`, etc. Two by-entity / by-summary indexes.
16. **`lcm_embedding_profile`** (storage.md §2.7) — `model_name TEXT PK`, `dim NOT NULL`, `registered_at`, `active`, `archive_after`.
17. **`lcm_embedding_meta`** — composite PK `(embedded_id, embedded_kind, embedding_model)`. CHECK `embedded_kind IN ('summary','entity','theme')`. **No FK on embedded_id** (polymorphic — see storage.md §2.7 explicit note). Indexes `_active_idx ON (embedding_model, embedded_at DESC) WHERE archived = 0`, `_by_kind_idx`.

That is **17 named entities** — the issue title says "13 tables" matching storage.md §2.3+§2.4 (the original v4.1 omnibus headline grouping); the 4 eval tables (§2.5), 3 entity tables (§2.6), and 2 embedding registry tables (§2.7) are sub-counts that sum to **13 + 4 = 17 if you include eval, or "13 tables" if you say the 4 eval are a separate sub-bucket**. **Land all 17 in this issue** — they're all v4.1 always-on additions and the test set is interlocked.

### Out of scope explicitly

- **Tables removed in first-principles pass** (storage.md §2.9): `lcm_purge_rebuild_queue`, `lcm_voyage_rate_state`, `lcm_procedures`, `lcm_intentions`, `lcm_themes`, `lcm_theme_sources`. **DO NOT recreate.** Document the deferral in the migration docstring; if a future Hermes use case wants them, write a separate ADR.
- **Eva's fork-side tables** (storage.md §2.10): `lcm_rollups` (touched only if exists), `lcm_migration_flags` (never touched). The `backfillForkRollupsSessionKeys` step belongs to #01-15.
- **vec0 virtual tables** (`lcm_embeddings_<model_slug>`) — created on demand by Epic 05's `embeddings/store.py`; this epic creates only the sidecar tables.
- **Default prompt seeding** — plumb the `seed_default_prompts: Callable | None = None` parameter; actual seeding lives in Epic 07.

## Dependencies

- Depends on: #01-04 (core tables — FKs from queue / audit / mentions point at summaries, conversations).
- Blocks: #01-05 (the `lcm_embedding_meta_cleanup_summary` trigger from #01-04 references `lcm_embedding_meta` created here — ordering inside `run_lcm_migrations()` must create this table before the trigger), #01-15 (versioned backfills run after these tables exist), Epic 05 (embeddings load), Epic 07 (entity / synthesis).

## Acceptance criteria

- [ ] All 17 v4.1 tables created on a fresh DB.
- [ ] CHECK constraints enforced: inserting `kind='bad'` into `lcm_extraction_queue` raises `IntegrityError`; same for `tier_label='quarterly'` on `lcm_synthesis_cache`, `stratum='hard'` on `lcm_eval_query`, `trigger='cron'` on `lcm_eval_run`, `embedded_kind='other'` on `lcm_embedding_meta`.
- [ ] FK CASCADE verified: deleting a leaf summary cascades to `lcm_extraction_queue` rows referencing that `leaf_id`.
- [ ] FK CASCADE verified: deleting an entity cascades to `lcm_entity_mentions`.
- [ ] Polymorphic FK behavior: deleting a summary triggers the `lcm_embedding_meta_cleanup_summary` trigger (from #01-04) which removes corresponding `lcm_embedding_meta` rows (since there's no FK on `embedded_id` itself).
- [ ] UNIQUE `(session_key, canonical_text COLLATE NOCASE)` on `lcm_entities` rejects insertion of `('s1', 'Foo')` followed by `('s1', 'FOO')`.
- [ ] **Cache-recreate path:** on a DB with the old narrow CHECK on `lcm_synthesis_cache.tier_label` (e.g. missing `'yearly'`), the migration deletes any `lcm_synthesis_audit` rows referencing `target_cache_id`, drops the cache table, and recreates it with the new CHECK. Test by hand-constructing the old schema and running migration.
- [ ] Default prompt seeding parameter wiring: `run_lcm_migrations(conn, seed_default_prompts=None)` runs cleanly with `lcm_prompt_registry` empty; passing a callable invokes it after table creation.
- [ ] All TS tests in this surface are ported (per `tests-and-config.md` line 494 and storage.md §8):
  - `test/v41-support-tables.test.ts` — 4 cases (feature_flags, worker_lock, extraction_queue, session_key_audit) → `tests/test_v41_support_tables.py`.
  - `test/v41-eval-tables.test.ts` — 8 cases → `tests/test_v41_eval_tables.py`.
  - `test/v41-entity-layer-tables.test.ts` — 7 cases → `tests/test_v41_entity_layer.py`.
  - `test/v41-embedding-meta-tables.test.ts` — 6 cases → `tests/test_v41_embedding_meta.py`.
  - `test/v41-summaries-columns.test.ts` — 12 cases (every v4.1 column with default, FK behavior, idempotency) → `tests/test_v41_summaries_columns.py`. (Note: the summaries table itself is in #01-04; this issue verifies the v4.1 columns added via ALTER are present.)
  - `test/v41-indexes.test.ts` — 6 cases → `tests/test_v41_indexes.py`.
  - `test/v41-pre-existing-schema-migration.test.ts` — 2 cases (v4.1 columns added to legacy DB; idempotency) → `tests/test_v41_pre_existing_schema.py`.
- [ ] `pytest tests/test_v41_*.py` passes (45 cases total in this issue).
- [ ] `mypy --strict` passes.
- [ ] PR description cites LCM commit `1f07fbd` and lists the 17 tables ported.

## Estimated effort

**16–22 hours.**

## Confidence

**92%** — schema is fully specified in storage.md §2.3–§2.7. Residual risk: (a) the cache-recreate path needs careful test fixturing to construct the old narrow CHECK schema; (b) the polymorphic FK + trigger interaction is novel relative to TS testing — verify the trigger fires under Python sqlite3's foreign_keys-on mode.
