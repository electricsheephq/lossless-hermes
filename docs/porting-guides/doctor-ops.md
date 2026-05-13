# Porting Guide: Doctor + Ops

**Source LOC:** 5,363 across in-scope files (TS, branch `pr-613`).
**Python target LOC:** ~4,000 (some operator features drop or simplify — e.g., the JSONL transcript-rewrite path, Voyage embedding autostart if Hermes uses a different embedding provider).
**Confidence target:** 90% (the cleaners, integrity checks, prune cascade, and purge cascade are well-specified; what reduces confidence is the unsettled question of how Hermes invokes doctor — CLI subcommand vs. background autorun — and how operator-only gating is enforced in a non-plugin host).
**Estimated effort:** 32–48 hours.
**Epic:** 08-cli-ops.

**Source LOC table (`wc -l`):**

| File | LOC |
|---|---:|
| `src/plugin/lcm-doctor-apply.ts` | 541 |
| `src/plugin/lcm-doctor-cleaners.ts` | 641 |
| `src/plugin/lcm-doctor-shared.ts` | 270 |
| `src/integrity.ts` | 600 |
| `src/prune.ts` | 392 |
| `src/transcript-repair.ts` | 300 |
| `src/transaction-mutex.ts` | 202 |
| `src/operator/backfill-autostart.ts` | 264 |
| `src/operator/eval-runner.ts` | 193 |
| `src/operator/extraction-autostart.ts` | 214 |
| `src/operator/health.ts` | 442 |
| `src/operator/purge.ts` | 390 |
| `src/operator/reconcile-session-keys.ts` | 301 |
| `src/operator/semantic-infra-init.ts` | 196 |
| `src/operator/worker-llm.ts` | 167 |
| `src/operator/worker-orchestrator.ts` | 250 |
| **Total** | **5,363** |

## Doctor contract API (canonical)

**No file named `doctor-contract-api.d.ts` exists in the lossless-claw tree on `pr-613`.** The "formal contract" is the union of exported types and functions across the three plugin doctor modules. The two top-level entry points are `applyScopedDoctorRepair` (per-conversation summary repair) and `applyDoctorCleaners` (DB-wide row-deletion cleaners). The shared module exposes the marker detector and the canonical `DoctorTargetRecord` type used by both surfaces.

The canonical surfaces, transcribed directly:

```typescript
// ── from src/plugin/lcm-doctor-shared.ts ─────────────────────────────────────

export const FALLBACK_SUMMARY_MARKER =
  "[LCM fallback summary; truncated for context management]";
export const FALLBACK_SUMMARY_MARKER_V41_TRUNC =
  "[LCM fallback summary — model unavailable; raw source truncated for context management]";
export const FALLBACK_SUMMARY_MARKER_V41_FULL =
  "[LCM fallback summary — model unavailable; raw source preserved verbatim below]";
export const TRUNCATED_SUMMARY_PREFIX = "[Truncated from ";
export const TRUNCATED_SUMMARY_WINDOW = 40;
export const FALLBACK_SUMMARY_WINDOW = 80;

export type DoctorMarkerKind = "old" | "new" | "fallback";

export type DoctorSummaryCandidate = {
  conversationId: number;
  summaryId: string;
  markerKind: DoctorMarkerKind;
};

export type DoctorConversationCounts = {
  total: number;
  old: number;
  truncated: number;
  fallback: number;
};

export type DoctorSummaryStats = {
  candidates: DoctorSummaryCandidate[];
  total: number;
  old: number;
  truncated: number;
  fallback: number;
  byConversation: Map<number, DoctorConversationCounts>;
};

export type DoctorTargetRecord = {
  conversationId: number;
  summaryId: string;
  kind: string;            // "leaf" | "condensed"
  depth: number;
  tokenCount: number;
  content: string;
  createdAt: string;
  childCount: number;
  markerKind: DoctorMarkerKind;
};

export function detectDoctorMarker(content: string): DoctorMarkerKind | null;
export function loadDoctorTargets(db, conversationId?): DoctorTargetRecord[];
export function getDoctorSummaryStats(db, conversationId?): DoctorSummaryStats;

// ── from src/plugin/lcm-doctor-apply.ts ──────────────────────────────────────

export type DoctorApplyResult =
  | {
      kind: "applied";
      detected: number;
      repaired: number;
      unchanged: number;
      skipped: Array<{ summaryId: string; reason: string }>;
      repairedSummaryIds: string[];
    }
  | { kind: "unavailable"; reason: string };

export async function applyScopedDoctorRepair(params: {
  db: DatabaseSync;
  config: LcmConfig;
  conversationId: number;
  deps?: LcmDependencies;
  summarize?: LcmSummarizeFn;
  runtimeConfig?: unknown;
}): Promise<DoctorApplyResult>;

// ── from src/plugin/lcm-doctor-cleaners.ts ───────────────────────────────────

export type DoctorCleanerId =
  | "archived_subagents"
  | "cron_sessions"
  | "null_subagent_context";

export type DoctorCleanerFilterStat = {
  id: DoctorCleanerId;
  label: string;
  description: string;
  conversationCount: number;
  messageCount: number;
  examples: Array<{
    conversationId: number;
    sessionKey: string | null;
    messageCount: number;
    firstMessagePreview: string | null;
  }>;
};

export type DoctorCleanerScan = {
  filters: DoctorCleanerFilterStat[];
  totalDistinctConversations: number;
  totalDistinctMessages: number;
};

export type DoctorCleanerApplyResult =
  | {
      kind: "applied";
      filterIds: DoctorCleanerId[];
      deletedConversations: number;
      deletedMessages: number;
      vacuumed: boolean;
      backupPath: string;
    }
  | { kind: "unavailable"; reason: string };

export function getDoctorCleanerFilters(): Array<Pick<...>>;
export function getDoctorCleanerFilterIds(): DoctorCleanerId[];
export function scanDoctorCleaners(db, filterIds?): DoctorCleanerScan;
export function applyDoctorCleaners(
  db,
  options: { databasePath: string; filterIds?: DoctorCleanerId[]; vacuum?: boolean },
): DoctorCleanerApplyResult;
export function getDoctorCleanerApplyUnavailableReason(databasePath: string): string | null;
```

