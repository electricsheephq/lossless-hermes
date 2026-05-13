-- LCM reference schema, generated from runLcmMigrations()
-- LCM_TS_PATH: /Volumes/LEXAR/Claude/lossless-claw
-- Generated: 2026-05-13T10:22:42.892Z
-- Total objects: 92

-- index: bootstrap_state_path_idx
CREATE INDEX bootstrap_state_path_idx
      ON conversation_bootstrap_state (session_file_path, updated_at);

-- index: compaction_telemetry_state_idx
CREATE INDEX compaction_telemetry_state_idx
      ON conversation_compaction_telemetry (cache_state, updated_at);

-- index: context_items_conv_idx
CREATE INDEX context_items_conv_idx ON context_items (conversation_id, ordinal);

-- index: conversations_active_session_key_idx
CREATE UNIQUE INDEX conversations_active_session_key_idx
      ON conversations (session_key)
      WHERE session_key IS NOT NULL AND active = 1;

-- index: conversations_session_id_active_created_idx
CREATE INDEX conversations_session_id_active_created_idx
      ON conversations (session_id, active, created_at);

-- index: conversations_session_key_active_created_idx
CREATE INDEX conversations_session_key_active_created_idx
      ON conversations (session_key, active, created_at);

-- index: conversations_session_key_v41_idx
CREATE INDEX conversations_session_key_v41_idx
          ON conversations (session_key)
          WHERE session_key IS NOT NULL;

-- index: large_files_conv_idx
CREATE INDEX large_files_conv_idx ON large_files (conversation_id, created_at);

-- index: lcm_cache_leaf_refs_by_leaf_idx
CREATE INDEX lcm_cache_leaf_refs_by_leaf_idx
          ON lcm_cache_leaf_refs (leaf_summary_id);

-- index: lcm_embedding_meta_active_idx
CREATE INDEX lcm_embedding_meta_active_idx
          ON lcm_embedding_meta (embedding_model, embedded_at DESC)
          WHERE archived = 0;

-- index: lcm_embedding_meta_by_kind_idx
CREATE INDEX lcm_embedding_meta_by_kind_idx
          ON lcm_embedding_meta (embedded_kind, embedded_id);

-- index: lcm_entities_canonical_uniq
CREATE UNIQUE INDEX lcm_entities_canonical_uniq
          ON lcm_entities (session_key, canonical_text COLLATE NOCASE);

-- index: lcm_entities_lookup_idx
CREATE INDEX lcm_entities_lookup_idx
          ON lcm_entities (session_key, entity_type, last_seen_at DESC);

-- index: lcm_entity_mentions_by_entity_idx
CREATE INDEX lcm_entity_mentions_by_entity_idx
          ON lcm_entity_mentions (entity_id, mentioned_at DESC);

-- index: lcm_entity_mentions_by_summary_idx
CREATE INDEX lcm_entity_mentions_by_summary_idx
          ON lcm_entity_mentions (summary_id);

-- index: lcm_eval_drift_recent_idx
CREATE INDEX lcm_eval_drift_recent_idx
          ON lcm_eval_drift (query_set_id, computed_at DESC);

-- index: lcm_eval_query_must_not_regress_idx
CREATE INDEX lcm_eval_query_must_not_regress_idx
          ON lcm_eval_query (query_set_id)
          WHERE must_not_regress = 1;

-- index: lcm_eval_query_set_stratum_idx
CREATE INDEX lcm_eval_query_set_stratum_idx
          ON lcm_eval_query (query_set_id, stratum);

-- index: lcm_eval_run_recent_idx
CREATE INDEX lcm_eval_run_recent_idx
          ON lcm_eval_run (query_set_id, ran_at DESC);

-- index: lcm_extraction_queue_dead_letter_idx
CREATE INDEX lcm_extraction_queue_dead_letter_idx
          ON lcm_extraction_queue (attempts)
          WHERE attempts >= 5;

-- index: lcm_extraction_queue_pending_idx
CREATE INDEX lcm_extraction_queue_pending_idx
          ON lcm_extraction_queue (queued_at)
          WHERE picked_at IS NULL;

-- index: lcm_prompt_registry_active_idx
CREATE INDEX lcm_prompt_registry_active_idx
          ON lcm_prompt_registry (memory_type, tier_label, pass_kind)
          WHERE active = 1;

