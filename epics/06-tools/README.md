# Epic 06 — Agent Tools

Port the LCM agent-tool surface to Python. The tools are the model-facing API: every Hermes provider call advertises them via `LCMEngine.get_tool_schemas()`, every tool-use response is dispatched through `LCMEngine.handle_tool_call(name, args, **kwargs) -> str`. This is the surface on which agent behaviour stands or falls — every description string is load-bearing prose, every refusal path is a documented contract.

See [`docs/porting-guides/tools.md`](../../docs/porting-guides/tools.md) for the full per-tool specification.

## Goal

All 8 LCM agent tools registered via `get_tool_schemas()` and dispatched via `handle_tool_call()`. v0.1.0 ships **7 of 8** — `lcm_expand_query` is explicitly **deferred to v2 per [ADR-012](../../docs/adr/012-subagent-defer.md)** because its delegated-sub-agent dispatch is coupled to OpenClaw's plugin-initiated spawn API that has no v1 Hermes equivalent.

| Tool | v0.1.0 wave | Notes |
|---|---|---|
| `lcm_grep` (regex + full_text + verbatim) | Wave A | Pure SQLite |
| `lcm_grep` (hybrid + semantic) | Wave B | Voyage + vec0; blocks on Epic 05 |
| `lcm_describe` | Wave A | Highest blow-up-risk tool — `runWithTokenGate` mandatory |
| `lcm_expand` (primitive) | Wave A | Sub-agent-only; main-agent invocations refused |
| `lcm_synthesize_around` | Wave A/B | Period parser is its own unit; semantic window kind needs Voyage |
| `lcm_get_entity` | Wave A | DB-only |
| `lcm_search_entities` | Wave A | DB-only |
| `lcm_compact` | Wave A | Engine-side gates |
| ~~`lcm_expand_query`~~ | **DEFERRED v2** | ADR-012 |

## Deliverables

- 7 of 8 tools registered + dispatched (everything except `lcm_expand_query`).
- `tools/_common.py`, `tools/conversation_scope.py`, `tools/expansion_recursion_guard.py`, `tools/entity_shared.py` shared infrastructure modules.
- `runWithTokenGate` ported as **middleware in `LCMEngine.handle_tool_call`** per Wave-12 F5 (NOT decorator — [ADR-029](../../docs/adr/029-wave-fix-provenance.md)).
- TypeBox → Python dict translation per [ADR-016](../../docs/adr/016-typebox-translation.md) (hand-translate; verbatim description prose).
- Schema-wellformedness CI test (`jsonschema.Draft7Validator`) + fixture-comparison test against committed TS schema export.
- Description-string-verbatim linting test (model-facing prose is load-bearing).
- ~6,500 LOC in `src/lossless_hermes/tools/` mirroring TS layout per [ADR-024](../../docs/adr/024-project-layout.md).

## Dependencies

- **Epic 02 — engine skeleton** (`LCMEngine` class shell, `get_tool_schemas()` / `handle_tool_call()` ABC override surface, `get_runtime_context()` for token-state cache).
- **Epic 01 — storage** (every tool reads from `summaries`, `messages`, `lcm_entities`, `lcm_entity_mentions`, `summaries_fts`, `messages_fts`; describe and get_entity also hit the `summaries.suppressed_at` join).
- **Epic 05 — embeddings** (Wave B only: `lcm_grep` hybrid/semantic + `lcm_synthesize_around` semantic window kind require `voyage_client.py` + `hybrid_search.py` + `semantic_search.py`).
- **Epic 04 — compaction** (`lcm_compact` calls `LCMEngine.compact(...)`).

## Blocks

- **Epic 09 — eval** (the harness exercises tool dispatch end-to-end against the recall query set).

## Critical path

**NO** — this epic runs in parallel with Epic 04 (compaction) and Epic 05 (embeddings). Wave A (5 of 7 tools) is unblocked once Epic 02 + Epic 01 land. Wave B (hybrid/semantic) gates on Epic 05 but can ship as a follow-up release.

## Wave plan

