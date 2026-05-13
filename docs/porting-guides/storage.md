# Porting Guide: Storage Layer

**Confidence target:** 95%+
**Total TS LOC (in-scope):** 8,392
**Estimated Python LOC:** ~9,500 (including test bench)
**Estimated effort:** 120–160 hours (3–4 engineer-weeks of focused work)
**Epic:** 01-storage
**Upstream pin:** branch `pr-613` @ commit `1f07fbd` (v4.1 omnibus). Note: #628 (stub-tier + `scripts/lcm-blob-migrate.mjs`) is on `main` (commit `13780e9`) but **NOT** in `pr-613`. See "Migration script #628" below.

---

## 1. Source file inventory

| TS file | LOC | Purpose | Python target | Py LOC est | Hours est |
|---|---:|---|---|---:|---:|
| `src/db/migration.ts` | 2037 | All DDL, idempotent column adds, versioned backfills, FTS recreate, prompt seeding | `src/lossless_hermes/db/migration.py` | 2200 | 30–40 |
| `src/db/config.ts` | 629 | LCM runtime config schema, env+plugin-config resolution, defaults | `src/lossless_hermes/db/config.py` | 700 | 8–12 |
| `src/db/connection.ts` | 170 | Open/track/close connections; apply PRAGMAs incl. `allowExtension`, FK assertion | `src/lossless_hermes/db/connection.py` | 220 | 4–6 |
| `src/db/features.ts` | 61 | Probe FTS5 + trigram tokenizer availability, cached per-conn | `src/lossless_hermes/db/features.py` | 80 | 2 |
| `src/store/conversation-store.ts` | 1071 | Conversations + messages + message_parts CRUD + FTS/LIKE/regex search | `src/lossless_hermes/store/conversation.py` | 1300 | 18–22 |
| `src/store/summary-store.ts` | 1668 | Summaries + context_items + large_files + bootstrap_state CRUD; CJK-aware search; subtree walks; transcript GC | `src/lossless_hermes/store/summary.py` | 2000 | 22–28 |
| `src/store/compaction-telemetry-store.ts` | 204 | Cache-aware compaction telemetry upsert/get | `src/lossless_hermes/store/compaction_telemetry.py` | 230 | 2 |
| `src/store/compaction-maintenance-store.ts` | 219 | Deferred-compaction debt state machine | `src/lossless_hermes/store/compaction_maintenance.py` | 250 | 2 |
| `src/store/conversation-scope.ts` | 34 | Build `WHERE conversation_id IN (...)` fragment | `src/lossless_hermes/store/conversation_scope.py` | 45 | 0.5 |
| `src/store/fts5-sanitize.ts` | 50 | Wrap user tokens in `"…"` so FTS5 operators don't fire | `src/lossless_hermes/store/fts5_sanitize.py` | 60 | 1 |
| `src/store/full-text-sort.ts` | 21 | BM25 + recency hybrid ORDER BY builder | `src/lossless_hermes/store/full_text_sort.py` | 30 | 0.5 |
| `src/store/full-text-fallback.ts` | 84 | LIKE search plan + snippet (used when FTS5 unavailable + for CJK) | `src/lossless_hermes/store/full_text_fallback.py` | 100 | 1.5 |
| `src/store/parse-utc-timestamp.ts` | 26 | Reinterpret SQLite `datetime('now')` as UTC | `src/lossless_hermes/store/parse_utc_timestamp.py` | 30 | 0.5 |
| `src/store/message-identity.ts` | 13 | SHA-256 of `role\x00content` for dedupe | `src/lossless_hermes/store/message_identity.py` | 20 | 0.5 |
| `src/store/index.ts` | 44 | Re-export barrel | `src/lossless_hermes/store/__init__.py` | 30 | 0.5 |
| `src/large-files.ts` | 567 | Parse `<file>` blocks, MIME → ext mapping, deterministic exploration summaries (JSON/CSV/code), file-id extraction | `src/lossless_hermes/large_files.py` | 650 | 8 |
| `src/integrity.ts` | 600 | 8 integrity checks + metrics collector + repair plan | `src/lossless_hermes/integrity.py` | 700 | 6–8 |
| `src/prune.ts` | 392 | Age-based conversation pruning, dry-run/confirm/vacuum modes | `src/lossless_hermes/prune.py` | 450 | 5 |
| `src/transcript-repair.ts` | 300 | Pair tool_use ↔ tool_result; sanitize OpenAI reasoning placement | `src/lossless_hermes/transcript_repair.py` | 360 | 5 |
| `src/transaction-mutex.ts` | 202 | Per-DB async transaction lock + savepoint-based reentrancy + timeout | `src/lossless_hermes/transaction_mutex.py` | 240 | 4–6 |
| **Total** | **8392** | | | **~9695** | **120–160** |

Notes:
- Python LOC estimate is +15% over TS because Python lacks TS's concise interface/type-row shape literals — but the SQL strings, which dominate `migration.py`, translate 1:1.
- Hours are *implementation only* — schema authoring + line-by-line port + unit-test parity. They do **not** include cross-store integration testing, ADR work, or sync-vs-async API design (handled at epic level).

---

## 2. Tables — complete inventory

The schema below is the *complete* state after `runLcmMigrations()` completes on a fresh DB, with v4.1 omnibus applied. Tables are grouped by subsystem.

### 2.1 Core conversation/message tables (always created)

#### `conversations`
| Column | Type | Constraint | Notes |
|---|---|---|---|
| `conversation_id` | INTEGER | PRIMARY KEY AUTOINCREMENT | |
| `session_id` | TEXT | NOT NULL | host-supplied opaque ID |
| `session_key` | TEXT | NULL | v3.1 A1 cross-conv identity; backfilled to `legacy:conv_<id>` if NULL |
| `active` | INTEGER | NOT NULL DEFAULT 1 | |
| `archived_at` | TEXT | NULL | |
| `title` | TEXT | NULL | |
| `bootstrapped_at` | TEXT | NULL | added via ALTER |
| `created_at` | TEXT | NOT NULL DEFAULT `datetime('now')` | |
| `updated_at` | TEXT | NOT NULL DEFAULT `datetime('now')` | |

**Indexes:** `conversations_active_session_key_idx` (UNIQUE on `session_key WHERE session_key IS NOT NULL AND active = 1`), `conversations_session_key_active_created_idx`, `conversations_session_id_active_created_idx`, `conversations_session_key_v41_idx` (partial `WHERE session_key IS NOT NULL`).
**Dropped:** `conversations_session_key_idx` (old global UNIQUE; replaced by the partial-active one).
**Reads/writes:** `conversation-store.ts`.

#### `messages`
| Column | Type | Constraint | Notes |
|---|---|---|---|
| `message_id` | INTEGER | PRIMARY KEY AUTOINCREMENT | |
| `conversation_id` | INTEGER | NOT NULL REFERENCES `conversations(conversation_id)` ON DELETE CASCADE | |
| `seq` | INTEGER | NOT NULL | per-conv ordinal; UNIQUE with conv_id |
| `role` | TEXT | NOT NULL CHECK IN ('system','user','assistant','tool') | |
| `content` | TEXT | NOT NULL | |
| `token_count` | INTEGER | NOT NULL | |
| `identity_hash` | TEXT | NULL | sha256(role\x00content); backfilled |
| `created_at` | TEXT | NOT NULL DEFAULT `datetime('now')` | |
| `suppressed_at` | TEXT | NULL | v3.1 A3 lossless-forget; cascade target |

**UNIQUE:** `(conversation_id, seq)`.
**Indexes:** `messages_conv_seq_idx`, `messages_conv_identity_hash_idx`, `messages_suppressed_idx` (partial `WHERE suppressed_at IS NOT NULL`).
**Reads/writes:** `conversation-store.ts`, `summary-store.ts` (GC + linkage), `prune.ts`.

