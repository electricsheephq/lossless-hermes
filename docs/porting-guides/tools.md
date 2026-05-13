# Porting Guide: Agent Tools

**Source LOC:** ~7,693 across 13 files in `src/tools/` (8 tool factories + 5 shared helpers)
**Python target LOC:** ~6,500 (Python is ~15% denser than TS for this surface — SQL strings dominate)
**Confidence target:** 95%
**Estimated effort:** 40–60 hours
**Epic:** 06-tools

## Tool inventory + dispatch shape

Each tool is registered via Hermes's `ContextEngine.get_tool_schemas()` (returns list of OpenAI-format `{name, description, parameters}` dicts). Dispatch flows through `ContextEngine.handle_tool_call(name, args, messages=, **kwargs) -> str` — called from `run_agent.py:11249` (the elif branch that fires when `function_name in self._context_engine_tool_names`). The engine must return a JSON string (the runtime wraps it as a tool-role message).

In TS, every tool exposes the same factory shape via `createXxxTool({deps, getLcm, sessionKey, getRuntimeContext})` returning `AnyAgentTool` (`= {name, label, description, parameters: TypeBoxSchema, execute(toolCallId, params)}`). In Python the dispatch surface collapses to a single `LCMEngine.handle_tool_call`, with handlers stored in a `TOOL_DISPATCH` dict.

| Tool name | TS file | LOC | Python target | DB-only? | Blocked by sqlite-vec? |
|---|---|---:|---|---|---|
| `lcm_grep` | lcm-grep-tool.ts | 1179 | tools/grep.py | partial — regex/full_text/verbatim are pure SQLite; hybrid/semantic need Voyage + vec0 | YES (hybrid + semantic modes); other 3 modes ship now |
| `lcm_describe` | lcm-describe-tool.ts | 766 | tools/describe.py | yes | no |
| `lcm_expand` | lcm-expand-tool.ts | 455 | tools/expand.py | yes (DAG walk, sub-agent only) | no |
| `lcm_expand_query` | lcm-expand-query-tool.ts | 1467 | tools/expand_query.py | yes + dispatch a sub-agent run via gateway | no |
| `lcm_synthesize_around` | lcm-synthesize-around-tool.ts | 1477 | tools/synthesize_around.py | DB + LLM call (via dispatch.py) | partial — `window_kind='semantic'` only |
| `lcm_get_entity` | lcm-get-entity-tool.ts | 342 | tools/get_entity.py | yes | no |
| `lcm_search_entities` | lcm-search-entities-tool.ts | 377 | tools/search_entities.py | yes | no |
| `lcm_compact` | lcm-compact-tool.ts | 378 | tools/compact.py | engine.compact() call (no DB writes from tool itself) | no |
| `lcm-expand-tool.delegation.ts` | 580 | (helper: `expand_query` sub-agent loop) | — | no |
| `lcm-conversation-scope.ts` | 162 | tools/conversation_scope.py | yes (DB lookup) | no |
| `lcm-expansion-recursion-guard.ts` | 373 | tools/expansion_recursion_guard.py | (in-memory state) | no |
| `lcm-entity-shared.ts` | 84 | tools/entity_shared.py | (shared CTE string) | no |
| `common.ts` | 53 | tools/_common.py | utilities | no |

## Per-tool detailed spec

> Convention: every `description` string below is **verbatim from the TS source** — these are load-bearing for the model. Do not paraphrase. Strip TS template-literal concatenation but preserve exact wording (including the parenthesized mode labels, parameter caps, env knob names, and operator-facing hints).

---

### `lcm_grep`

**Source:** `lcm-grep-tool.ts` (lines 1–1179)

**Description string (verbatim, assembled from line 196–204):**

```
Search compacted conversation history with FIVE modes (`mode` parameter): (1) `regex` — literal or regex pattern over summary content; (2) `full_text` — FTS5 keyword search; queries use FTS5 AND semantics by default, so keep them short and focused; quoted phrases stay intact and optional sort modes can prioritize relevance for older topics; (3) `hybrid` — FTS5 + Voyage semantic + rerank (PRIMARY for Type B topic-anchored queries: 'have we ever discussed X', 'what work has been done on Y' — handles paraphrases like 'merge mess' → 'rebase blew up'); (4) `semantic` — pure-vector KNN over summaries via Voyage embed (no rerank, cheaper than hybrid). Use for paraphrastic exploration where keyword precision doesn't matter; (5) `verbatim` — returns FULL untruncated source messages (PRIMARY for Type C verbatim/citation queries: 'what exactly did X say about Y', 'quote me the original wording'). Optional `summaryKinds` filter (mode='semantic' / 'hybrid' only) scopes hits to ['leaf'] or ['condensed'] — useful when you want fresh source leaves vs higher-level rollups. Returns matching snippets with summary/message IDs for follow-up with lcm_describe (one-hop) or lcm_expand_query (multi-hop drilldown). Tool result is hard-capped at LCM_TOOL_RESULT_TOKEN_BUDGET (default 10K tokens / 40K chars) — when context is near full, prefer narrower queries (smaller `limit`, more specific `pattern`) over big sweeps; chained calls accumulate context, and compaction only fires post-turn.
```

**JSON schema (Python dict, translated from TypeBox at lines 43–125):**

```python
LCM_GREP_SCHEMA = {
    "name": "lcm_grep",
    "description": "<verbatim, see above>",
    "parameters": {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": (
                    "Search pattern. Interpreted as regex when mode is \"regex\", or as an FTS5 "
                    "text query when mode is \"full_text\". In full_text mode, FTS5 defaults to AND "
                    "matching, so prefer 1-3 distinctive terms or one quoted multi-word phrase "
                    "instead of padding with synonyms or extra keywords."
                ),
            },
            "mode": {
                "type": "string",
                "enum": ["regex", "full_text", "hybrid", "semantic", "verbatim"],
                "description": "<verbatim, see TS line 51>",
            },
            "scope": {
                "type": "string",
                "enum": ["messages", "summaries", "both"],
                "description": "What to search... Default: \"both\".",
            },
            "conversationId": {"type": "number", "description": "Physical conversation ID..."},
            "allConversations": {"type": "boolean", "description": "Set true to..."},
            "since": {"type": "string", "description": "Only return matches created at or after this ISO timestamp."},
            "before": {"type": "string", "description": "Only return matches created before this ISO timestamp."},
            "limit": {"type": "number", "minimum": 1, "maximum": 200, "description": "Maximum number of results (default 50)."},
            "sort": {"type": "string", "enum": ["recency", "relevance", "hybrid"], "description": "..."},
            "role": {"type": "string", "enum": ["user", "assistant", "tool", "system", "all"], "description": "..."},
            "summaryKinds": {"type": "array", "items": {"type": "string", "enum": ["leaf", "condensed"]}, "description": "..."},
        },
        "required": ["pattern"],
    },
}
```

**Behavior per mode (5 modes, dispatched at lines 291–351):**