-- index: lcm_prompt_registry_uniq_lookup
CREATE UNIQUE INDEX lcm_prompt_registry_uniq_lookup
          ON lcm_prompt_registry (
            memory_type, COALESCE(tier_label, ''), pass_kind, version
          );

-- index: lcm_session_key_audit_conv_idx
CREATE INDEX lcm_session_key_audit_conv_idx
          ON lcm_session_key_audit (conversation_id, applied_at DESC);

-- index: lcm_synthesis_audit_completed_gc_idx
CREATE INDEX lcm_synthesis_audit_completed_gc_idx
          ON lcm_synthesis_audit (ran_at)
          WHERE status IN ('completed', 'failed');

-- index: lcm_synthesis_audit_session_idx
CREATE INDEX lcm_synthesis_audit_session_idx
          ON lcm_synthesis_audit (pass_session_id);

-- index: lcm_synthesis_audit_started_gc_idx
CREATE INDEX lcm_synthesis_audit_started_gc_idx
          ON lcm_synthesis_audit (ran_at)
          WHERE status = 'started';

-- index: lcm_synthesis_audit_target_cache_idx
CREATE INDEX lcm_synthesis_audit_target_cache_idx
          ON lcm_synthesis_audit (target_cache_id, ran_at DESC)
          WHERE target_cache_id IS NOT NULL;

-- index: lcm_synthesis_audit_target_summary_idx
CREATE INDEX lcm_synthesis_audit_target_summary_idx
          ON lcm_synthesis_audit (target_summary_id, ran_at DESC)
          WHERE target_summary_id IS NOT NULL;

-- index: lcm_synthesis_cache_built_idx
CREATE INDEX lcm_synthesis_cache_built_idx
          ON lcm_synthesis_cache (session_key, built_at DESC);

-- index: lcm_synthesis_cache_lookup_uniq
CREATE UNIQUE INDEX lcm_synthesis_cache_lookup_uniq
          ON lcm_synthesis_cache (session_key, range_start, range_end,
                                  leaf_fingerprint,
                                  COALESCE(grep_filter, ''),
                                  tier_label,
                                  prompt_id);

-- index: lcm_synthesis_cache_status_building_idx
CREATE INDEX lcm_synthesis_cache_status_building_idx
          ON lcm_synthesis_cache (building_started_at)
          WHERE status = 'building';

-- index: message_parts_message_idx
CREATE INDEX message_parts_message_idx ON message_parts (message_id);

-- index: message_parts_type_idx
CREATE INDEX message_parts_type_idx ON message_parts (part_type);

-- index: messages_conv_identity_hash_idx
CREATE INDEX messages_conv_identity_hash_idx ON messages (conversation_id, identity_hash);

-- index: messages_conv_seq_idx
CREATE INDEX messages_conv_seq_idx ON messages (conversation_id, seq);

-- index: messages_suppressed_idx
CREATE INDEX messages_suppressed_idx
          ON messages (suppressed_at)
          WHERE suppressed_at IS NOT NULL;

-- index: summaries_contains_suppressed_idx
CREATE INDEX summaries_contains_suppressed_idx
          ON summaries (contains_suppressed_leaves)
          WHERE contains_suppressed_leaves = 1 AND superseded_by IS NULL;

-- index: summaries_conv_created_idx
CREATE INDEX summaries_conv_created_idx ON summaries (conversation_id, created_at);

-- index: summaries_conv_depth_kind_idx
CREATE INDEX summaries_conv_depth_kind_idx ON summaries (conversation_id, depth, kind);

-- index: summaries_session_key_kind_latest_idx
CREATE INDEX summaries_session_key_kind_latest_idx
          ON summaries (session_key, kind, latest_at DESC)
          WHERE session_key != '';

-- index: summaries_suppressed_idx
CREATE INDEX summaries_suppressed_idx
          ON summaries (suppressed_at)
          WHERE suppressed_at IS NOT NULL;

-- index: summary_messages_message_idx
CREATE INDEX summary_messages_message_idx ON summary_messages (message_id);

-- index: summary_parents_parent_summary_idx
CREATE INDEX summary_parents_parent_summary_idx ON summary_parents (parent_summary_id);