In Python this becomes `lossless_hermes.doctor.contract` — a single module with Pydantic models for each TS shape and protocol interfaces for the two apply functions. Cross-reference the Hermes `hermes_cli/doctor.py` once implemented; it must consume `DoctorApplyResult` / `DoctorCleanerApplyResult` as the wire format for CLI render.

Two doctor sub-systems live here, with **disjoint semantics**:

1. **`applyScopedDoctorRepair`** — rewrites individual broken summaries within ONE conversation. Runs the LLM summarizer. Mutates `summaries.content`, `summaries.token_count`, `summaries_fts`.
2. **`applyDoctorCleaners`** — bulk-deletes whole conversations matching predefined predicates (archived sub-agents, cron sessions, null-key sub-agent context). Writes a backup file first. Mutates many tables via temp-table-staged cascade.

Both are owner-gated in the plugin (`ctx.senderIsOwner`). In Hermes, gate via the equivalent CLI/operator-mode check.

## Cleaners — full inventory

There are exactly **three** cleaner definitions (in `lcm-doctor-cleaners.ts:71-96`), enumerated as the `DoctorCleanerId` type:

| Cleaner ID | Label | Detects (predicate) | Fixes (cascade) | Owner-gated |
|---|---|---|---|---|
| `archived_subagents` | Archived subagents | `conversations.active = 0 AND conversations.session_key LIKE 'agent:main:subagent:%'` | Deletes matched conversations + cascades through `summary_messages`, `summary_parents`, `context_items` (all three ref types), `messages_fts`, `summaries_fts`, `summaries_fts_cjk`, finally `conversations` itself. Optional `VACUUM` + `PRAGMA wal_checkpoint(TRUNCATE)`. | Yes |
| `cron_sessions` | Cron sessions | `conversations.session_key LIKE 'agent:main:cron:%'` (no active filter — purges live & archived) | Same cascade. | Yes |
| `null_subagent_context` | NULL-key subagent context | `conversations.session_key IS NULL AND conversations.active = 0 AND conversations.archived_at IS NOT NULL AND <first message preview> LIKE '[Subagent Context]%'` | Same cascade. Requires `needsFirstMessage` join — staged via a temp window function table that pulls the earliest `messages.content` (256-char prefix) per conversation. | Yes |

Apply-time guards (in `applyDoctorCleaners`):
- **Backup is mandatory.** Calls `getDoctorCleanerApplyUnavailableReason(databasePath)` first. If the DB is in-memory (no file path), returns `{ kind: "unavailable", reason: "Cleaner apply requires a file-backed SQLite database..." }`. On success, writes a backup via `writeLcmDatabaseBackup` to a path under `buildLcmDatabaseBackupPath(databasePath, "doctor-cleaners")` BEFORE the BEGIN IMMEDIATE.
- **Temp-table staging** (4 temp tables): `doctor_cleaner_candidate_conversations`, `doctor_cleaner_first_messages` (only if any selected cleaner has `needsFirstMessage`), `doctor_cleaner_conversation_ids`, `doctor_cleaner_summary_ids`, `doctor_cleaner_message_ids`. Always dropped in `finally`.
- **FTS branches are best-effort.** `hasTable(db, "messages_fts" | "summaries_fts" | "summaries_fts_cjk")` gates each. Hermes should mirror this — never assume an FTS table exists.
- **Vacuum only fires if `vacuum: true` AND `deletedConversations > 0`** (so a no-op apply is cheap).