- **`regex`** — straight LIKE/REGEXP over `summaries.content` and/or `messages.content` via `retrieval.grep(...)`. Pure SQLite. Honors `scope`, `since`/`before`, `conversationId(s)`.
- **`full_text`** — FTS5 MATCH query against `summaries_fts` / `messages_fts`. The store-layer `sanitizeFts5Query` (`src/store/fts5-sanitize.ts`) already wraps problematic chars in phrase quotes; do not re-sanitize. Supports `sort: relevance|hybrid|recency`.
- **`hybrid`** (lines 474–760) — `runHybridSearch()` from `src/embeddings/hybrid-search.ts`. Fans out two arms: FTS5 (via the same store path) and Voyage semantic vec0 KNN. Over-fetches `max(50, limit*3)` from each arm (capped 500), then RRF-fuses, then calls Voyage **rerank** to score the union. Voyage retry/timeout pinned (`voyageMaxRetries: 1, voyageTimeoutMs: 15_000`) to cap agent hot-path wall-time. Returns provenance tag per hit (`[from FTS+semantic]` / `[from FTS only]` / `[from semantic only]`). **Degrades gracefully:** if vec0 missing → `degradedToFtsOnly=true`; if rerank fails → RRF-only with `degradedSkippedRerank=true`.
- **`semantic`** (cheaper than hybrid; no rerank) — `runSemanticSearch()` from `src/embeddings/semantic-search.ts`. Pure vec0 KNN over Voyage-embedded summaries. Supports `summaryKinds: ["leaf", "condensed"]` filter.
- **`verbatim`** (hard-capped at 20 rows) — bypasses FTS, runs `SELECT * FROM messages WHERE content LIKE ?` (or FTS-with-phrase-quote-wrap via local `sanitizeFts5Pattern` at lines 154–178). Returns full untruncated message rows for citation / quote-back. Honors `role` filter (`user|assistant|tool|system`). The 20-cap protects against blowing past `MAX_RESULT_CHARS`.

**Handler signature (Python):**

```python
async def handle_lcm_grep(args, *, db, retrieval, voyage_client, messages, session_key, runtime_ctx, **_) -> str:
    return await run_with_token_gate(
        tool_name="lcm_grep",
        params=args,
        session_key=session_key,
        runtime_ctx=runtime_ctx,
        inner=lambda: _lcm_grep_impl(args, db=db, retrieval=retrieval, voyage=voyage_client),
    )
```

**Dependencies:**
- DB tables: `summaries`, `summaries_fts`, `messages`, `messages_fts`, `summary_vectors` (vec0 vtab), `conversations`
- External: Voyage API (`VOYAGE_API_KEY`) for hybrid + semantic only
- Cross-tool: relies on `resolveLcmConversationScope` for `since/before/conversationId/allConversations`

**Failure modes:**
- `pattern == ""` → `{"error": "`pattern` is required..."}` (line 234)
- `VOYAGE_API_KEY` missing → `{"error": "...hybrid mode requires it. Use mode='full_text' for keyword-only search."}` (line 631)
- vec0 not loaded → semantic mode `SemanticSearchUnavailableError` → fall back to FTS-only or refuse (degraded flag set)
- FTS5 syntax error in pattern → store-side sanitizer auto-wraps in phrase quotes; for verbatim the local `sanitizeFts5Pattern` does the same
- since >= before → `{"error": "`since` must be earlier than `before`."}`
- Time filter on conversation row not in scope → just returns zero matches
- Result exceeds `MAX_RESULT_CHARS` → truncation notice appended (regex pinned by tests — see N3 retro in result-budget.ts)

**Token-budget gating:** Wrapped in `runWithTokenGate` (line 216). Estimator (`estimateResultTokens` in needs-compact-gate.ts:66–94):
- `regex`/`full_text`: `200 + limit * 200` chars
- `hybrid`: `250 + limit * 230` chars
- `semantic`: `350 + limit * 215` chars
- `verbatim`: `70 + min(20, limit) * 2400` chars (large because full message rows; pinned to assistant-median bias)

---

### `lcm_describe`

**Source:** `lcm-describe-tool.ts` (lines 1–766)

**Description string (verbatim, lines 146–155):**

```
Look up an LCM item by ID, with optional one-hop drilldown. PRIMARY tool for Type E queries (drilldown / source-tracing): 'where did this synthesized claim come from?', 'show me the source leaves for this summary'. Set expandChildren=true to inline child summaries (capped 20, max 50) and/or expandMessages=true to inline raw source messages. Inspects summaries (sum_xxx) or stored files (file_xxx). For multi-hop drilldown that needs to read more than one level, use lcm_expand_query (delegated sub-agent expansion). Returns summary content, lineage, token counts, file exploration, and (with expand flags) one-hop child/message detail.
```

**JSON schema (translated from TypeBox at lines 61–116):**

```python
LCM_DESCRIBE_SCHEMA = {
    "name": "lcm_describe",
    "description": "<verbatim, see above>",
    "parameters": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "The LCM ID to look up. Use sum_xxx for summaries, file_xxx for files."},
            "conversationId": {"type": "number", "description": "..."},
            "allConversations": {"type": "boolean", "description": "..."},
            "tokenCap": {"type": "number", "minimum": 1, "description": "Optional budget cap..."},
            "expandChildren": {"type": "boolean", "description": "..."},
            "expandChildrenLimit": {"type": "number", "minimum": 1, "maximum": 50, "description": "default 20"},
            "expandMessages": {"type": "boolean", "description": "..."},
            "expandMessagesLimit": {"type": "number", "minimum": 1, "maximum": 50, "description": "default 20"},
            "expandMessagesOffset": {"type": "number", "minimum": 0, "description": "Skip the first N..."},
        },
        "required": ["id"],
    },
}
```

**Behavior:**
1. Resolves conversation scope. If no scope can be resolved → `{"error": "No LCM conversation found..."}`
2. Calls `retrieval.describe(id)` which returns `{type: "summary", summary: {...}}` or `{type: "file", file: {...}}` or null.
3. **Summary path (lines 219–650):** emits `LCM_SUMMARY <id>` with meta line (kind, depth, tok counts, range, created), parents, children, then a `manifest` block walking the subtree with per-node `cost[s=,m=]` and `budget[s=in/over,m=in/over]` flags computed against `resolvedTokenCap`. If `expandChildren=true`, fetches first-hop children's full content (suppression-filtered, raw-count exposed). If `expandMessages=true` AND target is a leaf, fetches the source messages with `expandMessagesOffset` pagination.
4. **File path (lines 651+):** emits file metadata + content with same line-by-line truncation policy.
5. **Delegated session budget enforcement:** if running in a sub-agent session with a delegated grant, looks up `remainingTokenBudget`; if base summary tokens exceed remaining, redacts content and surfaces `budget exhausted`. Charges the grant ledger AFTER successful emit.

**Handler signature (Python):**

```python
async def handle_lcm_describe(args, *, db, retrieval, expansion_auth_manager, deps, session_key, runtime_ctx, **_) -> str:
    return await run_with_token_gate(
        tool_name="lcm_describe",
        params=args,
        session_key=session_key,
        runtime_ctx=runtime_ctx,
        inner=lambda: _lcm_describe_impl(args, db=db, retrieval=retrieval, deps=deps),
    )
```

**Failure modes:**
- ID not found → `{"error": "Not found: <id>", "hint": "Check the ID format..."}`
- ID found but outside scope → `{"error": "Not found in this session scope: <id>", "hint": "Use allConversations=true..."}`
- Delegated session with no grant → grant lookup returns null, behaves as non-delegated
- Output > `MAX_RESULT_CHARS` → `truncateLinesToCap` appends truncation notice (pinned regex, see N3 in result-budget.ts)