**Wave A — ships v0.1.0:**
1. Shared infrastructure (`_common.py`, `conversation_scope.py`, `expansion_recursion_guard.py`, `entity_shared.py`).
2. `runWithTokenGate` middleware + result-budget plumbing.
3. `lcm_describe`, `lcm_get_entity`, `lcm_search_entities`, `lcm_compact` (DB-only).
4. `lcm_grep` (regex + full_text + verbatim modes only).
5. `lcm_synthesize_around` (period + time window kinds; period parser is its own standalone unit).
6. `lcm_expand` (primitive; sub-agent-only refusal path).
7. Description-prose verbatim linting + schema wellformedness CI.

**Wave B — ships v0.2.0 (gated on Epic 05):**
8. `lcm_grep` (hybrid + semantic modes; Voyage + vec0 paths).
9. `lcm_synthesize_around` (semantic window kind).

**Deferred to v2 (separate epic):**
- `lcm_expand_query` per ADR-012. The delegated-sub-agent loop in `lcm-expand-tool.delegation.ts` (580 LOC) does NOT port until Hermes's `delegate_task` integration model is validated.

## Estimated total effort

**3–4 weeks (~80–100 hours)**

- Wave A: ~60 hours (5 DB-only tools + grep partial + synthesize_around + expand primitive + shared infra + tests).
- Wave B: ~20 hours (hybrid/semantic paths; assumes Epic 05 already landed Voyage client + vec0 wiring).
- Contingency / test-harness build-out: ~10 hours.

Test porting dominates: the TS suite for this surface is 6,279 LOC across 10 files. Mirror 1:1 in pytest per [ADR-028](../../docs/adr/028-vitest-to-pytest.md).

## Confidence

**90%** — high confidence on every tool except `lcm_expand_query` (which is explicitly out of scope here). The remaining 10% uncertainty is concentrated in:

- The Hermes sub-agent session-key recognition predicate (`isSubagentSessionKey` equivalent) needed by `lcm_expand` (~5%).
- The Voyage client + vec0 surface for Wave B is documented but not yet built; Epic 05 may surface integration issues that bleed back into the tool layer (~3%).
- The period-parser timezone math (half-hour offsets, DST transitions) — Python's `zoneinfo` covers it but the test fixtures from `v41-period-timezone.test.ts` still need to translate cleanly (~2%).

## Issue index

| # | Title | Hours | Wave |
|---|---|---:|---|
| [06-01](06-01-typebox-translation-conventions.md) | TypeBox → Python dict translation conventions | 2 | A |
| [06-02](06-02-tool-dispatch-table.md) | `TOOL_DISPATCH` table + `handle_tool_call` dispatch | 3 | A |
| [06-03](06-03-runwithtokengate-middleware.md) | `runWithTokenGate` middleware (Wave-12 F5) | 6 | A |
| [06-04](06-04-tools-common.md) | `tools/_common.py` shared utilities | 2 | A |
| [06-05](06-05-conversation-scope.md) | `tools/conversation_scope.py` | 4 | A |
| [06-06](06-06-expansion-recursion-guard.md) | `tools/expansion_recursion_guard.py` | 5 | A |
| [06-07](06-07-lcm-describe.md) | `lcm_describe` (766 LOC) | 14 | A |
| [06-08](06-08-lcm-grep-regex-fulltext.md) | `lcm_grep` — regex + full_text + verbatim modes | 12 | A |
| [06-09](06-09-lcm-grep-hybrid-semantic.md) | `lcm_grep` — hybrid + semantic modes | 10 | B |
| [06-10](06-10-lcm-get-entity.md) | `lcm_get_entity` | 6 | A |
| [06-11](06-11-lcm-search-entities.md) | `lcm_search_entities` | 6 | A |
| [06-12](06-12-lcm-expand.md) | `lcm_expand` primitive + delegation refusal | 10 | A |
| [06-13](06-13-lcm-synthesize-around.md) | `lcm_synthesize_around` (1477 LOC) + period parser | 16 | A/B |
| [06-14](06-14-lcm-compact.md) | `lcm_compact` + engine-side gates | 6 | A |
| [06-15](06-15-tool-descriptions-verbatim.md) | Verbatim-description linting test | 3 | A |