#### `summaries`
| Column | Type | Constraint | Notes |
|---|---|---|---|
| `summary_id` | TEXT | PRIMARY KEY | |
| `conversation_id` | INTEGER | NOT NULL REFERENCES `conversations(conversation_id)` ON DELETE CASCADE | |
| `kind` | TEXT | NOT NULL CHECK IN ('leaf','condensed') | |
| `depth` | INTEGER | NOT NULL DEFAULT 0 | leaves=0; condensed=max(parent)+1 |
| `content` | TEXT | NOT NULL | |
| `token_count` | INTEGER | NOT NULL | |
| `earliest_at` | TEXT | NULL | computed from descendant leaves |
| `latest_at` | TEXT | NULL | |
| `descendant_count` | INTEGER | NOT NULL DEFAULT 0 | |
| `descendant_token_count` | INTEGER | NOT NULL DEFAULT 0 | |
| `source_message_token_count` | INTEGER | NOT NULL DEFAULT 0 | |
| `created_at` | TEXT | NOT NULL DEFAULT `datetime('now')` | |
| `file_ids` | TEXT | NOT NULL DEFAULT '[]' | JSON array |
| `model` | TEXT | NOT NULL DEFAULT 'unknown' | added via ALTER |
| `session_key` | TEXT | NOT NULL DEFAULT '' | v3.1 A1; backfilled from conversations |
| `suppressed_at` | TEXT | NULL | v3.1 A3 |
| `entity_index` | TEXT | NULL | JSON sidecar (§7.2 coref) |
| `contains_suppressed_leaves` | INTEGER | NOT NULL DEFAULT 0 | v3.1 A3 marker |
| `suppress_reason` | TEXT | NULL | v4.1.1 A2 |
| `superseded_by` | TEXT | NULL REFERENCES `summaries(summary_id)` ON DELETE SET NULL | v4.1.1 A2 forwarder |
| `leaf_summarizer_cap_was` | INTEGER | NULL | v4.1 forensic marker for 2415-token cap fix |

**Indexes:** `summaries_conv_created_idx`, `summaries_conv_depth_kind_idx`, `summaries_session_key_kind_latest_idx` (partial `WHERE session_key != ''`), `summaries_suppressed_idx` (partial `WHERE suppressed_at IS NOT NULL`), `summaries_contains_suppressed_idx` (partial `WHERE contains_suppressed_leaves = 1 AND superseded_by IS NULL`).
**Triggers:** `lcm_embedding_meta_cleanup_summary` (AFTER DELETE — cleans polymorphic embedding meta sidecar rows).
**Reads/writes:** `summary-store.ts`, integrity checks, prune cascade.

#### `message_parts`
| Column | Type | Constraint | Notes |
|---|---|---|---|
| `part_id` | TEXT | PRIMARY KEY | |
| `message_id` | INTEGER | NOT NULL REFERENCES `messages(message_id)` ON DELETE CASCADE | |
| `session_id` | TEXT | NOT NULL | |
| `part_type` | TEXT | NOT NULL CHECK IN (12 values) | text, reasoning, tool, patch, file, subtask, compaction, step_start, step_finish, snapshot, agent, retry |
| `ordinal` | INTEGER | NOT NULL | |
| `text_content` | TEXT | NULL | |
| `is_ignored` | INTEGER | NULL | |
| `is_synthetic` | INTEGER | NULL | |
| `tool_call_id` | TEXT | NULL | backfilled from `metadata.toolCallId` / `metadata.raw.id/call_id/toolCallId/tool_call_id` |
| `tool_name` | TEXT | NULL | backfilled |
| `tool_status` | TEXT | NULL | |
| `tool_input` | TEXT | NULL | backfilled |
| `tool_output` | TEXT | NULL | |
| `tool_error` | TEXT | NULL | |
| `tool_title` | TEXT | NULL | |
| `patch_hash`, `patch_files`, `file_mime`, `file_name`, `file_url`, `subtask_prompt`, `subtask_desc`, `subtask_agent`, `step_reason`, `step_cost` (REAL), `step_tokens_in` (INT), `step_tokens_out` (INT), `snapshot_hash`, `compaction_auto` (INT), `metadata` (TEXT/JSON) | various | NULL | per-part-type fields; sparse |

**UNIQUE:** `(message_id, ordinal)`.
**Indexes:** `message_parts_message_idx`, `message_parts_type_idx`.
**Belt-and-suspenders:** `ensureMessagePartsTable()` re-runs this CREATE outside the bulk block — guards against node:sqlite multi-statement aborts that left this table missing in production.

#### `summary_messages` (leaf → message edges)
| Column | Type | Constraint |
|---|---|---|
| `summary_id` | TEXT | NOT NULL REFERENCES `summaries(summary_id)` ON DELETE CASCADE |
| `message_id` | INTEGER | NOT NULL REFERENCES `messages(message_id)` ON DELETE **RESTRICT** |
| `ordinal` | INTEGER | NOT NULL |

**PK:** `(summary_id, message_id)`. **Indexes:** `summary_messages_message_idx` (created twice — once in bulk block + once after).

#### `summary_parents` (condensed → parent-summary edges)
Identical shape, with `parent_summary_id` REFERENCES `summaries(summary_id)` ON DELETE RESTRICT. **PK:** `(summary_id, parent_summary_id)`. **Index:** `summary_parents_parent_summary_idx`.

#### `context_items` (the assembled prompt ordering)
| Column | Type | Constraint |
|---|---|---|
| `conversation_id` | INTEGER | NOT NULL REFERENCES `conversations(conversation_id)` ON DELETE CASCADE |
| `ordinal` | INTEGER | NOT NULL |
| `item_type` | TEXT | NOT NULL CHECK IN ('message','summary') |
| `message_id` | INTEGER | NULL REFERENCES `messages(message_id)` ON DELETE RESTRICT |
| `summary_id` | TEXT | NULL REFERENCES `summaries(summary_id)` ON DELETE RESTRICT |
| `created_at` | TEXT | NOT NULL DEFAULT `datetime('now')` |

**PK:** `(conversation_id, ordinal)`. **CHECK:** exactly one of message_id/summary_id is non-NULL matching `item_type`. **Index:** `context_items_conv_idx`.

#### `large_files`
| Column | Type | Constraint |
|---|---|---|
| `file_id` | TEXT | PRIMARY KEY |
| `conversation_id` | INTEGER | NOT NULL REFERENCES `conversations(conversation_id)` ON DELETE CASCADE |
| `file_name` | TEXT | NULL |
| `mime_type` | TEXT | NULL |
| `byte_size` | INTEGER | NULL |
| `storage_uri` | TEXT | NOT NULL |
| `exploration_summary` | TEXT | NULL |
| `created_at` | TEXT | NOT NULL DEFAULT `datetime('now')` |

**Index:** `large_files_conv_idx`.

#### `conversation_bootstrap_state`
| Column | Type | Constraint |
|---|---|---|
| `conversation_id` | INTEGER | PRIMARY KEY REFERENCES `conversations(conversation_id)` ON DELETE CASCADE |
| `session_file_path` | TEXT | NOT NULL |
| `last_seen_size` | INTEGER | NOT NULL |
| `last_seen_mtime_ms` | INTEGER | NOT NULL |
| `last_processed_offset` | INTEGER | NOT NULL |
| `last_processed_entry_hash` | TEXT | NULL |
| `updated_at` | TEXT | NOT NULL DEFAULT `datetime('now')` |

**Index:** `bootstrap_state_path_idx ON (session_file_path, updated_at)`.

#### `conversation_compaction_telemetry`
PK `conversation_id` (FK CASCADE). Columns: `last_observed_cache_read INT`, `last_observed_cache_write INT`, `last_observed_prompt_token_count INT`, `last_observed_cache_hit_at TEXT`, `last_observed_cache_break_at TEXT`, `cache_state TEXT NOT NULL DEFAULT 'unknown' CHECK IN ('hot','cold','unknown')`, `consecutive_cold_observations INT NOT NULL DEFAULT 0`, `retention TEXT`, `last_leaf_compaction_at TEXT`, `turns_since_leaf_compaction INT NOT NULL DEFAULT 0`, `tokens_accumulated_since_leaf_compaction INT NOT NULL DEFAULT 0`, `last_activity_band TEXT NOT NULL DEFAULT 'low' CHECK IN ('low','medium','high')`, `last_api_call_at TEXT`, `last_cache_touch_at TEXT`, `provider TEXT`, `model TEXT`, `updated_at TEXT NOT NULL DEFAULT datetime('now')`. **Index:** `compaction_telemetry_state_idx ON (cache_state, updated_at)`.

#### `conversation_compaction_maintenance`
PK `conversation_id` (FK CASCADE). Columns: `pending INT NOT NULL DEFAULT 0`, `requested_at TEXT`, `reason TEXT`, `running INT NOT NULL DEFAULT 0`, `last_started_at TEXT`, `last_finished_at TEXT`, `last_failure_summary TEXT`, `token_budget INT`, `current_token_count INT`, `updated_at TEXT NOT NULL DEFAULT datetime('now')`.

#### `lcm_migration_state`
| Column | Type | Constraint |
|---|---|---|
| `step_name` | TEXT | NOT NULL |
| `algorithm_version` | INTEGER | NOT NULL |
| `completed_at` | TEXT | NOT NULL DEFAULT `datetime('now')` |

