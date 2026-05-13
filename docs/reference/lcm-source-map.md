# LCM Source Map: TypeScript → Python

**Source:** `/Volumes/LEXAR/Claude/lossless-claw` @ branch `pr-613`, commit `1f07fbdd38c5e02f33eb5d6d23d7317e22e18500`
**Date captured:** 2026-05-13
**Confidence target:** 95%
**Actual TS LOC (src/ + index.ts):** **48,847** (substantially more than the original `~32000` estimate — the spec template was stale; engine.ts alone is 8,731 and the plugin barrel + lcm-command.ts add ~5,700 more)
**Estimated Python LOC:** **~38,000–42,000**
  - Drop ~2,000 LOC of JSONL bootstrap / auto-rotate / file-anchor logic from `engine.ts`
  - Drop the entire `openclaw-bridge.ts` (26 LOC) and replace with a thin `hermes_bridge.py` (~30 LOC)
  - `startup-banner-log.ts` (54 LOC) is cosmetic and folds into `__init__.py` or drops
  - `transcript-repair.ts` (300 LOC) carries NO JSONL — keep verbatim shape, no shrink
  - Python is ~15–20 % more compact than TS at the same functionality level (no curly braces, fewer type-noise lines, walrus/comprehensions); roughly cancels with the boilerplate Pydantic-model overhead.

> **Note on the original task spec:** the spec listed `doctor-contract-api.d.ts` and `doctor-contract-api.js` as files to catalog. Neither exists in the `pr-613` tree — they may be historical artifacts of a prior plugin layout. The closest analog is the in-source `src/plugin/lcm-doctor-*.ts` triplet (apply / cleaners / shared), all already covered below.

---

## Bucket summary

| Bucket | TS files | TS LOC | Python files | Python LOC | Epic |
|---|---:|---:|---:|---:|---|
| Entry point | 2 | 12 | 1 | ~80 | 02 |
| Storage / DB / stores | 19 | 9,308 | 19 | ~8,300 | 01 |
| Engine | 1 | 8,731 | 1 | ~6,800 | 02–04 |
| Assembler / Compaction / Summarize | 3 | 4,996 | 3 | ~4,500 | 03–04 |
| Retrieval & expansion | 5 | 1,946 | 5 | ~1,800 | 04 |
| Tools | 13 | 7,646 | 13 | ~7,200 | 06 |
| Embeddings / Voyage | 5 | 2,718 | 5 | ~2,400 | 05 |
| Concurrency / workers | 3 | 600 | 3 | ~550 | 05 |
| Extraction (entities) | 2 | 732 | 2 | ~700 | 07 |
| Synthesis | 3 | 1,557 | 3 | ~1,450 | 07 |
| Plugin glue / commands | 10 | 5,621 | 10 | ~4,800 | 02, 08 |
| Doctor | 3 | 1,452 | 3 | ~1,300 | 08 |
| Operator / lifecycle | 9 | 2,417 | 9 | ~2,250 | 08 |
| Eval | 4 | 1,093 | 4 | ~1,000 | 09 |
| Misc (integrity, prune, transcript-repair, mutex, etc.) | 9 | 2,018 | 9 | ~1,800 | 01–04 |
| `openclaw-bridge.ts` | 1 | 26 | DROP — replaced by `hermes_bridge.py` | ~30 | 02 |
| **Total (src+index)** | **92** | **48,847** | **~92** | **~38,000–42,000** | |

(`vitest.config.ts` (21 LOC) excluded — replaced by `pyproject.toml` / `pytest.ini` test config and not part of the runtime port.)

---

## Complete file map

### Entry & top-level

