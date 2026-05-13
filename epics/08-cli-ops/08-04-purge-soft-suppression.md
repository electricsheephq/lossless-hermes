---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-08] cli-ops: port purge.ts + 6-step soft-suppression cascade'
labels: 'port, epic-08-cli-ops'
---

## Source (TypeScript)

- File: `src/operator/purge.ts`
- Lines: 390 LOC
- Function(s)/class(es): `runPurge(params): PurgeResult`, `previewPurgeAffected(params)`, `PurgeError`, internal helpers `_buildPurgeCriteria`, `_resolveTargetLeafIds`, `_writePurgeAudit`

## Target (Python)

- File: `src/lossless_hermes/operator/purge.py`
- Estimated LOC: ~440

## What this issue covers

The 6-step soft-suppression cascade — the heart of `/lcm purge`. This is NOT a hard delete; the suppressed rows stay in the DB, but `suppressed_at` is set so all 45 read paths exclude them. Per doctor-ops.md §"Prune cascade":

> **The `mode='immediate'` hard-delete drainer was REMOVED in the first-principles pass (2026-05-06).** `runPurge` always returns `mode: "soft"`.

The full cascade in one `BEGIN IMMEDIATE` transaction (per doctor-ops.md §"runPurge SOFT SUPPRESSION" lines 261–268):