Scan-time surface (in `scanDoctorCleaners`):
- Returns per-filter conversation+message counts plus top-3 example conversations (sorted by `message_count DESC, created_at DESC, conversation_id DESC`).
- First-message preview is normalized (whitespace collapsed, 256-char prefix, then trimmed to 120 chars with "..." ellipsis if longer).
- Scan + apply use the same predicate SQL, so the dry-run count equals the apply count.

**Marker-based summary repair is NOT a cleaner.** `applyScopedDoctorRepair` operates on different criteria (presence of fallback/truncated markers in `summaries.content`) and uses a different code path. The two are surfaced under sibling `/lcm doctor apply` and `/lcm doctor clean apply` commands. Don't conflate them in the Python module layout.

### Doctor marker detection (the summary-repair side)

`detectDoctorMarker(content)` returns one of `"old" | "new" | "fallback" | null` based on:
- **`"fallback"`** if `content.startsWith(FALLBACK_SUMMARY_MARKER_V41_TRUNC)` or `FALLBACK_SUMMARY_MARKER_V41_FULL` (v4.1 prefix form), OR if the legacy `FALLBACK_SUMMARY_MARKER` appears as a trailing suffix within the last 80 chars.
- **`"old"`** if `content.startsWith(FALLBACK_SUMMARY_MARKER)` — the legacy marker as a prefix. (Practically unreachable on real data — pre-Wave-4 only emitted it as a suffix — but defended for defense-in-depth.)
- **`"new"`** if `TRUNCATED_SUMMARY_PREFIX` ("[Truncated from ") appears in the last 40 chars (trailing-suffix marker; "summary was emitted but content was truncated for size").
- **`null`** otherwise.

`loadDoctorTargets` SELECTs from `summaries` with a 4-marker INSTR pre-filter, then re-runs `detectDoctorMarker` per row to classify. Ordering: `depth ASC, created_at ASC, summary_id ASC` (deterministic).

`applyScopedDoctorRepair` then:
1. Orders targets: active leaves by `context_items.ordinal` ASC, then orphan leaves by `(depth, created_at, summary_id)`, then condensed in the same order. Repair must happen **leaves first** because condensed re-summarization reads its leaf children's (possibly already-rewritten) content from the in-memory `overrides` map.
2. For each target, builds source text:
   - **Leaf:** joins `summary_messages` → `messages`, concatenating `[timestamp]\ncontent` for each message in `sm.ordinal` order.
   - **Condensed:** joins `summary_parents` → `summaries` for each child (recursively re-using overrides for children just rewritten in this same pass).
3. Resolves "previous summary" context via three fallbacks (in order): `context_items` lookup, `summary_parents` lookup, then `created_at` timestamp neighbor.
4. Calls the resolved summarizer; rejects empty output or output that still contains a marker.
5. Skips a target with `reason: "rewritten content still contains a doctor marker"` rather than overwriting (avoids loops).
6. Writes all rewrites in a single `withDatabaseTransaction(db, "BEGIN IMMEDIATE", ...)` block at the end. Updates `summaries.content`, `summaries.token_count`, and the `summaries_fts` mirror (best-effort).

Returns `{ kind: "applied", detected, repaired, unchanged, skipped, repairedSummaryIds }` or `{ kind: "unavailable", reason }` when no summarizer can be resolved.

## Integrity checks

`src/integrity.ts` exposes an `IntegrityChecker` class with eight checks. Every check runs even if an earlier one fails (the report is always complete). Each returns `{ name, status: "pass" | "fail" | "warn", message, details? }`.

| Check | What it verifies | When fired | Status on failure |
|---|---|---|---|
| `conversation_exists` | `conversation_id` row is in `conversations` | Always | `fail` |
| `context_items_contiguous` | `context_items.ordinal` for the conversation is `[0..N-1]` (no gaps, sorted) | Always | `fail` (`details.gaps[]`) |
| `context_items_valid_refs` | Every `context_items.message_id` resolves in `messages`; every `context_items.summary_id` resolves in `summaries` | Always | `fail` (`details.danglingRefs[]`) |
| `summaries_have_lineage` | Each `leaf` summary has ≥1 row in `summary_messages`; each `condensed` summary has ≥1 row in `summary_parents` | Always | `fail` (`details.missingLineage[]`) |
| `no_orphan_summaries` | Each summary appears either in `context_items` or as a child in `summary_parents` | Always | **`warn`** (`details.orphanedSummaryIds[]`) — note this is the only check that warns, not fails |
| `context_token_consistency` | Sum of `messages.token_count` + `summaries.token_count` referenced by `context_items` matches the aggregate-query result | Always | `fail` (`details.manualSum`, `aggregateTotal`, `difference`) |
| `message_seq_contiguous` | `messages.seq` for the conversation is `[0..N-1]` | Always | `fail` (`details.gaps[]`) |
| `no_duplicate_context_refs` | No `message_id` or `summary_id` appears twice in `context_items` | Always | `fail` (`details.duplicates[]`) |