| TS file | LOC | Python target | Notes |
|---|---:|---|---|
| `index.ts` | 6 | `src/lossless_hermes/__init__.py` | Re-exports the plugin default + 3 helpers — fold into package init |
| `src/openclaw-bridge.ts` | 26 | **DROP** — replaced by `src/lossless_hermes/hermes_bridge.py` | OpenClaw seam (re-exports from `openclaw/plugin-sdk`); Python target has a Hermes-side bridge equivalent |
| `src/types.ts` | 187 | `src/lossless_hermes/types.py` | Shared type contracts (deps interfaces); Python = `TypedDict` + Protocol mix or Pydantic models |

### Engine (single largest module)

| TS file | LOC | Python target | Notes |
|---|---:|---|---|
| `src/engine.ts` | **8,731** | `src/lossless_hermes/engine.py` | Main orchestrator class `LcmContextEngine`. **DROP ~1,800–2,000 LOC of JSONL bootstrap, auto-rotate, file-anchor checkpointing, session-file rollover detection** (~187 lines reference jsonl/rotate/bootstrap directly; the surrounding methods add up to ~2k LOC). Hermes session storage is SQLite-only — there is no session JSONL file to bootstrap from, repair, or rotate. Estimated Python target: ~6,800 LOC. |

### Assembler / Compaction / Summarize

| TS file | LOC | Python target | Notes |
|---|---:|---|---|
| `src/assembler.ts` | 1,469 | `src/lossless_hermes/assembler.py` | Builds the assembled-context pyramid (freshTail + condensed evictable layers); pure logic, port verbatim |
| `src/compaction.ts` | 1,831 | `src/lossless_hermes/compaction.py` | Compaction decision + leaf/condensed creation; pure logic |
| `src/summarize.ts` | 1,696 | `src/lossless_hermes/summarize.py` | `createLcmSummarizeFromLegacyParams` — LLM-summarizer adapter w/ fallback handling |

### Retrieval & expansion

| TS file | LOC | Python target | Notes |
|---|---:|---|---|
| `src/retrieval.ts` | 424 | `src/lossless_hermes/retrieval.py` | `RetrievalEngine` — grep + expand surface used by tools |
| `src/expansion.ts` | 386 | `src/lossless_hermes/expansion.py` | `ExpansionOrchestrator` — sub-tree expansion w/ token cap |
| `src/expansion-policy.ts` | 305 | `src/lossless_hermes/expansion_policy.py` | Routes intent → action (answer / shallow / delegate) |
| `src/expansion-auth.ts` | 365 | `src/lossless_hermes/expansion_auth.py` | Delegated-expansion grant manager |
| `src/large-files.ts` | 567 | `src/lossless_hermes/large_files.py` | `<file>` block extraction + `file_<sha>` ID handling |

### Tools (`lcm_*` agent tools)

| TS file | LOC | Python target | Notes |
|---|---:|---|---|
| `src/tools/lcm-grep-tool.ts` | 1,179 | `src/lossless_hermes/tools/grep.py` | FTS + hybrid + semantic search tool |
| `src/tools/lcm-describe-tool.ts` | 766 | `src/lossless_hermes/tools/describe.py` | DB / conversation describe |
| `src/tools/lcm-expand-tool.ts` | 455 | `src/lossless_hermes/tools/expand.py` | `lcm_expand` — pull summaries by ID |
| `src/tools/lcm-expand-tool.delegation.ts` | 580 | `src/lossless_hermes/tools/expand_delegation.py` | Sub-agent delegation glue for `lcm_expand` |
| `src/tools/lcm-expand-query-tool.ts` | 1,467 | `src/lossless_hermes/tools/expand_query.py` | Query-then-expand combined flow |
| `src/tools/lcm-synthesize-around-tool.ts` | 1,477 | `src/lossless_hermes/tools/synthesize_around.py` | Synthesize content around a topic (semantic window) |
| `src/tools/lcm-get-entity-tool.ts` | 342 | `src/lossless_hermes/tools/get_entity.py` | Look up entity by canonical name |
| `src/tools/lcm-search-entities-tool.ts` | 377 | `src/lossless_hermes/tools/search_entities.py` | Fuzzy/browse entity catalog |
| `src/tools/lcm-compact-tool.ts` | 378 | `src/lossless_hermes/tools/compact.py` | Agent-triggered compaction tool |
| `src/tools/lcm-conversation-scope.ts` | 162 | `src/lossless_hermes/tools/conversation_scope.py` | Resolve session_key → conversation_id(s) |
| `src/tools/lcm-expansion-recursion-guard.ts` | 373 | `src/lossless_hermes/tools/expansion_recursion_guard.py` | Prevent recursive expand-from-subagent |
| `src/tools/common.ts` | 53 | `src/lossless_hermes/tools/common.py` | `jsonResult`, param readers, `AnyAgentTool` re-export |
| `src/tools/lcm-entity-shared.ts` | 84 | `src/lossless_hermes/tools/entity_shared.py` | Shared SQL CTE for entity-aggregate queries |