**Token-budget gating:** Wrapped in `runWithTokenGate` (line 165). Estimator:
- Base: `350 + 5*250 + 3200 = 4800` chars
- +`k * 2000` for `expandChildren` (k = expandChildrenLimit ?? 20)
- +`k * 600` for `expandMessages` (k = expandMessagesLimit ?? 20)
- Capped at `HARD_CAP_TOKENS` (= `MAX_RESULT_TOKENS`)

This is THE highest blow-up-risk tool (per needs-compact-gate.ts docstring line 27).

---

### `lcm_expand`

**Source:** `lcm-expand-tool.ts` (lines 1–455)

**Description string (verbatim, lines 134–142):**

```
SUB-AGENT ONLY. Main-agent sessions get a runtime error if they invoke this tool — instead, main agents should use lcm_describe with expandChildren/expandMessages flags (one-hop drilldown), or lcm_expand_query (delegated multi-hop drilldown that spawns a sub-agent). When called from a sub-agent: expands the LCM summary DAG to retrieve children and source messages. Provide summaryIds (direct expansion) or query (grep-first, then expand top matches). Returns a compact text payload plus cited IDs for follow-up.
```

**JSON schema (lines 24–66):**

```python
LCM_EXPAND_SCHEMA = {
    "name": "lcm_expand",
    "description": "<verbatim>",
    "parameters": {
        "type": "object",
        "properties": {
            "summaryIds": {"type": "array", "items": {"type": "string"}, "description": "Summary IDs to expand..."},
            "query": {"type": "string", "description": "Text query to grep for matching summaries before expanding..."},
            "maxDepth": {"type": "number", "minimum": 1, "description": "Max traversal depth per summary (default: 3)."},
            "tokenCap": {"type": "number", "minimum": 1, "description": "Max tokens across the entire expansion result."},
            "includeMessages": {"type": "boolean", "description": "Whether to include raw source messages at leaf level (default: false)."},
            "conversationId": {"type": "number"},
            "allConversations": {"type": "boolean"},
        },
        "required": [],  # at least one of summaryIds/query must be present, validated at runtime
    },
}
```

**Behavior:**
1. **Main-agent refusal (line 165):** if `!deps.isSubagentSessionKey(sessionKey)` → return `{"error": "lcm_expand is only available in sub-agent sessions..."}`. The Python port needs an equivalent `is_subagent_session_key(session_key)` helper that recognizes the sub-agent session prefix scheme (`subagent:` or whatever Hermes uses for delegated runs).
2. **Delegated grant lookup:** sub-agent session resolves the delegated expansion grant via `resolveDelegatedExpansionGrantId(sessionKey)` and wraps the orchestrator with `wrapWithAuth(orchestrator, runtimeAuthManager)`.
3. **Conversation scope** → if no conversation resolved, error out.
4. **Two entry shapes:**
   - `summaryIds`: call `runExpand({summaryIds, conversationId, maxDepth, tokenCap, includeMessages})` directly.
   - `query`: grep first (`retrieval.grep({query, mode: "full_text"})`), take the top summary IDs from results, then expand.
5. Calls `ExpansionOrchestrator.expand(...)` (`src/expansion.ts`) which walks the DAG breadth-first under the token cap; with `includeMessages`, hydrates leaf messages.
6. Output is a compact text payload + `citedIds` array for the sub-agent to cite back.

**Cross-tool dependencies:**
- `src/expansion-auth.ts` — runtime auth manager (in-memory grant ledger; needs Python equivalent)
- `src/expansion.ts` — `ExpansionOrchestrator`, `distillForSubagent`
- `src/expansion-policy.ts` — `decideLcmExpansionRouting` (route-vs-delegate decision)
- `src/tools/lcm-expand-tool.delegation.ts` — `runDelegatedExpansionLoop` (used by expand_query; not by expand itself)

**Failure modes:**
- Main-agent invocation → structured error
- Delegated session with no grant → `{"error": "Delegated expansion requires a valid grant..."}`
- Grant budget exhausted mid-expansion → `ExpansionOrchestrator` truncates; output flagged `truncated: true`

**Token-budget gating:** **NOT wrapped** in `runWithTokenGate`. Has its own grant ledger (sub-agent-scoped). The needs-compact-gate.ts docstring (line 38) explicitly skips this tool.

---

### `lcm_expand_query`

**Source:** `lcm-expand-query-tool.ts` (lines 1–1467) + `lcm-expand-tool.delegation.ts` (lines 1–580)

**Description string (from the schema's `prompt` parameter at lines 44–47 and the tool's description string further down):**

The tool itself accepts a NL `prompt` plus `query`/`summaryIds` and returns a synthesized answer with citations. Main-agent entry point for "multi-hop drilldown that paraphrases" — opposite of `lcm_describe` (which is read-only).

```
Multi-hop LCM expansion that answers a natural-language prompt. Spawns a delegated sub-agent session with grant-limited budget, lets it walk the DAG via lcm_expand + lcm_describe + lcm_grep (sub-agent tool surface), and returns a synthesized markdown answer with citedIds. PRIMARY for Type F queries (synthesis with provenance): 'summarize what we discussed about X with source citations'. Use when lcm_describe one-hop is too shallow but lcm_expand bare DAG-walking is too noisy.
```

**JSON schema (lines 32–73):**

```python
LCM_EXPAND_QUERY_SCHEMA = {
    "name": "lcm_expand_query",
    "description": "<verbatim>",
    "parameters": {
        "type": "object",
        "properties": {
            "summaryIds": {"type": "array", "items": {"type": "string"}, "description": "Summary IDs to expand (sum_xxx). Required when query is not provided."},
            "query": {"type": "string", "description": "FTS5 query used to find summaries via the same full-text search path as lcm_grep before expansion..."},
            "prompt": {"type": "string", "description": "Natural-language question or task to answer using expanded context. Put the answer request here, not in query."},
            "conversationId": {"type": "number"},
            "allConversations": {"type": "boolean"},
            "maxTokens": {"type": "number", "minimum": 1, "description": "Maximum answer tokens to target (default: 2000)."},
            "tokenCap": {"type": "number", "minimum": 1, "description": "Expansion retrieval token budget across all delegated lcm_expand calls for this query."},
        },
        "required": ["prompt"],
    },
}
```

**Behavior — this is the most complex tool by far:**

1. **Recursion guard** (`evaluateExpansionRecursionGuard`): a delegated session calling `lcm_expand_query` would recurse infinitely. Hard-blocks with `EXPANSION_RECURSION_BLOCKED`.
2. **Concurrency slot** (`acquireExpansionConcurrencySlot`): one in-flight delegation per origin session. Second concurrent caller from the same origin is blocked with `EXPANSION_CONCURRENCY_BLOCKED`.
3. **Find candidate summaries:**
   - `summaryIds` mode → use directly.
   - `query` mode → `retrieval.grep({query, mode: "full_text"})` and harvest top matches.
