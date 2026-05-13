# Porting Guide: Assembler + Compaction

**Source LOC:** ~5076 (assembler 1469 + compaction 1831 + summarize 1696 + estimate-tokens 80)
**Python target LOC:** ~5000 (Python is typically slightly more terse for control flow but slightly more verbose for the same dataclasses; net wash)
**Confidence target:** 95%
**Estimated effort:** 60–80 hours
**Epic:** 03-ingest-assembly + 04-compaction

> **Source branches surveyed.** Assembler/compaction/summarize/estimate-tokens were read from the `pr-613` branch HEAD (`1f07fbd`, "Wave-12 retro A1 — promote toolResultTokenBudget to LcmConfig field"). The **#628 stub-tier** code is NOT on `pr-613`; it lives on `main` only, merged as commit `13780e9` ("feat(v4.2): stub-tier stratification — externalize old tool results"). The stub-tier section of this guide reads from `main` (`13780e9 src/assembler.ts`). The Hermes target was read from `agent/context_compressor.py`, `agent/context_engine.py`, and `agent/auxiliary_client.py`.

## Files in scope

| TS file | LOC | Python target |
|---|---:|---|
| `src/assembler.ts` (pr-613) | 1469 | `src/lossless_hermes/assembler.py` |
| `src/assembler.ts` (main, with #628) | 1603 | (additional ~134 LOC for stub-tier) |
| `src/compaction.ts` (pr-613) | 1831 | `src/lossless_hermes/compaction.py` |
| `src/summarize.ts` (pr-613) | 1696 | `src/lossless_hermes/summarize.py` |
| `src/estimate-tokens.ts` (pr-613) | 80 | `src/lossless_hermes/estimate_tokens.py` |

External LCM modules referenced (NOT in this guide's scope but on the import path):
- `./store/conversation-store.js` — `ConversationStore`, `MessageRecord`, `MessagePartRecord`, `MessageRole`, `CreateMessagePartInput`
- `./store/summary-store.js` — `SummaryStore`, `ContextItemRecord`, `SummaryRecord`
- `./transcript-repair.js` — `sanitizeToolUseResultPairing` (final repair pass)
- `./large-files.js` — `extractFileIdsFromContent`, `formatToolOutputReference` (stub-tier)
- `./openclaw-bridge.js` — `ContextEngine` (only used for an `AgentMessage` type alias)
- `./lcm-log.js` — `LcmLogger`, `NOOP_LCM_LOGGER`, `describeLogError`
- `./types.js` — `LcmDependencies` (the injected-deps bag — see "summarize seam" below)

---

## Assembler — full algorithm walkthrough

**File:** `src/assembler.ts` (line numbers from pr-613).

The assembler is the **hot path**. Every turn calls `ContextAssembler.assemble({conversationId, tokenBudget, ...})` and gets back a list of `AgentMessage` ready to feed the provider. It does *not* call the LLM; it only selects, normalizes, sanitizes.

### Public surface (lines 128–177)

```ts
interface AssembleContextInput {
  conversationId: number;
  tokenBudget: number;
  freshTailCount?: number;          // default 8
  freshTailMaxTokens?: number;       // optional token cap; newest is always kept
  prompt?: string;                   // user query for BM25-lite eviction
  promptAwareEviction?: boolean;     // default true; false forces chronological
  orphanStrippingOrdinal?: number;   // stable hot-cache epoch boundary
  stubLargeToolPayloads?: boolean;   // v4.2 §B (#628 on main only)
}
interface AssembleContextResult {
  messages: AgentMessage[];
  estimatedTokens: number;
  stats: { rawMessageCount; summaryCount; totalContextItems };
  debug?: { ... ~16 fields incl. overflowDiagnostics ... };
}
```

### Step-by-step (from `assemble`, lines 1102–1332)

1. **Read context items** (1107): `summaryStore.getContextItems(conversationId)` returns ordered `ContextItemRecord[]` (each item is either `itemType: "message"` with `messageId`, or `itemType: "summary"` with `summaryId`).
2. **Resolve** (1118): `resolveItems()` → `ResolvedItem[]`. Each resolved item carries: `ordinal`, `message: AgentMessage`, `tokens` (via `estimateTokens` on the serialized content), `isMessage` flag, plain `text` (used for BM25 scoring), plus DB metadata (`messageId`, `seq`, `sourceRole`, or `summary` record). See `resolveMessageItem` (1374) and `resolveSummaryItem` (1450).
   - Tool-result without `toolCallId` is **degraded to assistant** (line 1399) — Anthropic-compatible APIs reject tool_result blocks missing the call id. This preserves text instead of dropping it.
   - Summary items are formatted as an XML wrapper (`formatSummaryContent`, line 814): `<summary id="..." kind="..." depth="..." ...><content>...</content></summary>` plus `<parents>` for `kind: "condensed"`.
3. **Compute fresh-tail boundary ordinal** (`resolveFreshTailOrdinal`, lines 983–1032 + call at 1132). Walks raw-message items from newest to oldest, protecting up to `freshTailCount` items, stopping early if `freshTailMaxTokens` would be exceeded (but always preserves at least the newest message).
4. **Compute orphan-stripping ordinal** (1137): defaults to `freshTailOrdinal`; callers can override with a stable boundary so cache-prefix hash stays consistent across same-prefix turns.
5. **Index all tool-result ordinals** (1143–1155): build `allToolResultOrdinalsById: Map<toolCallId, ordinal[]>` over the *resolved* set (before selection). Used later in orphan stripping.
6. **Split** (1156–1158): `evictable = resolved.filter(ordinal < freshTailOrdinal)`, `freshTail = resolved.filter(ordinal >= freshTailOrdinal)`.
7. **(v4.2 §B / #628 stub-tier)** if `stubLargeToolPayloads`, call `applyStubSubstitution(evictable)` **before** the budget walk. Documented in detail below.
8. **Token-budget walk** (1162–1230):
   - `tailTokens = sum(freshTail.tokens)`. Tail is always included, even if it alone exceeds budget.
   - `remainingBudget = max(0, tokenBudget - tailTokens)`.
   - **Three selection modes** (the `selectionMode` debug field):
     - **`full-fit`** (1181–1184) — `evictableTotalTokens <= remainingBudget`. Keep everything.
     - **`prompt-aware`** (1185–1209) — `promptAwareEviction !== false && hasSearchablePrompt(prompt)`. Score every evictable item by `scoreRelevance(item.text, prompt)` (BM25-lite, lines 1049–1075: TF normalized by item-term-count, one accumulator per *unique* prompt term, ties broken by recency). Sort by score desc + recency desc. Greedy-fill remaining budget. Re-sort kept items by `ordinal` before appending.
     - **`chronological`** (1210–1230) — default fallback. Walk evictable from *newest* index downward; once an item doesn't fit, stop (drops all older too). Reverse for chronological order.
9. **Append fresh tail** (1233): `selected = [...evictableKept, ...freshTail]`.
10. **Build overflow diagnostics** (1236, helper at 956–981): tokens-per-item-kind, duplicate clusters (`buildRefDuplicateClusters` by messageId/summaryId; `buildMessageContentDuplicateClusters` by SHA-256 of `item.text`), top-5 token contributors per kind.
11. **`filterNonFreshAssistantToolCalls`** (lines 687–778, called 1244). For each assistant message:
    - Index *selected* tool_result ordinals by id.
    - For each `toolCall`/`tool_use`/`functionCall` block in assistant content, check whether some selected tool_result with this id appears at an ordinal **strictly greater** than the assistant message.
    - If yes → keep. If no AND `item.ordinal < orphanStrippingOrdinal` → strip. If no AND the id has *any* resolved tool_result somewhere (`allToolResultOrdinalsById`) → keep (don't strip cache-marginal turns). Else → strip.
    - If all blocks stripped, drop the whole assistant message.
12. **Normalize assistant content** (1250–1274): string-content → `[{type:"text", text}]`. Then filter blank text blocks out.
13. **Clean empty assistant turns** (1283–1293): drop any assistant whose content is empty, blank-only, or thinking-only (`isThinkingOnlyContent`, line 97). Bedrock/Anthropic reject these.
14. **Pre-sanitize hashing** (1295–1300): SHA-256 hash of evictable, freshTail, and combined `cleaned` messages — for cache-stability debugging.
15. **`sanitizeToolUseResultPairing(cleaned)`** (1301): imported from `transcript-repair.ts`. Final repair pass: pairs orphaned tool_result with synthesized tool_use blocks, drops still-unpaired turns. Result is the final `messages`.
16. **Return** with `estimatedTokens = evictableTokens + tailTokens`, plus `debug` containing every intermediate hash, ordinal, count, and the overflow diagnostics.

### Supporting helpers (assembler.ts)

| Helper | Lines | What it does |
|---|---:|---|
| `parseJson` | 188–197 | Tolerant `JSON.parse`, returns `undefined` on failure |
| `getOriginalRole` / `getPartMetadata` | 199–239 | Decode `message_parts.metadata` JSON for `originalRole`, `rawType`, `raw` |
| `tryRestoreOpenAIReasoning` | 265–278 | Reverse OpenClaw's normalization back to OpenAI Responses API `{type:"reasoning", id:"rs_…"}` |
| `toolCallBlockFromPart` | 281–328 | Reconstruct tool_use/toolCall/function_call block (handles per-provider keying: `arguments` vs `input`; ensures `id` is always set) |
| `toolResultBlockFromPart` | 331–390 | Reconstruct tool_result/function_call_output block; preserves `is_error`/`isError` |
| `toRuntimeRole` | 392–418 | Map DB role + parts metadata → runtime role (`user`/`assistant`/`toolResult`); special-cases `tool` and `system` |
| `blockFromPart` | 421–535 | The big switch: reasoning / tool / tool-result / text / metadata-raw fallback |
| `contentFromParts` | 538–565 | Map parts → content array; single-text-user-block → plain string for OpenAI Chat shape |
| `pickToolCallId` / `pickToolName` / `pickToolIsError` | 568–640 | Scan parts metadata for tool-call identity |
| `formatSummaryContent` | 814–852 | Render `<summary ...>` XML wrapper |
| `topContributors` / `buildRefDuplicateClusters` / `buildMessageContentDuplicateClusters` | 877–954 | Overflow diagnostics |
| `tokenizeText` | 1037–1042 | `lowercase → split /[^a-z0-9]+/ → filter len>1` |
| `scoreRelevance` | 1049–1075 | BM25-lite: TF / item-term-count, one accumulator per unique prompt term |

---

## #628 stub-tier substitution — detailed algorithm

> **Where it lives.** The stub-tier code is on `main` (commit `13780e9`), NOT on `pr-613`. The Python port should include this from day one — the README explicitly names it as one of the two source PRs.

### What it does

When a `messages.large_content` sidecar column points at an externalized `file_xxx` payload (i.e. the tool-result was previously streamed to disk via the large-files subsystem), the assembler **replaces the evictable copy with a compact `[LCM Tool Output: file_xxx | tool=... | N bytes]` stub** before the budget walk runs. The fresh-tail copy is never stubbed — agents always see the full latest result.

The stub is the same `formatToolOutputReference()` text the v4.1 retrieval path already emitted, so agents see a known shape and a known drilldown command (`lcm_describe(id="file_xxx", expandFile=true)`).

### Wire-up in `resolveMessageItem` (main, lines ~1512–1573)

```ts
// Inside resolveMessageItem, after computing role/content/tokens:
const fileIdFromSidecar =
  typeof msg.largeContent === "string" && msg.largeContent.startsWith("file_")
    ? msg.largeContent
    : null;
let fileMeta: { byteSize: number; summary?: string } | null = null;
if (fileIdFromSidecar) {
  const fileRow = await this.summaryStore.getLargeFile(fileIdFromSidecar);
  if (fileRow) {
    fileMeta = { byteSize: fileRow.byteSize ?? 0, summary: fileRow.explorationSummary ?? undefined };
  }
}
const stubEligible = fileIdFromSidecar != null && fileMeta != null && role === "toolResult";

return {
  ordinal, message, tokens, isMessage: true, text, messageId, seq, sourceRole,
  ...(stubEligible && fileIdFromSidecar ? { fileId: fileIdFromSidecar } : {}),
  ...(stubEligible && fileMeta       ? { fileByteSize: fileMeta.byteSize } : {}),
  ...(stubEligible && fileMeta?.summary ? { fileSummary: fileMeta.summary } : {}),
  ...(stubEligible && toolName       ? { stubToolName: toolName } : {}),
  ...(stubEligible && toolCallId     ? { stubToolCallId: toolCallId } : {}),
};
```

Eligibility requires **all** of: sidecar set, `large_files` row found, role is `toolResult` (NOT the legacy-row-downgraded `assistant` path). If any condition fails, the item is resolved normally and never stubbed.

### `applyStubSubstitution` (main, lines ~836–870)

Pseudocode (port directly):

```python
def apply_stub_substitution(evictable: list[ResolvedItem]) -> dict:
    stubbed_count = 0
    tokens_saved = 0
    for item in evictable:
        if item.file_id is None:        continue   # not externalized
        if item.message_id is None:     continue   # defense-in-depth
        if item.message["role"] != "toolResult":   continue  # role must be toolResult, not downgraded-assistant

        stub_text = format_tool_output_reference(
            file_id=item.file_id,
            tool_name=item.stub_tool_name,           # may be None → "unknown"
            byte_size=item.file_byte_size or 0,
            summary=item.file_summary or "",
        )
        new_tokens = estimate_tokens(stub_text)
        old_tokens = item.tokens

        was_array = isinstance(item.message["content"], list)
        new_content = [{"type": "text", "text": stub_text}] if was_array else stub_text

        item.message = {**item.message, "content": new_content}
        item.tokens  = new_tokens
        item.text    = stub_text                    # so duplicate-cluster + BM25 see the stub
        stubbed_count += 1
        tokens_saved += max(0, old_tokens - new_tokens)

    return {"stubbed_count": stubbed_count, "tokens_saved": tokens_saved}
```

**Critical invariants from the PR's adversarial-review notes** (preserve when porting):
- **Multi-block tool_result content stays an array** (P1 fix). If the original was `[{type, ...}, {type, ...}]`, the stub becomes a 1-element text-block array, NOT a plain string — Anthropic requires the array shape.
- **Legacy-row-downgraded `assistant` items are NEVER stubbed.** A row with DB role `tool` but no `toolCallId` was downgraded to `assistant` by `resolveMessageItem`. There's no upstream `tool_use` to pair the stub with, so emitting a `[LCM Tool Output: …]` reference would create a phantom drilldown.
- **Fresh tail is never touched.** The substitution runs over `evictable` only (step 7 in the walkthrough). The newest copy of the tool result is always full-fat.

### `formatToolOutputReference` shape (main, `src/large-files.ts` ~527)

```
[LCM Tool Output: file_abc123 | tool=read_file | 12,345 bytes]

Exploration Summary:
<summary text or "(no summary available)">

Call lcm_describe(id="<file_id above>", expandFile=true) to fetch the full output content from disk.
```

The byte count uses `Number.prototype.toLocaleString("en-US")` for the thousands separator — port as `f"{byte_size:,}"` in Python.

### Telemetry

Returned in `debug.stubStats: { stubbedCount, tokensSaved }`. Add to Python `AssembleDebug` dataclass.

---

## Compaction — full algorithm walkthrough

**File:** `src/compaction.ts` (line numbers from pr-613).

### Public surface (lines 11–61)

```ts
interface CompactionDecision { shouldCompact; reason; currentTokens; threshold }
interface CompactionResult   { actionTaken; tokensBefore; tokensAfter; createdSummaryId?; condensed; level?; authFailure? }
interface CompactionConfig {
  contextThreshold;          // default 0.75
  freshTailCount;            // default 8
  freshTailMaxTokens?;
  leafMinFanout;             // default 8
  condensedMinFanout;        // default 4
  condensedMinFanoutHard;    // default 2 (hard-trigger sweeps relax fanout)
  incrementalMaxDepth;       // default 1 (passes after each leaf compaction)
  leafChunkTokens?;          // default 20_000
  leafTargetTokens;          // default 600 (config); 4000 in summarize.ts default
  condensedTargetTokens;     // default 900 (config); 2000 in summarize.ts default
  maxRounds;                 // default 10
  timezone?;                 // IANA tz for timestamps in summaries
  summaryMaxOverageFactor;   // default 3
}

type CompactionLevel = "normal" | "aggressive" | "fallback" | "capped";
```

### Trigger evaluation

Two distinct triggers:

1. **`evaluate(conversationId, tokenBudget, observedTokenCount?)`** (lines 408–438) — context-level. `currentTokens = max(storedTokens, liveTokens)` vs `threshold = floor(contextThreshold * tokenBudget)`. Returns `{shouldCompact: currentTokens > threshold, reason: "threshold"|"none"}`.
2. **`evaluateLeafTrigger(conversationId, leafChunkTokensOverride?)`** (lines 447–459) — soft incremental trigger. Sums raw-message tokens *outside* the fresh tail; if `>= leafChunkTokens` (default 20k), recommend a single leaf pass. Lets callers run incremental maintenance before the global threshold trips.

### Entry points

| Method | Lines | Use case |
|---|---:|---|
| `compact({...})` | 464–474 | Wraps `compactFullSweep` in `withContextCache` (per-conversation cache) |
| `compactLeaf({...})` | 481–492 | Soft-trigger path — one leaf pass + optional condensation passes |
| `compactFullSweep({...})` | 626–774 | Hard-trigger sweep: phase-1 leaves, then phase-2 condensed |
| `compactUntilUnder({...})` | 779–867 | Overflow-recovery loop; up to `maxRounds` |

### Leaf-pass algorithm (`leafPass`, lines 1492–1607)

Goal: collapse one chunk of contiguous raw messages outside the fresh tail into one **leaf summary**.

1. **Select chunk** — `selectOldestLeafChunk` (1005–1057):
   - Walk context items oldest → newest, skipping non-message items until the first raw message.
   - Once started, stop on any non-message item OR when adding the next message would push chunk tokens over `leafChunkTokens` (always include at least one message).
   - Stop AT (don't include) any item with `ordinal >= freshTailOrdinal`.
2. **Resolve prior leaf summary context** (1065–1104) — last up-to-2 summary items before the chunk's start ordinal, joined by `\n\n`. Becomes `options.previousSummary` for the summarizer (iterative continuity).
3. **Fetch full messages** for each chunk item (1500–1519). Annotate media: `annotateMediaContent` (1457–1485) replaces media-only messages with `"[Media attachment]"`; media-mostly messages keep their text + `" [with media attachment]"` suffix.
4. **Concatenate** (1521–1531) — each message becomes `[YYYY-MM-DD HH:mm TZ]\n<text>` (`formatTimestamp`, 125–150). Reasoning/thinking blocks are stripped via `extractMeaningfulMessageText` (313–331). Empty messages filtered.
5. **Extract file ids** (1532–1534) for the summary's `file_ids` index — `extractFileIdsFromContent` from `large-files.ts`.
6. **Summarize** via `summarizeWithEscalation` (1334–1443) with `targetTokens = config.leafTargetTokens`, `options.isCondensed = false`.
7. **On success**, persist atomically in a transaction (1565–1603):
   - `insertSummary({summaryId: "sum_" + sha256(content+now).slice(0,16), kind:"leaf", depth:0, content, tokenCount, fileIds, earliestAt, latestAt, descendantCount:0, descendantTokenCount:0, sourceMessageTokenCount, model})`.
   - `linkSummaryToMessages(summaryId, messageIds)` — DAG edges.
   - `replaceContextRangeWithSummary({conversationId, startOrdinal, endOrdinal, summaryId})` — atomic swap of the message range with one summary item.
8. **Invalidate context cache** (1604) so subsequent passes see the new state.
9. **Return** `{summaryId, level, content, removedTokens, addedTokens}`. `removedTokens` is sum of source `resolveMessageTokenCount` (NOT what the DB will report — token_count column may be 0 for some rows; this fed into the running-delta optimization but is bounded).

**Auth-failure short-circuit:** if the summarizer raises `LcmProviderAuthError`, `summarizeWithEscalation` returns `null` and `leafPass` returns `null` — caller treats it as a non-compacting skip and sets `authFailure: true` on the `CompactionResult`. Crucial: this avoids persisting fallback-truncation summaries that would silently corrupt the DAG when the provider is down for transient reasons.

### Condensation algorithm (`condensedPass`, lines 1614–1751)

Goal: collapse N contiguous summaries at the **same depth** into one **condensed** summary at depth+1.

1. **Pick candidate depth** — `selectShallowestCondensationCandidate` (1193–1222):
   - Get `distinct_depths_in_context` (call to `summaryStore`, with `maxOrdinalExclusive: freshTailOrdinal`).
   - For each depth from shallowest, compute fanout: `resolveFanoutForDepth(depth, hardTrigger)` (1173–1181) — hard trigger uses `condensedMinFanoutHard` (default 2); soft uses `leafMinFanout` at depth 0 (default 8) and `condensedMinFanout` (default 4) elsewhere.
   - `selectOldestChunkAtDepth(conversationId, targetDepth, freshTailOrdinal?)` (1230–1282): walk items, terminate on any non-summary, depth mismatch, or token overflow.
   - **Skip** if `chunk.items.length < fanout` or `chunk.summaryTokens < resolveCondensedMinChunkTokens()` (= max of `condensedTargetTokens` and 10% of `leafChunkTokens`).
2. **Fetch summary records** for the chunk (1622–1631).
3. **Concatenate** with date-range header `[<earliest> - <latest>]\n<content>` per summary, joined by `\n\n`.
4. **Resolve prior summary context** (only at depth 0; lines 1648–1651). At depth 0 also walk back up to 4 same-depth summaries, take the last 2.
5. **Summarize** with `summarizeWithEscalation`, `targetTokens = condensedTargetTokens`, `options.isCondensed = true`, `options.depth = targetDepth + 1`.
6. **Persist** in a transaction (1673–1743):
   - `insertSummary` with `kind: "condensed"`, `depth: targetDepth+1`, aggregated `earliestAt`/`latestAt`, accumulated `descendantCount`, `descendantTokenCount`, `sourceMessageTokenCount`.
   - `linkSummaryToParents(summaryId, parentSummaryIds)` — DAG edges to the child summaries.
   - `replaceContextRangeWithSummary` — atomic swap.

### Anti-thrashing logic

Three independent guards:

1. **Per-pass progress** (lines 705–712 in `compactFullSweep` phase-1; mirror in phase-2): `if (passTokensAfter >= passTokensBefore || passTokensAfter >= previousTokens) break;` — stop if a pass didn't make progress relative to either the immediate or the running floor.
2. **`compactUntilUnder`** (line 849): `if (!result.actionTaken || result.tokensAfter >= lastTokens) return success:false` — bail out instead of infinite-looping.
3. **`summarizeWithEscalation` "didn't compress" guard** (lines 1411 + 1422): if normal output ≥ input, retry aggressive; if aggressive also ≥ input, fall to deterministic.

> NOTE: LCM has NO equivalent of Hermes's `_ineffective_compression_count` (the "back off after 2 weak compressions" guard at `context_compressor.py:493`). LCM's anti-thrashing is per-pass progress checks; Hermes's is cross-call. Both are valid; pick one per the ADR below.

### Fallback model chain (escalation, lines 1334–1443)

```
sourceText → normal mode → if output ≥ input → aggressive mode
                                            → if output ≥ input → deterministic fallback (FALLBACK_MAX_TOKENS = 512)
```

Deterministic fallback prepends `"[LCM fallback summary - model unavailable; raw source preserved verbatim below]"` (or `"...truncated for context management..."` if source > char budget). Wave-4/9 audit fixes ensure the marker is ALWAYS present, even when source <= cap, so operators can distinguish "LLM down" from "LLM produced this."

Hard cap enforcement (lines 1428–1440): if `summaryTokens > targetTokens * summaryMaxOverageFactor`, call `capSummaryText` (lines 101–122) which tries appending diagnostic suffixes (`[Capped from N tokens to ~M]`, etc.) and truncating, falling back to plain truncation. Level becomes `"capped"`.

### Telemetry write paths

- **`persistCompactionEvents`** (lines 1754–1812) — called after each phase. For each non-null result, calls `persistCompactionEvent`.
- **`persistCompactionEvent`** (lines 1815–1830) — currently **only logs** via `this.log.info`. Despite the name, NO row is currently written to a synthetic chat message. (Earlier versions of LCM appended a synthetic assistant message describing the compaction; that was removed to avoid history pollution.)
- The summary write itself (in `leafPass`/`condensedPass` transactions) is the canonical persistence point.

### Per-conversation context-items cache

Lines 362–403. Ref-counted (`_contextItemsCacheRefCount`) so concurrent compactions on different conversations don't trample each other. Active only during `withContextCache` (entered by `compact`, `compactLeaf`, `compactUntilUnder`). Outside callers (e.g. `engine.ts evaluateLeafTrigger`) get uncached reads. Invalidated by `invalidateContextCache(conversationId)` after each `replaceContextRangeWithSummary`.

---

## Summarize — LLM seam

**File:** `src/summarize.ts` (line numbers from pr-613).

This is the LLM-call seam. It owns prompt construction, provider resolution, auth handling, retries, fallbacks, and output normalization. The compaction engine calls into a `CompactionSummarizeFn` callback — at runtime that callback is built here by `createLcmSummarizeFromLegacyParams`.

### Prompt templates

Three distinct prompts:

**1. Leaf summary** (`buildLeafSummaryPrompt`, lines 881–928). Quoted verbatim:

```
You summarize a SEGMENT of an OpenClaw conversation for future model turns.
Treat this as incremental memory compaction input, not a full-conversation summary.

<policy block — normal mode>:
Normal summary policy:
- Preserve key decisions, rationale, constraints, and active tasks.
- Keep essential technical details needed to continue work safely.
- Remove obvious repetition and conversational filler.

<policy block — aggressive mode>:
Aggressive summary policy:
- Keep only durable facts and current task state.
- Remove examples, repetition, and low-value narrative details.
- Preserve explicit TODOs, blockers, decisions, and constraints.

<instructionBlock — custom or "(none)">

Output requirements:
- Plain text only.
- No preamble, headings, or markdown formatting.
- Keep it concise while preserving required details.
- Track file operations (created, modified, deleted, renamed) with file paths and current status.
- If no file operations appear, include exactly: "Files: none".
- End with exactly: "Expand for details about: <comma-separated list of what was dropped or compressed>".
- Target length: about <targetTokens> tokens or less.

<previous_context>
<previousSummary or "(none)">
</previous_context>

<conversation_segment>
<text>
</conversation_segment>
```

**2. Condensed summary** (`buildCondensedSummaryPrompt`, dispatches by depth, lines 1052–1067):
- **Depth ≤ 1** → `buildD1Prompt` (930–978): "leaf-level conversation summaries into a single condensed memory node…"
- **Depth == 2** → `buildD2Prompt` (980–1014): "session-level summaries into a higher-level memory node…"
- **Depth ≥ 3** → `buildD3PlusPrompt` (1016–1050): "high-level memory node from multiple phase-level summaries…"

All three follow the same shape: instructionBlock, Preserve/Drop bullet lists, timeline directive ("hour or half-hour" / "dates and approximate time of day" / "date ranges"), `Expand for details about:` end marker, target-length line, and the source text inside `<conversation_to_condense>`. Only D1 includes `<previous_context>` because depth-≥2 condensations don't carry prior-summary continuity.

**3. Deterministic fallback** (`buildDeterministicFallbackSummary`, lines 1075–1102). Two markers depending on whether truncation was needed:

```
[LCM fallback summary — model unavailable; raw source preserved verbatim below]
<source>
```

or

```
[LCM fallback summary — model unavailable; raw source truncated for context management]
<source[:maxChars]>
```

`maxChars = max(256, targetTokens * 4)`. Wave-4 P0 fix: **ALWAYS** prepend a marker, even when source fits — otherwise operators couldn't tell "LLM down" from "LLM ran cleanly."

### System prompt

Single fixed string (`LCM_SUMMARIZER_SYSTEM_PROMPT`, line 59):

```
You are a context-compaction summarization engine. Follow user instructions exactly and return plain text summary content only.
```

### Pi-ai integration (the `deps.complete` callback)

LCM does **not** import pi-ai dynamically. Instead, every provider interaction is routed through the **injected** `LcmDependencies` bag (`src/types.ts` lines 115–187). The relevant fields:

- `deps.complete(params): Promise<{content: ContentBlock[], ...}>` — calls the model. This is what OpenClaw's plugin runtime wires up to its provider stack (pi-ai under the hood).
- `deps.resolveModel(modelRef, providerHint?)` — alias → `{provider, model}`.
- `deps.getApiKey(provider, model, {profileId, agentDir, runtimeConfig, skipModelAuth?})` — key lookup, with a `skipModelAuth: true` retry path.
- `deps.isRuntimeManagedAuthProvider?(provider, providerApi?)` — distinguishes OAuth-managed providers (don't retry with `skipModelAuth`).
- `deps.log.{info,warn,error,debug}` — structured logger.

**Implication for Python port:** the `LcmDependencies` injection pattern maps cleanly to a constructor-injected `LlmClient` protocol. **Python does NOT need dynamic imports** — `from lossless_hermes.llm_client import LlmClient` is fine. (Compare: Hermes's `agent/auxiliary_client.py call_llm(task=..., messages=..., temperature=..., max_tokens=...)` at line 4088. That's the analog Hermes call.)

### Provider candidate resolution (`resolveSummaryCandidates`, lines 1131–1250)

Five layers, tried in order, deduped on `(provider, model)`:

1. **Env vars** — `LCM_SUMMARY_MODEL` + `LCM_SUMMARY_PROVIDER`.
2. **Plugin config** — `config.plugins.entries["lossless-claw"].config.summaryModel` + `.summaryProvider`.
3. **OpenClaw `agents.defaults.compaction.model`**.
4. **OpenClaw `agents.defaults.model`**.
5. **Legacy runtime/session model** (the `model` field on a session).

Plus appended: **explicit fallback providers** from `config.fallbackProviders[]`.

Each candidate is resolved via `deps.resolveModel` and dropped if resolution fails. Final list: `dedupeResolvedCandidates`.

### Retry / circuit-breaker / fallback chain (the main loop, lines 1295–1685)

For each candidate in order:

1. `attemptSummarizerCall("initial")` — one call. On `requireStructuralSignal` auth failure (HTTP 401 or explicit `error.kind === "provider_auth"`), trigger `retryWithoutModelAuth` (lines 1374–1449): warn, call `getApiKey` with `skipModelAuth: true`, retry. If still auth-failing or runtime-managed, throw `LcmProviderAuthError`.
2. On `LcmProviderAuthError` from the candidate, log "PROVIDER FALLBACK", apply **exponential backoff** `min(500 * 2^index, 8000)ms`, move to next candidate.
3. On `LcmProviderResponseError` (explicit 4xx/5xx, finish=`error|failed|cancelled`, or non-auth `error.kind`), warn, backoff, move to next candidate.
4. On `SummarizerTimeoutError` (default 60s timeout via `withTimeout`, lines 153–161), log "timed out", backoff, move to next. If no more candidates AND it was a timeout, return `buildDeterministicFallbackSummary`.
5. On success, **normalize** via `normalizeCompletionSummary` (line 319) — collects text-like fields, dedupes exact fragments preserving first-seen order, drops reasoning/thinking blocks.
6. If empty after content extraction, try **envelope-aware extraction** (`normalizeCompletionSummary(result)` against the full envelope, not just `result.content`).
7. If still empty OR `extractIncompleteResponseSignals` non-empty, **retry once** with `reasoning: "low"` (a more conservative call). Retry empty → next candidate; retry succeeded → log "retry succeeded".
8. After all candidates fail and a final `lastAuthError` exists, throw it. Else return `buildDeterministicFallbackSummary`.

### Auth-failure detection (`extractProviderAuthFailure`, lines 525–560)

Two modes:
- **`requireStructuralSignal: true`** — used on success-path responses. Only triggers on HTTP 401 OR explicit `error.kind === "provider_auth"`. Plain text matches in the response body are NOT sufficient (an LLM summary may legitimately discuss auth errors).
- **`requireStructuralSignal: false`** (default) — used on caught errors. Triggers on 401, scope signals (`model.request`, `missing scope`, `insufficient scope`), or `AUTH_ERROR_TEXT_PATTERN` (401, unauthorized, invalid token/api key, etc.).

### Target-token resolution (`resolveTargetTokens`, lines 855–873)

```
condensed:               max(512, condensedTargetTokens)
leaf normal:             max(192, min(leafTargetTokens, floor(inputTokens * 0.35)))
leaf aggressive:         max(96,  min(aggressiveCap,    floor(inputTokens * 0.20)))
  where aggressiveCap = max(96, min(leafTargetTokens, floor(leafTargetTokens * 0.55)))
```

Default constants:
- `DEFAULT_LEAF_TARGET_TOKENS = 4000` (raised from 2400 in v4.1 §A.10).
- `DEFAULT_CONDENSED_TARGET_TOKENS = 2000`.

These are summarize.ts defaults. Compaction.ts has its own defaults (`leafTargetTokens: 600`, `condensedTargetTokens: 900`) — the **config** is the source of truth and will fall back to whichever default the resolver hits.

---

## Python class skeletons

```python
# src/lossless_hermes/estimate_tokens.py
def estimate_tokens(text: str) -> int: ...
def truncate_text_to_estimated_tokens(text: str, max_tokens: int) -> str: ...

# src/lossless_hermes/assembler.py
from dataclasses import dataclass, field
from hashlib import sha256

@dataclass(slots=True)
class ResolvedItem:
    ordinal: int
    message: dict
    tokens: int
    is_message: bool
    text: str
    message_id: int | None = None
    seq: int | None = None
    source_role: str | None = None
    summary: SummaryRecord | None = None
    # v4.2 §B (#628 stub-tier)
    file_id: str | None = None
    file_byte_size: int | None = None
    stub_tool_name: str | None = None
    stub_tool_call_id: str | None = None
    file_summary: str | None = None

@dataclass(slots=True)
class AssembleInput:
    conversation_id: int
    token_budget: int
    fresh_tail_count: int = 8
    fresh_tail_max_tokens: int | None = None
    prompt: str | None = None
    prompt_aware_eviction: bool = True
    orphan_stripping_ordinal: int | None = None
    stub_large_tool_payloads: bool = False

@dataclass(slots=True)
class AssembleResult:
    messages: list[dict]
    estimated_tokens: int
    stats: dict
    debug: dict | None = None

class ContextAssembler:
    def __init__(self, conversation_store, summary_store, timezone: str | None = None): ...
    async def assemble(self, inp: AssembleInput) -> AssembleResult: ...
    async def _resolve_items(self, ctx_items) -> list[ResolvedItem]: ...
    async def _resolve_message_item(self, item) -> ResolvedItem | None: ...
    async def _resolve_summary_item(self, item) -> ResolvedItem | None: ...
    @staticmethod
    def _resolve_fresh_tail_ordinal(resolved, fresh_tail_count, fresh_tail_max_tokens) -> int: ...
    @staticmethod
    def _apply_stub_substitution(evictable: list[ResolvedItem]) -> dict: ...
    @staticmethod
    def _budget_walk(evictable, fresh_tail, token_budget, prompt, prompt_aware) -> tuple[list[ResolvedItem], str]: ...
    @staticmethod
    def _score_relevance(item_text: str, prompt: str) -> float: ...
    @staticmethod
    def _filter_non_fresh_assistant_tool_calls(items, fresh_tail_ords, orphan_strip_ord, all_tool_result_ords): ...

# src/lossless_hermes/compaction.py
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal
import asyncio

CompactionLevel = Literal["normal", "aggressive", "fallback", "capped"]

@dataclass(slots=True)
class CompactionConfig:
    context_threshold: float = 0.75
    fresh_tail_count: int = 8
    fresh_tail_max_tokens: int | None = None
    leaf_min_fanout: int = 8
    condensed_min_fanout: int = 4
    condensed_min_fanout_hard: int = 2
    incremental_max_depth: int = 1
    leaf_chunk_tokens: int = 20_000
    leaf_target_tokens: int = 600
    condensed_target_tokens: int = 900
    max_rounds: int = 10
    timezone: str | None = None
    summary_max_overage_factor: float = 3.0

@dataclass(slots=True)
class CompactionResult:
    action_taken: bool
    tokens_before: int
    tokens_after: int
    created_summary_id: str | None = None
    condensed: bool = False
    level: CompactionLevel | None = None
    auth_failure: bool = False

SummarizeFn = Callable[..., Awaitable[str]]  # (text, aggressive=False, options={previous_summary, is_condensed, depth}) → str

class CompactionEngine:
    def __init__(self, conversation_store, summary_store, config: CompactionConfig, logger): ...
    # public
    async def evaluate(self, conversation_id, token_budget, observed_token_count=None) -> CompactionDecision: ...
    async def evaluate_leaf_trigger(self, conversation_id, leaf_chunk_override=None) -> dict: ...
    async def compact(self, *, conversation_id, token_budget, summarize: SummarizeFn, force=False, hard_trigger=False, summary_model=None) -> CompactionResult: ...
    async def compact_leaf(self, ...) -> CompactionResult: ...
    async def compact_until_under(self, ...) -> dict: ...
    # private
    async def _leaf_pass(self, conv_id, message_items, summarize, prev_summary, model) -> dict | None: ...
    async def _condensed_pass(self, conv_id, summary_items, target_depth, summarize, model) -> dict | None: ...
    async def _summarize_with_escalation(self, *, source_text, summarize, options, target_tokens) -> dict | None: ...
    async def _select_oldest_leaf_chunk(self, conv_id, override=None) -> dict: ...
    async def _select_shallowest_condensation_candidate(self, *, conv_id, hard_trigger) -> dict | None: ...
    async def _select_oldest_chunk_at_depth(self, conv_id, target_depth, fresh_tail_override=None) -> dict: ...

# src/lossless_hermes/summarize.py
class LcmProviderAuthError(Exception):
    def __init__(self, provider, model, failure): ...

class LcmProviderResponseError(Exception): ...

class SummarizerTimeoutError(Exception): ...

@dataclass(slots=True)
class SummarizeOptions:
    previous_summary: str | None = None
    is_condensed: bool = False
    depth: int | None = None

class LcmSummarizer:
    def __init__(self, llm_client, config, logger, custom_instructions: str | None = None): ...
    async def summarize(self, text: str, aggressive: bool = False, options: SummarizeOptions | None = None) -> str: ...
    # internal helpers
    @staticmethod
    def _build_leaf_prompt(text, mode, target_tokens, previous_summary, custom_instructions) -> str: ...
    @staticmethod
    def _build_condensed_prompt(text, target_tokens, depth, previous_summary, custom_instructions) -> str: ...
    @staticmethod
    def _build_deterministic_fallback(text, target_tokens) -> str: ...
    @staticmethod
    def _normalize_completion_summary(content) -> dict: ...
    @staticmethod
    def _extract_provider_auth_failure(value, *, require_structural_signal=False) -> dict | None: ...
```

---

## TS → Python translation notes

### Cheap copies / slicing
- TS `arr.slice()`, `.slice(0,5)`, `.slice(-2)` → Python `arr[:]`, `arr[:5]`, `arr[-2:]`.
- TS `Array.prototype.filter` returns a new array; Python list comprehension is equivalent.
- TS spread `{...obj, field: x}` → Python `{**obj, "field": x}`.

### Sorting comparators
TS `arr.sort((a,b) => b.tokens - a.tokens || a.ordinal - b.ordinal)` → Python `arr.sort(key=lambda x: (-x.tokens, x.ordinal))`. The double-key with a negation handles the descending-then-ascending pattern.

### BM25-lite scoring (`scoreRelevance`)
Port straight. No native deps. Watch out for:
- TS `split(/[^a-z0-9]+/)` → Python `re.split(r"[^a-z0-9]+", text.lower())`.
- TS `filter(t => t.length > 1)` → list comprehension `[t for t in tokens if len(t) > 1]`.

### Token-budget walk
The TS impl is O(n): one pass to sum `evictableTotalTokens`, one greedy walk, one optional sort for prompt-aware mode. **Ensure the Python port doesn't accidentally O(n²)** — Python's `list.append` + `list.sort` are linear/n-log-n; avoid `list = list + [item]` (quadratic copying) inside the loop.

### Crypto
TS `createHash("sha256").update(...).digest("hex").slice(0,16)` → Python `hashlib.sha256(data).hexdigest()[:16]`.

### JSON tolerance
TS `parseJson(value)` returns `undefined` on failure. Python equivalent:
```python
def _parse_json(value: str | None):
    if not value or not value.strip(): return None
    try: return json.loads(value)
    except (ValueError, TypeError): return None
```

### Timezone formatting (`formatTimestamp`)
TS uses `Intl.DateTimeFormat`. Python uses `zoneinfo.ZoneInfo` (3.9+) + `datetime.strftime`. For the `tzAbbr` short form, Python's `datetime.now(ZoneInfo(tz)).tzname()` returns `"PST"`, `"PDT"`, etc.

### `Intl.DateTimeFormat("en-CA", ...)` (ISO-ish `YYYY-MM-DD`)
Use `dt.strftime("%Y-%m-%d %H:%M")` + `dt.tzname()`. Watch out: locale `"en-CA"` was chosen specifically because it formats dates as `YYYY-MM-DD`. Python's `strftime` is locale-independent for `%Y-%m-%d`.

### Concurrency
- LCM has an in-memory **per-conversation cache** with a refcount (`withContextCache`, `_contextItemsCacheRefCount`). It does NOT have a per-session lock — concurrency is handled at the OpenClaw-bridge layer.
- For Python, use `asyncio.Lock` keyed by `conversation_id` (a `defaultdict(asyncio.Lock)`) so concurrent calls to `compact()` on the same conversation serialize. The context-items cache is a separate concern — port it as-is with a refcount, or simplify to a TTL cache.

### Dynamic import — NOT NEEDED
LCM's `LcmDependencies` is constructor injection. The TS code does NO dynamic imports; everything is injected at registration time. Python port: direct import + constructor injection.

```python
# In the plugin entry point:
from lossless_hermes.llm_client import HermesLlmClient
from lossless_hermes.assembler import ContextAssembler
from lossless_hermes.compaction import CompactionEngine
from lossless_hermes.summarize import LcmSummarizer

summarizer = LcmSummarizer(llm_client=HermesLlmClient(...), config=cfg, logger=log)
engine     = CompactionEngine(stores..., config=cfg, logger=log)
```

### `withTimeout` → `asyncio.wait_for`
```python
try:
    result = await asyncio.wait_for(llm_client.complete(...), timeout=summarizer_timeout_ms / 1000)
except asyncio.TimeoutError:
    raise SummarizerTimeoutError(...)
```

### Live ESM bindings (result-budget.ts)
TS uses `export let MAX_RESULT_TOKENS` with `applyResultBudgetConfig(...)` mutating it post-init. Python doesn't have live bindings, but module-level mutable state works the same way: `lossless_hermes.config.MAX_RESULT_TOKENS = ...`. Consumers must `from lossless_hermes import config; ... config.MAX_RESULT_TOKENS ...` (NOT `from ... import MAX_RESULT_TOKENS` which binds at import-time).

### Async vs sync
LCM assembler is async (`async assemble`) because store reads are async (better-sqlite3 is sync under the hood but the store wrappers return promises). Hermes's `ContextEngine.compress` is **sync** (`def compress(...)`). See ADR section.

### Async generators
LCM doesn't use generators. Port iterative loops as plain `for` loops with `await` calls inside — fine in async Python.

---

## Test inventory

Existing LCM tests for the in-scope files (`/Volumes/LEXAR/Claude/lossless-claw/test/`):

| Test file | What it covers |
|---|---|
| `assembler-blocks.test.ts` | Block reconstruction (`toolCallBlockFromPart`, `toolResultBlockFromPart`, `blockFromPart`); reasoning block restoration; provider-specific keying |
| `compaction-maintenance-store.test.ts` | Per-conversation cache + ref-counting; replaceContextRangeWithSummary atomicity |
| `lcm-summarizer-reasoning.test.ts` | Auth-failure detection; structural-signal mode; reasoning-block normalization |
| `summarize.test.ts` | Prompt templates; fallback chain; provider escalation; deterministic-fallback markers |
| `v42-stub-tier.test.ts` (main branch, **NOT** on pr-613) | #628 stub substitution; resolveMessageItem sidecar lookup; multi-block array preservation |

Recommend porting these tests **first** in the Python tree — they encode the most important invariants (Wave-12 audit caught many regressions that became these tests).

---

## Open architecture decisions

### ADR: Async vs sync assembler

**Context.** TS assembler is async because better-sqlite3 wrappers return Promises. Hermes's `ContextEngine.compress(messages, current_tokens, focus_topic) -> List[dict]` is **synchronous** in the ABC at `agent/context_engine.py:77`.

**Options.**
1. **Sync assembler + sync compaction.** Matches Hermes's ABC exactly. DB calls block, but SQLite is local and fast.
2. **Async assembler + sync `compress()` wrapper.** Inside `compress` call `asyncio.run(asyncio.wait_for(self._async_compress(...), timeout=...))`. Hermes is happy.
3. **Make `compress()` async-compatible.** Requires Hermes ABC change; out of scope.

**Recommendation.** **Option 1 (sync)**. SQLite calls are microseconds; LLM calls run via Hermes's existing sync `auxiliary_client.call_llm`. Removing the async-await boilerplate makes the port substantially shorter (Python async is more verbose than TS) and matches the host plugin's contract.

The only place async genuinely helps is `summarize` (LLM timeouts) — but `asyncio.wait_for` is awkward inside sync code. Use `concurrent.futures.ThreadPoolExecutor` + `Future.result(timeout=...)` instead.

### ADR: Token estimator — port LCM's or use Hermes's `_content_length_for_budget`?

**Context.** LCM has `estimateTokens` (Unicode code-point-aware, CJK 1.5×, emoji 2×, ASCII 0.25×). Hermes has `_content_length_for_budget` (char-length proxy, with `_IMAGE_CHAR_EQUIVALENT = 1600 * 4 = 6400 chars/image`). They model different things.

**Options.**
1. **Port LCM's `estimateTokens` as-is.** Pure function, 80 LOC, no deps.
2. **Use Hermes's `_content_length_for_budget` everywhere.** Loses CJK/emoji weighting; gains multimodal image-counting.
3. **Hybrid: LCM's `estimateTokens` for text; Hermes's image-weighting for multimodal lists.**

**Recommendation.** **Option 3 (hybrid)**. CJK accuracy is core to LCM's correctness (the comment in `estimate-tokens.ts` says naive formula underestimates CJK by 6×, causing compaction to trigger too late). Image weighting is core to Hermes's correctness (computer-use sessions with 5+ screenshots would be miscounted near-zero). Combine.

```python
def estimate_tokens(content) -> int:
    if isinstance(content, str): return _estimate_text_tokens(content)
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, dict) and part.get("type") in {"image", "image_url", "input_image"}:
                total += _IMAGE_TOKEN_ESTIMATE   # 1600
            else:
                total += _estimate_text_tokens(part.get("text", "") if isinstance(part, dict) else str(part))
        return total
    return _estimate_text_tokens(str(content))
```

### ADR: Summarizer LLM client — Hermes provides one; how is the model selected per tier?

**Context.** LCM resolves `(provider, model)` from 5 layers (env → plugin config → `agents.defaults.compaction.model` → `agents.defaults.model` → session). Hermes routes auxiliary calls via `auxiliary_client.call_llm(task="compression", ...)` which reads `auxiliary.compression.{provider, model, timeout}` from `config.yaml`.

**Options.**
1. **Mirror LCM's 5-layer resolution** in Python, with one new env-var (`LCM_SUMMARY_MODEL`) and a `plugins.lossless-hermes.summary_model` config field.
2. **Defer entirely to Hermes's `call_llm(task="compression")`**. One source of truth; no parallel config.
3. **Hybrid: Hermes's `call_llm(task="lcm_summary")` (new task entry) + LCM's `fallbackProviders[]` list.**

**Recommendation.** **Option 3**. Hermes's `auxiliary.<task>` config is the right primary location (operators already know it). Add a new task `lcm_summary` with its own provider/model/timeout. Also support `lcm_summary_fallbacks: [{provider, model}]` for the explicit fallback chain — this is real operator pain when the primary provider's quota is exhausted at 3am.

### ADR: Prompt template versioning — LCM uses `lcm_prompt_registry` table; keep or simplify?

**Context.** The brief mentions `lcm_prompt_registry` but I did **not** find such a table in pr-613's `summarize.ts` or `compaction.ts`. Prompt templates are inline string-builders. If a registry exists, it's elsewhere (possibly an unmerged WIP).

**Options.**
1. **Inline templates** (current TS state). Hardcoded in `_build_leaf_prompt` / `_build_condensed_prompt`.
2. **Templates as YAML files** in `src/lossless_hermes/prompts/`. Hot-reloadable; operators can A/B test without code changes.
3. **DB-backed registry** with versioning and rollout flags. Heaviest.

**Recommendation.** **Option 1 (inline)** for v1. Simpler to port, no schema migration, no test fixtures to maintain. Promote to Option 2 only if operator A/B testing becomes a real need.

### ADR: Anti-thrashing semantics — LCM per-pass vs Hermes cross-call

**Context.** LCM checks per-pass progress: `if passAfter >= passBefore: break`. Hermes checks cross-call: `if last 2 compressions saved <10%: skip`. They're complementary.

**Options.**
1. **Port LCM's guards only.**
2. **Port Hermes's `_ineffective_compression_count` only.**
3. **Both.**

**Recommendation.** **Option 3**. They protect against different failure modes. LCM's catches single-call regressions (a summarizer that returns nearly-input-sized output). Hermes's catches drift over many calls (each pass shaves 2% off; thrashing every turn). Both as one paragraph: track `last_compression_savings_pct` + `ineffective_compression_count`; the LCM per-pass break is already inside `compact_full_sweep`.

---

## Remaining 5% risk

1. **`messages.large_content` column.** The #628 stub-tier requires this column on the `messages` table. The lossless-hermes storage epic (`epics/01-storage`) must include this in the schema, or `apply_stub_substitution` will be a no-op forever. Risk: silent feature dead-end.

2. **`large_files` table + `getLargeFile()` store method.** Same as above. This is a separate epic (`epics/06-tools` includes `lcm_describe`, but the storage shape is foundational).

3. **`MessageRecord.tokenCount` column population.** Compaction's running-delta arithmetic (`tokensAfter = tokensBefore - removed + added`) relies on `resolveMessageTokenCount` returning a value consistent with what `getContextTokenCount()` returns. The TS code's comment at line 1554 acknowledges divergence is possible for rows with `token_count <= 0`. **Python port should populate `token_count` at insert time** so the divergence vanishes (port `estimateTokens(content)` on every message insert).

4. **Hermes's `call_llm` doesn't expose a `reasoning` parameter.** LCM's retry calls `attemptSummarizerCall("retry", "low")` — passes `reasoning: "low"` to `deps.complete`. Hermes's `auxiliary_client.call_llm` (line 4088) does NOT have a `reasoning` argument. Either pipe it through `extra_body={"reasoning_effort": "low"}` (OpenAI-style) or accept that the conservative retry just retries with the same settings (lower-impact but still useful for transient overload).

5. **Anthropic block-shape preservation in `applyStubSubstitution`.** The "multi-block tool_result content stays an array" fix (P1 in the PR review) is subtle. **Add a regression test on day one** that creates a `ResolvedItem` with a 2-element content array, runs `apply_stub_substitution`, and asserts the result is `[{"type": "text", "text": "..."}]` and NOT a plain string. Easy to break under refactor.

6. **`sanitizeToolUseResultPairing` is in a SEPARATE TS file** (`transcript-repair.ts`) and is NOT in this guide's scope. It's the **final** repair pass before `assemble()` returns. The Python port of this function is a prerequisite, not a follow-up — assembler depends on it for the last line of `assemble`.

7. **The `extractFileIdsFromContent` import** from `large-files.ts` is used by `leafPass` and `condensedPass` to populate the summary's `file_ids` index. If file-id tracking is descoped from the storage epic, these lines can `return []` safely (summaries still work; cross-references are degraded).

8. **Empirical test of CJK/emoji estimation in Python.** TS's `for (const char of text)` iterates Unicode code points (surrogate-pair-aware). Python 3's `for char in text` iterates code points natively in CPython, BUT the per-character `codePointAt(0)` translates to `ord(char)`. Validate with a test fixture containing CJK, emoji ZWJ sequences, and combining marks — Python's `ord` of a combining mark gives a different value than TS's surrogate-pair handling.