The module also exports `repairPlan(report)` — a pure function that turns failing/warning checks into human-readable suggestion strings. This is **planning only** — does not perform repairs.

A separate `collectMetrics(conversationId, conversationStore, summaryStore)` collects observability metrics (`contextTokens`, `messageCount`, `summaryCount`, `contextItemCount`, `leafSummaryCount`, `condensedSummaryCount`, `largeFileCount`). Not strictly an "integrity" check, but shipped from the same module.

In Python: keep `IntegrityChecker` as a class that takes the stores (no direct DB handle), and have each check be an `async def _check_<name>` for parity. The `repairPlan` becomes a free function.

## Prune cascade — soft-suppression + hard-delete behaviors

There are TWO distinct prune surfaces:

### A. `pruneConversations` (`src/prune.ts`) — DATA RETENTION HARD DELETE

This is the age-based prune used for "delete conversations where all messages are older than 90 days". Not soft-suppression — actual DELETE rows. Inputs: `before` duration string (parsed by `parseDuration` — supports `d|day|week|w|m|month|y|year`), `confirm` (default false = dry-run), `batchSize` (default 100), `maxBatches`, `vacuum`, `now` (test override).

**Hard-delete cascade order** (`deleteCandidates`, all keyed via three staged temp tables):

1. `summary_messages` — delete by `summary_id IN candidate_summary_ids`
2. `summary_messages` — delete by `message_id IN candidate_message_ids`
3. `summary_parents` — delete by `summary_id IN candidate_summary_ids`
4. `summary_parents` — delete by `parent_summary_id IN candidate_summary_ids`
5. `context_items` — delete by `message_id IN candidate_message_ids`
6. `context_items` — delete by `summary_id IN candidate_summary_ids`
7. `context_items` — delete by `conversation_id IN candidate_conversation_ids` (catches summary/message-typed rows whose parent IDs weren't caught above)
8. `messages_fts` — best-effort, if table exists
9. `summaries_fts` — best-effort, if table exists
10. `summaries_fts_cjk` — best-effort, if table exists
11. `conversations` — final DELETE

Each batch runs in its own `BEGIN IMMEDIATE` transaction. `loadPruneCandidates` uses `julianday(...) < julianday(cutoff)` so mixed timestamp formats compare chronologically (not lexically).

### B. `runPurge` (`src/operator/purge.ts`) — SOFT SUPPRESSION

This is the operator's `/lcm purge` — soft suppression of leaves matching a criteria. NOT a hard delete (the rows stay; `suppressed_at` is set). All cascades happen in a single `BEGIN IMMEDIATE` transaction:

1. **`summaries.suppressed_at = datetime('now')`** + `summaries.suppress_reason = ?` for matched leaf summary IDs. This UPDATE fires the **per-model vec0 trigger** `lcm_embed_suppress_<slug>` (created by `ensureEmbeddingsTable` in `src/embeddings/store.ts:232`), which updates the metadata col on the per-model `lcm_embeddings_<slug>` vec0 table (`suppressed=1`) so semantic search filters them out automatically.
2. **`summaries.contains_suppressed_leaves = 1`** for condensed summaries whose `summary_parents.parent_summary_id` is one of the suppressed leaves. Flags them for idle rebuild.
3. **DELETE `context_items` WHERE `item_type='summary' AND summary_id IN (...)`** — removes the assembler's pointer so the suppressed summary cannot be re-emitted into the prompt.
4. **DELETE `context_items` WHERE `item_type='message' AND message_id IN (SELECT message_id FROM summary_messages WHERE summary_id IN (...))`** — cuts the message-level pointer for the same reason.
5. **UPDATE `messages.suppressed_at = datetime('now')`** for messages linked via `summary_messages` to suppressed leaves — **gated by `NOT EXISTS` on any non-suppressed referencing summary outside the purge set**, so a message shared with a non-purged leaf is not orphaned.
6. **DELETE `lcm_synthesis_cache` WHERE `cache_id IN (SELECT DISTINCT cache_id FROM lcm_cache_leaf_refs WHERE leaf_summary_id IN (...))`** — invalidates rebuildable synthesis caches that referenced the suppressed leaves. (The cache schema's `ON DELETE CASCADE` only fires on hard DELETE; soft suppression must do this explicitly.)

**The `mode='immediate'` hard-delete drainer was REMOVED in the first-principles pass (2026-05-06).** `runPurge` always returns `mode: "soft"`. The byte-level deletion path is deferred (preserved in draft PR #616 — not yet shipped). For GDPR-compliant byte erasure today, operator runs raw `DELETE` + `VACUUM` out-of-band — soft purge alone does NOT remove the row bytes.

### Read paths that filter `suppressed_at IS NULL`

Hermes must implement the same invariant on every agent-facing read path: **45 occurrences** of `suppressed_at IS NULL` across the TS source, distributed:

- `src/store/summary-store.ts` — 11 occurrences (primary read surface)
- `src/store/conversation-store.ts` — 5 occurrences
- `src/embeddings/backfill.ts` — 2 (don't embed suppressed)
- `src/embeddings/store.ts` — 2 (vec0 metadata)
- `src/embeddings/semantic-search.ts` — 2 (KNN filter)
- `src/extraction/entity-coreference.ts` — 3 (don't extract from suppressed)
- `src/tools/lcm-grep-tool.ts` — 4
- `src/tools/lcm-describe-tool.ts` — 3
- `src/tools/lcm-search-entities-tool.ts` — 2
- `src/tools/lcm-synthesize-around-tool.ts` — 2
- `src/tools/lcm-get-entity-tool.ts` — 1
- `src/tools/lcm-entity-shared.ts` — 1
- `src/operator/health.ts` — 1 (counter for the suppression-health snapshot)

**Internal callers opt out** via an `includeSuppressed: true` flag — used by integrity, compaction, and doctor itself (which by design needs to see suppressed rows). The default everywhere is exclusion.

### Schema additions to support suppression

Per `src/db/migration.ts`:
- `summaries.suppressed_at TEXT` (nullable)
- `summaries.suppress_reason TEXT` (nullable)
- `summaries.contains_suppressed_leaves INTEGER NOT NULL DEFAULT 0`
- `summaries.superseded_by TEXT REFERENCES summaries(summary_id) ON DELETE SET NULL` (forwarder pattern for idle rebuild)
- `messages.suppressed_at TEXT` (nullable)
- Partial indexes: `summaries(suppressed_at) WHERE suppressed_at IS NOT NULL`, same for `messages`.
- Per-model vec0 triggers: `lcm_embed_suppress_<slug>` (AFTER UPDATE OF suppressed_at ON summaries) and `lcm_embed_delete_<slug>` (AFTER DELETE ON summaries).

## Operator modules

| File | Role | Surface | Python target | DROP? |
|---|---|---|---|---|
| `operator/purge.ts` | `/lcm purge` — soft suppression of leaves by criteria (summaryIds OR sessionKey/since/before/minTokenCount) | `runPurge`, `previewPurgeAffected`, `PurgeError` | `operator/purge.py` | No |
| `operator/health.ts` | `/lcm health` — v4.1 health snapshot of embeddings, workers, synthesis, eval, suppression | `getV41HealthSnapshot` (pure read-only, tolerant of missing tables) | `operator/health.py` | No |
| `operator/reconcile-session-keys.ts` | `/lcm reconcile-session-keys` — merge legacy session_keys into a chosen target | `reconcileSessionKeys`, `listLegacyCandidates`, `ReconcileError` | `operator/reconcile.py` | No |
| `operator/backfill-autostart.ts` | Embedding-backfill autostart (background worker, opt-in via `VOYAGE_API_KEY`) | `tryStartBackfillAutostart` | `operator/backfill_autostart.py` | Re-evaluate — depends on whether Hermes embeds via Voyage |
| `operator/extraction-autostart.ts` | Entity-coreference autostart (background, default ON, opt-out via `LCM_EXTRACTION_LLM_ENABLED=false`) | `tryStartExtractionAutostart` | `operator/extraction_autostart.py` | No |
| `operator/eval-runner.ts` | `/lcm eval` — recall@K eval with drift comparison | `runEval`, `formatEvalReport`, `EvalRunnerError` | `operator/eval_runner.py` | No |
| `operator/semantic-infra-init.ts` | One-time `vec0` extension load + embedding-profile registration | `initSemanticInfraIfPossible` | `operator/semantic_infra.py` | Yes if Hermes uses pgvector/Qdrant/other |
| `operator/worker-llm.ts` | Adapter that wraps `deps.complete` into the `LlmCall` signature consumed by `dispatchSynthesis` | `createWorkerLlmCall` | Merge into `operator/worker_orchestrator.py` or `synthesis/dispatch.py` | Merge — small (167 LOC), no independent state |
| `operator/worker-orchestrator.ts` | Thin coordinator over backfill/extraction/lock APIs; powers `/lcm worker tick <kind>` | `getWorkerStatusSnapshot`, `tickEmbeddingBackfill`, `tickExtraction`, `forceReleaseLock`, `heartbeatAllHeldLocks` | `operator/worker_orchestrator.py` | No |

### Background workers vs. operator commands

**Background workers** (autostart-driven, invoked from plugin init in `src/plugin/index.ts:2748-2766`):
- `semantic-infra-init.ts` — fires once at plugin load (`initSemanticInfraIfPossible`).
- `backfill-autostart.ts` — fires once; spawns `setInterval` ticking every 5 min (default). Auto-stops on 3 consecutive idle ticks or 3 consecutive Voyage failures.
- `extraction-autostart.ts` — fires once; spawns `setInterval` ticking every 60 s. Auto-stops on 3 consecutive failures.

**Operator commands** (manually invoked via `/lcm` slash command in `src/plugin/lcm-command.ts`):
- `purge.ts` — `/lcm purge`
- `health.ts` — `/lcm health`
- `reconcile-session-keys.ts` — `/lcm reconcile-session-keys`
- `eval-runner.ts` — `/lcm eval`
- `worker-orchestrator.ts` — `/lcm worker status`, `/lcm worker tick <kind>`
- (Doctor commands `/lcm doctor`, `/lcm doctor apply`, `/lcm doctor clean`, `/lcm doctor clean apply` route into the doctor plugin modules, not into `operator/`.)

`worker-llm.ts` is **infrastructure**, not a worker or command — it's the adapter that lets background workers and the synthesis dispatch surface the host's LLM provider.

### Operator gate (plugin-specific code path that Hermes must re-express)

In `src/plugin/lcm-command.ts`, every mutating subcommand starts with `if (!ctx.senderIsOwner) return <403-shaped response>`. The cleaners read-only listing was added to the gate in Wave-12 (it leaks session_keys + message previews across the global conversation set). In Hermes, the same boundary should be enforced — likely as a `--operator` flag plus a "this CLI is owner-only" assumption baked into the entry point.

## Transcript-repair

`src/transcript-repair.ts` (300 LOC) implements `sanitizeToolUseResultPairing<T>(messages: T[]): T[]` — a generic function over an `AgentMessage`-shaped type that:

1. Moves matching `toolResult` messages directly after their assistant `toolCall` turn.
2. Inserts synthetic error `toolResult`s for any `tool_use` ID that has no matching result (so Anthropic / Cloud Code Assist won't reject the transcript).
3. Drops duplicate `toolResult`s for the same ID (counts them in `droppedDuplicateCount`).
4. Drops orphaned `toolResult`s with no matching tool call.
5. Skips `tool_use` extraction for aborted/errored assistant messages (`stopReason === "error" | "aborted"`) — the blocks may be incomplete.
6. Normalizes OpenAI-shape reasoning blocks that landed AFTER the tool call (specific shape: single function call followed by reasoning).

Tool-call types it recognizes: `"toolCall" | "toolUse" | "tool_use" | "tool-use" | "functionCall" | "function_call"` (handles both Anthropic and OpenAI block shapes).

**Hermes port note:** the LCM source is copied verbatim from `openclaw core (src/agents/session-transcript-repair.ts + src/agents/tool-call-id.ts)`. The repair LOGIC ports cleanly (pure function over the in-memory message list). What changes:
- LCM consumes a JSONL transcript on disk in some code paths; **drop the JSONL-rewrite branch** for Hermes. The Hermes transcript IS the in-memory message list — no on-disk rewrite.
- The minimal `AgentMessageLike` shape becomes a Pydantic `BaseModel` (or a `TypedDict`) — covers `role`, `content`, `toolCallId`/`toolUseId`, `toolName`, `stopReason`, `isError`, `timestamp`.

## Transaction-mutex

`src/transaction-mutex.ts` (202 LOC) implements a **per-database async mutex** keyed on the `DatabaseSync` instance via a `WeakMap` (so DBs are GC'd normally). Exposes:

- `acquireTransactionLock(db)` — low-level, non-reentrant. Returns a release function.
- `acquireTransactionLockWithTimeout(db, timeoutMs)` — same plus `DatabaseTransactionTimeoutError` on deadline.
- `withExclusiveDatabaseLock(db, { timeoutMs }, op)` — hold the lock without opening a transaction.
- `withDatabaseTransaction(db, "BEGIN" | "BEGIN IMMEDIATE", op)` — the main entry point. Acquires the lock, opens the transaction, COMMITs on success, ROLLBACKs on throw.

**Reentrancy via savepoints:** the implementation uses `AsyncLocalStorage<Map<DatabaseSync, number>>` to track the held-lock depth per async context. A nested call to `withDatabaseTransaction` on the same DB detects depth > 0 and uses `SAVEPOINT lcm_txn_savepoint_<N>` / `RELEASE` / `ROLLBACK TO` instead of trying to nest BEGIN (which SQLite rejects).

**Python port:** keyed on `session_id` (Hermes' boundary), not on a connection instance:

- `dict[str, asyncio.Lock]` — one lock per session.
- A `contextvars.ContextVar[dict[str, int]]` for the held-depth tracking (replaces `AsyncLocalStorage`).
- `withDatabaseTransaction` becomes `@asynccontextmanager async def database_transaction(session_id, begin_mode)`.

The reentrancy/savepoint pattern ports cleanly — SQLite savepoint syntax is the same.

The trigger for this whole module was `https://github.com/Martian-Engineering/lossless-claw/issues/260` (multiple async operations sharing one synchronous `DatabaseSync` handle ran into "cannot start a transaction within a transaction"). Hermes' Python with `aiosqlite` or `sqlite3` over `asyncio.to_thread` has the same issue — keep the mutex.

## Python module layout

```
src/lossless_hermes/
├── doctor/
│   ├── __init__.py
│   ├── contract.py        # ← Pydantic models for DoctorMarkerKind, DoctorTargetRecord,
│   │                         DoctorApplyResult, DoctorCleanerId, DoctorCleanerScan,
│   │                         DoctorCleanerApplyResult; FALLBACK_/TRUNCATED_ constants.
│   ├── apply.py           # ← applyScopedDoctorRepair → apply_scoped_doctor_repair.
│   │                         Handles LLM resolution + ordered leaves-first repair.
│   ├── cleaners.py        # ← scanDoctorCleaners + applyDoctorCleaners, plus the three
│   │                         CleanerDefinition records (archived_subagents, cron_sessions,
│   │                         null_subagent_context). Temp-table staging stays SQL-level.
│   └── shared.py          # ← detectDoctorMarker, loadDoctorTargets, getDoctorSummaryStats.
├── integrity.py           # ← IntegrityChecker class with 8 check methods + repairPlan
│                              free function + collectMetrics.
├── prune.py               # ← pruneConversations (data-retention hard delete) + parseDuration.
├── transcript_repair.py   # ← sanitize_tool_use_result_pairing; drop JSONL-rewrite branch.
├── transaction_mutex.py   # ← dict[str, asyncio.Lock] + contextvars depth tracking;
│                              database_transaction async ctxmgr with savepoint reentrancy.
└── operator/
    ├── __init__.py
    ├── purge.py                  # ← runPurge soft suppression + previewPurgeAffected + PurgeError.
    ├── health.py                 # ← getV41HealthSnapshot tolerant read-only snapshot.
    ├── reconcile.py              # ← reconcileSessionKeys + listLegacyCandidates + ReconcileError.
    ├── backfill_autostart.py     # ← tryStartBackfillAutostart (asyncio.create_task instead of setInterval).
    ├── extraction_autostart.py   # ← tryStartExtractionAutostart.
    ├── eval_runner.py            # ← runEval + formatEvalReport + EvalRunnerError.
    ├── semantic_infra.py         # ← initSemanticInfraIfPossible — only if Hermes uses vec0.
    └── worker_orchestrator.py    # ← getWorkerStatusSnapshot, tickEmbeddingBackfill, tickExtraction,
                                     forceReleaseLock, heartbeatAllHeldLocks; merge in worker-llm.ts.
```

## Test inventory

In-scope tests in `lossless-claw/test/`:

| Test file | Covers |
|---|---|
| `test/transcript-repair.test.ts` | `sanitizeToolUseResultPairing` |
| `test/transaction-mutex.test.ts` | `withDatabaseTransaction` + savepoint reentrancy |
| `test/concurrency-model.test.ts` | Worker-lock fundamentals (used by orchestrator) |
| `test/prune.test.ts` | `pruneConversations`, `parseDuration` |
| `test/operator-health.test.ts` | `getV41HealthSnapshot` |
| `test/operator-purge.test.ts` | `runPurge` cascade |
| `test/operator-reconcile-session-keys.test.ts` | `reconcileSessionKeys`, `listLegacyCandidates` |
| `test/operator-eval-runner.test.ts` | `runEval` |
| `test/operator-worker-orchestrator.test.ts` | Orchestrator surfaces |
| `test/v41-suppression-cascade-trigger.test.ts` | The `lcm_embed_suppress_<slug>` trigger fires correctly |
| `test/v41-suppression-fts-filter.test.ts` | FTS read paths filter suppressed rows |
| `test/v41-suppression-invariants.test.ts` | The `suppressed_at IS NULL` invariant across read surfaces |
| `test/v41-data-cleanup.test.ts` | Bulk-delete + cascade behavior |

Doctor cleaners and `applyScopedDoctorRepair` have no dedicated test file on this branch — their behavior is exercised via `test/lcm-command.test.ts` (`/lcm doctor` command tests) and `test/v41-authorization-invariants.test.ts` (owner-gate tests). **Confirm if porting**: this is a coverage gap worth filling in the Python port.

The TUI also has `tui/doctor.go` + `tui/doctor_test.go` — independent Go-side reimplementation of doctor with slightly different surface. Not in TS scope, but useful cross-reference for Hermes (which is also Go-adjacent in some lifecycle paths).

## Hermes cross-reference

The task brief mentioned `hermes_cli/doctor.py` and `hermes_cli/backup.py`. **Neither exists in `/Volumes/LEXAR/Claude/lossless-hermes/` yet** — the repo currently only contains `README.md`, `docs/`, and `epics/`. No `hermes_cli/` directory. The Hermes-side doctor + backup story is greenfield: this guide is the spec.

When the Hermes CLI lands:
- `hermes_cli/doctor.py` should expose `hermes doctor`, `hermes doctor apply`, `hermes doctor clean`, `hermes doctor clean apply` — mirroring the `/lcm doctor` surface in `src/plugin/lcm-command.ts`.
- `hermes_cli/backup.py` corresponds to `src/plugin/lcm-db-backup.ts` (which `applyDoctorCleaners` uses to write a backup file before the destructive apply). Same module is consumed by `/lcm backup` and `/lcm rotate` — keep that pattern.

## Open decisions

- **ADR-?: Doctor invocation model.** TS routes everything through the plugin slash-command surface inside `openclaw-1`. Hermes options: (a) `hermes doctor ...` CLI subcommand only, (b) `hermes doctor` CLI + opt-in background autorun on session-end, (c) full daemon model parallel to TS. Recommend (a) for parity + (b) as a follow-up if operators report "I forgot to run it" as the failure mode. Background autorun would also need owner-only gating semantics carried over.
- **ADR-?: Doctor "fix log" storage.** Currently `applyScopedDoctorRepair` returns the log to the caller (DoctorApplyResult), which renders it to stdout via the plugin's text-mode response. It is NOT persisted. Options: (a) keep as ephemeral (parity), (b) write a new `lcm_doctor_audit` table, (c) fold into the existing `lcm_session_key_audit` shape. Recommend (a) for parity unless operators ask for retroactive forensics.
- **ADR-?: Autostart concurrency model.** TS uses Node `setInterval` + a single in-flight guard per autostart. Python options: (a) `asyncio.create_task` with the same in-flight guard, (b) APScheduler-style cron, (c) a single background-task supervisor that owns all autostarts. Recommend (a) (asyncio.create_task) — keeps the auto-stop-on-failures logic local and avoids a scheduling dependency. Per-kind tasks; not multiplexed.
- **ADR-?: `/lcm doctor clean` cleaner inventory.** The three predefined cleaners (`archived_subagents`, `cron_sessions`, `null_subagent_context`) target sub-agent and cron pollution specific to the OpenClaw plugin host. Hermes may have different "garbage" patterns. Decide whether to (a) port the three as-is (some may be no-ops on Hermes), (b) keep the cleaner framework but seed it with Hermes-specific predicates, or (c) build a generic "predicate cleaner" surface and let operators add their own.
- **ADR-?: Voyage vs alternative embedder.** `backfill-autostart.ts` is hardcoded to `VOYAGE_API_KEY`; `semantic-infra-init.ts` is hardcoded to `sqlite-vec`. If Hermes uses a different embedder/vector store (pgvector, Qdrant, OpenAI embeddings), these two modules either become provider-shaped abstractions or get dropped in favor of host-native equivalents.

## Remaining 5% risk

- **`doctor-contract-api.d.ts` does not exist.** The task brief expected a formal `.d.ts` contract; the actual contract is implicit in the three plugin doctor module exports. Risk: someone reading the brief expects a single canonical file, doesn't find it, and reinvents a different shape. Mitigation: this guide IS the contract; cross-reference it from the Python `doctor/contract.py` module-docstring.
- **`applyScopedDoctorRepair` LLM coupling.** The TS module pulls in `createLcmSummarizeFromLegacyParams` (a plugin-specific summarizer factory) plus `LcmDependencies` (a plugin DI shape). Porting that surface requires Hermes to have an equivalent — what it has now is unclear from this repo alone. Risk: the abstraction-boundary translation gets wrong and the Python port either over-couples or under-couples to Hermes' LLM surface.
- **Worker-lock module not in scope.** `operator/worker-orchestrator.ts` imports `acquireLock`, `releaseLock`, `heartbeatLock`, `lockInfo`, `generateWorkerId` from `src/concurrency/worker-lock.js` plus `WORKER_JOB_KINDS` from `src/concurrency/model.js`. Those modules aren't in this guide's scope but are required for the orchestrator to function — keep them on the radar for whichever porting guide owns `src/concurrency/`.
- **`/lcm doctor` semantics around cross-conversation scope.** The plugin command resolves "current conversation" via the request context (`ctx`). Hermes CLI has no such context implicitly — every invocation must specify `--conversation-id N` or similar. Risk: the UX changes meaningfully; operators used to "doctor the current thread" lose that convenience.
- **No dedicated test for `applyScopedDoctorRepair`.** Coverage is implicit via the plugin command tests. The Python port should ship a dedicated `test_doctor_apply.py` — both for ordered leaves-first repair and for the override-map condensed re-summarization path.