4. **Bucket candidates by `conversationId`** (lines 340–425). Max `DEFAULT_MAX_CONVERSATION_BUCKETS = 3` buckets.
5. **For each bucket:**
   - Create a `DelegatedExpansionGrant` (token budget = `tokenCap`).
   - Build sub-agent task message (`buildDelegatedExpandQueryTask` at lines 268–332) — a detailed system prompt explaining: tools available, scope, strategy, JSON-only output requirement.
   - Dispatch via `runDelegatedExpansionLoop` (in delegation.ts) which spins up a sub-agent session and waits up to `DEFAULT_DELEGATED_WAIT_TIMEOUT_MS = 120s` (gateway timeout `GATEWAY_TIMEOUT_MS = 10s` for inner round-trips).
   - Stamp delegated context for recursion guard, record telemetry, release on completion.
6. **Parse sub-agent reply** as JSON (with fenced-code-block fallback). Validate `citedIds` against `summaries` table — drop fabricated IDs (Wave-4/6/9 anti-fabrication). Cap validation at 1000 IDs to avoid full-table scans.
7. **Merge per-bucket results** → single `ExpandQueryReply` with merged citedIds, sourceConversationIds, totalSourceTokens, and `conversationBreakdown` array.
8. **Surface fabrication counts** (`citedIdsRejectedAsFabricated`, `citedIdsExceededValidationCap`) so the agent has programmatic signal that the LLM hallucinated.

**Critical Python porting note:** The delegated sub-agent dispatch in TS goes through openclaw's `requesterAgentId` + gateway runId machinery. In Hermes the equivalent is launching a **subagent** via the `subagent` tool / `delegate_task` path. The port should map `runDelegatedExpansionLoop` onto Hermes's subagent launch primitive (see `run_agent.py` around the `_delegate_result` path at line 11231). This is the load-bearing architecture decision for the wave-2 ship of `lcm_expand_query`.

**Failure modes:**
- Recursion blocked → `{"code": "EXPANSION_RECURSION_BLOCKED", "reason": "depth_cap"|"idempotent_reentry", "message": "..."}`
- Concurrency blocked → `{"code": "EXPANSION_CONCURRENCY_BLOCKED", "reason": "origin_session_in_flight", ...}`
- Sub-agent timeout (per pass) → mark pass as `timeout`, surface in `conversationBreakdown`
- Sub-agent throws → mark pass as `error`, capture error text via `collectExpansionFailureText`
- Sub-agent returns non-JSON → `formatInvalidDelegatedReply` snippet, returned as the answer text with truncation
- Model-override unauthorized (401/403/etc.) → `shouldRetryWithoutOverride` retries without overrides
- Citedids all fabricated → empty array + `citedIdsRejectedAsFabricated > 0`

**Token-budget gating:** Wrapped in `runWithTokenGate`. Estimator: `maxTokens + 200` chars (capped at HARD_CAP).

---

### `lcm_synthesize_around`

**Source:** `lcm-synthesize-around-tool.ts` (lines 1–1477)

**Description string (verbatim, lines 641–653):**

```
Synthesize a fresh summary of leaves over a window (replaces old lcm_recent). Three modes: 'period' (date range or shortcut like 'yesterday' / 'last-7-days' / 'this-month' — target OPTIONAL; this is the direct "what did we work on yesterday" surface), 'time' (leaves within ±windowHours of a target summary's timestamp — target REQUIRED), or 'semantic' (top windowK most-similar leaves to target content/query — target REQUIRED). Period boundaries are computed in the operator's local timezone (configured on the LCM engine; handles half-hour offsets like Asia/Kolkata and DST transitions). Returns a markdown summary backed by lcm_synthesis_cache so subsequent identical calls hit the cache. The actual LLM call goes through the operator's configured summarizer chain (summaryModel/summaryProvider) for inheritance of auth retries + fallback handling; the audit table records the resolved model that actually ran (Wave-12 fix — was previously recording the dispatched recommendation). Distinct from lcm_grep --mode semantic (which returns ranked snippets, not a synthesized rollup).
```

**JSON schema (lines 61–144):**

```python
LCM_SYNTHESIZE_AROUND_SCHEMA = {
    "name": "lcm_synthesize_around",
    "description": "<verbatim>",
    "parameters": {
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "Target to anchor the window on. REQUIRED for window_kind='time' and 'semantic'..."},
            "window_kind": {"type": "string", "enum": ["time", "semantic", "period"], "description": "..."},
            "period": {"type": "string", "description": "Period shortcut for window_kind='period' (case-insensitive). Accepted: 'today'|'yesterday'|'this-week'|'last-week'|'this-month'|'last-month'|'last-7-days'|'last-30-days'|'last-Nh'|'last-Nd'..."},
            "windowHours": {"type": "number", "minimum": 1, "maximum": 672, "description": "Half-window for time mode (default 24)..."},
            "windowK": {"type": "number", "minimum": 1, "maximum": 200, "description": "Top-K for semantic mode (default 30)..."},
            "tier": {"type": "string", "enum": ["custom", "filtered"], "description": "Synthesis tier (default 'custom')..."},
            "conversationId": {"type": "number"},
            "allConversations": {"type": "boolean"},
            "since": {"type": "string", "description": "Optional ISO timestamp lower bound..."},
            "before": {"type": "string", "description": "Optional ISO timestamp upper bound..."},
        },
        "required": ["window_kind"],
    },
}
```

**Behavior:**

1. **Validate `window_kind`** — must be one of `time`/`semantic`/`period`. Target is required for time/semantic; optional for period.
2. **Numeric clamps:** `windowHours ∈ [1, 672 (4 weeks)]`, `windowK ∈ [1, 200]`.
3. **Resolve `tier`** → `'custom'` or `'filtered'`.
4. **Parse `since`/`before`** as ISO timestamps; combine with window bounds.
5. **Resolve conversation scope.**
6. **Resolve `sessionKeyForCache`** — non-trivial: walks `targetSummary?.session_key → input.sessionKey → conversation.session_key → 'agent:main:main'`. Prevents cross-session cache pollution.
7. **Pick leaves:**
   - `period`: `selectTimeWindowLeaves(db, {rangeStart, rangeEnd, scope})` — pure SQL, `kind='leaf' AND suppressed_at IS NULL AND julianday(COALESCE(latest_at, created_at)) BETWEEN ? AND ?`.
   - `time`: anchor on `targetSummary.created_at`, ±`windowHours`, then `selectTimeWindowLeaves`.
   - `semantic`: `runSemanticSearch(db, voyage, {queryText, topK: windowK, kind: 'leaf', conversationIds})`. **Requires Voyage + vec0.**
8. **Build source text** (`buildSourceText`) — concatenates leaves with `### Leaf <id> (<ts>)` separator, hard-caps at `MAX_SOURCE_TEXT_TOKENS = 50_000` (dispatch-side input cap).
9. **Cache lookup** — `SELECT * FROM lcm_synthesis_cache WHERE session_key=? AND range_start=? AND range_end=? AND leaf_fingerprint=? AND tier=? AND prompt_id=?` (single-flight via `INSERT OR IGNORE` on the UNIQUE index).
10. **If cache miss → call `dispatchSynthesis`** (`src/synthesis/dispatch.ts`) with tier + prompt + sourceText. The summarizer chain provides auth retries + fallback. `buildLlmCallFromSummarizer` wraps the summarizer for telemetry.
11. **Persist to `lcm_synthesis_cache`** (single-flight `INSERT OR IGNORE` keyed on `(session_key, range_start, range_end, leaf_fingerprint, tier, prompt_id)`).
12. **Return markdown** with rich telemetry: `window`, `tier`, `cacheHit`, `model`, `latencyMs`, `voyageTokensConsumed` (semantic only), `leafIds[]`, `totalSourceTokens`, `truncatedAt`.