### Storage layer (`src/store/`)

| TS file | LOC | Python target | Notes |
|---|---:|---|---|
| `src/store/conversation-store.ts` | 1,071 | `src/lossless_hermes/store/conversation.py` | Conversations + messages + message_parts + FTS |
| `src/store/summary-store.ts` | 1,668 | `src/lossless_hermes/store/summary.py` | Summaries + context_items + large_files + bootstrap state |
| `src/store/compaction-telemetry-store.ts` | 204 | `src/lossless_hermes/store/compaction_telemetry.py` | Cache/activity bands per conversation |
| `src/store/compaction-maintenance-store.ts` | 219 | `src/lossless_hermes/store/compaction_maintenance.py` | Background-compaction maintenance queue |
| `src/store/conversation-scope.ts` | 34 | `src/lossless_hermes/store/conversation_scope.py` | `appendConversationScopeConstraint` SQL builder |
| `src/store/fts5-sanitize.ts` | 50 | `src/lossless_hermes/store/fts5_sanitize.py` | Escape user input for `MATCH` queries |
| `src/store/full-text-sort.ts` | 21 | `src/lossless_hermes/store/full_text_sort.py` | `buildFtsOrderBy` (recency / relevance / hybrid) |
| `src/store/full-text-fallback.ts` | 84 | `src/lossless_hermes/store/full_text_fallback.py` | LIKE-fallback path for CJK queries |
| `src/store/parse-utc-timestamp.ts` | 26 | `src/lossless_hermes/store/parse_utc_timestamp.py` | SQLite "now" → Date (drops the Z bug) |
| `src/store/message-identity.ts` | 13 | `src/lossless_hermes/store/message_identity.py` | Identity hash for dedup |
| `src/store/index.ts` | 44 | `src/lossless_hermes/store/__init__.py` | Barrel re-exports |

### Database layer (`src/db/`)

| TS file | LOC | Python target | Notes |
|---|---:|---|---|
| `src/db/migration.ts` | **2,037** | `src/lossless_hermes/db/migration.py` | Schema migration ratchet — DDL only, large but mechanical |
| `src/db/connection.ts` | 170 | `src/lossless_hermes/db/connection.py` | Per-path SQLite connection registry; `apsw` or `sqlite3` in Python |
| `src/db/features.ts` | 61 | `src/lossless_hermes/db/features.py` | FTS5 / trigram tokenizer availability probes |
| `src/db/config.ts` | 629 | `src/lossless_hermes/db/config.py` | `resolveLcmConfigWithDiagnostics` — env + state-dir + diagnostics |

### Embeddings / Voyage (`src/embeddings/`, `src/voyage/`)