**PK:** `(step_name, algorithm_version)`. Tracks versioned backfills (`backfillSummaryDepths`, `backfillSummaryMetadata`, `backfillToolCallColumns` — all at algorithm_version 1 in pr-613).

### 2.2 FTS5 virtual tables (created only when `fts5Available`)

- **`messages_fts`**: `CREATE VIRTUAL TABLE messages_fts USING fts5(content, tokenize='porter unicode61')`. Standalone (not content-tracked); seeded by `INSERT … SELECT message_id, content FROM messages`. ConversationStore writes to it directly on every message insert/delete.
- **`summaries_fts`**: `fts5(summary_id UNINDEXED, content, tokenize='porter unicode61')`. Same standalone pattern.
- **`summaries_fts_cjk`** (created only when `trigramTokenizerAvailable`): `fts5(summary_id UNINDEXED, content, tokenize='trigram')` — for CJK substring search via OR semantics. Dropped automatically when trigram isn't available.

All three are recreated when a stale-schema marker is detected via `shouldRecreateStandaloneFtsTable()` (e.g. an old `content_rowid='summary_id'` config that's no longer compatible). Drops include the five shadow tables `<name>_{data,idx,content,docsize,config}`.

### 2.3 v4.1 schema additions (always created)

- **`lcm_feature_flags`** — `flag TEXT NOT NULL PK, value TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT datetime('now')`. Used for runtime-disable of optional features (e.g. semantic retrieval if vec0 fails to load).
- **`lcm_worker_lock`** — cross-process job lock for the worker sidecar. PK `job_kind TEXT NOT NULL`, plus `worker_id TEXT NOT NULL`, `acquired_at TEXT NOT NULL DEFAULT datetime('now')`, `expires_at TEXT NOT NULL`, `last_heartbeat_at TEXT NOT NULL DEFAULT datetime('now')`, `job_session_key TEXT`, `job_metadata TEXT`.
- **`lcm_extraction_queue`** — gateway atomic-write-with-queue-row pattern for entity coref / procedure-recheck. PK `queue_id TEXT`, FK `leaf_id` → `summaries(summary_id)` ON DELETE CASCADE, `kind TEXT CHECK IN ('entity','procedure-recheck')`, plus `queued_at`, `picked_at`, `worker_id`, `completed_at`, `attempts INT NOT NULL DEFAULT 0`, `last_error`. **Indexes:** `_pending_idx WHERE picked_at IS NULL`, `_dead_letter_idx WHERE attempts >= 5`.
- **`lcm_session_key_audit`** — log of session_key re-keys (for `/lcm undo-session-key-rekey`). PK `audit_id`, FK `conversation_id` CASCADE, `original_session_key`, `new_session_key NOT NULL`, `reason NOT NULL`, `applied_at`, `applied_by NOT NULL DEFAULT 'migration'`. **Index:** `_conv_idx ON (conversation_id, applied_at DESC)`.

### 2.4 Synthesis layer (v4.1 A.04 / B4)

- **`lcm_prompt_registry`** — versioned prompts per `memory_type × tier × pass_kind`. CHECKs enforce 6 memory_types (`episodic-leaf`, `episodic-condensed`, `episodic-yearly`, `procedural-extract`, `entity-extract`, `theme-consolidation`) and 3 pass_kinds (`single`, `verify_fidelity`, `best_of_n_judge`). UNIQUE `(memory_type, tier_label, pass_kind, version)` + a null-safe COALESCE UNIQUE INDEX `lcm_prompt_registry_uniq_lookup`. Partial index for active rows. **Seeded** by `seedDefaultPrompts()` at migration time (unless caller passes `seedDefaultPrompts: false`).
- **`lcm_synthesis_cache`** — rebuildable cache for ad-hoc synthesize() output. UNIQUE on `(session_key, range_start, range_end, leaf_fingerprint, COALESCE(grep_filter, ''), tier_label, prompt_id)` enables INSERT OR IGNORE cross-process single-flight. `tier_label CHECK IN ('year','yearly','monthly','weekly','daily','custom','filtered')` — note `year` is kept alongside `yearly` for backwards-compat. **Migration drops + recreates** if old narrow CHECK detected (cache rows are derivable, safe to wipe). Indexes: `_built_idx`, `_status_building_idx WHERE status = 'building'`, plus the UNIQUE index above. Also: orphaned `lcm_synthesis_audit` rows referencing `target_cache_id` are deleted before DROP to prevent dangling refs.
- **`lcm_cache_leaf_refs`** — inverse index from cache row → leaves it referenced. PK `(cache_id, leaf_summary_id)` with both FKs ON DELETE CASCADE. **Index:** `_by_leaf_idx ON (leaf_summary_id)`. Used by suppression path to drop stale caches.
- **`lcm_synthesis_audit`** — per-pass log for the synthesis pipeline. `target_summary_id` and `target_cache_id` both NULLable FKs; `CHECK (target_summary_id IS NOT NULL OR target_cache_id IS NOT NULL)`. Indexes by target + by `pass_session_id` + two partial GC indexes (`WHERE status='started'` for orphan sweep + `WHERE status IN ('completed','failed')` for 30-day retention sweep).

### 2.5 Eval harness (v4.1 A.05)

- **`lcm_eval_query_set`** — versioned query set roots.
- **`lcm_eval_query`** — individual queries with `stratum CHECK IN ('fts-easy','fts-medium','paraphrastic')`, `expected_topics`, `expected_sources`, `reference_summary`, `must_not_regress INT NOT NULL DEFAULT 0`, `rubric NOT NULL`. Indexes: `_stratum_idx`, `_must_not_regress_idx WHERE must_not_regress = 1`.
- **`lcm_eval_run`** — `retrieval_recall_score REAL`, `synthesis_quality_score REAL`, `per_query_scores TEXT` (JSON), `judge_models TEXT` (JSON), `noise_floor_sd REAL`, `trigger CHECK IN ('manual','prompt-update','model-update','ci','nightly')`. Index: `_recent_idx ON (query_set_id, ran_at DESC)`.
- **`lcm_eval_drift`** — cumulative regression delta tracking.

### 2.6 Entity layer (v4.1 A.06)

- **`lcm_entity_type_registry`** — `type_name TEXT PK, first_seen_at, occurrence_count`. Types are freeform (no CHECK enum per v4.1.1 §C).
- **`lcm_entities`** — `entity_id TEXT PK`, `session_key NOT NULL`, `canonical_text NOT NULL`, `entity_type NOT NULL`, `first_seen_at`, `last_seen_at`, `first_seen_in_summary_id` FK SET NULL, `occurrence_count`, `alternate_surfaces TEXT` (JSON), `metadata TEXT`. UNIQUE `(session_key, canonical_text COLLATE NOCASE)` enables single-flight INSERT OR IGNORE. Index `_lookup_idx ON (session_key, entity_type, last_seen_at DESC)`.
- **`lcm_entity_mentions`** — `mention_id PK`, FK `entity_id` CASCADE, FK `summary_id` CASCADE, `surface_form NOT NULL`, `span_start INT`, `span_end INT`, `mentioned_at NOT NULL`. Two by-entity / by-summary indexes.

### 2.7 Embedding registry (v4.1 A.07 — managed sidecar)

- **`lcm_embedding_profile`** — `model_name TEXT PK`, `dim NOT NULL`, `registered_at`, `active`, `archive_after`.
- **`lcm_embedding_meta`** — composite PK `(embedded_id, embedded_kind, embedding_model)`. `embedded_kind CHECK IN ('summary','entity','theme')`. **No FK on embedded_id** (polymorphic) — cleanup handled by trigger below for the summary case. Indexes: `_active_idx ON (embedding_model, embedded_at DESC) WHERE archived = 0`, `_by_kind_idx`.

The vec0 virtual table itself (`lcm_embeddings_<model_slug>`) is **NOT** created in this migration — `src/embeddings/store.ts` (Epic 04 in the Python port) loads sqlite-vec at runtime and creates it on demand.

### 2.8 Triggers

There is **exactly one trigger** declared by `migration.ts`:

```sql
CREATE TRIGGER IF NOT EXISTS lcm_embedding_meta_cleanup_summary
  AFTER DELETE ON summaries
  BEGIN
    DELETE FROM lcm_embedding_meta
      WHERE embedded_id = OLD.summary_id
        AND embedded_kind = 'summary';
  END
```

Additional triggers for vec0 cascade (suppression flip + DELETE) and per-model embeddings live in `src/embeddings/store.ts` — out of scope for storage epic.

### 2.9 Tables removed in first-principles pass (2026-05-06)

Documented as comments in `migration.ts` but **no schema impact**. Preserved in deferred-features draft PR #616:
- `lcm_purge_rebuild_queue` — hard-delete drainer queue (never built)
- `lcm_voyage_rate_state` — voyage rate-limit state (zero readers/writers)
- `lcm_procedures` — procedures feature (no agent tool, no LLM injection)
- `lcm_intentions` — prospective intent (zero producer/consumer)
- `lcm_themes` / `lcm_theme_sources` — themes feature (half-shipped UX)

The Python port should **NOT** recreate these. Document the deferral choice in an ADR if Hermes wants any of them.

### 2.10 Tables touched but not created

`lcm_rollups` — Eva's fork-side legacy table. Migration step `backfillForkRollupsSessionKeys` backfills its session_key column **only if the table exists**. Upstream installs (and lossless-hermes) never have this table; safe no-op.

`lcm_migration_flags` — Eva's fork-side legacy. Explicitly NOT touched (v4.1.1 A8 originally proposed extending it; superseded by clean new `lcm_feature_flags`).

---

## 3. PRAGMA + connection setup

Applied by `configureConnection()` in `src/db/connection.ts`, in this exact order:

| PRAGMA | Value | Rationale |
|---|---|---|
| `journal_mode` | `WAL` | concurrent readers + 1 writer |
| `busy_timeout` | `30_000` ms | 30s — production saw 10+ concurrent writers OOM-ing the default 5s |
| `foreign_keys` | `ON` | required for every CASCADE/RESTRICT in the schema |
| (assert FK enforcement is **actually on**) | — | calls `assertForeignKeysEnabled(db)` — guards against future opens that bypass this function |
| `cache_size` | `-65536` | 64 MB page cache (demand-allocated, released on close) |
| `synchronous` | `NORMAL` | crash-safe for app crashes; power-failure risk acceptable since bootstrap re-ingests |
| `temp_store` | `MEMORY` | keeps temp indexes/tables in RAM |

On close, `PRAGMA optimize` runs best-effort.

Connection-open option `{ allowExtension: true }` is required to permit `sqlite-vec.load(conn)` later in Group B. **Python equivalent:** `conn = sqlite3.connect(path)`; then `conn.enable_load_extension(True)`. The spike (`docs/spike-results/001-sqlite-vec-python.md`) confirmed stdlib `sqlite3` exposes this on Homebrew python@3.12/3.14 and python.org installers — but **NOT** on macOS system `/usr/bin/python3` 3.9.

Connection tracking (`connectionsByPath`, `connectionIndex`) exists primarily for test fixtures that need to close-by-path. Python port: replicate the registry with a module-level `dict[str, set[Connection]]` guarded by a `threading.Lock` (Python sqlite3 is thread-safe but connections aren't shareable across threads — each thread should get its own, unless we adopt apsw).

---

## 4. Stores — class-by-class spec

### 4.1 `ConversationStore` (`src/store/conversation-store.ts`, 1071 LOC)

- **Python target:** `src/lossless_hermes/store/conversation.py`
- **Constructor:** `new ConversationStore(db, { fts5Available })`
- **Public methods** (all async in TS — port to async or sync depending on Epic 01 ADR):

| Method | Signature | Purpose |
|---|---|---|
| `createConversation` | `(input: CreateConversationInput) → ConversationRecord` | Insert + return shaped record |
| `getConversation` | `(id) → ConversationRecord | null` | By PK |
| `getConversationBySessionId` | `(sessionId) → ConversationRecord | null` | Newest active by session_id |
| `getConversationBySessionKey` | `(sessionKey) → ConversationRecord | null` | Active row matching the partial UNIQUE index |
| `getConversationFamilyIds` | `({ sessionKey \| sessionId, includeArchived }) → number[]` | Cross-conv ID list |
| `getConversationForSession` | `({ sessionId, sessionKey }) → ConversationRecord | null` | Resolve which conv to ingest into |
| `listActiveConversations` | `(limit?) → ConversationRecord[]` | Recent active |
| `getOrCreateConversation` | `(input, opts?) → ConversationRecord` | Atomic find-or-insert via UPSERT path |
| `markConversationBootstrapped` | `(id) → void` | Sets `bootstrapped_at` |
| `archiveConversation` | `(id) → void` | active=0 + archived_at=now |
| `createMessage` | `(input) → MessageRecord` | Single insert; auto-computes identity_hash; indexes FTS |
| `createMessagesBulk` | `(inputs[]) → MessageRecord[]` | Same wrapped in transaction |
| `getMessages` | `(conversationId, { since?, before?, limit?, offset? }) → MessageRecord[]` | Range |
| `getLastMessage` | `(conversationId) → MessageRecord | null` | |
| `hasMessage` | `(conversationId, identityHash) → boolean` | Dedupe check |
| `countMessagesByIdentity` | `(conversationId, identityHash) → number` | |
| `getMessageById` | `(messageId) → MessageRecord | null` | |
| `createMessageParts` | `(messageId, parts[]) → void` | Bulk insert of typed parts |
| `getMessageParts` | `(messageId) → MessagePartRecord[]` | |
| `getMessageCount` | `(conversationId) → number` | |
| `getMaxSeq` | `(conversationId) → number` | For next-seq derivation |
| `deleteMessages` | `(messageIds[]) → number` | Returns row count; cascades parts + FTS |
| `searchMessages` | `(MessageSearchInput) → MessageSearchResult[]` | Dispatcher for FTS / LIKE / regex |
| `withTransaction<T>` | `(fn) → Promise<T>` | Convenience wrapper via `withDatabaseTransaction` |
| `private indexMessageForFullText` | `(messageId, content) → void` | FTS5 INSERT |
| `private deleteMessageFromFullText` | `(messageId) → void` | FTS5 DELETE |
| `private searchFullText` / `searchLike` / `searchRegex` | various | Backend implementations |

- **Dependencies on other stores:** none. Uses `transaction-mutex`, `conversation-scope`, `fts5-sanitize`, `full-text-fallback`, `full-text-sort`, `message-identity`, `parse-utc-timestamp`.
- **TS-specific gotchas:**
  - **BigInt-vs-Number boundary** in `node:sqlite`: TS code calls `Number(row.message_id)` defensively in places. Python `int` is arbitrary-precision — drop these casts.
  - `JSON.parse(metadata)` for message_parts metadata — handle invalid JSON gracefully (TS code uses try/catch + warns).
  - **Regex search path** uses Node's RegExp engine. Python port: `re.search()` with the same flags, but be aware of subtle Unicode-class differences. Add a parity test.
  - **Snippet building** uses byte offsets into the content TEXT. Python str slicing is by code point — matches TS String slicing semantics, so this should port directly (both are UTF-16 in TS — Python is code-point-based — possible edge cases with surrogate pairs in CJK).
  - The `fts5Available` flag toggles which code paths run. Probe lazily (once per DB) and cache.

### 4.2 `SummaryStore` (`src/store/summary-store.ts`, 1668 LOC)

- **Python target:** `src/lossless_hermes/store/summary.py`
- **Constructor:** `new SummaryStore(db, { fts5Available, trigramTokenizerAvailable })`
- **Public methods:**

| Method | Signature | Purpose |
|---|---|---|
| `insertSummary` | `(CreateSummaryInput) → SummaryRecord` | Insert leaf/condensed; updates FTS |
| `getSummary` | `(summaryId) → SummaryRecord | null` | |
| `getSummariesByConversation` | `(convId) → SummaryRecord[]` | |
| `linkSummaryToMessages` | `(summaryId, messageIds[]) → void` | summary_messages rows |
| `linkSummaryToParents` | `(summaryId, parentIds[]) → void` | summary_parents rows |
| `getSummaryMessages` | `(summaryId) → number[]` | |
| `getConversationMaxSummaryDepth` | `(convId) → number | null` | |
| `getLeafSummaryLinksForMessageIds` | `(ids[]) → MessageLeafSummaryLinkRecord[]` | Inverse lookup for search-hit expansion |
| `listTranscriptGcCandidates` | `(opts) → TranscriptGcCandidateRecord[]` | Find messages safe to GC (covered by ≥1 leaf summary; not in context_items) |
| `getSummaryChildren` | `(summaryId) → SummaryRecord[]` | |
| `getSummaryParents` | `(summaryId) → SummaryRecord[]` | |
| `getSummarySubtree` | `(summaryId) → SummarySubtreeNodeRecord[]` | Recursive walk via CTE |
| `getContextItems` | `(convId) → ContextItemRecord[]` | Assembled prompt ordering |
| `getDistinctDepthsInContext` | `(convId) → number[]` | |
| `pruneForNewSession` | `(convId, retainDepth) → void` | Truncate context_items |
| `appendContextMessage` | `(convId, messageId) → void` | |
| `appendContextMessages` | `(convId, ids[]) → void` | Bulk |
| `appendContextSummary` | `(convId, summaryId) → void` | |
| `replaceContextRangeWithSummary` | `({convId, fromOrdinal, toOrdinal, summaryId}) → void` | Atomic; uses tx mutex |
| `getContextTokenCount` | `(convId) → number` | Sum across rows joined to messages/summaries |
| `searchSummaries` | `(SummarySearchInput) → SummarySearchResult[]` | FTS5 / FTS-CJK / LIKE / LIKE-CJK / regex dispatcher |
| `insertLargeFile` | `(CreateLargeFileInput) → LargeFileRecord` | |
| `getLargeFile` | `(fileId) → LargeFileRecord | null` | |
| `getLargeFilesByConversation` | `(convId) → LargeFileRecord[]` | |
| `getConversationBootstrapState` | `(convId) → ConversationBootstrapStateRecord | null` | |
| `upsertConversationBootstrapState` | `(UpsertConversationBootstrapStateInput) → void` | |
| Many private CJK helpers | `extractCjkSegments`, `extractLatinTokens`, `splitCjkChunks`, `searchCjkTrigram`, `searchLikeCjk` | CJK-aware path |
| `withTransaction<T>` | `(fn) → T` | |

- **Dependencies:** Same as conversation-store + `transaction-mutex`.
- **Notable gotchas:**
  - **Externalized tool-output references** are detected by a regex in `searchFullText` so we don't surface `lcm_describe` boilerplate as relevant content. Port this carefully (see `test/fts-fallback.test.ts:106` `"ignores lcm_describe helper text"`).
  - **CJK detection** (`containsCjk`) covers CJK Unified, Compat, Kana, Hangul. Python regex translates 1:1.
  - **Recursive subtree walks** use SQLite's `WITH RECURSIVE` — port verbatim.

### 4.3 `CompactionTelemetryStore` (`src/store/compaction-telemetry-store.ts`, 204 LOC)

- **Python target:** `src/lossless_hermes/store/compaction_telemetry.py`
- Methods: `withTransaction`, `getConversationCompactionTelemetry(convId)`, `upsertConversationCompactionTelemetry(input)`.
- Pure CRUD around `conversation_compaction_telemetry`. Mostly mechanical port.

### 4.4 `CompactionMaintenanceStore` (`src/store/compaction-maintenance-store.ts`, 219 LOC)

- **Python target:** `src/lossless_hermes/store/compaction_maintenance.py`
- Methods: `withTransaction`, `getConversationCompactionMaintenance`, `requestProactiveCompactionDebt`, `markProactiveCompactionRunning`, `markProactiveCompactionFinished`. Coalesced single-row state machine per conversation (no queue).

### 4.5 Support modules

- **`conversation-scope.ts`** (34 LOC): `appendConversationScopeConstraint({ where, args, columnExpr, conversationId?, conversationIds? })` — mutates the `where` / `args` arrays to append `column = ?` or `column IN (?, ?, ...)`. Pure function. Port as `append_conversation_scope_constraint(where_list, args_list, column_expr, conversation_id=None, conversation_ids=None)`.
- **`fts5-sanitize.ts`** (50 LOC): `sanitizeFts5Query(raw) → string`. Trivial regex tokenizer. Pure function.
- **`full-text-sort.ts`** (21 LOC): `buildFtsOrderBy(sort, createdAtExpr) → string`. Constant `AGE_DECAY_RATE = 0.001`.
- **`full-text-fallback.ts`** (84 LOC): `containsCjk(text) → bool`, `buildLikeSearchPlan(column, query) → { terms, where, args }`, `createFallbackSnippet(content, terms) → string`. Pure functions.
- **`parse-utc-timestamp.ts`** (26 LOC): `parseUtcTimestamp`, `parseUtcTimestampOrNull`. Python: use `datetime.fromisoformat()` after appending 'Z' / replacing space with 'T'. Set `tzinfo=UTC` explicitly.
- **`message-identity.ts`** (13 LOC): `buildMessageIdentityKey(role, content)` = `f"{role}\x00{content}"`, `buildMessageIdentityHash(role, content)` = `hashlib.sha256(role.encode() + b'\x00' + content.encode()).hexdigest()`. The TS version uses three sequential `.update()` calls with ` ` between — bit-for-bit equivalent in Python with `hashlib`.

---

## 5. Migration script #628 (status check)

The migration script `scripts/lcm-blob-migrate.mjs` (365 LOC) was added on **main** in commit `13780e9` (PR #628, merged 2026-05-11). It is **NOT** present on the `pr-613` branch we're porting.

Decision required (ADR):
- **Option A — port pr-613 first, defer #628 stub-tier work to Epic later.** Cleaner scope: the v4.2 stub-tier externalization adds 32 lines to `migration.ts` (one `is_externalized` column on message_parts), 46 lines to `conversation-store.ts`, plus the 365-line external script. Easier to land Phase 1 storage without entangling with stub-tier policy.
- **Option B — rebase pr-613 on main locally and include #628.** Larger initial scope but ships v4.2 storage immediately.

The script itself does idempotent batch externalization of old tool-result rows: walks `message_parts WHERE part_type='tool' AND tool_output IS NOT NULL AND length(tool_output) > threshold`, copies tool_output into a file under `largeFilesDir/`, replaces `tool_output` with an externalized reference, marks `is_externalized=1`. Stateless re-run is safe.

**Recommended:** Option A. Port pr-613 verbatim, then a dedicated v4.2 epic adds the column + script. Reflected in port-order below.

---

## 6. Path mapping

```
TS                                                  →   Python
src/db/connection.ts                                →   src/lossless_hermes/db/connection.py
src/db/features.ts                                  →   src/lossless_hermes/db/features.py
src/db/config.ts                                    →   src/lossless_hermes/db/config.py
src/db/migration.ts                                 →   src/lossless_hermes/db/migration.py
src/store/conversation-store.ts                     →   src/lossless_hermes/store/conversation.py
src/store/summary-store.ts                          →   src/lossless_hermes/store/summary.py
src/store/compaction-telemetry-store.ts             →   src/lossless_hermes/store/compaction_telemetry.py
src/store/compaction-maintenance-store.ts           →   src/lossless_hermes/store/compaction_maintenance.py
src/store/conversation-scope.ts                     →   src/lossless_hermes/store/conversation_scope.py
src/store/fts5-sanitize.ts                          →   src/lossless_hermes/store/fts5_sanitize.py
src/store/full-text-sort.ts                         →   src/lossless_hermes/store/full_text_sort.py
src/store/full-text-fallback.ts                     →   src/lossless_hermes/store/full_text_fallback.py
src/store/parse-utc-timestamp.ts                    →   src/lossless_hermes/store/parse_utc_timestamp.py
src/store/message-identity.ts                       →   src/lossless_hermes/store/message_identity.py
src/store/index.ts                                  →   src/lossless_hermes/store/__init__.py
src/large-files.ts                                  →   src/lossless_hermes/large_files.py
src/integrity.ts                                    →   src/lossless_hermes/integrity.py
src/prune.ts                                        →   src/lossless_hermes/prune.py
src/transcript-repair.ts                            →   src/lossless_hermes/transcript_repair.py
src/transaction-mutex.ts                            →   src/lossless_hermes/transaction_mutex.py
```

---

## 7. Python module layout (recommended)

```
src/lossless_hermes/
├── __init__.py
├── db/
│   ├── __init__.py
│   ├── connection.py        # ← src/db/connection.ts
│   ├── features.py          # ← src/db/features.ts
│   ├── config.py            # ← src/db/config.ts
│   └── migration.py         # ← src/db/migration.ts
├── store/
│   ├── __init__.py          # ← src/store/index.ts (re-exports)
│   ├── message_identity.py  # ← src/store/message-identity.ts
│   ├── parse_utc_timestamp.py  # ← src/store/parse-utc-timestamp.ts
│   ├── conversation_scope.py   # ← src/store/conversation-scope.ts
│   ├── fts5_sanitize.py        # ← src/store/fts5-sanitize.ts
│   ├── full_text_sort.py       # ← src/store/full-text-sort.ts
│   ├── full_text_fallback.py   # ← src/store/full-text-fallback.ts
│   ├── conversation.py         # ← src/store/conversation-store.ts
│   ├── summary.py              # ← src/store/summary-store.ts
│   ├── compaction_telemetry.py # ← src/store/compaction-telemetry-store.ts
│   └── compaction_maintenance.py # ← src/store/compaction-maintenance-store.ts
├── transaction_mutex.py     # ← src/transaction-mutex.ts
├── large_files.py           # ← src/large-files.ts
├── integrity.py             # ← src/integrity.ts
├── prune.py                 # ← src/prune.ts
└── transcript_repair.py     # ← src/transcript-repair.ts
```

`store/__init__.py` exports the same names as `src/store/index.ts`: stores + the typed-dict shapes (or Pydantic models if the Epic-01 ADR picks Pydantic).

---

## 8. Test inventory (storage-relevant)

| TS test file | Tests | LOC | Coverage area | Python target |
|---|---:|---:|---|---|
| `test/db-connection.test.ts` | 4 | 28 | Path-helper purity | `tests/test_db_connection.py` |
| `test/migration.test.ts` | 16 | 1208 | Migration end-to-end: depth backfill, identity hashes, FTS recreate (stale schema + missing shadow), session_key uniqueness flip, message_parts belt-and-suspenders, tool_call_id backfill, idempotency, savepoint retry | `tests/test_migration.py` |
| `test/v41-pre-existing-schema-migration.test.ts` | 2 | 233 | v4.1 columns added to legacy DB; idempotency | `tests/test_v41_pre_existing_schema.py` |
| `test/v41-summaries-columns.test.ts` | 12 | 196 | Every v4.1 column with default, FK behavior, idempotency | `tests/test_v41_summaries_columns.py` |
| `test/v41-data-cleanup.test.ts` | 6 | 197 | session_key NULL→`legacy:conv_<id>` backfill, audit row, summary session_key fill from conv | `tests/test_v41_data_cleanup.py` |
| `test/v41-indexes.test.ts` | 6 | 73 | Five v4.1 indexes created + idempotent | `tests/test_v41_indexes.py` |
| `test/v41-support-tables.test.ts` | 4 | 142 | feature_flags, worker_lock, extraction_queue, session_key_audit | `tests/test_v41_support_tables.py` |
| `test/v41-embedding-meta-tables.test.ts` | 6 | 118 | embedding_profile, embedding_meta, polymorphic FK behavior | `tests/test_v41_embedding_meta.py` |
| `test/v41-entity-layer-tables.test.ts` | 7 | 130 | entities (UNIQUE COLLATE NOCASE), entity_mentions, type_registry | `tests/test_v41_entity_layer.py` |
| `test/v41-eval-tables.test.ts` | 8 | 159 | query_set, query, run, drift; CHECK constraints | `tests/test_v41_eval_tables.py` |
| `test/v41-suppression-cascade-trigger.test.ts` | 9 | 303 | embedding_meta_cleanup_summary trigger; idempotency. vec0 cases gated by extension. | `tests/test_v41_suppression_cascade.py` |
| `test/v41-schema-drift-invariants.test.ts` | 19 | 654 | Cross-file source-of-truth invariants (FK ON DELETE clauses; TierLabel CHECK ⊆ runtime values; manifest ↔ factories; etc.) | `tests/test_v41_schema_drift.py` |
| `test/summary-store.test.ts` | 2 | 168 | Shallow-tree helpers; LIKE fallback ordering | `tests/test_summary_store.py` |
| `test/message-identity.test.ts` | 1 | 60 | Exact-match lookup on identity_hash with many same-hash rows | `tests/test_message_identity.py` |
| `test/parse-utc-timestamp.test.ts` | 5 | 34 | UTC reinterpretation edge cases | `tests/test_parse_utc_timestamp.py` |
| `test/fts5-sanitize.test.ts` | 17 | 76 | Sanitization across boolean ops, NEAR, caret, quotes, phrases | `tests/test_fts5_sanitize.py` |
| `test/fts-fallback.test.ts` | 6 | 366 | LIKE search; CJK bypass; lcm_describe boilerplate ignored | `tests/test_fts_fallback.py` |
| `test/large-files.test.ts` | 8 | 120 | Block parsing, MIME→ext, file-id extraction, exploration summaries | `tests/test_large_files.py` |
| `test/prune.test.ts` | 18 | 460 | parseDuration; dry-run; batch deletion; VACUUM; cascade behavior | `tests/test_prune.py` |
| `test/transcript-repair.test.ts` | 3 | 67 | Tool-use ↔ tool-result pairing; reasoning placement | `tests/test_transcript_repair.py` |
| `test/transaction-mutex.test.ts` | (8 it-blocks) | 481 | Lock serialization, nested savepoint, cross-store, 10-way stress | `tests/test_transaction_mutex.py` |
| `test/compaction-maintenance-store.test.ts` | 1 | 65 | State machine flow | `tests/test_compaction_maintenance.py` |
| `test/lcm-integration.test.ts` | 77 | 3965 | End-to-end pyramid behavior — storage *is* the substrate but many cases are assembler/compaction. Partition: storage-only cases (~25) port here; rest live in Epic 02. | `tests/test_lcm_integration_storage.py` (partial — split by epic) |

**Total storage-relevant tests:** ~225 cases across ~9300 LOC of TS. Estimate Python test LOC at ~10,500 (Python's pytest is slightly more verbose than vitest's `describe/it` for fixture wiring).

Test fixtures (`test/fixtures/`):
- `v41-mock-llm.ts` — stub LLM for end-to-end synthesis. Out of storage scope.
- `v41-stress-corpus.ts`, `v41-test-corpus.ts` — seed data builders. **In scope**: port the seed builders so Python tests can build identical fixtures.
- `v41-tool-harness.ts` — tool-call mock. Out of storage scope.

---

## 9. Port order (dependency-aware)

### Phase 0: pure-function leaves (no deps)
1. `store/message_identity.py` — sha256
2. `store/parse_utc_timestamp.py` — UTC parsing
3. `store/conversation_scope.py` — WHERE-fragment builder
4. `store/fts5_sanitize.py` — query sanitizer
5. `store/full_text_sort.py` — ORDER BY builder
6. `store/full_text_fallback.py` — LIKE plan + snippet
7. `db/features.py` — FTS5 / trigram probe (depends on a Connection but trivial)

### Phase 1: connection + config (depends only on stdlib)
8. `db/connection.py` — open/track/close + PRAGMAs + extension flag
9. `db/config.py` — env + plugin-config resolution (purely declarative; no DB deps)

### Phase 2: transaction primitive
10. `transaction_mutex.py` — depends on a `Connection` type. Python concurrency model question (asyncio vs threading) ⟵ **ADR required before this step** (see §10).

### Phase 3: schema
11. `db/migration.py` — depends on `db/connection.py`, `db/features.py`, `store/message_identity.py`, `store/parse_utc_timestamp.py`. Defer the `seedDefaultPrompts` import (synthesis module is Epic 03) — for storage epic, accept the prompt-registry table being empty and have callers explicitly seed later.

### Phase 4: core stores
12. `store/conversation.py` — depends on connection + transaction_mutex + helpers + identity
13. `store/summary.py` — depends on connection + transaction_mutex + helpers (parallel-portable with #12 by different engineer — they share no methods)

### Phase 5: derivative stores
14. `store/compaction_telemetry.py`
15. `store/compaction_maintenance.py`
16. `store/__init__.py` — re-export barrel

### Phase 6: top-level utilities
17. `large_files.py` — only depends on stdlib (regex, hashlib, JSON). No DB. Highly parallel-portable.
18. `transcript_repair.py` — pure functions; no DB. Parallel-portable.
19. `prune.py` — depends on connection only. Pure SQL + helper.
20. `integrity.py` — depends on `store/conversation.py` + `store/summary.py`. Port last.

Parallelism: a 3-engineer team can run #12 + #13 + #17 + #18 concurrently after Phase 3 lands.

---

## 10. Migration story for existing OpenClaw LCM users

Existing data lives at `~/.openclaw/lcm.db` (or `$OPENCLAW_STATE_DIR/lcm.db`). The Python port targets `$HERMES_HOME/lcm/lcm.db` (assuming Hermes adopts a dedicated LCM subdirectory — confirm in Hermes-side ADR).

### 10.1 Copy strategy

The TS migration is **fully idempotent** — `runLcmMigrations()` on an existing DB is a no-op for already-present columns/tables/indexes and re-runs only versioned backfills (tracked in `lcm_migration_state`). Therefore the cleanest migration is:

1. **Detect** an existing `~/.openclaw/lcm.db` (or `$OPENCLAW_STATE_DIR/lcm.db`) at first launch of `lossless-hermes`.
2. **Copy** it to `$HERMES_HOME/lcm/lcm.db` (use `shutil.copy2` for atime/mtime preservation; copy WAL + SHM files too if present, though they're auto-recreated).
3. **Run** the Python `run_lcm_migrations()`. Because it's idempotent, any partial-fork-side schema (Eva's `lcm_rollups`, `lcm_migration_flags`) is left alone; the `backfillForkRollupsSessionKeys` step picks it up if present.
4. **Record** the migration timestamp in a Hermes-side `state_meta` row (key `lcm_db_imported_at`) so we don't re-import on next launch.

The TS code does not delete `~/.openclaw/lcm.db` after copy — leave it intact so the original OpenClaw install keeps working until the user removes it. Document this.

### 10.2 Schema-version compatibility

LCM does **not** use a monotonic `schema_version` integer (unlike Hermes's `hermes_state.py` with `SCHEMA_VERSION = 11`). Instead it relies on:
- `CREATE TABLE IF NOT EXISTS` + PRAGMA-driven column adds = forward-compatible
- `lcm_migration_state` (`step_name`, `algorithm_version`) for **versioned backfills** that have algorithm-version semantics. Today: `backfillSummaryDepths` v1, `backfillSummaryMetadata` v1, `backfillToolCallColumns` v1.

The Python port should preserve the same `lcm_migration_state` schema and algorithm-version integers. If the import-from-OpenClaw step finds rows already at v1, those backfills are skipped — exactly the desired behavior.

**If we ever introduce schema regressions** (drops, type changes, retired tables), we'd need a `lcm_schema_version` integer table. Document this as out-of-scope today; revisit only when an actual regression is needed.

### 10.3 Edge case: vec0 + sqlite-vec extension across the migration

If the source DB has `lcm_embeddings_<model>` virtual tables, they survive the file copy. On reopen in Python, sqlite-vec must be loadable (spike 001 confirms it is, on Homebrew python 3.12+/3.14). If it fails, queries against those tables error — but the migration itself doesn't touch them. Document the failure mode: feature-flag the semantic-retrieval path to `disabled` and surface a one-time warning.

### 10.4 WAL-on-network-filesystem caveat (inherited from Hermes)

`hermes_state.py` already has WAL-fallback-to-DELETE for NFS/SMB/FUSE filesystems (see lines 40–60). The LCM connection helper should reuse the same `apply_wal_with_fallback()` pattern — refactor Hermes's helper out of `hermes_state.py` into a shared `db_utils.py` and import from both sides.

---

## 11. Open architecture decisions (ADRs to write)

### ADR-001: DB location vs. Hermes state DB
- **Question:** Own DB at `$HERMES_HOME/lcm/lcm.db` (separate file) vs. attach as a single Hermes `state.db` with `lcm_` table prefix everywhere.
- **Recommendation:** **Separate file.** Rationale: LCM's WAL pressure is high (frequent message inserts + FTS5 updates), and isolating it keeps Hermes's session/cron/kanban DBs un-blocked. Also enables independent backup/restore + simpler import from `~/.openclaw/lcm.db`. Schema names stay `lcm_*` for clarity but no rename needed.

### ADR-002: Python sqlite3 backend
- **Question:** stdlib `sqlite3` vs. `pysqlite3-binary` vs. `apsw`.
- **Recommendation:** **stdlib `sqlite3` as primary, `apsw` as documented fallback.** Spike 001 PASS at 95% confidence — Homebrew python 3.12/3.14 and python.org installers all expose `enable_load_extension`. `pysqlite3-binary` has no macOS wheels (ruled out). `apsw` works as fallback but has different API (e.g. `enableloadextension`, no PEP 249 cursor() needed) — keep behind a flag/conditional import so we don't pay the API-divergence cost unless an env actually needs it.
- **Companion spike:** 005 (TBD) — verify behavior under thread/process concurrency and confirm WAL parity with Node's `node:sqlite`.

### ADR-003: Sync vs. async DB layer
- **Question:** The TS code is async (Promise-returning methods + `withDatabaseTransaction`). Hermes-agent's `hermes_state.py` is **synchronous** (uses a single `threading.Lock` + `_execute_write` retry loop).
- **Options:**
  - **A (sync-and-thread):** match `hermes_state.py` — synchronous `Connection` per thread, `threading.Lock` for the per-DB mutex, `_execute_write` retry loop for BUSY. Port `transaction_mutex.py` as a `threading.Lock`-backed reentrant mutex with a `contextlib`-managed savepoint stack. **Pro:** consistent with Hermes. **Con:** loses the async parallelism the TS code exploits.
  - **B (async, asyncio):** wrap stdlib `sqlite3` with `asyncio.to_thread` (Python 3.9+) or `aiosqlite`. Preserves the TS shape but adds dependency + complexity. **Pro:** preserves API parity. **Con:** Hermes-agent isn't asyncio-native.
- **Recommendation:** **A (sync, threading-based)** matching Hermes's existing patterns. Document the divergence from TS semantics; most callers in Hermes are sync today.

### ADR-004: lcm_migration_state and versioned-backfill strategy
- **Question:** Keep TS's `(step_name, algorithm_version)` two-column PK pattern, or switch to a monotonic integer `schema_version`?
- **Recommendation:** **Keep as-is.** It's the minimum viable invariant — additive forward migrations work without it; only algorithm-version semantics need it. Adding a monotonic version is breakage-driven (re-evaluate when a future change actually requires it).

### ADR-005: How to seed default synthesis prompts during storage-epic migration
- **Question:** `runLcmMigrations` calls `seedDefaultPrompts(db)` from `src/synthesis/seed-default-prompts.js`. The Python synthesis module won't exist until Epic 03.
- **Options:**
  - **A:** Have `migration.py` accept a `seed_default_prompts: Callable[[Connection], dict] | None = None` parameter; skip seeding when None. Storage epic passes None; Epic 03 wires a real callback. **(Recommended.)**
  - **B:** Inline the prompt JSON into `migration.py`. Couples storage to synthesis content.
  - **C:** Ship an empty prompt registry and document that synthesis calls return `missing_prompt` until Epic 03. (Same as the v4.1 regression that PR #613 fixed — bad idea to re-introduce.)

### ADR-006: TS-vs-Python integer semantics at the sqlite3 boundary
- **Question:** TS uses `number` (53-bit safe int) for `message_id` / `conversation_id`. Python `int` is arbitrary-precision. Wire-format ingestion (JSON from Node bootstraps) loses precision past 2^53.
- **Recommendation:** Document the lossless invariant: all primary keys are `INTEGER PRIMARY KEY AUTOINCREMENT` — SQLite never allocates IDs past `9223372036854775807`, and we'd hit storage limits long before that in practice. Risk is in **import** from external sources (a foreign JSON file claiming `message_id: 2^60`). Add a parser-side range guard in the bootstrap path: reject any `int > 2**53 - 1` at JSON parse time with a clear error.

### ADR-007: Connection registry threading model
- **Question:** TS code uses `connectionsByPath` + `connectionIndex` (global maps). Python needs the same registry to support test fixtures closing-by-path, but Python `sqlite3.Connection` is not thread-safe.
- **Recommendation:** **One Connection per thread per path**, registry keyed by `(path, thread_id)`. Provide `get_lcm_connection()` that returns the thread's connection or creates one. Tests can call `close_lcm_connection(path=X)` to close all threads' connections to that path. Matches `hermes_state.py:309` `class SessionDB:` pattern (one `_conn`, one `_lock`).

---

## 12. Remaining 5% risk

What we don't yet know:

1. **Concurrency model under heavy multi-thread load** — the TS code runs in Node's single-threaded event loop. Python multi-thread (or asyncio-via-to_thread) plus stdlib `sqlite3` has different lock contention shapes. Spike 005 (TBD per Epic 01) should run a 10-way concurrent transaction stress test parallel to `test/transaction-mutex.test.ts` line 437 ("handles 10 concurrent transactions from different simulated sessions without errors").
2. **`AsyncLocalStorage` ↔ contextvars semantics for nested transactions** — TS's `transaction-mutex.ts` uses Node `async_hooks` to track held-lock depth per async path. Python's `contextvars` is the right primitive, but the nested-savepoint semantics need a test mirroring `test/transaction-mutex.test.ts:190` "supports nested transaction scopes on the same async path".
3. **`PRAGMA optimize` on close** — TS does this best-effort. Python sqlite3's `Connection.close()` doesn't expose a hook; we'd run it explicitly before `.close()`. Confirm no surprise behavior.
4. **FTS5 trigram tokenizer availability across Linux distros** — the spike was macOS-only. Linux manylinux Python builds typically include FTS5 but trigram is a relatively newer tokenizer (SQLite 3.34+, 2020-12-01). Add a CI check matrix or trust the `probeTrigramTokenizer` graceful-degrade path.
5. **The `node:sqlite` "multi-statement exec aborted silently" bug** that motivated the `ensureMessagePartsTable()` belt-and-suspenders guard — Python's `sqlite3.Connection.executescript()` raises on any failure (no silent partial-success). The guard is still cheap and harmless to port, but understand it's defensive for a Node-only failure mode.
6. **FTS5 standalone-vs-content-tracked schema drift detection** — the `staleSchemaPatterns` heuristic in `shouldRecreateStandaloneFtsTable` matches substrings of the raw CREATE SQL stored in `sqlite_master`. Python sqlite3 returns the same SQL text — should port cleanly, but worth a parity test.
7. **TS regex flags vs. Python re module** — flag mismatches (e.g. case-insensitive Unicode classes, `\w` semantics) could subtly affect `searchRegex`. Recommend porting the regex *expressions* literally and adding a side-by-side parity table to the integration tests.
8. **Hermes adoption of an `apply_wal_with_fallback()` shared helper** — depends on Hermes maintainers accepting a refactor of `hermes_state.py` to extract the helper. Plan B: copy the function into `lossless_hermes/db/connection.py` rather than refactor upstream.

---

## Appendix A: Quick reference — module → table inventory

| Subsystem owner | Tables it reads/writes |
|---|---|
| `store/conversation.py` | `conversations`, `messages`, `message_parts`, `messages_fts` |
| `store/summary.py` | `summaries`, `summary_messages`, `summary_parents`, `context_items`, `large_files`, `conversation_bootstrap_state`, `summaries_fts`, `summaries_fts_cjk` |
| `store/compaction_telemetry.py` | `conversation_compaction_telemetry` |
| `store/compaction_maintenance.py` | `conversation_compaction_maintenance` |
| `db/migration.py` | All of the above, plus `lcm_migration_state`, `lcm_feature_flags`, `lcm_worker_lock`, `lcm_extraction_queue`, `lcm_session_key_audit`, `lcm_prompt_registry`, `lcm_synthesis_cache`, `lcm_cache_leaf_refs`, `lcm_synthesis_audit`, `lcm_eval_query_set`, `lcm_eval_query`, `lcm_eval_run`, `lcm_eval_drift`, `lcm_entity_type_registry`, `lcm_entities`, `lcm_entity_mentions`, `lcm_embedding_profile`, `lcm_embedding_meta` |
| `prune.py` | `conversations` (CASCADE drives everything else) |
| `integrity.py` | reads: `conversations`, `messages`, `summaries`, `summary_messages`, `summary_parents`, `context_items`, `large_files` |
| `large_files.py` | (no direct DB access — produces records consumed by `summary.py.insertLargeFile`) |

## Appendix B: Index inventory (consolidated)

| Index | Table | Columns | Notes |
|---|---|---|---|
| `messages_conv_seq_idx` | messages | `(conversation_id, seq)` | |
| `messages_conv_identity_hash_idx` | messages | `(conversation_id, identity_hash)` | post-backfill |
| `messages_suppressed_idx` | messages | `(suppressed_at)` | partial `WHERE suppressed_at IS NOT NULL` |
| `summaries_conv_created_idx` | summaries | `(conversation_id, created_at)` | |
| `summaries_conv_depth_kind_idx` | summaries | `(conversation_id, depth, kind)` | post-backfill |
| `summaries_session_key_kind_latest_idx` | summaries | `(session_key, kind, latest_at DESC)` | partial `WHERE session_key != ''` |
| `summaries_suppressed_idx` | summaries | `(suppressed_at)` | partial |
| `summaries_contains_suppressed_idx` | summaries | `(contains_suppressed_leaves)` | partial `WHERE contains_suppressed_leaves = 1 AND superseded_by IS NULL` |
| `summary_messages_message_idx` | summary_messages | `(message_id)` | created twice (idempotent) |
| `summary_parents_parent_summary_idx` | summary_parents | `(parent_summary_id)` | |
| `message_parts_message_idx` | message_parts | `(message_id)` | |
| `message_parts_type_idx` | message_parts | `(part_type)` | |
| `context_items_conv_idx` | context_items | `(conversation_id, ordinal)` | |
| `large_files_conv_idx` | large_files | `(conversation_id, created_at)` | |
| `bootstrap_state_path_idx` | conversation_bootstrap_state | `(session_file_path, updated_at)` | |
| `compaction_telemetry_state_idx` | conversation_compaction_telemetry | `(cache_state, updated_at)` | |
| `conversations_active_session_key_idx` | conversations | `(session_key)` | UNIQUE, partial `WHERE session_key IS NOT NULL AND active = 1` |
| `conversations_session_key_active_created_idx` | conversations | `(session_key, active, created_at)` | |
| `conversations_session_id_active_created_idx` | conversations | `(session_id, active, created_at)` | |
| `conversations_session_key_v41_idx` | conversations | `(session_key)` | partial `WHERE session_key IS NOT NULL` |
| `lcm_extraction_queue_pending_idx` | lcm_extraction_queue | `(queued_at)` | partial `WHERE picked_at IS NULL` |
| `lcm_extraction_queue_dead_letter_idx` | lcm_extraction_queue | `(attempts)` | partial `WHERE attempts >= 5` |
| `lcm_session_key_audit_conv_idx` | lcm_session_key_audit | `(conversation_id, applied_at DESC)` | |
| `lcm_prompt_registry_active_idx` | lcm_prompt_registry | `(memory_type, tier_label, pass_kind)` | partial `WHERE active = 1` |
| `lcm_prompt_registry_uniq_lookup` | lcm_prompt_registry | `(memory_type, COALESCE(tier_label, ''), pass_kind, version)` | UNIQUE |
| `lcm_synthesis_cache_lookup_uniq` | lcm_synthesis_cache | 7-tuple incl. COALESCE(grep_filter) | UNIQUE; cross-process single-flight |
| `lcm_synthesis_cache_built_idx` | lcm_synthesis_cache | `(session_key, built_at DESC)` | |
| `lcm_synthesis_cache_status_building_idx` | lcm_synthesis_cache | `(building_started_at)` | partial `WHERE status = 'building'` |
| `lcm_cache_leaf_refs_by_leaf_idx` | lcm_cache_leaf_refs | `(leaf_summary_id)` | |
| `lcm_synthesis_audit_target_summary_idx` | lcm_synthesis_audit | `(target_summary_id, ran_at DESC)` | partial |
| `lcm_synthesis_audit_target_cache_idx` | lcm_synthesis_audit | `(target_cache_id, ran_at DESC)` | partial |
| `lcm_synthesis_audit_session_idx` | lcm_synthesis_audit | `(pass_session_id)` | |
| `lcm_synthesis_audit_started_gc_idx` | lcm_synthesis_audit | `(ran_at)` | partial `WHERE status = 'started'` |
| `lcm_synthesis_audit_completed_gc_idx` | lcm_synthesis_audit | `(ran_at)` | partial `WHERE status IN ('completed','failed')` |
| `lcm_eval_query_set_stratum_idx` | lcm_eval_query | `(query_set_id, stratum)` | |
| `lcm_eval_query_must_not_regress_idx` | lcm_eval_query | `(query_set_id)` | partial `WHERE must_not_regress = 1` |
| `lcm_eval_run_recent_idx` | lcm_eval_run | `(query_set_id, ran_at DESC)` | |
| `lcm_eval_drift_recent_idx` | lcm_eval_drift | `(query_set_id, computed_at DESC)` | |
| `lcm_entities_lookup_idx` | lcm_entities | `(session_key, entity_type, last_seen_at DESC)` | |
| `lcm_entities_canonical_uniq` | lcm_entities | `(session_key, canonical_text COLLATE NOCASE)` | UNIQUE |
| `lcm_entity_mentions_by_entity_idx` | lcm_entity_mentions | `(entity_id, mentioned_at DESC)` | |
| `lcm_entity_mentions_by_summary_idx` | lcm_entity_mentions | `(summary_id)` | |
| `lcm_embedding_meta_active_idx` | lcm_embedding_meta | `(embedding_model, embedded_at DESC)` | partial `WHERE archived = 0` |
| `lcm_embedding_meta_by_kind_idx` | lcm_embedding_meta | `(embedded_kind, embedded_id)` | |

**Total: 42 indexes** across the in-scope schema (this is on top of the implicit PK indexes).

---

*End of guide.*