**Period parser (`parsePeriodShortcut`, lines 279–433):** A self-contained timezone-aware date math utility — `getLocalDayStartUtc` (iterative-converge handles half-hour offsets + DST transitions), `getLocalDayDurationMs` (handles 23h/25h DST days). Accepts `today|yesterday|this-week|last-week|this-month|last-month|last-7-days|last-30-days|last-Nh|last-Nd`. **Python port:** use `zoneinfo` (3.9+) + `Intl`-equivalent formatting. Test against `/Volumes/LEXAR/Claude/lossless-claw/test/v41-period-timezone.test.ts` — the parser is exported for testing.

**Failure modes:**
- Invalid `window_kind` → structured error
- Missing target for time/semantic → structured error
- Invalid ISO timestamp → structured error
- since >= before → structured error
- target sum_xxx not in scope → structured error
- semantic mode with VOYAGE_API_KEY missing → `VoyageError(kind: 'auth')` → structured error suggesting fallback
- vec0 unavailable → `SemanticSearchUnavailableError` → structured error
- Synthesis dispatch fails (auth, quota, content-filter) → `SynthesisDispatchError` → structured error w/ retry hint
- Period parser unrecognized → structured error w/ examples

**Token-budget gating:** Wrapped in `runWithTokenGate` (Wave-12 W2A1 fix; previously skipped). Estimator: flat `6_000` tokens.

---

### `lcm_get_entity`

**Source:** `lcm-get-entity-tool.ts` (lines 1–342)

**Description string (verbatim, lines 123–136):**

```
Look up a NAMED entity (person, project, customer, library, identifier — things automatically extracted by the entity coreference worker) by canonical name and return its mentions across the session corpus. PRIMARY tool for Type D pattern-anchored entity queries when the user NAMES a specific entity: 'tell me about <X>', 'history of customer <Y>', 'work I've done with <library Z>'. If the user is asking a paraphrastic topic question without naming an entity ('have we discussed X-shaped problems', 'what work has been done on rate limiting'), prefer lcm_grep --mode hybrid instead — it handles paraphrase across the corpus without needing a canonical entity to exist. For browsing many entities by substring or by entity_type, use lcm_search_entities. For raw leaf content similarity (no entity needed), use lcm_grep --mode semantic.
```

**JSON schema (lines 39–67):**

```python
LCM_GET_ENTITY_SCHEMA = {
    "name": "lcm_get_entity",
    "description": "<verbatim>",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Entity name to look up. Matched COLLATE NOCASE against canonical_text..."},
            "sessionKey": {"type": "string", "description": "Session key scope. If omitted, defaults to current session's key."},
            "entityType": {"type": "string", "description": "Optional entity_type filter. Common values: 'person_name', 'pr_number', 'agent_id', 'session_key', 'command', 'file_path', 'date'..."},
            "mentionLimit": {"type": "number", "minimum": 1, "maximum": 100, "description": "default 20"},
        },
        "required": ["name"],
    },
}
```

**Behavior:**
1. Validate `name` (non-empty).
2. Resolve `effectiveSessionKey` — param wins, else `input.sessionKey`. If neither → error.
3. Optional `entity_type` filter (case-folded).
4. **Lookup entity row** via the shared `VISIBLE_MENTIONS_CTE` + `entityAggCte({includeFirstIn: true})` SQL (see `lcm-entity-shared.ts`). The CTE recomputes aggregates (`occurrence_count`, `first/last_seen_at`, `alternate_surfaces`) from UNSUPPRESSED mentions only — Wave-12 P1 fix to prevent suppressed-mention data leaking via aggregate columns.
5. **If not found:** returns `{"found": false, "fallback_suggestions": [3 suggestions]}` — the fallback suggestions are concrete: try `lcm_search_entities` with prefix mode, `lcm_grep mode=hybrid`, `lcm_grep mode=verbatim`. **Critical UX detail — keep verbatim in the port.**
6. **Mention list:** `SELECT m.* FROM lcm_entity_mentions m JOIN summaries s ON s.summary_id = m.summary_id WHERE m.entity_id=? AND s.suppressed_at IS NULL ORDER BY m.mentioned_at DESC LIMIT ?`.
7. Render as markdown with metadata + mention list. Strip canonical form from `alternateSurfaces` display.

**DB tables:** `lcm_entities`, `lcm_entity_mentions`, `summaries` (for suppression join)

**Failure modes:**
- `name` empty → error
- No session_key resolved → error
- Entity not found → `{found: false, fallback_suggestions: [...]}` (this is INTENTIONALLY indistinguishable from "all mentions suppressed" — prevents existence-probing by attackers)

**Token-budget gating:** Wrapped. Estimator: `250 + mentionLimit*110` chars.

---

### `lcm_search_entities`

**Source:** `lcm-search-entities-tool.ts` (lines 1–377)

**Description string (verbatim, lines 137–150):**

```
PRIMARY tool for entity discovery / browse — use when you DON'T know the canonical name yet, or want to see what's in the catalog. Three use modes covered by this single tool: (1) **browse by type**: pass `entityType` (e.g. 'pr_number', 'person_name', 'file_path') with no query to list all entities of a type — useful for 'what PRs have we discussed?', 'what kinds of entities are in this corpus?'; (2) **fuzzy lookup**: pass partial / approximate `query` with `mode='like'` (default, substring) or `mode='prefix'` — useful for 'I'm looking for that customer with the VM issues, can't remember the exact name' or 'show me anything starting with Voy'; (3) **catalog probe**: empty-query + entityType filter to enumerate. Returns ranked entities (occurrence_count DESC, last_seen DESC) with their type + occurrence count + last-seen time. Once you have a canonical name, follow up with `lcm_get_entity` for the full mention list. Backed by the async entity coreference worker.
```

**JSON schema (lines 43–83):**

```python
LCM_SEARCH_ENTITIES_SCHEMA = {
    "name": "lcm_search_entities",
    "description": "<verbatim>",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query... OPTIONAL when entityType is provided."},
            "mode": {"type": "string", "enum": ["like", "prefix", "exact"], "description": "..."},
            "sessionKey": {"type": "string"},
            "entityType": {"type": "string", "description": "Optional entity_type filter..."},
            "limit": {"type": "number", "minimum": 1, "maximum": 100, "description": "default 20"},
        },
        "required": [],  # validates at runtime: query required unless entityType present
    },
}
```