| TS file | LOC | Python target | Notes |
|---|---:|---|---|
| `src/voyage/client.ts` | 616 | `src/lossless_hermes/voyage/client.py` | Raw `fetch` Voyage client — use `httpx` |
| `src/embeddings/store.ts` | 609 | `src/lossless_hermes/embeddings/store.py` | vec0 virtual-table wrapper |
| `src/embeddings/backfill.ts` | 637 | `src/lossless_hermes/embeddings/backfill.py` | Worker tick: walk unembedded summaries → Voyage → vec0 |
| `src/embeddings/semantic-search.ts` | 419 | `src/lossless_hermes/embeddings/semantic_search.py` | Embed-query → KNN → join-back |
| `src/embeddings/hybrid-search.ts` | 437 | `src/lossless_hermes/embeddings/hybrid_search.py` | FTS + semantic + Voyage rerank |

### Concurrency (`src/concurrency/`)

| TS file | LOC | Python target | Notes |
|---|---:|---|---|
| `src/concurrency/worker-loop.ts` | 238 | `src/lossless_hermes/concurrency/worker_loop.py` | Single-process cooperative job loop |
| `src/concurrency/worker-lock.ts` | 215 | `src/lossless_hermes/concurrency/worker_lock.py` | Cross-process row-uniqueness lock w/ TTL+heartbeat |
| `src/concurrency/model.ts` | 147 | `src/lossless_hermes/concurrency/model.py` | §0 invariants + assertions |

### Extraction (`src/extraction/`)

| TS file | LOC | Python target | Notes |
|---|---:|---|---|
| `src/extraction/entity-coreference.ts` | 498 | `src/lossless_hermes/extraction/coreference.py` | Worker that drains `lcm_extraction_queue` |
| `src/extraction/entity-extractor-llm.ts` | 234 | `src/lossless_hermes/extraction/llm_extractor.py` | Prompt + parse for entity LLM call |

### Synthesis (`src/synthesis/`)

| TS file | LOC | Python target | Notes |
|---|---:|---|---|
| `src/synthesis/dispatch.ts` | 817 | `src/lossless_hermes/synthesis/dispatch.py` | Per-tier model + pass-strategy selection |
| `src/synthesis/prompt-registry.ts` | 305 | `src/lossless_hermes/synthesis/prompt_registry.py` | Versioned prompt templates |
| `src/synthesis/seed-default-prompts.ts` | 435 | `src/lossless_hermes/synthesis/seed_prompts.py` | Boot-time seeding of default prompts |

### Plugin glue (`src/plugin/`)

| TS file | LOC | Python target | Notes |
|---|---:|---|---|
| `src/plugin/index.ts` | 2,804 | `src/lossless_hermes/plugin/__init__.py` | OpenClaw plugin registration — **Hermes equivalent is smaller**: drops the OpenClaw-specific event bus wiring + multi-agent-context init. Target: ~2,000 LOC |
| `src/plugin/lcm-command.ts` | 2,884 | `src/lossless_hermes/plugin/commands.py` | All `/lcm …` slash-command handlers (status, eval, doctor, worker, prune, etc.). Largest file outside engine + migration. |
| `src/plugin/shared-init.ts` | 72 | `src/lossless_hermes/plugin/shared_init.py` | Process-global init singleton (Hermes will use the same global-symbol pattern or `weakref` module-level) |
| `src/plugin/needs-compact-gate.ts` | 327 | `src/lossless_hermes/plugin/needs_compact_gate.py` | Pre-tool gate that refuses with `needsCompact: true` |
| `src/plugin/result-budget.ts` | 131 | `src/lossless_hermes/plugin/result_budget.py` | Shared per-tool result-token cap |
| `src/plugin/token-state.ts` | 273 | `src/lossless_hermes/plugin/token_state.py` | Per-session token-state cache from `llm_output` events |

### Doctor (under `src/plugin/`)

