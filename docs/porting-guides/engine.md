# Porting Guide: Engine Orchestrator

**Source:** `src/engine.ts` (8731 LOC — note: the spec said 8940; actual `wc -l` on `pr-613` is 8731)
**Python target:** `src/lossless_hermes/engine.py` (estimated ~6500–7000 LOC — smaller because JSONL/bootstrap/auto-rotate logic drops, and Python is roughly equivalent in density)
**Confidence target:** 95%
**Estimated effort:** 80–120 hours
**Epic:** 02-engine-skeleton + 03-ingest-assembly + 04-compaction

---

## Architecture summary

`engine.ts` is the **single LcmContextEngine class** that implements OpenClaw's `ContextEngine` interface (the contract at `openclaw/plugin-sdk/src/context-engine/types.d.ts`). It is the orchestrator that owns the SQLite connection and composes a small set of stores (`ConversationStore`, `SummaryStore`, `CompactionTelemetryStore`, `CompactionMaintenanceStore`), a `ContextAssembler`, a `CompactionEngine`, and a `RetrievalEngine`. Every public method routes through a per-session async queue (`withSessionQueue`) and into the appropriate store/engine collaborators inside a SQLite transaction.

The file is roughly 350 lines of module-private helpers (token estimation, JSONL parsing, image/media interception scaffolding, hash utilities) followed by one ~6950-line class declaration. Most of the LOC is **not algorithmic complexity** — it is policy: circuit-breaker state machines, cache-aware deferral gates, dynamic leaf-chunk sizing, bootstrap fast-paths against the JSONL session file, JSONL auto-rotation for huge transcripts, transcript-GC, image/large-file externalization. Algorithmic depth lives in `compaction.ts`, `assembler.ts`, and `summarize.ts` (sister files, out of scope for this guide).

What makes this file the heart of the system: it owns the **decision logic** for *when* to compact, *which* compaction to run (leaf-incremental vs. budget-recovery sweep), *whether* to defer (cache-hot delay), and *how* to handle the deferred debt (sync drain at critical pressure, async drain otherwise). It also owns the **always-on assembly substitution** — every `assemble()` call is the bridge between Hermes-style "give me messages back" and LCM's true behavior of "I rewrite the prompt each turn from the DAG". The Python port preserves the algorithm but moves the call-site from OpenClaw's per-turn `engine.assemble()` to Hermes's `compress()` (overflow-only) plus a new pre-turn injection point — see "Always-on assembly problem" below.

---

## State owned by LcmContextEngine

| Field | Type | Purpose |
|---|---|---|
| `info` | `ContextEngineInfo` | Identity / `ownsCompaction` flag (set false if migration failed → degrades to no-op) |
| `config` | `LcmConfig` | Frozen config snapshot (thresholds, circuit-breaker, cache TTL, leaf chunk bounds, ignore/stateless patterns) |
| `conversationStore` | `ConversationStore` | Raw messages + parts + conversation rows + FTS5 index |
| `summaryStore` | `SummaryStore` | Summaries + context_items (the DAG of "what to show") + bootstrap checkpoint rows |
| `compactionTelemetryStore` | `CompactionTelemetryStore` | Per-conversation cache-state, retention, prompt-token observations |
| `compactionMaintenanceStore` | `CompactionMaintenanceStore` | Deferred-debt rows (pending/running) — durable retry queue |
| `assembler` | `ContextAssembler` | Builds the message list under a token budget from context_items |
| `compaction` | `CompactionEngine` | Algorithmic guts: evaluate(), compactLeaf(), compactUntilUnder(), compact() |
| `retrieval` | `RetrievalEngine` | BM25/FTS for lcm_grep tool (out of scope here) |
| `db` | `DatabaseSync` (better-sqlite3 sync API) | Shared SQLite connection (singleton across plugin lifetime) |
| `migrated` | `boolean` | Set at construction once `runLcmMigrations` succeeds |
| `fts5Available` | `boolean` | Probed at construction; affects search-fallback behavior |
| `ignoreSessionPatterns` | `RegExp[]` | Compiled from config — bypass all LCM processing for matching sessions |
| `statelessSessionPatterns` | `RegExp[]` | Compiled — skip writes, allow reads |
| `sessionOperationQueues` | `Map<string, {promise, refCount}>` | **Per-session async mutex chain** — see "Per-session async queue" below |
| `previousAssembledMessagesByConversation` | `Map<number, AssemblePrefixSnapshot>` | Last assembled message list per conversation — drives prefix-stability debug logs |
| `stableOrphanStrippingOrdinalsByConversation` | `Map<number, number>` | Stable boundary for orphan-tool-result stripping across hot-cache turns (prevents prefix churn) |
| `recentBootstrapImportsByConversation` | `Map<number, BootstrapImportObservation>` | Last-N-bootstrap diagnostics for overflow log decoration |
| `oversizedAutoRotateCheckpointByQueueKey` | `Map<string, number>` | Auto-rotate guard (avoid re-rotating an already-rotated file) |
| `largeFileTextSummarizer` (+ resolved flag) | `(prompt)=>Promise<string|null>` | Lazy-resolved optional model summarizer for large text files |
| `deps` | `LcmDependencies` | Logger + plugin context (host abstractions) |
| `lastFullReadFileState` | `Map<number, {size, mtimeMs}>` | Per-conversation JSONL file checkpoint — skip full re-read on unchanged file |
| `circuitBreakerStates` | `Map<string, CircuitBreakerState>` | Auth-failure circuit breakers per session/provider scope |
| `afterTurnReconcileFullReadStates` | `Map<string, {size, mtimeMs}>` (bounded 4096 FIFO) | Skip O(file-size) afterTurn slow-path when file unchanged |
| `cacheContextUnknownLogged` | `Set<number>` | Per-process dedupe for cache-context-unknown info log |

