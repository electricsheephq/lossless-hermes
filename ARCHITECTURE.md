# Architecture: lossless-hermes

> **Audience:** anyone (human or agent) picking up Phase-2 execution.
> **Source of truth:** this file references everything; if anything contradicts an [ADR](./docs/adr/), the ADR wins.

## One-paragraph summary

`lossless-hermes` is a **Python port** of [Martian-Engineering/lossless-claw](https://github.com/Martian-Engineering/lossless-claw) — a Lossless Context Management plugin — from its native TypeScript/OpenClaw runtime to the Python/Hermes-agent runtime. It is distributed as a Hermes plugin (entry-point group `hermes_agent.plugins`) that registers a `ContextEngine` subclass plus a `/lcm` slash command. The plugin owns its own SQLite database at `$HERMES_HOME/lossless-hermes/lcm.db` (27 tables, FTS5 + sqlite-vec), maintains a lossless **depth-aware summary DAG** (raw messages → leaf summaries → condensed summaries) of conversation history, and exposes 7 agent tools (deferring `lcm_expand_query` to v2 per [ADR-012](./docs/adr/012-subagent-defer.md)) for recall, synthesis, and entity lookup.

## Why this exists

- **OpenClaw is having stability issues**; the team wants to evaluate Hermes as a successor.
- LCM is the team's biggest piece of original IP on top of OpenClaw — moving it forward is the highest-leverage migration.
- Hermes's `ContextEngine` ABC was [explicitly designed](https://github.com/NousResearch/hermes-agent/blob/main/agent/context_engine.py#L5) with LCM as a planned tenant. The fit is structural.
- Phase 1 (this work) verified feasibility at 95% confidence; Phase 2 is execution.

## What "lossless" means

Raw `messages` rows and `summaries.kind='leaf'` rows are **never byte-deleted** during normal operation. The depth-aware summary DAG is built on top, not in place of. Operator deletes are soft (`suppressed_at` column, filtered through 10+ read paths). Agent-visible recall via `lcm_grep --mode verbatim` always returns the original message text.

## High-level system diagram

```
┌──────────────────────────────────────────────────────────────────┐
│                       HERMES-AGENT (Python host)                 │
│                                                                  │
│   run_agent.py                                                   │
│     ├── post_llm_call hook ──────┐                               │
│     ├── pre_llm_call hook ───┐   │                               │
│     ├── on_session_start   ┐ │   │                               │
│     ├── handle_tool_call ┐ │ │   │                               │
│     └── compress()    ┐  │ │ │   │                               │
│                       │  │ │ │   │                               │
└───────────────────────┼──┼─┼─┼───┼───────────────────────────────┘
                        ▼  ▼ ▼ ▼   ▼
┌──────────────────────────────────────────────────────────────────┐
│                  LCM-HERMES PLUGIN (this repo)                   │
│                                                                  │
│   LCMEngine (ContextEngine subclass)                             │
│   ├─ engine/lifecycle.py        on_session_*, register hooks     │
│   ├─ engine/ingest.py           diff-on-each-turn ingest         │
│   ├─ engine/assemble.py         per-turn substitution            │
│   ├─ engine/compact.py          leaf + condensed passes          │
│   │                                                              │
│   ├─ assembler.py     ContextAssembler (budget + #628 stub-tier) │
│   ├─ compaction.py    CompactionEngine                           │
│   ├─ summarize.py     LcmSummarizer (LLM seam → Hermes llm)      │
│   │                                                              │
│   ├─ tools/  ────  lcm_grep, lcm_describe, lcm_expand, ...       │
│   ├─ extraction/   entity coref worker                           │
│   ├─ synthesis/    on-demand tiered synthesis                    │
│   ├─ embeddings/   Voyage HTTP + sqlite-vec store + worker       │
│   ├─ operator/     /lcm health, purge, reconcile, doctor, eval   │
│   ├─ doctor/       summary repair + DB-wide cleaners             │
│   ├─ eval/         recall + drift                                │
│   │                                                              │
│   └─ db/  store/   SQLite (27 tables, FTS5, sqlite-vec)          │
└────────────────────────────┬─────────────────────────────────────┘
                             │
                             ▼
                ┌─────────────────────────┐
                │ $HERMES_HOME/           │
                │   lossless-hermes/      │
                │     lcm.db   (SQLite)   │
                │     large-files/<conv>/ │
                │     credentials/        │
                │     backups/            │
                └─────────────────────────┘
                             │
                             ▼  (HTTPS)
                ┌─────────────────────────┐
                │  Voyage AI              │
                │    /v1/embeddings       │
                │    /v1/rerank           │
                └─────────────────────────┘
```

## Subsystem map

| Subsystem | Source TS | Python target | Epic | Confidence |
|---|---|---|---|---|
| Entry / plugin registration | `index.ts`, `src/plugin/index.ts` | [`src/lossless_hermes/__init__.py`](./epics/02-engine-skeleton/02-07-hook-registrations.md) | 02 | 95% |
| Engine orchestrator | `src/engine.ts` (8731 LOC) | `src/lossless_hermes/engine/` (mixin pattern per [ADR-027](./docs/adr/027-engine-splitting.md)) | 02-04 | 90% |
| Storage layer | `src/db/`, `src/store/` | `src/lossless_hermes/db/`, `store/` | 01 | 95% |
| Assembler + #628 stub-tier | `src/assembler.ts` | `src/lossless_hermes/assembler.py` (stub-tier deferred to v0.2.0 per [ADR-030](./docs/adr/030-pr-628-stub-tier-deferred.md)) | 03 | 90% |
| Compaction + summarize | `src/compaction.ts`, `summarize.ts` | `src/lossless_hermes/compaction.py`, `summarize.py` | 04 | 90% |
| 8 agent tools | `src/tools/` | `src/lossless_hermes/tools/` | 06 | 90% |
| Embedding pipeline | `src/voyage/`, `embeddings/`, `concurrency/` | `src/lossless_hermes/voyage/`, `embeddings/`, `concurrency/` | 05 | 95% |
| Entity extraction | `src/extraction/` | `src/lossless_hermes/extraction/` | 07 | 88% |
| Synthesis dispatch | `src/synthesis/` | `src/lossless_hermes/synthesis/` | 07 | 85% |
| Operator commands | `src/operator/`, `src/plugin/lcm-command.ts` | `src/lossless_hermes/operator/`, `plugin/commands.py` | 08 | 90% |
| Doctor | `src/plugin/lcm-doctor-*.ts` | `src/lossless_hermes/doctor/` | 08 | 88% |
| Eval | `src/eval/` | `src/lossless_hermes/eval/` | 09 | 90% |

## Critical interfaces

### Hermes hook contract (in)

| Hermes mechanism | LCM use | ADR |
|---|---|---|
| `ContextEngine` ABC | Subclass as `LCMEngine` | [027](./docs/adr/027-engine-splitting.md) |
| `register(ctx: PluginContext)` entry point | Plugin registration | [001](./docs/adr/001-plugin-distribution-model.md) |
| `post_llm_call` hook | Per-turn message ingest (diff against last_seen_idx) | [009](./docs/adr/009-per-message-ingest.md) |
| `pre_llm_call` hook | Inject LOSSLESS_RECALL_POLICY_PROMPT into user message | [014](./docs/adr/014-recall-policy-injection.md) |
| `compress(messages, current_tokens, focus_topic)` | Compaction + always-on substitution | [010](./docs/adr/010-always-on-assembly.md) |
| `get_tool_schemas()` + `handle_tool_call(name, args, **kwargs)` | 7 LCM tools | [016](./docs/adr/016-typebox-translation.md) |
| `ctx.register_command("lcm", handler, ...)` | `/lcm` slash subcommand router | [013](./docs/adr/013-owner-gating.md) |

### LCM internal data contract

- **Identity:** `identity_hash = sha256(role + 0x00 + content)` — byte-identical TS/Go/Python (verified by [spike 003](./docs/spike-results/003-identity-hash.md))
- **Summary-DAG invariant:** `messages` and `summaries.kind='leaf'` are append-only + soft-suppress; `summaries.kind='condensed'` may be superseded by deeper passes
- **Context manifest:** `context_items` row sequence drives what the assembler sees
- **Embedding contract:** only `summaries.kind='leaf'` are embedded (Voyage `voyage-4-large`, 1024-dim, into per-model `vec0` virtual tables)
- **Cache contract:** `lcm_synthesis_cache` keyed by 7-tuple `(session_key, range_start, range_end, leaf_fingerprint, grep_filter, tier_label, prompt_id)` per [Wave-10 P1 fix](./docs/adr/029-wave-fix-provenance.md)

## Data flow walkthroughs

### New message → DB
1. User sends a message to Hermes
2. Hermes invokes LLM, gets response, appends both turns to `messages` list
3. Hermes fires `post_llm_call` hook with `conversation_history=list(messages)`
4. `LCMEngine._on_post_llm_call` diffs `messages[last_seen_idx:]` and ingests new rows
5. `ConversationStore.append_message` writes `messages` + `message_parts` rows + identity hash
6. Large blocks externalize to `large_files/<conv_id>/`
7. `lcm_extraction_queue` enqueues new leaf for entity extraction
8. `afterTurn`-equivalent (inside `_on_post_llm_call`): if raw tokens outside fresh tail exceed `leafChunkTokens`, kick incremental compaction

### Token threshold hit → compaction
1. Hermes calls `should_compress(prompt_tokens)` after each API response
2. Returns True (over threshold)
3. Hermes calls `compress(messages, current_tokens, focus_topic)`
4. LCMEngine acquires per-session async lock
5. `CompactionEngine.evaluate()` decides leaf-pass vs condensation
6. LLM call via `LcmSummarizer.summarize_leaf()` (or condensation)
7. Persist `summaries` row + `summary_messages` links inside fresh tx (NO LLM call inside tx)
8. Replace message range in `context_items` with summary ref
9. Return assembled messages

### Agent calls `lcm_grep --mode hybrid`
1. Hermes parses tool call, dispatches via `handle_tool_call("lcm_grep", args, messages=...)`
2. `LCMEngine` routes to `tools.grep.handle()` via `TOOL_DISPATCH`
3. `runWithTokenGate` middleware ([Wave-12 F5](./docs/adr/029-wave-fix-provenance.md)) checks budget; refuses if over-critical
4. Parallel `asyncio.gather([fts_search(50), semantic_search(50)])`
5. Dedupe by `summary_id`
6. Voyage `/v1/rerank` call with merged candidates
7. Return top-N ranked hits + JSON-stringified citations

## Filesystem layout (target Python)

```
src/lossless_hermes/
├── __init__.py                 # register(ctx) entry point
├── hermes_bridge.py            # replaces openclaw-bridge.ts (~30 LOC)
├── types.py                    # shared types
├── log.py                      # logging shim
├── estimate_tokens.py
├── engine/
│   ├── __init__.py             # LCMEngine class (mixin host)
│   ├── lifecycle.py            # on_session_*, hooks
│   ├── ingest.py               # _on_post_llm_call, diff-on-turn
│   ├── assemble.py             # preassemble / always-on substitution
│   └── compact.py              # compress() impl
├── assembler.py
├── compaction.py
├── summarize.py
├── retrieval.py
├── large_files.py
├── integrity.py
├── prune.py
├── transcript_repair.py
├── transaction_mutex.py
├── session_patterns.py
├── expansion.py
├── expansion_auth.py
├── expansion_policy.py
├── db/
│   ├── connection.py           # WAL + PRAGMAs + sqlite-vec load
│   ├── config.py               # LcmConfig pydantic model
│   ├── features.py             # runtime probes (vec0, trigram)
│   └── migration.py            # 27 tables, 42 indexes, versioned backfills
├── store/
│   ├── conversation.py
│   ├── summary.py
│   ├── message_identity.py
│   ├── compaction_telemetry.py
│   ├── compaction_maintenance.py
│   ├── fts5_sanitize.py
│   ├── full_text_sort.py
│   ├── full_text_fallback.py
│   ├── parse_utc_timestamp.py
│   └── conversation_scope.py
├── tools/
│   ├── _common.py
│   ├── _period_parser.py       # standalone timezone-aware parser
│   ├── conversation_scope.py
│   ├── expansion_recursion_guard.py
│   ├── grep.py
│   ├── describe.py
│   ├── expand.py
│   ├── get_entity.py
│   ├── search_entities.py
│   ├── synthesize_around.py
│   └── compact.py
├── embeddings/
│   ├── store.py                # vec0 wrapper
│   ├── backfill.py             # async worker
│   ├── semantic_search.py
│   └── hybrid_search.py
├── voyage/
│   └── client.py               # httpx async client
├── concurrency/
│   ├── worker_loop.py
│   ├── worker_lock.py
│   └── model.py
├── extraction/
│   ├── coreference.py
│   └── llm_extractor.py
├── synthesis/
│   ├── dispatch.py
│   ├── prompt_registry.py
│   └── seed_prompts.py
├── operator/
│   ├── purge.py
│   ├── health.py
│   ├── reconcile.py
│   ├── backfill_autostart.py
│   ├── extraction_autostart.py
│   ├── eval_runner.py
│   ├── semantic_infra.py
│   └── worker_orchestrator.py
├── doctor/
│   ├── shared.py
│   ├── apply.py
│   ├── cleaners.py
│   └── contract.py             # pydantic models
├── plugin/
│   ├── commands.py             # /lcm subcommand dispatcher
│   ├── shared_init.py
│   └── db_backup.py
└── eval/
    ├── run.py
    ├── recall.py
    ├── judge.py
    ├── query_set.py
    └── drift.py

tests/
├── _matchers.py                # asymmetric pytest matchers
├── conftest.py                 # fixtures (db, fake_voyage, fake_llm)
├── fixtures/
│   ├── v41_mock_llm.py
│   ├── v41_test_corpus.py
│   └── v41_tool_harness.py
└── test_*.py                   # 113 files mirroring TS tests
```

## Total scope

| Metric | Count |
|---|---|
| TS source LOC | ~48,800 |
| Python target LOC | ~38,000–42,000 |
| Python source files | ~70 |
| Tests | 113 files / ~1,595 cases |
| SQLite tables | 27 |
| SQLite indexes | 42 |
| Agent tools | 7 (v0.1.0) + 1 deferred (v2) |
| Slash subcommands | 17 |
| ADRs | 30 |
| Spikes | 5 |
| Epics | 10 |
| Issues | 122 |
| Estimated effort | 600-900 hours (3-5 months one engineer) |

## Cross-references

- [`ROADMAP.md`](./ROADMAP.md) — milestones, critical path, calendar
- [`docs/risks.md`](./docs/risks.md) — consolidated risk register
- [`docs/adr/`](./docs/adr/) — 30 architecture decision records
- [`docs/spike-results/`](./docs/spike-results/) — 5 de-risking spikes
- [`docs/porting-guides/`](./docs/porting-guides/) — 10 per-subsystem porting guides
- [`docs/reference/`](./docs/reference/) — hooks reference, source map, dependencies
- [`epics/`](./epics/) — 10 epic READMEs + 122 issue specs
