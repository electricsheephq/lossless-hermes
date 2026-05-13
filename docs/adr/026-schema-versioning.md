# ADR-026: Schema versioning strategy

**Status:** Accepted
**Date:** 2026-05-13
**Confidence:** 95%
**Supersedes:** —
**Superseded by:** —

## Context

LCM's storage layer tracks schema state via two mechanisms (see `docs/porting-guides/storage.md` §2.5 — `lcm_migration_state` table; lines 585–587):

1. **Structural state** — implicit, derived from the live schema. `runLcmMigrations()` uses `PRAGMA table_info(...)`, `PRAGMA index_list(...)`, and `sqlite_master` queries to determine whether a column/index/table exists; ALTER TABLE / CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS are inherently idempotent.
2. **Algorithm-versioned state** — explicit, recorded in `lcm_migration_state(step_name TEXT PRIMARY KEY, algorithm_version INTEGER NOT NULL, completed_at TEXT NOT NULL DEFAULT datetime('now'))`. Today's known rows: `backfillSummaryDepths v1`, `backfillSummaryMetadata v1`, `backfillToolCallColumns v1`.

The constraint forcing a choice: should `lossless-hermes` introduce a third mechanism — a monotonic integer `schema_version` (à la Django, Rails, Alembic) that increments on every migration — or preserve LCM's existing two-mechanism design?

Storage porting guide flags this as an open question (`docs/porting-guides/storage.md` §"ADR-004: lcm_migration_state and versioned-backfill strategy", lines 619–621):

> **Recommendation:** **Keep as-is.** It's the minimum viable invariant — additive forward migrations work without it; only algorithm-version semantics need it. Adding a monotonic version is breakage-driven (re-evaluate when a future change actually requires it).

## Options considered

### Option A: Keep LCM's two-mechanism design verbatim

- Description: Port `lcm_migration_state(step_name, algorithm_version, completed_at)` 1:1. Use structural probes (`PRAGMA table_info`, `PRAGMA index_list`, `sqlite_master`) for additive migrations. Use `lcm_migration_state` rows only when a backfill has algorithm-version semantics (the algorithm changed and the row needs reprocessing).
- Pros:
  - **Converted OpenClaw users' rows continue to be honored.** A user migrating from `lossless-claw` via ADR-025's import command has existing `backfillSummaryDepths=1` rows; the Python port reads them and skips the backfill. Byte-compatible.
  - Structural reconciliation handles 95% of migrations without ledger overhead.
  - No "phantom rollback" problem: if a column was added in TS v0.5 and the Python port runs on a TS v0.7 DB, the column already exists → ALTER TABLE IF NOT EXISTS is a no-op. No drift in either direction.
  - LCM v4.1 has been in production for 6+ months; the design works.
- Cons:
  - No single answer to "what version is this DB?" — operators must enumerate present columns/rows to know. (In practice, `/lcm doctor` reports this; not a real operator pain point.)
  - A future hostile change (e.g. column type change requiring data conversion) cannot be detected by structural probe alone. Would need a new `lcm_migration_state` step.
- Evidence cited:
  - `docs/porting-guides/storage.md` line 585–587 — exact behavior + the three known rows.
  - `docs/porting-guides/storage.md` line 619–621 — recommendation to keep as-is.
  - `docs/porting-guides/storage.md` line 572 — "TS migration is fully idempotent".
  - `docs/porting-guides/storage.md` line 587 — "If the import-from-OpenClaw step finds rows already at v1, those backfills are skipped — exactly the desired behavior."

### Option B: Introduce a monotonic `schema_version` integer

- Description: Add `schema_meta(version INTEGER NOT NULL)` with a single row. Increment on every migration. Migration code reads the integer and runs ALL ladder steps strictly newer than the stored version.
- Pros:
  - Familiar pattern from Django/Rails/Alembic. Easy mental model: "DB is at v17; code expects v23; run steps 18..23".
  - Single source of truth for "where is this DB?"
- Cons:
  - **Breaks the converted-user invariant.** Existing OpenClaw `lcm.db` files have no `schema_meta` row. The first Python run would assume version=0 and try to re-run every migration step — destroying idempotency at best, double-inserting at worst.
  - Mitigation requires a one-shot "bootstrap the schema_meta row from observed schema state" step on first contact, which is itself error-prone.
  - Doubles the cognitive load: now there are TWO ledgers (`lcm_migration_state` for algorithm versions + `schema_meta` for monotonic). Drift between them becomes a new failure mode.
  - LCM's design choice was deliberate — `docs/porting-guides/storage.md` line 621 explicitly identifies monotonic versioning as "breakage-driven (re-evaluate when a future change actually requires it)".
- Evidence cited:
  - `docs/porting-guides/storage.md` line 619–621 — explicit recommendation against introducing monotonic versioning.

### Option C: Replace `lcm_migration_state` with monotonic `schema_version`

- Description: Drop the algorithm-version ledger entirely; rely on monotonic version for all gating.
- Pros: Simpler in principle (one mechanism instead of two).
- Cons:
  - Worst of both: requires the bootstrap step from Option B AND loses algorithm-version semantics. If a future `backfillSummaryDepths` algorithm changes, there is no way to re-run only that step on already-migrated DBs without re-running everything.
  - Strictly more breakage for converted users than Option B.

## Decision

Chosen: **Option A — preserve LCM's two-mechanism design verbatim**.

`src/lossless_hermes/db/migration.py` (per ADR-024) implements:

```python
def run_lcm_migrations(conn: sqlite3.Connection, *, fts5_available: bool = True) -> None:
    """Apply the LCM schema migration ladder. Fully idempotent.

    Two mechanisms:
    1. Structural: PRAGMA table_info / PRAGMA index_list / sqlite_master probes for
       additive ALTER / CREATE IF NOT EXISTS. Implicit; no ledger needed.
    2. Algorithm-versioned: lcm_migration_state(step_name, algorithm_version,
       completed_at) for backfills whose algorithm has semantic versioning.
       Today's rows: backfillSummaryDepths=1, backfillSummaryMetadata=1,
       backfillToolCallColumns=1.

    See ADR-026.
    """
    ...
    _ensure_lcm_migration_state_table(conn)
    ...
    for step_name, version, fn in BACKFILL_LADDER:
        if _step_at_or_above(conn, step_name, version):
            continue
        fn(conn)
        _record_step_completion(conn, step_name, version)
```

The Python `_ensure_lcm_migration_state_table` creates the table with exactly the TS shape (`docs/porting-guides/storage.md` §2.5):

```sql
CREATE TABLE IF NOT EXISTS lcm_migration_state (
    step_name TEXT PRIMARY KEY,
    algorithm_version INTEGER NOT NULL,
    completed_at TEXT NOT NULL DEFAULT (datetime('now'))
)
```

## Rationale

Storage-porting-guide §10.2 explicitly establishes the migration story (`docs/porting-guides/storage.md` lines 569–587):

1. Copy `lcm.db` from OpenClaw to Hermes (ADR-025 step 3).
2. Run the Python `run_lcm_migrations()`. Because it's idempotent and uses both mechanisms LCM uses, it is a no-op for already-present columns/indexes/tables AND for backfills already at `algorithm_version >= 1`.

Introducing a monotonic version (Option B/C) breaks this contract. The migration story for converted users becomes: "your old DB has no `schema_meta` row, so the Python port will try to re-run every migration step from scratch." Fixing that requires a bootstrap heuristic — "if no `schema_meta` row, infer version from observed columns" — which is fragile and exactly the structural probing we already do, just wrapped in a more complicated abstraction.

The structural+algorithm-version split is the minimum viable invariant. Additive forward migrations (the 95% case) don't need a ledger entry. Only algorithm-version semantics need one. This matches Django's `RunPython` migrations' implicit acknowledgement: most migrations are DDL and idempotent; only data-mutation steps need version gates.

Future-proof mitigation: if a future change requires monotonic versioning (e.g. a column-type change that requires data conversion), it can be added as a NEW step in `lcm_migration_state` (e.g. `convertCacheStateToJson v1`). The two-mechanism design accommodates new algorithm-versioned steps trivially.

## Consequences

- **Converted users migrate without re-running backfills.** Their existing `lcm_migration_state` rows (`backfillSummaryDepths=1`, `backfillSummaryMetadata=1`, `backfillToolCallColumns=1`) are honored. ADR-025's import-openclaw command works end-to-end.
- **Adding a new backfill** requires:
  1. Pick a unique `step_name` (e.g. `backfillContextItemBoundaries`).
  2. Start at `algorithm_version = 1`.
  3. Add to `BACKFILL_LADDER` in `db/migration.py`.
  4. Write the backfill function as idempotent (re-runnable; uses `INSERT OR IGNORE` or equivalent — see ADR-029 §"Wave-1: race-safe `INSERT OR IGNORE`").
  5. Test on an empty DB AND a v4.1 OpenClaw-imported DB.
- **Changing an existing backfill algorithm** requires incrementing `algorithm_version` (e.g. v1 → v2). On next run, the migration ladder detects `stored_version < expected_version` and re-runs that step. Document the new semantics in the function docstring + an entry in `docs/CHANGELOG.md`.
- **No `schema_version` row** is introduced. Operators asking "what version is my DB?" get pointed to `/lcm doctor`, which enumerates `lcm_migration_state` rows + reports observed structural state.
- **Invariant:** `lcm_migration_state.step_name` is a UNIQUE primary key. Two migrations cannot share a step name. The Python port enforces this at insert time via `INSERT INTO ... ON CONFLICT DO UPDATE SET algorithm_version = excluded.algorithm_version`.
- **Invariant:** every backfill function MUST be idempotent (safe to re-run with the row already at the latest version). Tests must include "run twice" coverage.
- **Doctor surface:** `/lcm doctor schema` lists `lcm_migration_state` rows verbatim. `/lcm doctor schema --diff <ref-db>` diffs the structural state vs. a reference DB (per `docs/reference/lcm-source-map.md` "Open questions" #2).

## Open questions / 5% uncertainty

1. **A future hostile change (column type conversion, table rename) is not detectable by structural probe alone.** When that lands, we will add a new `lcm_migration_state` step that performs the conversion and records `step_name = convertFooTypeToJson, algorithm_version = 1`. This is not a problem today — none of the planned v0.1 ports require type changes. Document the pattern in the migration.py docstring so the precedent is set.
2. **Concurrent migration runs.** If two Hermes processes both call `run_lcm_migrations()` simultaneously against the same DB, the structural probes are racy: both might see "column missing" and both ALTER, with the second failing. LCM solves this via a transaction wrapping the ladder; the Python port does the same (`db/migration.py` uses `BEGIN EXCLUSIVE` around the whole ladder, per `docs/porting-guides/storage.md` §10 lines 575–576). Verified by `tests/test_migration.py` "savepoint retry" case (`docs/porting-guides/tests-and-config.md` line 494).
3. **Drift between TS and Python migration ladders.** If a future LCM release adds a step to `migration.ts` that we don't backport to `db/migration.py`, an operator who imports their lcm.db gets stuck. Mitigation: the schema-diff CI check (`docs/reference/lcm-source-map.md` "Open questions" #2) compares the Python-generated schema against a TS-generated reference DB; drift fails CI. Run this in both directions.