**Python target — drop these:** `lastFullReadFileState`, `recentBootstrapImportsByConversation`, `oversizedAutoRotateCheckpointByQueueKey`, `afterTurnReconcileFullReadStates`, `largeFileTextSummarizer`. They all key off the JSONL session file which Hermes does not have. The Python class will be visibly smaller as a result.

---

## OpenClaw `ContextEngine` method-by-method

### bootstrap(params)

- **Signature:** `bootstrap({ sessionId, sessionFile, sessionKey? }) → BootstrapResult`
- **Lines:** 4983–5424
- **Behavior:** Imports historical JSONL messages into the LCM DB on first contact with a session. Layered fast-paths:
  1. **Checkpoint hit** — if file size/mtime match the stored `lastSeenSize/lastSeenMtimeMs` and there is a bootstrap row, no-op.
  2. **Append-only fast-path** — same file, larger size, same `lastProcessedEntryHash` at the checkpoint offset → tail-scan and `readAppendedLeafPathMessages()` from the offset onward; ingest only the delta.
  3. **File-level cache guard** — `lastFullReadFileState` matches → skip reading entirely.
  4. **Cold path** — `readLeafPathMessages()` parses the whole JSONL, `trimBootstrapMessagesToBudget()` keeps it under `bootstrapMaxTokens`, ingest each via `ingestSingle()`.
  5. **Existing-conversation reconcile** — `reconcileSessionTail()` repairs gaps when a crash dropped messages mid-turn.
  6. **Session-file rollover guard** — if the tracked JSONL file moved/disappeared but the sessionKey is the same, `applySessionReplacement()` archives the old conversation and creates a fresh one.
  7. **Post-bootstrap HEARTBEAT_OK pruning** — `pruneHeartbeatOkTurns()` removes synthetic ack turns.
- **State mutation:** Creates/updates `conversations`, `messages`, `message_parts`, `context_items`, `conversation_bootstrap_state`, `summary_store_bootstrap`. Sets `lastFullReadFileState[conversationId]`, clears `stableOrphanStrippingOrdinals`.
- **Returns:** `{ bootstrapped, importedMessages, reason? }` — `bootstrapped=true` only when at least one new message landed in the DB.
- **Python mapping:** `LCMEngine.on_session_start(self, session_id: str, **kwargs)`. kwargs include `boundary_reason`, `old_session_id`, `hermes_home`, possibly `model`/`platform`.
- **What changes:**
  - **DROP**: every JSONL fast-path (checkpoint-hit, append-only, file-cache-guard, transcript reconcile, session-file rollover). Hermes has no JSONL session file — its persistence is `session.db` (sqlite), which Hermes already manages.
  - **DROP**: `auto-rotate session file` calls (gated behind `autoRotateSessionFiles.startup`).
  - **DROP**: `bootstrapMaxTokens` trimming (no historical-message import path on first contact — for Hermes, sessions begin fresh and ingest happens per-turn).
  - **ADD**: optional one-time backfill from Hermes's `session.db` via a separate offline CLI (e.g. `hermes lcm backfill --session <id>`). Not in the per-session hot path.
  - **KEEP**: `HEARTBEAT_OK pruning` if Hermes has heartbeat ack turns; otherwise drop.
  - **KEEP**: `applySessionReplacement()` semantics if Hermes has /reset or session rollover.
- **Confidence:** 85% — pending decision on whether `on_session_start` should opportunistically backfill from `session.db` or leave that to a separate command.

### ingest(params) / ingestBatch(params)

- **Signatures:**
  - `ingest({ sessionId, sessionKey?, message, isHeartbeat? }) → IngestResult`
  - `ingestBatch({ sessionId, sessionKey?, messages, isHeartbeat? }) → IngestBatchResult`
- **Lines:** 5899–6134 (`ingestSingle` private body 5899–6064, public `ingest` 6066–6090, `ingestBatch` 6092–6134)
- **Behavior:**
  - Skip when `isHeartbeat`, ignored-session, stateless-session, or non-persistable role.
  - Skip assistant messages with `stopReason=error|aborted` and empty content (prevents retry pollution loop).
  - Run media-interception pipeline: `interceptInlineImagesInToolMessage`, `interceptNativeUserImageBlocks`, `interceptInlineImages`, `interceptLargeFiles`, `interceptLargeToolResults`, `interceptLargeRawPayload`. These mutate `messageForParts` and `stored.content` to externalize large/binary blobs to `largeFilesDir`.
  - **One atomic SQLite transaction** wraps the three-write sequence (`getMaxSeq` → `createMessage` → `createMessageParts` → `appendContextMessage`). Wave-4 P0 fix — without `BEGIN IMMEDIATE` you get orphan rows on partial failure or `UNIQUE` conflicts on concurrent ingest race.
  - `ingestBatch` just loops `ingestSingle` under one queue acquisition.
- **State mutation:** appends to `messages`, `message_parts`, `context_items`; writes externalized media files to disk.
- **Returns:** `{ ingested: boolean }` or `{ ingestedCount: number }`.
- **Python mapping options:**
  - **A. ABC upstream patch:** add `engine.ingest(message)` (or `engine.on_message(role, content, **kwargs)`) to the Hermes `ContextEngine` ABC, plus ~25 call-site additions in `run_agent.py` (every place the messages list grows). High coupling cost, high blast radius.
  - **B. `post_llm_call` diff-on-each-turn:** register `post_llm_call` hook (already fires once per turn at `run_agent.py:15403–15420`); receive `conversation_history=list(messages)`; diff against `self.last_seen_message_idx[session_id]`; ingest each new message. Zero Hermes core changes. **Misses pure-tool-call turns where the loop exits before `post_llm_call` fires** — verify by inspecting `final_response and not interrupted` gate at line 15407.
  - **C. `handle_tool_call` piggyback:** every tool call passes through `handle_tool_call(name, args, **kwargs)` and `messages=` is in kwargs (line 159 of `context_engine.py`); same diff pattern, fires on tool-call turns. Use together with B as a belt-and-braces.
  - **D. Polling thread:** asyncio task reads Hermes's `session.db` periodically. Highest latency, worst for stop-on-overflow guarantees.
