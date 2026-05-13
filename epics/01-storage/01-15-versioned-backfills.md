---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-01] storage: port 3 versioned backfills tracked in lcm_migration_state'
labels: 'port, epic-01-storage'
---

## Source (TypeScript)

- File: `src/db/migration.ts` — the three backfill functions per storage.md §2.1 (`lcm_migration_state` row enumeration):
  - `backfillSummaryDepths()` — algorithm_version 1
  - `backfillSummaryMetadata()` — algorithm_version 1
  - `backfillToolCallColumns()` — algorithm_version 1

Plus the fork-side helper `backfillForkRollupsSessionKeys()` (storage.md §2.10) — runs only if `lcm_rollups` table exists (Eva's fork-side legacy table; never present in upstream / lossless-hermes installs — but the helper must be ported in case a user imports a fork-side DB).

Plus the `messages.identity_hash` rehash loop for legacy NULL/empty rows per spike-003 §"Remaining 5% risk" row 3 + `src/db/migration.ts:326-344`.

## Target (Python)

- File: `src/lossless_hermes/db/migration.py` (additive — extends the file from #01-04 / #01-05 / #01-06)
- Estimated LOC: ~400 (the backfill functions + the BACKFILL_LADDER constant + the `_step_at_or_above` / `_record_step_completion` helpers).

## What this issue covers

The three versioned backfills (per storage.md §2.1 `lcm_migration_state` row 12 + ADR-026 §Decision) and the related identity-hash rehash loop.

### The BACKFILL_LADDER pattern

Per ADR-026 §Decision:

```python
BACKFILL_LADDER: list[tuple[str, int, Callable[[Connection], None]]] = [
    ("backfillSummaryDepths", 1, _backfill_summary_depths),
    ("backfillSummaryMetadata", 1, _backfill_summary_metadata),
    ("backfillToolCallColumns", 1, _backfill_tool_call_columns),
]
```

The orchestrator (`run_lcm_migrations`):

```python
for step_name, version, fn in BACKFILL_LADDER:
    if _step_at_or_above(conn, step_name, version):
        continue
    fn(conn)
    _record_step_completion(conn, step_name, version)
```

`_step_at_or_above(conn, step_name, version)` queries `lcm_migration_state` and returns True if a row exists with `step_name == ? AND algorithm_version >= ?`.

`_record_step_completion(conn, step_name, version)` upserts via `INSERT INTO lcm_migration_state (step_name, algorithm_version) VALUES (?, ?) ON CONFLICT (step_name, algorithm_version) DO UPDATE SET completed_at = datetime('now')`.

### Per-backfill behavior

1. **`_backfill_summary_depths(conn)`** — walks the `summaries` table and computes `depth` for every row where it's NULL or stale. Algorithm: leaf summaries (`kind='leaf'`) get `depth=0`; condensed summaries get `depth = max(parent_depths) + 1` via a recursive CTE walk over `summary_parents`. Updates `summaries.depth` in place. Idempotent: re-running computes the same depths.

2. **`_backfill_summary_metadata(conn)`** — walks the `summaries` table and computes `earliest_at`, `latest_at`, `descendant_count`, `descendant_token_count` from the descendant leaves' message data. Updates rows in place. Idempotent.

3. **`_backfill_tool_call_columns(conn)`** — walks `message_parts WHERE part_type='tool' AND tool_call_id IS NULL` and extracts `tool_call_id` from the `metadata` JSON blob (try keys in order: `metadata.toolCallId`, `metadata.raw.id`, `metadata.raw.call_id`, `metadata.raw.toolCallId`, `metadata.raw.tool_call_id` per storage.md §2.1 row 4). Also backfills `tool_name`, `tool_input` from `metadata`. Updates in place. Idempotent.

4. **`_backfill_fork_rollups_session_keys(conn)`** — **only runs if `lcm_rollups` table exists** (probe via `sqlite_master`). For Eva's fork-side data; safe no-op on upstream / lossless-hermes DBs. **This is not in the BACKFILL_LADDER** because it's conditional on table existence; runs once unconditionally inside `run_lcm_migrations` (idempotent by structure).

5. **`_rehash_legacy_identity_hashes(conn)`** — walks `messages WHERE identity_hash IS NULL OR identity_hash = ''` and recomputes via `build_message_identity_hash(role, content)` from #01-07. Updates in place. Spike-003 confirms this is byte-identical to Node's recompute path — no drift on re-import from `~/.openclaw/lcm.db`. **Not in the BACKFILL_LADDER** (it's structurally a one-shot for legacy data; doesn't have algorithm-version semantics). Runs once unconditionally; idempotent because hash recomputation on already-correct rows is a no-op.

### Idempotency invariant (per ADR-026 §Consequences)

Every backfill function MUST be idempotent (safe to re-run with the row already at the latest version). Tests must include "run twice" coverage.

### Concurrent-migration safety

Per ADR-026 §"Open questions" #2: backfills run inside the `BEGIN EXCLUSIVE` wrapping `run_lcm_migrations` (#01-04). Two concurrent `run_lcm_migrations` calls don't race — the second waits for the first to commit and then sees the ledger rows.

## Dependencies

- Depends on: #01-04 (the `lcm_migration_state` table itself, plus `summaries`, `message_parts`, `messages`), #01-07 (message_identity for rehash).
- Blocks: nothing downstream — these are the tail of the migration ladder.

## Acceptance criteria

- [ ] `BACKFILL_LADDER` constant declared with all 3 versioned steps at `algorithm_version=1`.
- [ ] `_step_at_or_above(conn, step_name, version)` returns False on a fresh DB; True after one `_record_step_completion` call.
- [ ] **Idempotency test:** run `run_lcm_migrations(conn)` twice on a fresh DB. After the first call, all 3 ledger rows exist. After the second call, the same 3 rows exist (no duplicates, no errors), and the backfill functions are NOT called the second time (verified via patched-callable counter).
- [ ] **Algorithm-version-bump test:** manually set one ledger row to `algorithm_version=0`. Run migration. Assert the corresponding backfill function ran exactly once and the row was upserted to version 1.
- [ ] **Pre-existing data test:** on a DB with `summaries` rows that have NULL `depth`, NULL `earliest_at`, etc., run migration. Assert the depths/metadata are correctly computed (4-level pyramid test fixture).
- [ ] **Tool-call extraction test:** insert a `message_parts` row with `part_type='tool'`, `tool_call_id=NULL`, `metadata='{"raw":{"call_id":"call_abc"}}'`. Run migration. Assert `tool_call_id='call_abc'` after.
- [ ] **Legacy fork-side test:** create a DB with the `lcm_rollups` table (mimicking Eva's fork-side shape). Run migration. Assert `_backfill_fork_rollups_session_keys` ran and the session_keys column is populated.
- [ ] **Identity-hash rehash test:** insert a `messages` row with `identity_hash=NULL`, `role='user'`, `content='hello'`. Run migration. Assert `identity_hash = '87ce4613405ac8c20165d125a5c2219e8b38a9e030616dffd73a89faaf7293c8'` (spike-003 case #2 — byte-identical to Node).
- [ ] All TS test cases relevant to these backfills (subset of `test/migration.test.ts` per storage.md §8 row 2 — "depth backfill, identity hashes, FTS recreate" — the depth + identity portion is here; FTS recreate is in #01-05) port to `tests/test_migration.py::test_backfill_*`.
- [ ] `pytest tests/test_migration.py::test_backfill_*` passes.
- [ ] `mypy --strict` passes.
- [ ] PR description cites LCM commit `1f07fbd`, `src/db/migration.ts:326-344` (identity rehash), and ADR-026 §Decision.

## Estimated effort

**6–8 hours.**

## Confidence

**92%** — backfill algorithms are documented in storage.md §2.1 (the lcm_migration_state row). Residual risk: (a) the `tool_call_id` extraction from `metadata.raw.*` JSON has multiple key-precedence rules that need exact TS-vs-Python parity; (b) the recursive depth computation under a wide pyramid (>1000 leaves) needs perf sanity (port the TS test fixture for that case).