| TS file | LOC | Python target | Notes |
|---|---:|---|---|
| `src/plugin/lcm-doctor-apply.ts` | 541 | `src/lossless_hermes/doctor/apply.py` | Doctor-scoped repair application |
| `src/plugin/lcm-doctor-cleaners.ts` | 641 | `src/lossless_hermes/doctor/cleaners.py` | Archived-subagent / cron-session / null-context cleaners |
| `src/plugin/lcm-doctor-shared.ts` | 270 | `src/lossless_hermes/doctor/shared.py` | Fallback markers, target loaders |
| `src/plugin/lcm-db-backup.ts` | 82 | `src/lossless_hermes/plugin/db_backup.py` | DB backup helper (used by doctor) — keep under `plugin/` |

### Operator (`src/operator/`) — lifecycle + autostarts

| TS file | LOC | Python target | Notes |
|---|---:|---|---|
| `src/operator/purge.ts` | 390 | `src/lossless_hermes/operator/purge.py` | Hard-forget (soft-suppression) |
| `src/operator/health.ts` | 442 | `src/lossless_hermes/operator/health.py` | v4.1 subsystem health snapshot |
| `src/operator/reconcile-session-keys.ts` | 301 | `src/lossless_hermes/operator/reconcile.py` | Merge legacy session keys |
| `src/operator/backfill-autostart.ts` | 264 | `src/lossless_hermes/operator/backfill_autostart.py` | Auto-runs embedding-backfill cron |
| `src/operator/extraction-autostart.ts` | 214 | `src/lossless_hermes/operator/extraction_autostart.py` | Auto-runs entity coreference worker |
| `src/operator/eval-runner.ts` | 193 | `src/lossless_hermes/operator/eval_runner.py` | `/lcm eval` recall + drift |
| `src/operator/semantic-infra-init.ts` | 196 | `src/lossless_hermes/operator/semantic_infra.py` | sqlite-vec + embedding-profile boot wiring |
| `src/operator/worker-llm.ts` | 167 | `src/lossless_hermes/operator/worker_llm.py` | LLM adapter for worker tasks |
| `src/operator/worker-orchestrator.ts` | 250 | `src/lossless_hermes/operator/worker_orchestrator.py` | `/lcm worker` ad-hoc trigger |

### Eval (`src/eval/`)

| TS file | LOC | Python target | Notes |
|---|---:|---|---|
| `src/eval/run.ts` | 375 | `src/lossless_hermes/eval/run.py` | Eval invocation recording + drift |
| `src/eval/recall.ts` | 236 | `src/lossless_hermes/eval/recall.py` | Recall@K + reciprocal-rank metrics |
| `src/eval/judge.ts` | 191 | `src/lossless_hermes/eval/judge.py` | Multi-judge ensemble scoring |
| `src/eval/query-set.ts` | 291 | `src/lossless_hermes/eval/query_set.py` | Query-set CRUD |

### Misc (top-level `src/`)

| TS file | LOC | Python target | Notes |
|---|---:|---|---|
| `src/integrity.ts` | 600 | `src/lossless_hermes/integrity.py` | Integrity checks (pass/fail/warn) |
| `src/prune.ts` | 392 | `src/lossless_hermes/prune.py` | Conversation pruning for data retention |
| `src/transcript-repair.ts` | 300 | `src/lossless_hermes/transcript_repair.py` | Tool-use/result pairing repair — **no JSONL inside; spec's "drop JSONL-rewrite path" is a misread**. Keep verbatim. |
| `src/transaction-mutex.ts` | 202 | `src/lossless_hermes/transaction_mutex.py` | Per-DB async mutex (Python: `asyncio.Lock` keyed on connection) |
| `src/session-patterns.ts` | 23 | `src/lossless_hermes/session_patterns.py` | Session-key glob compiler |
| `src/estimate-tokens.ts` | 80 | `src/lossless_hermes/estimate_tokens.py` | Code-point-aware token estimator |
| `src/lcm-log.ts` | 37 | `src/lossless_hermes/log.py` | NOOP logger + `describeLogError` |
| `src/startup-banner-log.ts` | 54 | **DROP or fold into `__init__.py`** | Once-per-process banner dedupe (cosmetic) |