- **Recommended:** **B + C combo**. B is the primary path (most turns end with a final assistant response). C is the safety net for pure-tool-call turns. Idempotent: the dedup guard (`deduplicateAfterTurnBatch` analog) makes a double-call harmless. Decision belongs in ADR-04.
- **Confidence:** 75% — needs a quick spike to confirm `post_llm_call` covers tool-call-only turns. If it does, C is unnecessary.

### afterTurn(params)

- **Signature:** `afterTurn({ sessionId, sessionKey?, sessionFile, messages, prePromptMessageCount, autoCompactionSummary?, isHeartbeat?, tokenBudget?, runtimeContext?, legacyCompactionParams? }) → void`
- **Lines:** 6220–6646
- **Behavior:** The fattest method. Sequence:
  1. Auto-rotate JSONL if it's huge (`maybeAutoRotateManagedSessionFile`, runtime phase).
  2. **Transcript-tail reconcile** (`reconcileTranscriptTailForAfterTurn`) — repairs JSONL crash gaps before deduping.
  3. **Dedup new messages** against the DB via `deduplicateAfterTurnBatch` (last-N message hash check, with `oversizedNoOverlap` fallback).
  4. **Summary-coverage skip** — drop messages already covered by `autoCompactionSummary` text.
  5. **Ingest the batch** via `this.ingestBatch(...)`.
  6. **HEARTBEAT_OK pruning** if the batch looks like a heartbeat ack turn.
  7. **Compaction telemetry update** (`updateCompactionTelemetry`) — recompute cache state from prompt-cache observation in `runtimeContext`.
  8. **Decision time**: `evaluateIncrementalCompaction` (leaf-trigger + cache state + activity band) AND `compaction.evaluate` (budget-trigger).
  9. **Inline mode** (`proactiveThresholdCompactionMode === "inline"`): if leaf-decision fires, schedule `runAfterTurnInlineLeafCompaction` (async-fire-and-forget under queue); otherwise call `this.compact(...)` synchronously with target="threshold".
  10. **Deferred-debt mode** (default): if threshold OR leaf trigger fires → `recordDeferredCompactionDebt` (write to `compactionMaintenanceStore`), then schedule async drain via `scheduleDeferredCompactionDebtDrain` — **EXCEPT** at critical pressure (`isUnderCriticalBudgetPressure` ≥ 0.70 of budget) → drain synchronously to guarantee the next assemble sees compacted state.
  11. Refresh bootstrap checkpoint state, run runtime auto-rotate again.