-- table: context_items
CREATE TABLE context_items (
      conversation_id INTEGER NOT NULL REFERENCES conversations(conversation_id) ON DELETE CASCADE,
      ordinal INTEGER NOT NULL,
      item_type TEXT NOT NULL CHECK (item_type IN ('message', 'summary')),
      message_id INTEGER REFERENCES messages(message_id) ON DELETE RESTRICT,
      summary_id TEXT REFERENCES summaries(summary_id) ON DELETE RESTRICT,
      created_at TEXT NOT NULL DEFAULT (datetime('now')),
      PRIMARY KEY (conversation_id, ordinal),
      CHECK (
        (item_type = 'message' AND message_id IS NOT NULL AND summary_id IS NULL) OR
        (item_type = 'summary' AND summary_id IS NOT NULL AND message_id IS NULL)
      )
    );

-- table: conversation_bootstrap_state
CREATE TABLE conversation_bootstrap_state (
      conversation_id INTEGER PRIMARY KEY REFERENCES conversations(conversation_id) ON DELETE CASCADE,
      session_file_path TEXT NOT NULL,
      last_seen_size INTEGER NOT NULL,
      last_seen_mtime_ms INTEGER NOT NULL,
      last_processed_offset INTEGER NOT NULL,
      last_processed_entry_hash TEXT,
      updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

-- table: conversation_compaction_maintenance
CREATE TABLE conversation_compaction_maintenance (
      conversation_id INTEGER PRIMARY KEY REFERENCES conversations(conversation_id) ON DELETE CASCADE,
      pending INTEGER NOT NULL DEFAULT 0,
      requested_at TEXT,
      reason TEXT,
      running INTEGER NOT NULL DEFAULT 0,
      last_started_at TEXT,
      last_finished_at TEXT,
      last_failure_summary TEXT,
      token_budget INTEGER,
      current_token_count INTEGER,
      updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

-- table: conversation_compaction_telemetry
CREATE TABLE conversation_compaction_telemetry (
      conversation_id INTEGER PRIMARY KEY REFERENCES conversations(conversation_id) ON DELETE CASCADE,
      last_observed_cache_read INTEGER,
      last_observed_cache_write INTEGER,
      last_observed_prompt_token_count INTEGER,
      last_observed_cache_hit_at TEXT,
      last_observed_cache_break_at TEXT,
      cache_state TEXT NOT NULL DEFAULT 'unknown'
        CHECK (cache_state IN ('hot', 'cold', 'unknown')),
      consecutive_cold_observations INTEGER NOT NULL DEFAULT 0,
      retention TEXT,
      last_leaf_compaction_at TEXT,
      turns_since_leaf_compaction INTEGER NOT NULL DEFAULT 0,
      tokens_accumulated_since_leaf_compaction INTEGER NOT NULL DEFAULT 0,
      last_activity_band TEXT NOT NULL DEFAULT 'low'
        CHECK (last_activity_band IN ('low', 'medium', 'high')),
      last_api_call_at TEXT,
      last_cache_touch_at TEXT,
      provider TEXT,
      model TEXT,
      updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

-- table: conversations
CREATE TABLE conversations (
      conversation_id INTEGER PRIMARY KEY AUTOINCREMENT,
      session_id TEXT NOT NULL,
      session_key TEXT,
      active INTEGER NOT NULL DEFAULT 1,
      archived_at TEXT,
      title TEXT,
      bootstrapped_at TEXT,
      created_at TEXT NOT NULL DEFAULT (datetime('now')),
      updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

-- table: large_files
CREATE TABLE large_files (
      file_id TEXT PRIMARY KEY,
      conversation_id INTEGER NOT NULL REFERENCES conversations(conversation_id) ON DELETE CASCADE,
      file_name TEXT,
      mime_type TEXT,
      byte_size INTEGER,
      storage_uri TEXT NOT NULL,
      exploration_summary TEXT,
      created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

-- table: lcm_cache_leaf_refs
CREATE TABLE lcm_cache_leaf_refs (
          cache_id TEXT NOT NULL REFERENCES lcm_synthesis_cache(cache_id) ON DELETE CASCADE,
          leaf_summary_id TEXT NOT NULL REFERENCES summaries(summary_id) ON DELETE CASCADE,
          PRIMARY KEY (cache_id, leaf_summary_id)
        );

-- table: lcm_embedding_meta
CREATE TABLE lcm_embedding_meta (
          embedded_id TEXT NOT NULL,
          embedded_kind TEXT NOT NULL CHECK (embedded_kind IN ('summary', 'entity', 'theme')),
          embedding_model TEXT NOT NULL REFERENCES lcm_embedding_profile(model_name) ON DELETE RESTRICT,
          embedded_at TEXT NOT NULL DEFAULT (datetime('now')),
          source_token_count INTEGER NOT NULL,
          archived INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (embedded_id, embedded_kind, embedding_model)
        );

-- table: lcm_embedding_profile
CREATE TABLE lcm_embedding_profile (
          model_name TEXT NOT NULL PRIMARY KEY,
          dim INTEGER NOT NULL,
          registered_at TEXT NOT NULL DEFAULT (datetime('now')),
          active INTEGER NOT NULL DEFAULT 1,
          archive_after TEXT
        );

-- table: lcm_entities
CREATE TABLE lcm_entities (
          entity_id TEXT NOT NULL PRIMARY KEY,
          session_key TEXT NOT NULL,
          canonical_text TEXT NOT NULL,
          entity_type TEXT NOT NULL,
          first_seen_at TEXT NOT NULL,
          last_seen_at TEXT NOT NULL,
          first_seen_in_summary_id TEXT REFERENCES summaries(summary_id) ON DELETE SET NULL,
          occurrence_count INTEGER NOT NULL DEFAULT 1,
          alternate_surfaces TEXT,
          metadata TEXT
        );

-- table: lcm_entity_mentions
CREATE TABLE lcm_entity_mentions (
          mention_id TEXT NOT NULL PRIMARY KEY,
          entity_id TEXT NOT NULL REFERENCES lcm_entities(entity_id) ON DELETE CASCADE,
          summary_id TEXT NOT NULL REFERENCES summaries(summary_id) ON DELETE CASCADE,
          surface_form TEXT NOT NULL,
          span_start INTEGER,
          span_end INTEGER,
          mentioned_at TEXT NOT NULL
        );

-- table: lcm_entity_type_registry
CREATE TABLE lcm_entity_type_registry (
          type_name TEXT NOT NULL PRIMARY KEY,
          first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
          occurrence_count INTEGER NOT NULL DEFAULT 1
        );

-- table: lcm_eval_drift
CREATE TABLE lcm_eval_drift (
          drift_id TEXT NOT NULL PRIMARY KEY,
          query_set_id TEXT NOT NULL REFERENCES lcm_eval_query_set(query_set_id) ON DELETE CASCADE,
          cumulative_delta REAL NOT NULL,
          window_runs INTEGER NOT NULL,
          computed_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

-- table: lcm_eval_query
CREATE TABLE lcm_eval_query (
          query_id TEXT NOT NULL PRIMARY KEY,
          query_set_id TEXT NOT NULL REFERENCES lcm_eval_query_set(query_set_id) ON DELETE CASCADE,
          query_text TEXT NOT NULL,
          stratum TEXT NOT NULL CHECK (stratum IN ('fts-easy', 'fts-medium', 'paraphrastic')),
          expected_topics TEXT NOT NULL,
          expected_sources TEXT,
          reference_summary TEXT,
          must_not_regress INTEGER NOT NULL DEFAULT 0,
          rubric TEXT NOT NULL
        );

-- table: lcm_eval_query_set
CREATE TABLE lcm_eval_query_set (
          query_set_id TEXT NOT NULL PRIMARY KEY,
          version INTEGER NOT NULL,
          description TEXT,
          created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

-- table: lcm_eval_run
CREATE TABLE lcm_eval_run (
          run_id TEXT NOT NULL PRIMARY KEY,
          query_set_id TEXT NOT NULL REFERENCES lcm_eval_query_set(query_set_id) ON DELETE CASCADE,
          prompt_bundle_version INTEGER NOT NULL,
          ran_at TEXT NOT NULL DEFAULT (datetime('now')),
          retrieval_recall_score REAL NOT NULL,
          synthesis_quality_score REAL NOT NULL,
          per_query_scores TEXT NOT NULL,
          judge_models TEXT NOT NULL,
          noise_floor_sd REAL,
          trigger TEXT NOT NULL CHECK (trigger IN ('manual', 'prompt-update', 'model-update', 'ci', 'nightly'))
        );

-- table: lcm_extraction_queue
CREATE TABLE lcm_extraction_queue (
          queue_id TEXT NOT NULL PRIMARY KEY,
          leaf_id TEXT NOT NULL REFERENCES summaries(summary_id) ON DELETE CASCADE,
          kind TEXT NOT NULL CHECK (kind IN ('entity', 'procedure-recheck')),
          queued_at TEXT NOT NULL DEFAULT (datetime('now')),
          picked_at TEXT,
          worker_id TEXT,
          completed_at TEXT,
          attempts INTEGER NOT NULL DEFAULT 0,
          last_error TEXT
        );

-- table: lcm_feature_flags
CREATE TABLE lcm_feature_flags (
      flag TEXT NOT NULL PRIMARY KEY,
      value TEXT NOT NULL,
      updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

-- table: lcm_migration_state
CREATE TABLE lcm_migration_state (
      step_name TEXT NOT NULL,
      algorithm_version INTEGER NOT NULL,
      completed_at TEXT NOT NULL DEFAULT (datetime('now')),
      PRIMARY KEY (step_name, algorithm_version)
    );

-- table: lcm_prompt_registry
CREATE TABLE lcm_prompt_registry (
          prompt_id TEXT NOT NULL PRIMARY KEY,
          memory_type TEXT NOT NULL CHECK (memory_type IN (
            'episodic-leaf',
            'episodic-condensed',
            'episodic-yearly',
            'procedural-extract',
            'entity-extract',
            'theme-consolidation'
          )),
          tier_label TEXT,
          pass_kind TEXT NOT NULL CHECK (pass_kind IN ('single', 'verify_fidelity', 'best_of_n_judge')),
          version INTEGER NOT NULL,
          template TEXT NOT NULL,
          model_recommendation TEXT,
          created_at TEXT NOT NULL DEFAULT (datetime('now')),
          active INTEGER NOT NULL DEFAULT 1,
          bundle_version INTEGER NOT NULL DEFAULT 1,
          notes TEXT,
          UNIQUE(memory_type, tier_label, pass_kind, version)
        );

-- table: lcm_session_key_audit
CREATE TABLE lcm_session_key_audit (
          audit_id TEXT NOT NULL PRIMARY KEY,
          conversation_id INTEGER NOT NULL REFERENCES conversations(conversation_id) ON DELETE CASCADE,
          original_session_key TEXT,
          new_session_key TEXT NOT NULL,
          reason TEXT NOT NULL,
          applied_at TEXT NOT NULL DEFAULT (datetime('now')),
          applied_by TEXT NOT NULL DEFAULT 'migration'
        );

-- table: lcm_synthesis_audit
CREATE TABLE lcm_synthesis_audit (
          audit_id TEXT NOT NULL PRIMARY KEY,
          pass_session_id TEXT NOT NULL,
          target_summary_id TEXT REFERENCES summaries(summary_id) ON DELETE CASCADE,
          target_cache_id TEXT REFERENCES lcm_synthesis_cache(cache_id) ON DELETE CASCADE,
          prompt_id TEXT NOT NULL REFERENCES lcm_prompt_registry(prompt_id) ON DELETE RESTRICT,
          pass_kind TEXT NOT NULL,
          pass_input_truncated TEXT NOT NULL,
          pass_output TEXT,
          status TEXT NOT NULL DEFAULT 'started'
            CHECK (status IN ('started', 'completed', 'failed')),
          model_used TEXT NOT NULL,
          latency_ms INTEGER,
          cost_usd_cents INTEGER,
          last_error TEXT,
          ran_at TEXT NOT NULL DEFAULT (datetime('now')),
          CHECK (target_summary_id IS NOT NULL OR target_cache_id IS NOT NULL)
        );

-- table: lcm_synthesis_cache
CREATE TABLE lcm_synthesis_cache (
          cache_id TEXT NOT NULL PRIMARY KEY,
          session_key TEXT NOT NULL,
          range_start TEXT NOT NULL,
          range_end TEXT NOT NULL,
          grep_filter TEXT,
          leaf_fingerprint TEXT NOT NULL,
          content TEXT,
          entity_index TEXT NOT NULL DEFAULT '{}',
          model_used TEXT NOT NULL,
          prompt_id TEXT NOT NULL REFERENCES lcm_prompt_registry(prompt_id) ON DELETE RESTRICT,
          tier_label TEXT NOT NULL CHECK (tier_label IN ('year', 'yearly', 'monthly', 'weekly', 'daily', 'custom', 'filtered')),
          source_leaf_ids TEXT NOT NULL,
          source_condensed_ids TEXT,
          built_at TEXT NOT NULL DEFAULT (datetime('now')),
          source_token_count INTEGER NOT NULL,
          output_token_count INTEGER NOT NULL,
          actual_range_covered TEXT NOT NULL,
          leaf_count_synthesized INTEGER NOT NULL,
          status TEXT NOT NULL DEFAULT 'ready'
            CHECK (status IN ('building', 'ready', 'failed')),
          building_started_at TEXT,
          failure_reason TEXT
        );

-- table: lcm_worker_lock
CREATE TABLE lcm_worker_lock (
          job_kind TEXT NOT NULL PRIMARY KEY,
          worker_id TEXT NOT NULL,
          acquired_at TEXT NOT NULL DEFAULT (datetime('now')),
          expires_at TEXT NOT NULL,
          last_heartbeat_at TEXT NOT NULL DEFAULT (datetime('now')),
          job_session_key TEXT,
          job_metadata TEXT
        );

-- table: message_parts
CREATE TABLE message_parts (
      part_id TEXT PRIMARY KEY,
      message_id INTEGER NOT NULL REFERENCES messages(message_id) ON DELETE CASCADE,
      session_id TEXT NOT NULL,
      part_type TEXT NOT NULL CHECK (part_type IN (
        'text', 'reasoning', 'tool', 'patch', 'file',
        'subtask', 'compaction', 'step_start', 'step_finish',
        'snapshot', 'agent', 'retry'
      )),
      ordinal INTEGER NOT NULL,
      text_content TEXT,
      is_ignored INTEGER,
      is_synthetic INTEGER,
      tool_call_id TEXT,
      tool_name TEXT,
      tool_status TEXT,
      tool_input TEXT,
      tool_output TEXT,
      tool_error TEXT,
      tool_title TEXT,
      patch_hash TEXT,
      patch_files TEXT,
      file_mime TEXT,
      file_name TEXT,
      file_url TEXT,
      subtask_prompt TEXT,
      subtask_desc TEXT,
      subtask_agent TEXT,
      step_reason TEXT,
      step_cost REAL,
      step_tokens_in INTEGER,
      step_tokens_out INTEGER,
      snapshot_hash TEXT,
      compaction_auto INTEGER,
      metadata TEXT,
      UNIQUE (message_id, ordinal)
    );

-- table: messages
CREATE TABLE messages (
      message_id INTEGER PRIMARY KEY AUTOINCREMENT,
      conversation_id INTEGER NOT NULL REFERENCES conversations(conversation_id) ON DELETE CASCADE,
      seq INTEGER NOT NULL,
      role TEXT NOT NULL CHECK (role IN ('system', 'user', 'assistant', 'tool')),
      content TEXT NOT NULL,
      token_count INTEGER NOT NULL,
      identity_hash TEXT,
      created_at TEXT NOT NULL DEFAULT (datetime('now')), suppressed_at TEXT,
      UNIQUE (conversation_id, seq)
    );

-- table: messages_fts
CREATE VIRTUAL TABLE messages_fts USING fts5(
              content,
              tokenize='porter unicode61'
            );

-- table: messages_fts_config
CREATE TABLE 'messages_fts_config'(k PRIMARY KEY, v) WITHOUT ROWID;

-- table: messages_fts_content
CREATE TABLE 'messages_fts_content'(id INTEGER PRIMARY KEY, c0);

-- table: messages_fts_data
CREATE TABLE 'messages_fts_data'(id INTEGER PRIMARY KEY, block BLOB);

-- table: messages_fts_docsize
CREATE TABLE 'messages_fts_docsize'(id INTEGER PRIMARY KEY, sz BLOB);

-- table: messages_fts_idx
CREATE TABLE 'messages_fts_idx'(segid, term, pgno, PRIMARY KEY(segid, term)) WITHOUT ROWID;

-- table: summaries
CREATE TABLE summaries (
      summary_id TEXT PRIMARY KEY,
      conversation_id INTEGER NOT NULL REFERENCES conversations(conversation_id) ON DELETE CASCADE,
      kind TEXT NOT NULL CHECK (kind IN ('leaf', 'condensed')),
      depth INTEGER NOT NULL DEFAULT 0,
      content TEXT NOT NULL,
      token_count INTEGER NOT NULL,
      earliest_at TEXT,
      latest_at TEXT,
      descendant_count INTEGER NOT NULL DEFAULT 0,
      descendant_token_count INTEGER NOT NULL DEFAULT 0,
      source_message_token_count INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL DEFAULT (datetime('now')),
      file_ids TEXT NOT NULL DEFAULT '[]'
    , model TEXT NOT NULL DEFAULT 'unknown', session_key TEXT NOT NULL DEFAULT '', suppressed_at TEXT, entity_index TEXT, contains_suppressed_leaves INTEGER NOT NULL DEFAULT 0, suppress_reason TEXT, superseded_by TEXT REFERENCES summaries(summary_id) ON DELETE SET NULL, leaf_summarizer_cap_was INTEGER);

-- table: summaries_fts
CREATE VIRTUAL TABLE summaries_fts USING fts5(
              summary_id UNINDEXED,
              content,
              tokenize='porter unicode61'
            );

-- table: summaries_fts_cjk
CREATE VIRTUAL TABLE summaries_fts_cjk USING fts5(
                summary_id UNINDEXED,
                content,
                tokenize='trigram'
              );

-- table: summaries_fts_cjk_config
CREATE TABLE 'summaries_fts_cjk_config'(k PRIMARY KEY, v) WITHOUT ROWID;

-- table: summaries_fts_cjk_content
CREATE TABLE 'summaries_fts_cjk_content'(id INTEGER PRIMARY KEY, c0, c1);

-- table: summaries_fts_cjk_data
CREATE TABLE 'summaries_fts_cjk_data'(id INTEGER PRIMARY KEY, block BLOB);

-- table: summaries_fts_cjk_docsize
CREATE TABLE 'summaries_fts_cjk_docsize'(id INTEGER PRIMARY KEY, sz BLOB);

-- table: summaries_fts_cjk_idx
CREATE TABLE 'summaries_fts_cjk_idx'(segid, term, pgno, PRIMARY KEY(segid, term)) WITHOUT ROWID;

-- table: summaries_fts_config
CREATE TABLE 'summaries_fts_config'(k PRIMARY KEY, v) WITHOUT ROWID;

-- table: summaries_fts_content
CREATE TABLE 'summaries_fts_content'(id INTEGER PRIMARY KEY, c0, c1);

-- table: summaries_fts_data
CREATE TABLE 'summaries_fts_data'(id INTEGER PRIMARY KEY, block BLOB);

-- table: summaries_fts_docsize
CREATE TABLE 'summaries_fts_docsize'(id INTEGER PRIMARY KEY, sz BLOB);

-- table: summaries_fts_idx
CREATE TABLE 'summaries_fts_idx'(segid, term, pgno, PRIMARY KEY(segid, term)) WITHOUT ROWID;

-- table: summary_messages
CREATE TABLE summary_messages (
      summary_id TEXT NOT NULL REFERENCES summaries(summary_id) ON DELETE CASCADE,
      message_id INTEGER NOT NULL REFERENCES messages(message_id) ON DELETE RESTRICT,
      ordinal INTEGER NOT NULL,
      PRIMARY KEY (summary_id, message_id)
    );

-- table: summary_parents
CREATE TABLE summary_parents (
      summary_id TEXT NOT NULL REFERENCES summaries(summary_id) ON DELETE CASCADE,
      parent_summary_id TEXT NOT NULL REFERENCES summaries(summary_id) ON DELETE RESTRICT,
      ordinal INTEGER NOT NULL,
      PRIMARY KEY (summary_id, parent_summary_id)
    );

-- trigger: lcm_embedding_meta_cleanup_summary
CREATE TRIGGER lcm_embedding_meta_cleanup_summary
          AFTER DELETE ON summaries
          BEGIN
            DELETE FROM lcm_embedding_meta
              WHERE embedded_id = OLD.summary_id
                AND embedded_kind = 'summary';
          END;

-- pragmas
-- pragma foreign_keys: {"foreign_keys":1}
-- pragma journal_mode: {"journal_mode":"memory"}
-- pragma synchronous: {"synchronous":2}
-- pragma user_version: {"user_version":0}