---

## DROP list — files with no Python counterpart

| TS file | LOC | Why drop |
|---|---:|---|
| `src/openclaw-bridge.ts` | 26 | Re-exports OpenClaw plugin-sdk symbols. Hermes has its own plugin SDK seam; replace with `hermes_bridge.py` (~30 LOC) |
| `src/startup-banner-log.ts` | 54 | Cosmetic once-per-process banner dedupe; Python can use a module-level `set` inline or `functools.lru_cache` if needed |
| **JSONL bootstrap path inside `engine.ts`** | ~1,800 LOC inside engine.ts | `bootstrap()`, file-anchor checkpointing, `rotateSessionStorageWithBackup`, session-file rollover detection — all assume a JSONL transcript file exists on disk. Hermes session is SQLite-only; the equivalent surface is "import-from-Hermes-session.db" (much smaller) |

**Total dropped LOC:** ~1,900 (3.9 % of source)

---

## SIMPLIFY list — files that shrink significantly

| TS file | Original LOC | Python target LOC | Reason |
|---|---:|---:|---|
| `src/engine.ts` | 8,731 | ~6,800 | Drop bootstrap-JSONL + auto-rotate sections (≈1,900 LOC) |
| `src/plugin/index.ts` | 2,804 | ~2,000 | OpenClaw multi-agent-context registration → simpler Hermes-side registration; trim shared-init complexity ported from globalThis-Symbol pattern to Python module-level singleton |
| `src/plugin/lcm-command.ts` | 2,884 | ~2,400 | The `/lcm` command tree itself stays, but each sub-command shrinks slightly when JSONL-related options (`/lcm rotate`, JSONL repair flags) lose their backing logic |
| `src/operator/reconcile-session-keys.ts` | 301 | ~200 | Less defensive parsing needed (Python typed signatures + Pydantic catch malformed input upstream) |

**Total simplification savings:** ~3,200 LOC on top of the ~1,900 dropped → Python target ~38,000–42,000 LOC vs TS source 48,847.

---

## Open questions / port hazards

1. **`engine.ts` is 8,731 LOC in one file.** Single-file port is risky; consider splitting the Python target into `engine/{__init__.py, ingest.py, assemble.py, compact.py, bootstrap.py}` along the natural method-cluster boundaries already present (search for the section banners `// ── …` in `engine.ts`).
2. **`db/migration.ts` is 2,037 LOC of DDL.** Mechanical port, but every CREATE TABLE / CREATE INDEX must be byte-equivalent to keep the Doctor's schema-diff checks meaningful. Recommend a separate validation script that diffs the Python-generated schema against a TS-generated reference DB.
3. **`plugin/index.ts` shared-init singleton** uses `globalThis[Symbol.for(...)]`. Python's import-cache + a module-level dict is the equivalent, but the lifecycle differs across spawned subprocess workers vs. main process — need a clear concurrency invariant doc before porting.
4. **`voyage/client.ts` uses `fetch` + custom `tokenize`.** Python target should use `httpx` (async-native) and `tiktoken` for the local token-budget estimate; the spike doc referenced inside the file (`docs/projects/lcm-rollup-overhaul/voyage-spike-results.md`) is in the LCM repo and worth carrying forward as an ADR.
5. **`transcript-repair.ts` (300 LOC)** repairs tool-use/tool-result pairing in assembled-context — no JSONL inside. The original task spec said "drop JSONL-rewrite path" — this appears to be a misread; **keep verbatim, no shrink**.
6. **Test layout:** the LCM tree has 116 entries under `test/`; vitest config points to it. Pytest port will need an equivalent fixture set. Not in scope here but flag for Epic 09.
7. **`doctor-contract-api.{d.ts,js}`** referenced in the task spec — they don't exist on `pr-613`. If a teammate is expecting them, the closest analog is the in-source `lcm-doctor-*.ts` triplet, already covered above.