- **State mutation:** writes to all stores; mutates `cacheContextUnknownLogged`, `circuitBreakerStates` (via downstream compaction).
- **Returns:** `void`.
- **Python mapping:** `LCMEngine._on_post_llm_call(self, session_id, user_message, assistant_response, conversation_history, model, platform, **kwargs)` — registered as a `post_llm_call` hook via `PluginContext.register_hook`. Optional: an async background task for the deferred-debt drain.
- **What changes:**
  - **DROP**: `maybeAutoRotateManagedSessionFile`, `reconcileTranscriptTailForAfterTurn`, `pruneHeartbeatOkTurns` (unless Hermes has heartbeat semantics).
  - **DROP**: the synthetic `autoCompactionSummary` prepend (Hermes doesn't have a parallel runtime feature).
  - **KEEP**: dedup against DB (the in-memory `last_seen_message_idx` plays this role for Hermes — ingest is incremental).
  - **KEEP**: full compaction-decision logic (telemetry → leaf-decision → budget-decision → inline-vs-deferred → critical-pressure sync drain).
  - **REDESIGN**: deferred-debt drain. In Hermes there's no `maintain()` lifecycle hook firing later in the background — replace with an `asyncio.create_task` started at engine init that polls the maintenance store every N seconds, OR fire on `pre_llm_call` (consume debt before the next turn).
- **Confidence:** 80% — the deferred-debt-drain redesign is the biggest unknown.

### assemble(params)

- **Signature:** `assemble({ sessionId, sessionKey?, messages, tokenBudget?, availableTools?, citationsMode?, model?, prompt? }) → AssembleResult`
- **Lines:** 6648–6832
- **Behavior:** **The always-on substitution.** Every turn, OpenClaw calls `engine.assemble()` BEFORE sending to the LLM. LCM rebuilds the entire message list from the DAG (`contextItems` + summaries) under a token budget, completely replacing what the runtime gave it. Sequence:
  1. Ignored-session / no-conversation → return original messages as `safeFallback()` (strip assistant prefill tails).
  2. Consume any deferred debt that's safe to drain (`maybeConsumeDeferredCompactionDebtForAssemble`) — synchronous, can compact-and-then-assemble in the same turn.
  3. Resolve cache-aware state, pick `stableOrphanStrippingOrdinal` (hot-cache uses last known, cold clears it).
  4. Load context items. If only raw messages and they trail live → fall back to live.
  5. Delegate to `assembler.assemble({ conversationId, tokenBudget, freshTailCount, freshTailMaxTokens, promptAwareEviction, prompt, orphanStrippingOrdinal })`.
  6. Sanity checks: empty result → fallback; no user turn in result → fallback (prevent prefill errors).
  7. Update `previousAssembledMessagesByConversation` snapshot for next-turn prefix-stability diagnostics.
  8. Return `{ messages, estimatedTokens }`.
- **State mutation:** `previousAssembledMessagesByConversation`, `stableOrphanStrippingOrdinalsByConversation`, deferred-debt consumption.
- **Returns:** `{ messages: AgentMessage[], estimatedTokens: number }`.
- **Python mapping:** **This is the hardest port problem.** Hermes's `compress()` is only called when `should_compress()` returns True (overflow). It returns a shorter list. It is NOT called every turn.
  - **Option A — Force `should_compress() → True` every turn**: hijack the existing path so `compress()` runs every turn. Drawback: confuses the existing `compression_count` metric, may interact badly with `should_compress_preflight`.
  - **Option B — New `pre_llm_call` hook**: register `pre_llm_call`; receive `conversation_history`; replace it via an injection mechanism. Drawback: `pre_llm_call` currently injects *additional context* into the user message, not *replaces* the message list. Would need a new return contract (`{"replace_messages": [...]}`) — that's a Hermes ABC change.
  - **Option C — Add an ABC method**: extend `ContextEngine` with `assemble_messages(messages: List[Dict], tokenBudget: int, **kwargs) -> List[Dict]` and a call-site in `run_agent.py` right before the LLM call (line ~12262 area where messages are sealed for the API).
- **Recommended:** **C, pending spike 002.** The cleanest contract; one upstream Hermes patch; matches LCM's mental model exactly. Use `should_compress() → False` for LCM (compaction is independent of assembly).
- **Confidence:** 60% — depends on spike 002 results. Always-on assembly is the load-bearing architectural decision.

### compact(params)

- **Signature:** `compact({ sessionId, sessionKey?, sessionFile, tokenBudget?, currentTokenCount?, compactionTarget?, customInstructions?, runtimeContext?, legacyParams?, force? }) → CompactResult`
- **Lines:** 7185–7243 (public) + 3344–3528 (`executeCompactionCore` private body)
- **Behavior:** Run the configured compaction body under the per-session queue.
  - Resolve `summarize` callable from `runtimeContext.llm` / legacy provider+model (via `createLcmSummarizeFromLegacyParams`).
  - If `compactionTarget === "threshold"` or manual: `compaction.compact()` (sweep mode — one-shot).
  - Else: `compaction.compactUntilUnder()` (convergence loop — repeats summaries until under target).
  - Update circuit breaker on auth failure or success.
  - On success: `markLeafCompactionTelemetrySuccess`, `clearStableOrphanStrippingOrdinal`.
- **State mutation:** writes summaries, demotes raw context_items, updates telemetry, mutates circuit breaker state.
- **Returns:** `{ ok, compacted, reason?, result?: { summary?, tokensBefore, tokensAfter, details: { rounds, targetTokens, mode? } } }`.
- **Python mapping:** **The core of LCM's `compress(messages, current_tokens, focus_topic) → List[Dict]` method.** Translates 1:1 to the Hermes ABC's `compress()`. Pre-check: `should_compress(prompt_tokens) → bool` consults `prompt_tokens >= self.threshold_tokens` AND/OR the engine's own evaluator.
- **What changes:**
  - **DROP**: `sessionFile` parameter — not needed.
  - **DROP**: `runtimeContext.rewriteTranscriptEntries` (the JSONL branch-rewrite helper).
  - **KEEP**: token-budget capping, circuit breaker, `executeCompactionCore` algorithm body.
  - **REDESIGN**: summarizer resolution. Use `agent/llm_client.py` shim instead of `pi-ai`'s `createLcmSummarizeFromLegacyParams`. Pass an `LLMClient` into the engine at construction.
- **Confidence:** 90% — straightforward except for the LLM shim.

### maintain(params)

- **Signature:** `maintain({ sessionId, sessionFile, sessionKey?, runtimeContext? }) → ContextEngineMaintenanceResult`
- **Lines:** 5674–5898
- **Behavior:** OpenClaw fires `maintain()` opportunistically (host-controlled background hook). LCM uses it for two jobs:
  1. **Consume deferred compaction debt** (when `runtimeContext.allowDeferredCompactionExecution === true`) via `consumeDeferredCompactionDebt`, gated by `shouldDelayPromptMutatingDeferredCompaction` (cache-hot delay).
  2. **Transcript GC** (`config.transcriptGcEnabled`): list GC candidates from `summaryStore.listTranscriptGcCandidates`, build `TranscriptRewriteReplacement[]`, call back into the runtime's `runtimeContext.rewriteTranscriptEntries(request)` to mutate the JSONL DAG. Refresh bootstrap state on change.
- **Returns:** `{ changed, bytesFreed, rewrittenEntries, reason? }`.
- **Python mapping:** **No direct equivalent — split into two pieces:**
  - **Deferred-debt consumption** → **background asyncio task** started at engine init. Polls `compactionMaintenanceStore` every N seconds, calls `consumeDeferredCompactionDebt`. Alternatively: fire on `pre_llm_call` (consume any pending debt before the next turn). Pick one, document the trade-off.
  - **Transcript GC** → **DROP** entirely. **[Reason corrected 2026-05-19, issue #137 / review slice S8:** the original reason here — "Hermes has no JSONL transcript to GC" — is **wrong**. Transcript-GC is not JSONL-specific: the sibling `hermes-lcm` runs a transcript-GC that rewrites SQLite rows, and Hermes's `session.db` is rewritable. The drop is still correct, but because `lossless-hermes` has **no inline oversized-tool-result bloat to collect** (LCM storage already keeps large payloads out of the message rows), so a transcript-GC pass would reclaim nothing. The `session.db` also has its own retention.**]**
- **Confidence:** 70% — `maintain` is the messiest method to remap because OpenClaw's host-driven scheduling doesn't have a Hermes equivalent.

### prepareSubagentSpawn(params), onSubagentEnded(params)

- **Signatures:**
  - `prepareSubagentSpawn({ parentSessionKey, childSessionKey, ttlMs? }) → SubagentSpawnPreparation | undefined`
  - `onSubagentEnded({ childSessionKey, reason: "deleted"|"completed"|"swept"|"released" }) → void`
- **Lines:** 7245–7341
- **Behavior:** Manage delegated expansion grants for child agents — let a subagent call `lcm_expand`/`lcm_grep` against the parent's conversation under a token-cap + max-depth restriction. Backed by `expansion-auth.ts` runtime grant manager.
- **State mutation:** in-memory runtime grant manager (out-of-process for engine.ts scope).
- **Returns:** `{ rollback }` or `void`.
- **Python mapping:** **Likely drop or defer.** Hermes does spawn subagents (Task tool), but the LCM expansion-tool surface isn't ported yet. ADR needed: do we re-implement delegated expansion auth in Python, or do we ship LCM-on-Hermes without subagent context sharing?
- **What changes:**
  - **OPTION 1 — DROP**: no subagent context sharing in v1. Document as known limitation.
  - **OPTION 2 — DEFER**: implement after `lcm_expand` ports. Hermes subagents call back to the parent's `LCMEngine` instance (single-process, shared via plugin registry).
- **Confidence:** 50% — depends entirely on whether expansion tools ship in v1.

### dispose()

- **Lines:** 7343–7349
- **Behavior:** No-op — plugin singleton, DB connection shared.
- **Python mapping:** `on_session_end` already exists in ABC and is per-session, not per-engine. The engine-disposal seam doesn't really exist in Hermes; the engine is a process-lifetime singleton.

### Auxiliary public methods (not in OpenClaw `ContextEngine` interface)

These are LCM-specific tools the agent calls or operators use:

- **`evaluateLeafTrigger(sessionId, sessionKey?)`** (6835) — proxies `compaction.evaluateLeafTrigger`. Used by tests/operator tools.
- **`compactLeafAsync(params)`** (7015) — explicit incremental-leaf compaction trigger.
- **`getAgentCompactionGateState(params)`** (7118) — agent-callable read-only gate (used by the `lcm_compact` tool to refuse below-floor compactions without an LLM call).
- **`handleBeforeReset(params)`** (7415) — `/new` or `/reset` lifecycle hook.
- **`handleSessionEnd(params)`** (7468) — generic session-end hook.
- **`autoRotateManagedSessionFilesAtStartup()`** (8084) — startup-time JSONL rotation across all sessions.
- **`rotateSessionStorage*` family** (8327, 8374, 8436) — explicit rotation entry points for operators/tests.

**Python mapping:** keep `getAgentCompactionGateState` and the lcm_compact tool. Map `handleBeforeReset` to `on_session_reset` (already in ABC). **Drop** all `autoRotate*` and `rotateSessionStorage*` — they're JSONL-specific.

---

## Internal collaborators (within engine.ts)

### Per-session async queue pattern (lines 1761–1764, 2038–2084)

```ts
private sessionOperationQueues = new Map<string, { promise: Promise<void>; refCount: number }>();

private async withSessionQueue<T>(queueKey, operation, options?): Promise<T> { ... }
private resolveSessionQueueKey(sessionId?, sessionKey?): string { ... }
```

A FIFO mutex chain per `sessionKey` (or `sessionId` fallback). Every public method that mutates state acquires the queue; nested calls within the same queue acquisition recurse safely. The queue is necessary because OpenClaw can interleave `ingest`/`afterTurn`/`assemble` for the same session, and SQLite write contention plus runtime UUID recycling would otherwise corrupt state.

**Python translation:** `asyncio.Lock` per session_id, lazy-instantiated in a `defaultdict(asyncio.Lock)`. Trickier than the JS version because Python lacks fair FIFO locks out of the box — `asyncio.Lock` provides FIFO fairness as of 3.11. Add a `refCount` analog so the lock is cleaned up when no one is waiting. See `concurrency-model.test.ts` (line 1764 setup) for the invariants the queue must preserve.

### Circuit-breaker logic (lines 1782, 1963–2016)

State per `breakerKey` (provider/model scope): `{ failures: number, openSince: number | null }`. Opens after `circuitBreakerThreshold` consecutive auth failures from the summarizer; cooldown is `circuitBreakerCooldownMs`. While open, compaction is no-op (`reason: "circuit breaker open"`). On any success, reset.

**Python translation:** straightforward port — `dict[str, dict]` with `failures` and `openSince`. Verify via `circuit-breaker.test.ts`.

### Deferred-debt tracking

Three pieces:
1. **`recordDeferredCompactionDebt`** (3004) — writes `compactionMaintenanceStore.requestProactiveCompactionDebt` row (pending=true).
2. **`scheduleDeferredCompactionDebtDrain`** (3022) — `setImmediate` → `drainDeferredCompactionDebtIfIdle`. Idle-only: skip if queue is busy.
3. **`drainDeferredCompactionDebtIfIdle` / `consumeDeferredCompactionDebt`** (3041, 3128) — reload telemetry, check `shouldDelayPromptMutatingDeferredCompaction` gate, execute `executeCompactionCore` (threshold) or `executeLeafCompactionCore` (leaf), mark finished.
4. **`maybeConsumeDeferredCompactionDebtForAssemble`** (3266) — same path, but synchronous during assemble. Bypasses cache-hot delay when at critical pressure (prompt overflow emergency).

The DB row is the durable backstop — if the process crashes before the drain runs, the next `assemble()` or `maintain()` picks it up.

**Python translation:** keep the table and all four methods. Replace `setImmediate` with `asyncio.create_task` (background) and a periodic poll task as the "maintain" replacement. The critical-pressure synchronous-drain path is essential — without it, the cache-hot deferred-drain race lets context overflow into Hermes's emergency truncation.

### Auto-rotate managed session files

Lines 7505–8194 + 8287–8616. **All of this drops for Hermes.** It is entirely about managing the JSONL session file lifecycle.

### `evaluateIncrementalCompaction` (2824–3002)

Decides whether to run incremental leaf compaction this turn, and with what parameters. Inputs: telemetry (cache state, retention, observations), dynamic leaf-chunk bounds (token-budget-derived), activity band classification, leaf-trigger evaluator from compaction. Outputs `IncrementalCompactionDecision { shouldCompact, cacheState, maxPasses, rawTokensOutsideTail, threshold, reason, leafChunkTokens, fallbackLeafChunkTokens, activityBand, allowCondensedPasses }`. The reason strings are diagnostic ("below-leaf-trigger", "budget-trigger", "hot-cache-budget-headroom", "hot-cache-defer", "cold-cache-catchup", "leaf-trigger").

**Python translation:** port verbatim, it's pure decision logic. ~180 lines of state machine.

### Media interception pipeline

Lines 3673–4612 (image normalization, externalization, large file/payload interception). Big surface area but mostly leaf utility functions. Each `intercept*` method takes a message, mutates content to replace inline base64/large strings with file references, and writes the actual blob to `<largeFilesDir>/<conversationId>/<uuid>.<ext>`.

**Python translation:** port to `lossless_hermes/media.py` as a separate module. Keep the same DB schema for `message_parts` so externalized references survive round-trip.

---

## Integration seams (cross-module)

### engine.ts → conversation-store

Heavy use. Every ingest, bootstrap, dedup, and read path. Methods called: `getOrCreateConversation`, `getMessageCount`, `getConversationForSession`, `getConversationBySessionKey`, `getLastMessage`, `getMaxSeq`, `createMessage`, `createMessageParts`, `archiveConversation`, `createConversation`, `markConversationBootstrapped`, `withTransaction`.

### engine.ts → summary-store

For DAG state + context-items + bootstrap-state + summaries. Methods: `appendContextMessage`, `getContextItems`, `getContextTokenCount`, `getConversationBootstrapState`, `pruneForNewSession`, `listTranscriptGcCandidates`.

### engine.ts → compaction-maintenance-store

Deferred-debt persistence. Methods: `requestProactiveCompactionDebt`, `getConversationCompactionMaintenance`, `markProactiveCompactionRunning`, `markProactiveCompactionFinished`.

### engine.ts → compaction-telemetry-store

Cache-aware decision inputs. Methods: `getConversationCompactionTelemetry` (read), and an update call inside `updateCompactionTelemetry` (line 2663 area).

### engine.ts → compaction.ts

Heavy. `compaction.evaluate`, `compaction.evaluateLeafTrigger`, `compaction.compact`, `compaction.compactUntilUnder`, `compaction.compactLeaf`.

### engine.ts → assembler.ts

`assembler.assemble({ conversationId, tokenBudget, freshTailCount, freshTailMaxTokens, promptAwareEviction, prompt, orphanStrippingOrdinal })`. Returns `AssembleResult` with the assembled messages + debug snapshot.

### engine.ts → summarize.ts (createLcmSummarizeFromLegacyParams + LcmProviderAuthError)

The summarizer factory. Builds a `summarize: (text, aggressive?) => Promise<string>` callable from legacyParams (provider+model+credentials). Wraps `pi-ai` model resolution and auth.

**Python replacement:** `agent/llm_client.py` shim. Engine constructor accepts an injected `summarizer: Callable[[str, bool], Awaitable[str]]` so tests can use a stub.

### engine.ts → @mariozechner/pi-ai (dynamic import inside summarize.ts)

Provider-pluggable LLM client. Used only to produce the `summarize` callable above.

**Python replacement:** Hermes's own model layer (`agent/plugin_llm.py` / `agent/llm_client.py`). The summarizer is a thin wrapper around `client.complete(messages=[{"role":"user","content":prompt}], model=...)`.

### engine.ts → @mariozechner/pi-coding-agent (SessionManager)

Used only at lines 430 (`listTranscriptToolResultEntryIdsByCallId`) and 8198 (rotate-transcript rewrite). Both are JSONL-specific.

**Python replacement:** DROP. No JSONL.

### engine.ts → expansion-auth.ts

For subagent grants. Calls: `createDelegatedExpansionGrant`, `getRuntimeExpansionAuthManager`, `removeDelegatedExpansionGrantForSession`, `resolveDelegatedExpansionGrantId`, `revokeDelegatedExpansionGrantForSession`.

**Python replacement:** defer (see prepareSubagentSpawn ADR).

### engine.ts → large-files.ts

Helper formatters for image/file references — `extensionFromNameOrMime`, `formatFileReference`, `formatRawPayloadReference`, `formatToolOutputReference`, `generateExplorationSummary`, `parseFileBlocks`.

**Python replacement:** port to `lossless_hermes/large_files.py`.

### engine.ts → transaction-mutex.ts

`withExclusiveDatabaseLock`, `DatabaseTransactionTimeoutError` for compactionwide DB-lock acquisition (used in startup rotation).

**Python replacement:** straightforward — `aiosqlite` connection + an `asyncio.Lock` around `BEGIN EXCLUSIVE`. Most callers can drop the EXCLUSIVE lock if we serialize through the per-session queue.

### engine.ts → plugin/lcm-db-backup.ts

`createLcmDatabaseBackup` for pre-rotation safety.

**Python replacement:** keep as `lossless_hermes/plugin/db_backup.py`.

---

## Python class skeleton

```python
from agent.context_engine import ContextEngine
from typing import Awaitable, Callable, Dict, List, Optional, Any
from collections import defaultdict
import asyncio
import logging

logger = logging.getLogger("lcm.engine")


class LCMEngine(ContextEngine):
    """Lossless Context Management engine for Hermes."""

    name = "lcm"
    threshold_percent = 0.75    # default; configurable via config.yaml
    protect_first_n = 3
    protect_last_n = 8

    def __init__(
        self,
        hermes_home: str,
        config: Optional[dict] = None,
        summarizer: Optional[Callable[[str, bool], Awaitable[str]]] = None,
    ) -> None:
        self.config = LcmConfig.from_dict(config or {})
        self.db = open_lcm_db(hermes_home, self.config)
        run_lcm_migrations(self.db, log=logger)

        self.conversation_store = ConversationStore(self.db)
        self.summary_store = SummaryStore(self.db)
        self.compaction_telemetry_store = CompactionTelemetryStore(self.db)
        self.compaction_maintenance_store = CompactionMaintenanceStore(self.db)

        self.assembler = ContextAssembler(self.conversation_store, self.summary_store, self.config)
        self.compaction = CompactionEngine(self.conversation_store, self.summary_store, self.config, summarizer)
        self.retrieval = RetrievalEngine(self.conversation_store, self.summary_store)

        # State
        self._session_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last_seen_message_idx: dict[str, int] = {}
        self._circuit_breaker_states: dict[str, dict] = {}
        self._previous_assembled_messages_by_conversation: dict[int, AssemblePrefixSnapshot] = {}
        self._stable_orphan_stripping_ordinals: dict[int, int] = {}
        self._cache_context_unknown_logged: set[int] = set()

        self._summarizer = summarizer  # injected for testability
        self._background_drain_task: Optional[asyncio.Task] = None

    # --- ABC required ---

    def update_from_response(self, usage: Dict[str, Any]) -> None:
        prompt_tokens = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
        completion_tokens = usage.get("completion_tokens") or usage.get("output_tokens") or 0
        self.last_prompt_tokens = prompt_tokens
        self.last_completion_tokens = completion_tokens
        self.last_total_tokens = prompt_tokens + completion_tokens
        # Optional: record into compaction_telemetry_store for cache-aware state

    def should_compress(self, prompt_tokens: int = None) -> bool:
        # LCM does compaction via post_llm_call hook + background drain.
        # Hermes's `compress()` here is the OVERFLOW-RECOVERY entry point.
        observed = prompt_tokens or self.last_prompt_tokens
        return observed >= self.threshold_tokens

    def compress(
        self,
        messages: List[Dict[str, Any]],
        current_tokens: int = None,
        focus_topic: str = None,
    ) -> List[Dict[str, Any]]:
        # Synchronous wrapper. Maps to engine.compact() with target="budget".
        # Returns the assembled (compacted) message list.
        return asyncio.run(self._compress_async(messages, current_tokens, focus_topic))

    async def _compress_async(self, messages, current_tokens, focus_topic):
        session_id = self._infer_session_id_from_messages(messages)  # or pass explicitly
        async with self._session_locks[session_id]:
            await self._execute_compaction_core(
                session_id=session_id,
                token_budget=self.context_length,
                current_token_count=current_tokens,
                compaction_target="budget",
                custom_instructions=focus_topic,
            )
            return await self._assemble(session_id=session_id, messages=messages,
                                        token_budget=self.context_length, prompt=focus_topic)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [LCM_COMPACT_TOOL_SCHEMA, LCM_GREP_TOOL_SCHEMA, ...]  # see retrieval port

    def handle_tool_call(self, name: str, args: Dict[str, Any], **kwargs) -> str:
        if name == "lcm_compact":
            return asyncio.run(self._handle_lcm_compact(args, **kwargs))
        # ... lcm_grep, lcm_describe, lcm_expand
        return super().handle_tool_call(name, args, **kwargs)

    # --- Hook handlers (registered via PluginContext.register_hook in __init__.py) ---

    async def _on_post_llm_call(
        self,
        session_id: str,
        user_message: str,
        assistant_response: str,
        conversation_history: List[Dict],
        model: str,
        platform: str,
        **kwargs,
    ) -> None:
        """Replaces afterTurn(). Ingest + decide + compact-or-defer."""
        async with self._session_locks[session_id]:
            new_messages = self._diff_since_last_seen(session_id, conversation_history)
            ingested = await self._ingest_batch(session_id, new_messages)

            # Compaction decision (port of afterTurn() lines 6473–6638)
            telemetry = await self.compaction_telemetry_store.get(conversation_id)
            leaf_decision = await self._evaluate_incremental_compaction(...)
            threshold_decision = await self.compaction.evaluate(...)

            if leaf_decision.should_compact or threshold_decision.should_compact:
                if self._is_critical_pressure(...):
                    # Synchronous drain
                    await self._consume_deferred_debt(...)
                else:
                    await self._record_deferred_debt(...)
                    self._schedule_background_drain()

    async def _on_pre_llm_call(self, session_id, conversation_history, **kwargs):
        """Optional: consume deferred debt before the next turn fires.
        Alternative to a background poll task.
        """
        async with self._session_locks[session_id]:
            await self._consume_deferred_debt_if_pending(session_id)

    # --- Internal ---

    async def _ingest_single(self, session_id, message, is_heartbeat=False) -> bool: ...
    async def _ingest_batch(self, session_id, messages, is_heartbeat=False) -> int: ...
    async def _execute_compaction_core(self, **params) -> CompactResult: ...
    async def _evaluate_incremental_compaction(self, **params) -> IncrementalCompactionDecision: ...
    async def _consume_deferred_debt(self, **params) -> None: ...
    async def _record_deferred_debt(self, **params) -> None: ...
    async def _drain_deferred_debt_if_idle(self, session_id) -> None: ...
    async def _assemble(self, session_id, messages, token_budget, prompt=None) -> List[Dict]: ...

    def on_session_start(self, session_id: str, **kwargs) -> None: ...
    def on_session_end(self, session_id: str, messages: List[Dict]) -> None: ...
    def on_session_reset(self) -> None:
        super().on_session_reset()
        # Apply LCM lifecycle (archive conversation, optionally create replacement)
```

---

## Port order within this file

1. **State + lifecycle skeleton** (no-op `compress`) — class, stores, locks, on_session_start/end/reset. Smoke test: engine instantiates, plugin loads.
2. **Token tracking** (`update_from_response` → `last_prompt_tokens`, `threshold_tokens`, `last_total_tokens`).
3. **`should_compress` logic** — read `threshold_tokens` + observed prompt tokens. Hermes preflight path works.
4. **`_on_post_llm_call` ingest hook** — diff new messages, ingest each via `_ingest_single` and `_ingest_batch`. No compaction yet, just persistence. Tests: `bootstrap-message-only.test.ts`, `bootstrap-flood-regression.test.ts` analogs.
5. **`compress()` body** — port `executeCompactionCore` algorithm; delegates to `compaction.compactUntilUnder` for budget target.
6. **Assemble bridge** — implement `_assemble()` (delegates to assembler), wire to `compress()` return value. Confirm spike 002 outcome before deciding pre_llm_call vs ABC patch.
7. **Cache-aware deferral + deferred-debt drain** — port `evaluateIncrementalCompaction`, `recordDeferredCompactionDebt`, `consumeDeferredCompactionDebt`, `drainDeferredCompactionDebtIfIdle`. Wire to a background `asyncio.Task` or `_on_pre_llm_call` hook.
8. **Circuit breaker** — port `getCircuitBreakerState`, `recordCompactionAuthFailure`, `recordCompactionSuccess`, `isCircuitBreakerOpen`. Test: `circuit-breaker.test.ts`.
9. **Media interception** — port `interceptInlineImages`, `interceptLargeFiles`, etc. Last because it's the broadest leaf surface and least decision-critical.
10. **`get_tool_schemas` + `handle_tool_call`** — wire `lcm_compact` and `lcm_grep` (depends on retrieval port — separate agent).
11. **Subagent hooks** — only if expansion tools ship in v1; otherwise defer entirely.

---

## Open architecture decisions

- **ADR-04: Per-message ingest mechanism (A/B/C/D from above)** → recommend **B (post_llm_call)** with **C (handle_tool_call kwargs)** as fallback for pure-tool-call turns. Spike 001 (one-day): instrument both hooks for one session, confirm 100% message coverage with B alone.

- **ADR-05: Always-on assembly emulation (Option A/B/C from `assemble` section)** → recommend **C (extend ContextEngine ABC with `assemble_messages`)**, pending spike 002. Spike 002 (one-day): try Option A (force `should_compress=True` every turn) and measure overhead — if negligible and no metric pollution, A wins.

- **ADR-06: Deferred-debt drain mechanism** → recommend **background asyncio task** polling every 30s + opportunistic sync drain on `pre_llm_call`. Alternative: drop the async drain and only drain on assemble/pre_llm_call. Decide after the debt-table port lands.

- **ADR-07: Subagent prepareSubagentSpawn** → recommend **DROP for v1**. Hermes subagents (Task tool) cannot share LCM context across processes without significant work; expansion tools may not ship in v1 anyway.

- **ADR-08: Per-session async queue** → `asyncio.Lock` per session_id from `defaultdict(asyncio.Lock)`. Add a refcount + cleanup pass to avoid the dict growing without bound. Verify FIFO fairness on Python 3.11+ (3.10 lacks the guarantee).

- **ADR-09: Bootstrap / backfill story** → recommend **separate CLI command** (`hermes lcm backfill --session <id>`) rather than opportunistic `on_session_start` backfill. Keeps the hot path fast and makes the import an explicit operator action.

---

## Test inventory

`engine.test.ts` (11922 LOC, single file) is the primary suite — exercises bootstrap, ingest, ingestBatch, afterTurn, assemble, compact, maintain, subagent hooks, session-end/reset, auto-rotate, deferred-debt, and the dedup/reconcile fast-paths.

Other test files that directly exercise engine.ts behavior:

- `bootstrap-flood-regression.test.ts`
- `bootstrap-message-only.test.ts`
- `cache-aware-deferral-gate.test.ts`
- `circuit-breaker.test.ts`
- `compaction-maintenance-store.test.ts`
- `concurrency-model.test.ts` (per-session queue invariants)
- `session-operation-queues.test.ts`
- `v41-needs-compact-gate.test.ts`
- `v41-lcm-compact-tool.test.ts`
- `v41-concurrency-invariants.test.ts`
- `v41-cross-module-invariants.test.ts`
- `v41-adversarial-scenarios.test.ts`
- `transcript-repair.test.ts` (bootstrap reconcile path)
- `lcm-worker-lock.test.ts` (worker mutex)
- `plugin-config-registration.test.ts`
- `regression-2026-03-17.test.ts`

Tests we **drop** in the Python port (JSONL-specific): bootstrap fast-path tests, transcript-repair, transcript-GC inside maintain, auto-rotate scenarios. Keep their **invariants** as adapted DB-only tests (e.g., "afterTurn ingest is idempotent on replay" survives without the JSONL replay shape).

---

## Remaining 5% risk

1. **Always-on assembly semantics with prompt-cache reuse.** Hermes's prompt-caching design assumes the system prompt + tools are stable across turns. If LCM rewrites the message list every turn, the cache prefix is the system-prompt-only portion — still cached, but the user/assistant turn delta is now LCM-managed. Spike 002 must measure cache-hit rate under always-on assembly on a real Hermes session before we commit to the contract.

2. **Background drain task lifecycle.** `asyncio.create_task` in `__init__` requires an event loop already running — that's fine inside the engine's own coroutines but tricky in the synchronous `compress()` ABC method. Need to verify Hermes always runs the engine inside an asyncio loop, or add a sync→async bridge.

3. **`session.db` ↔ LCM DB cross-process consistency.** Hermes writes to `session.db` for its own session persistence; LCM writes to `lcm.db`. Both share the same session_id. If the two DBs disagree (e.g. a crash mid-write), assemble may include a message Hermes lost or vice versa. Not strictly an engine.ts concern (it's a DB-layout concern), but the porting story can't ignore it.

4. **OpenClaw `runtimeContext.rewriteTranscriptEntries` and `runtimeContext.llm` callbacks.** These are host-injected capabilities passed every turn. Hermes has no equivalent injection — `llm` is solved by the constructor-injected summarizer, but `rewriteTranscriptEntries` is JSONL-only and drops entirely. Verify no other engine method secretly depends on `runtimeContext` (a quick grep for `runtimeContext` in engine.ts shows ~30 hits, mostly within already-mapped methods).

5. **Heartbeat / synthetic-turn semantics.** LCM has explicit handling for `HEARTBEAT_OK` ack turns (prune them post-ingest). Whether Hermes has equivalent synthetic turns is unknown — need to grep `run_agent.py` and `gateway/` for heartbeat-like patterns. If yes, port the pruner; if no, drop it entirely.