1. **`summaries.suppressed_at = datetime('now')`** + `summaries.suppress_reason = ?` for matched leaf summary IDs. This UPDATE fires the per-model vec0 trigger `lcm_embed_suppress_<slug>` (created by `ensureEmbeddingsTable` in `src/embeddings/store.ts:232`), which updates the metadata col on the per-model `lcm_embeddings_<slug>` vec0 table (`suppressed=1`) so semantic search filters them out automatically.
2. **`summaries.contains_suppressed_leaves = 1`** for condensed summaries whose `summary_parents.parent_summary_id` is one of the suppressed leaves. Flags them for idle rebuild.
3. **DELETE `context_items` WHERE `item_type='summary' AND summary_id IN (...)`** — removes the assembler's pointer so the suppressed summary cannot be re-emitted into the prompt.
4. **DELETE `context_items` WHERE `item_type='message' AND message_id IN (SELECT message_id FROM summary_messages WHERE summary_id IN (...))`** — cuts the message-level pointer for the same reason.
5. **UPDATE `messages.suppressed_at = datetime('now')`** for messages linked via `summary_messages` to suppressed leaves — **gated by `NOT EXISTS` on any non-suppressed referencing summary outside the purge set**, so a message shared with a non-purged leaf is not orphaned.
6. **DELETE `lcm_synthesis_cache` WHERE `cache_id IN (SELECT DISTINCT cache_id FROM lcm_cache_leaf_refs WHERE leaf_summary_id IN (...))`** — invalidates rebuildable synthesis caches that referenced the suppressed leaves. (The cache schema's `ON DELETE CASCADE` only fires on hard DELETE; soft suppression must do this explicitly.)

CLI surface (per plugin-glue.md §"/lcm slash commands — full inventory" line 438):

```
/lcm purge --reason "..." [--session-key <k>] [--summary-ids id1,id2] [--since <iso>] [--before <iso>] [--min-token-count <n>] [--allow-main-session] [--apply]
```

- `--reason` is **required** (quoted string, free text; written to `summaries.suppress_reason` and `lcm_session_key_audit.reason`).
- At least one scope criterion must be specified (one of `--session-key`, `--summary-ids`, `--since`, `--before`, `--min-token-count`).
- `--allow-main-session` is required to target a session_key matching `agent:main:thread:*` (Eva's primary thread; the safeguard prevents accidental destructive operations on the operator's active conversation).
- `--apply` commits the cascade. Default is dry-run — `previewPurgeAffected` returns counts only.

Audit row: every applied purge writes to `lcm_session_key_audit` (existing table from Epic 01-06) with `action='purge'`, `session_key=<from-criteria>`, `reason=<reason>`, `affected_count=<count>`, `host=<hostname>`, `applied_at=now()`.

## Dependencies

- Depends on: #08-01 (dispatcher), Epic 01-06 (schema — `summaries.suppressed_at`, `summaries.suppress_reason`, `summaries.contains_suppressed_leaves`, `messages.suppressed_at`, partial indexes, `lcm_embed_suppress_<slug>` triggers all created in 01-06), Epic 01-09 (`SummaryStore`).
- Blocks: every soft-suppression-aware read path (Epic 03 assembler, Epic 04 compaction, Epic 05 embeddings/semantic-search, Epic 06 tools, Epic 07 entity coreference). The invariant test `tests/v41/test_suppression_invariants.py` is owned by Epic 08 but consumed by those epics.

## Acceptance criteria

- [ ] `run_purge(params: PurgeParams) -> PurgeResult` ports `runPurge` 1:1 with the same return shape (`mode: "soft"`, `affected_leaves`, `affected_messages`, `affected_condensed`, `affected_caches`, `audit_id`).
- [ ] `preview_purge_affected(params)` is the dry-run path; returns the same counts but writes nothing.
- [ ] All 6 cascade steps fire in one `BEGIN IMMEDIATE` (assertable via a single-shot transaction monitor).
- [ ] Step 5's `NOT EXISTS` clause is correct — a message referenced by both a purged leaf AND a non-purged leaf is NOT suppressed.
- [ ] Step 6's cache invalidation uses `lcm_cache_leaf_refs` (the existing junction table from Epic 07).
- [ ] `--reason` is mandatory; missing it raises `PurgeError` with a clear message.
- [ ] At least one scope criterion is required; an empty params set raises `PurgeError`.
- [ ] `--allow-main-session` flag is required to target `agent:main:thread:*` session keys.
- [ ] Audit row is written to `lcm_session_key_audit` on `--apply`.
- [ ] The 6-step cascade always returns `mode: "soft"`; there is no `mode: "immediate"` code path (per doctor-ops.md §"Prune cascade" line 270).
- [ ] All TS test cases in `test/operator-purge.test.ts` have ported pytest equivalents in `tests/operator/test_purge.py`.
- [ ] **New test:** `tests/operator/test_purge.py::test_cascade_full_six_steps` (per Epic README "Verification gates" #4) — runs `runPurge` against a seeded fixture (20 leaves, 5 condensed, 10 messages, 3 caches) and asserts each of the six cascade steps fired correctly.
- [ ] **New test:** `tests/operator/test_purge.py::test_message_shared_with_unsuppressed_leaf_not_purged` — step 5's `NOT EXISTS` invariant.
- [ ] **New test:** `tests/operator/test_purge.py::test_allow_main_session_required` — purging `agent:main:thread:foo` without the flag returns an error.
- [ ] **New test:** `tests/v41/test_suppression_cascade_trigger.py` (port of `test/v41-suppression-cascade-trigger.test.ts`) — the `lcm_embed_suppress_<slug>` trigger fires correctly on the UPDATE.
- [ ] Function signatures match the spec in [docs/porting-guides/doctor-ops.md](../../docs/porting-guides/doctor-ops.md) §"Operator modules" line 307.
- [ ] `pytest tests/operator/test_purge.py tests/v41/test_suppression_cascade_trigger.py` passes.
- [ ] No new mypy errors (`mypy --strict src/lossless_hermes/operator/purge.py`).
- [ ] PR description cites LCM commit `1f07fbd` (pr-613 head).

## Estimated effort

**10 hours.**

## Confidence

**90%** — the cascade is well-specified in the porting guide. 10% risk lives in step 5's `NOT EXISTS` correctness on edge cases (messages referenced by 3+ leaves, mixed suppress states), validated by the dedicated test.