**Behavior:**
1. Validate query OR entityType present (line 174).
2. `escapeLike` query for SQL LIKE (escapes `%`, `_`, `\` with `ESCAPE '\'`).
3. Build SQL using `VISIBLE_MENTIONS_CTE` + `entityAggCte({includeFirstIn: false})` shared with `get_entity`. Add EXISTS guard (mention with unsuppressed summary) — defense in depth.
4. **Rank:** `ORDER BY ea.occ_count DESC, ea.last_at DESC LIMIT ?`.
5. **Empty-result catalog probe (lines 286–303):** if zero results, runs cheap `EXISTS(SELECT 1 FROM lcm_entities WHERE session_key=? LIMIT 1)` and global probe to distinguish:
   - `active` — query just didn't match
   - `empty-for-session` — worker hasn't run on this session
   - `empty-globally` — worker hasn't run on this DB at all
   Surfaces this as `catalogStatus` — important UX so the agent doesn't conclude "entity doesn't exist" when it's actually "worker hasn't run yet".

**Failure modes:**
- Both `query` and `entityType` missing → error
- No session_key resolved → error
- Empty result → still returns `{totalMatches: 0, catalogStatus: "..."}` (always successful)

**Token-budget gating:** Wrapped. Estimator: `420 + limit*85` chars.

---

### `lcm_compact`

**Source:** `lcm-compact-tool.ts` (lines 1–378)

**Description string (verbatim, lines 233–240):**

```
PROACTIVELY compact this conversation's LCM context mid-turn to free room for chained tool calls. Use sparingly: only when (a) context is already past 70% of budget AND (b) you reasonably expect 2+ more tool calls this turn AND (c) waiting for post-turn auto-compaction is not viable. DOES blocking work — typical 5-30s, runs an LLM summarization call. REFUSES if: context is below the reserveFraction floor (default 50% — no point compacting when context is roomy), engine migration failed at boot, or you've exceeded 2 calls in the last 5 minutes. DOES NOT gate on prompt-cache state — agent-triggered compaction deliberately bypasses cache deferral that the automatic threshold path uses, because the cache is hot precisely when you most need to compact. After successful compaction, the next model call will see the compacted view automatically (LCM owns context-engine reassembly between tool calls). Returns structured reason on success/failure.
```

**JSON schema (lines 117–126):**

```python
LCM_COMPACT_SCHEMA = {
    "name": "lcm_compact",
    "description": "<verbatim>",
    "parameters": {
        "type": "object",
        "properties": {
            "reserveFraction": {"type": "number", "minimum": 0.5, "maximum": 1.0, "description": "Lower bound on (currentTokens / tokenBudget)... Default 0.5..."},
        },
        "required": [],
    },
}
```

**Behavior:**
1. **Operator opt-in gate:** `cfg.agentCompactionToolEnabled` must be `true`; else returns `{ok: false, reason: "operator-disabled"}`.
2. **Engine availability:** `getLcm()` must resolve; else `{reason: "engine-unavailable"}`.
3. **Session key required:** else `{reason: "no-session"}`.
4. **Engine-side gate** (`lcm.getAgentCompactionGateState(...)`): checks reserveFraction floor, migration health, etc. If `shouldRefuse` → `{reason: gate.refusalReason, contextRatio: gate.contextRatio}`.
5. **Per-window cap** (`checkAndIncrementCounter`): in-memory `Map<sessionKey, {count, firstAt}>`. Max 2 calls per 5-min window. **Wave-12 fix:** gate-refusals are FREE (don't burn the cap). NOT durable across plugin restart.
6. **Call `lcm.compact({sessionId, sessionKey, sessionFile, tokenBudget, currentTokenCount, force: false})`** — blocking, no timeout. Honors engine-side cache-hot + threshold gates.
7. **On success:** `noteSuccessfulCompact(sessionKey)` clears the token-state cache so the next wrapped tool sees fresh ground truth (Wave-12 W2A1 P0 fix — prevented compact→refuse loops).
8. **Map engine reason** via `mapEngineReason` → tool-facing enum: `compacted|noop|auth-failure|session-excluded|no-conversation|missing-budget|partial-compact|unknown`.

**Failure modes:**
- All 8 gate states above map to structured `{ok, compacted, reason, note, contextRatio?, retryAfterIso?}` — agent-readable.
- Engine throws → `{ok: false, reason: "exception", note: error.message}`.

**Token-budget gating:** **NOT wrapped** (status response only ~150 chars). Estimator returns 150 tokens.

---

## Shared infrastructure

### `common.ts` → `tools/_common.py`

53 LOC. Re-exports `AnyAgentTool` type + provides `jsonResult(payload)` (returns `{content: [{type: "text", text: json.dumps(payload)}], details: payload}`) and `readStringParam(params, key, options)`.

Python port: a single module with `tool_result(payload: dict) -> str` (since Hermes's `handle_tool_call` returns JSON string, not a structured dict — see `run_agent.py:11249`) and helper validators.

### `lcm-conversation-scope.ts` → `tools/conversation_scope.py`

162 LOC. Two exports:
- `parseIsoTimestampParam(params, key)` → `datetime | None`, raises on invalid
- `resolveLcmConversationScope({lcm, params, sessionId, sessionKey, deps})` → `{conversationId?, conversationIds?[], allConversations: bool}`

Resolution priority (lines 92–161):
1. Explicit `params.conversationId` (number) → `{conversationId, conversationIds: [it], allConversations: false}`
2. `params.allConversations === true` → `{allConversations: true}`
3. `sessionKey` → `conversationStore.getConversationBySessionKey(sessionKey)` + `getConversationFamilyIds(...)` for session-family scoping
4. Fall through to `sessionId` lookup via the store
5. No match → `{allConversations: false, conversationId: undefined}`

The Python port wraps the SQLite conversation-store; family expansion is `SELECT conversation_id FROM conversations WHERE root_conversation_id = ?`.

### `lcm-expansion-recursion-guard.ts` → `tools/expansion_recursion_guard.py`

373 LOC. Pure in-memory state (no DB). Three maps:
- `delegatedContextBySessionKey` — `{sessionKey: DelegatedExpansionContext}`
- `blockedRequestIdsBySessionKey` — `{sessionKey: Set<requestId>}`
- `activeRequestIdByOriginSessionKey` — `{originSessionKey: requestId}`

Exports:
- `createExpansionRequestId()` → uuid
- `resolveExpansionRequestId(sessionKey)` — inherits from stamped context
- `resolveNextExpansionDepth(sessionKey)` → 1, or stamped+1
- `stampDelegatedExpansionContext({sessionKey, requestId, expansionDepth, originSessionKey, stampedBy})`
- `clearDelegatedExpansionContext(sessionKey)`
- `evaluateExpansionRecursionGuard({sessionKey, requestId})` → blocked decision at depth >= 1 with `depth_cap` or `idempotent_reentry` reason
- `acquireExpansionConcurrencySlot({originSessionKey, requestId})` / `releaseExpansionConcurrencySlot({originSessionKey, requestId})`
- `recordExpansionDelegationTelemetry({deps, component, event, ...})` — logger.info/warn with monotonic counters

Python port: a singleton module-level `RecursionGuardState` dataclass with `threading.Lock` (the TS state is single-threaded; Python may face concurrent dispatch from `asyncio` or threads). Logger calls thread through `deps.log.info`/`warn` — wire to `logging.getLogger("lcm.expansion")`.

`EXPANSION_DELEGATION_DEPTH_CAP = 1` — hard-coded. Telemetry counters reset only via `reset...ForTests()`.

### `lcm-entity-shared.ts` → `tools/entity_shared.py`

84 LOC. Exports two SQL fragments used by `get_entity` + `search_entities`:
- `VISIBLE_MENTIONS_CTE` — `WITH visible_mentions AS (SELECT m.entity_id, m.summary_id, m.surface_form, m.mentioned_at FROM lcm_entity_mentions m JOIN summaries s ON s.summary_id = m.summary_id WHERE s.suppressed_at IS NULL)`
- `entityAggCte({includeFirstIn})` — builds the `, entity_agg AS (SELECT vm.entity_id, COUNT(*) AS occ_count, MIN(...) AS first_at, MAX(...) AS last_at, [first_in subquery,] json_group_array(DISTINCT vm.surface_form) AS visible_surfaces FROM visible_mentions vm GROUP BY vm.entity_id)` clause.

Pure SQL templating, no SQL-injection surface. Port directly.

### `lcm-expand-tool.delegation.ts` → folded into `tools/expand_query.py`

580 LOC. Helpers for the delegated sub-agent loop used by `lcm_expand_query`:
- `normalizeSummaryIds(ids: string[])` — dedup + trim
- `parseDelegatedExpansionReply(rawReply)` — JSON or fenced-JSON extraction
- `runDelegatedExpansionLoop({...})` — orchestrates one bucket's worth of sub-agent passes (up to N passes, each with timeout/error/ok status)

This is the single most TS-specific piece of the porting work because it interacts with the openclaw gateway's sub-agent dispatch machinery. The Python port wires this to Hermes's `subagent`/`delegate_task` primitive instead.

### `runWithTokenGate` / needs-compact-gate

Wrapper applied to 6/8 tools (`lcm_expand` and `lcm_compact` are exempt). Located in `src/plugin/needs-compact-gate.ts`. Combines:
1. **Pre-call gate** — estimate result size with per-tool formula. If `(currentTokenCount + estimate) / tokenBudget > REFUSAL_THRESHOLD (0.92)`, return structured `{ok: false, needsCompact: true, reason: "context-overflow-prevention", projectedRatio, suggested_actions}` without running the tool.
2. **Post-call tap** — `tapResultForTokenAccounting(sessionKey, toolName, resultText)` updates the in-memory token-state cache so the next tool call sees cumulative state.

Per-tool estimators codified above. Skipped (bypassed) when `currentTokenCount` or `tokenBudget` is undefined — conservative default for early-session calls before any `llm_output` hook has fired.

**Python port options:**
- (A) Decorator `@token_gated(tool_name)` wrapping each handler
- (B) Centralized middleware in `LCMEngine.handle_tool_call` that consults `TOKEN_GATE_TOOLS` set
- **Recommended: (B)** — keeps per-tool handlers clean and the gate logic in one place. The decorator pattern leaves room for forgetting to apply it (Wave-12 F5 fix in TS was about exactly this kind of antipattern).

### `MAX_RESULT_CHARS` / `MAX_RESULT_TOKENS` / `truncationNotice`

Lives in `src/plugin/result-budget.ts` (132 LOC). Operator-tunable via `LCM_TOOL_RESULT_TOKEN_BUDGET` env (floor 2000, default 10000 tokens). Live ESM bindings — env wins over config; config can raise at init via `applyResultBudgetConfig(...)`.

**The `truncationNotice` prose is pinned by tests as part of the agent-facing contract** (Wave-12 N3 retro). Regex: `truncated at ~\d+ tokens to protect agent context`. Keep verbatim in the Python port.

---

## Tool registration in Python

```python
# In LCMEngine (subclass of ContextEngine)

TOOL_DISPATCH = {
    "lcm_grep": handle_lcm_grep,
    "lcm_describe": handle_lcm_describe,
    "lcm_expand": handle_lcm_expand,
    "lcm_expand_query": handle_lcm_expand_query,
    "lcm_synthesize_around": handle_lcm_synthesize_around,
    "lcm_get_entity": handle_lcm_get_entity,
    "lcm_search_entities": handle_lcm_search_entities,
    "lcm_compact": handle_lcm_compact,
}

TOKEN_GATE_TOOLS = {  # bypass: lcm_expand, lcm_compact
    "lcm_grep", "lcm_describe", "lcm_expand_query",
    "lcm_synthesize_around", "lcm_get_entity", "lcm_search_entities",
}

class LCMEngine(ContextEngine):
    def get_tool_schemas(self) -> list[dict]:
        return [
            LCM_GREP_SCHEMA,
            LCM_DESCRIBE_SCHEMA,
            LCM_EXPAND_SCHEMA,
            LCM_EXPAND_QUERY_SCHEMA,
            LCM_SYNTHESIZE_AROUND_SCHEMA,
            LCM_GET_ENTITY_SCHEMA,
            LCM_SEARCH_ENTITIES_SCHEMA,
            LCM_COMPACT_SCHEMA,
        ]

    def handle_tool_call(self, name: str, args: dict, **kwargs) -> str:
        handler = TOOL_DISPATCH.get(name)
        if handler is None:
            return json.dumps({"error": f"Unknown LCM tool: {name}"})

        session_key = kwargs.get("session_key") or self._current_session_key
        runtime_ctx = self.get_runtime_context(session_key)  # current_token_count + token_budget

        if name in TOKEN_GATE_TOOLS:
            return run_with_token_gate(
                tool_name=name,
                params=args,
                session_key=session_key,
                runtime_ctx=runtime_ctx,
                inner=lambda: handler(
                    args,
                    db=self.db,
                    retrieval=self.retrieval,
                    voyage=self.voyage,
                    deps=self.deps,
                    session_key=session_key,
                    runtime_ctx=runtime_ctx,
                    messages=kwargs.get("messages"),
                ),
            )
        return handler(
            args,
            db=self.db,
            retrieval=self.retrieval,
            voyage=self.voyage,
            deps=self.deps,
            session_key=session_key,
            messages=kwargs.get("messages"),
        )
```

The `run_agent.py:11249` call site already passes `messages=messages` and is the only entry point.

---

## Port order (dependency-aware)

1. **`tools/_common.py`** + **`conversation_scope.py`** + **`expansion_recursion_guard.py`** + **`entity_shared.py`** (shared helpers; ~700 LOC total)
2. **`tools/result_budget.py`** + **`needs_compact_gate.py`** + **`token_state.py`** (the wrapper infra — shared with every gated tool). Includes the `truncationNotice` verbatim string.
3. **`tools/describe.py`** — pure DB, foundational, exercises the conversation_scope + delegated-grant + truncate-to-cap surfaces. Smallest content-emitting tool.
4. **`tools/grep.py`** Wave A (regex + full_text + verbatim) — pure SQLite paths, no Voyage required.
5. **`tools/get_entity.py`** + **`search_entities.py`** — depend on entity_shared.py; pure SQLite.
6. **`tools/compact.py`** — depends on `LCMEngine.compact()`; no DB writes from the tool itself.
7. **`tools/grep.py`** Wave B (hybrid + semantic) — gated behind sqlite-vec spike 001. Add `voyage_client.py` + `hybrid_search.py` + `semantic_search.py` (the `src/embeddings/*` surface) before this lands.
8. **`tools/synthesize_around.py`** — depends on synthesis dispatcher (`src/synthesis/dispatch.ts`) AND the period parser. Test the parser standalone against `v41-period-timezone.test.ts` cases first.
9. **`tools/expand.py`** + **`tools/expand_query.py`** — depends on the expansion orchestrator (`src/expansion.ts`) AND the sub-agent dispatch wiring. This is the highest-risk port; do it last so the surface area below it has stabilized.

---

## TypeBox → Python JSON Schema translation table

| TS form | Python dict |
|---|---|
| `Type.String({description})` | `{"type": "string", "description": ...}` |
| `Type.String({description, enum: [...]})` | `{"type": "string", "enum": [...], "description": ...}` |
| `Type.Number({description, minimum, maximum})` | `{"type": "number", "minimum": ..., "maximum": ..., "description": ...}` |
| `Type.Boolean({description})` | `{"type": "boolean", "description": ...}` |
| `Type.Array(Type.X, {description})` | `{"type": "array", "items": <X>, "description": ...}` |
| `Type.Object({props})` | `{"type": "object", "properties": {...}, "required": [...]}` |
| `Type.Optional(X)` | omit key from `required` array; keep in `properties` |
| `Type.Union([Type.Literal("a"), Type.Literal("b")])` | `{"enum": ["a", "b"]}` (rare in this surface — most enums use `Type.String({enum})`) |

**Translation policy:** hand-translate (do not auto-derive). The TS schemas mix TypeBox idioms with hand-written description strings that exceed 200 chars, and several use the older `Type.String({enum})` instead of `Type.Union` — automated translation would lose nuance. Rough hand-port estimate: 1 hour total for all 8 schemas.

---

## Test inventory

| Test file | LOC | What it covers |
|---|---:|---|
| `lcm-grep-tool-hybrid.test.ts` | 419 | hybrid mode, Voyage degradation, vec0 absence, summaryKinds filter |
| `lcm-grep-verbatim-mode.test.ts` | 435 | verbatim mode, role filter, 20-cap, sanitizeFts5Pattern edge cases |
| `lcm-describe-expand-flags.test.ts` | 415 | expandChildren/expandMessages/expandMessagesOffset, suppression filter, delegated-grant redaction |
| `lcm-expand-query-tool.test.ts` | 2365 | the kitchen sink — bucket sort, sub-agent dispatch, fabrication validation, recursion guard, concurrency slot, error formatting |
| `lcm-expand-tool.test.ts` | 496 | main-agent refusal, grant lookup, conversation scope, summaryIds-vs-query entry |
| `lcm-expand-tool.delegation.test.ts` | 185 | `runDelegatedExpansionLoop`, reply parsing (fenced + bare JSON) |
| `lcm-get-entity-tool.test.ts` | 480 | suppression filter, alternate surfaces, fallback_suggestions, case-folding |
| `lcm-search-entities-tool.test.ts` | 394 | three modes (like/prefix/exact), catalogStatus probe, browse-by-type, escapeLike |
| `lcm-synthesize-around-tool.test.ts` | 757 | all three window kinds, period parser, cache hit/miss, session_key fallback chain, semantic-vec0-missing |
| `v41-lcm-compact-tool.test.ts` | 333 | engine-disabled, below-floor, per-window cap, reason mapping, gate-refusal counter exemption |
| **Total** | **6279** | |

Plus the period parser is exported separately and exercised by `v41-period-timezone.test.ts` (half-hour offsets + DST transitions).

**Python port strategy:** mirror the test names 1:1 in pytest. The TS tests are mostly behavior-driven (set up DB → call tool → assert output shape + side effects); they translate cleanly to `pytest` + fixtures.

---

## Open architecture decisions

- **ADR-TOOLS-01: TypeBox → JSON-schema translation approach.** Hand-translate vs automated. Recommend hand-translate (the descriptions are load-bearing and hand-authored).
- **ADR-TOOLS-02: Sync vs async tool handlers.** Hermes's `handle_tool_call` returns `str` (sync signature). The TS tools are all `async`. For pure-SQLite tools (describe, get_entity, search_entities, compact, grep regex/full_text/verbatim) sync is fine. For hybrid/semantic/expand_query/synthesize_around (which call out to Voyage or sub-agents), the engine needs to drive an event loop. Options:
  - (a) Make `handle_tool_call` sync but wrap I/O in `asyncio.run()` (creates a fresh loop per call — works but loses connection pooling)
  - (b) Make `handle_tool_call` sync with a long-lived background loop (preferred — matches openclaw's actor-style runtime)
  - (c) Migrate Hermes's `handle_tool_call` interface to `async def` (lots of caller-side changes — `run_agent.py:11249` would need `await`)
  - **Recommend (b)** for v1, revisit if the tool-call hot path needs more concurrency.
- **ADR-TOOLS-03: Shared `runWithTokenGate` — decorator or middleware?** Recommend middleware in `LCMEngine.handle_tool_call` (Option B above). The TS code's reason for inline-per-tool was that each TS factory is a separate function; in Python the dispatch table makes middleware trivial.
- **ADR-TOOLS-04: How does `lcm_expand_query` spawn a sub-agent in Hermes?** TS uses openclaw's gateway runId machinery. Hermes has `delegate_task` (`run_agent.py:11231`). The port needs to (1) decide if expand_query reuses delegate_task or spins its own sub-runtime, (2) plumb the grant-ledger + token-budget through, (3) thread the recursion guard's session-key stamping. This is the load-bearing decision for Wave-2 (`lcm_expand_query`). Consider a `first-principles-architectural-decision` skill pass on this.
- **ADR-TOOLS-05: Hermes runtime context plumbing for `getRuntimeContext`.** TS plumbs `currentTokenCount + tokenBudget` from an `llm_output` hook into a per-session in-memory cache. Hermes's equivalent: `ContextEngine.update_from_response(usage)` is already called after each API response (see `context_engine.py:67`). Wire the cache to mirror this — every `update_from_response` call updates the cache for the current `session_id` / `session_key`.

---

## Remaining 5% risk

1. **Sub-agent dispatch model for `lcm_expand_query`** (ADR-04 above) — the single biggest unknown. Owns ~30% of the porting work effort (~15 hrs).
2. **`isSubagentSessionKey` semantics in Hermes.** The TS `deps.isSubagentSessionKey(sessionKey)` returns true when the session is a delegated child of a parent. Hermes's session-key model is different (`agent:profile:session`). Need to define the equivalent before porting `lcm_expand` (which hard-refuses main-agent invocations).
3. **`dispatchSynthesis` port.** `lcm_synthesize_around` calls `src/synthesis/dispatch.ts` for the LLM call. That dispatcher includes tier-aware model picking, audit logging, fallback chains. Port complexity is in the synthesis epic, not the tools epic — but the tool needs the surface signature locked before it can compile.
4. **Voyage client port.** `src/voyage/client.ts` + `runHybridSearch` / `runSemanticSearch` (`src/embeddings/*`) — gated behind sqlite-vec spike 001. Once vec0 ships in Python, the Voyage HTTP client + rerank are pure HTTP and port in ~4 hours.
5. **Test-helper porting.** The TS tests use a setup harness (`makeTestDeps`, `seedConversation`, `seedSummary`, etc.) that doesn't exist in Python yet. Estimate ~5 hours to build the equivalent pytest fixtures before any tool tests can run.
6. **Tool-result truncation regex pinning.** The `truncationNotice` prose is part of the agent-facing contract. Three places must stay in lockstep: the function output, the TS test regex (`v41-tool-budget-guardrail.test.ts`), the tool-description text in `lcm-grep-tool.ts` line ~208. Port note: write a single `TRUNCATION_NOTICE_FORMAT` constant and reference it from both the runtime and the test assertion.
