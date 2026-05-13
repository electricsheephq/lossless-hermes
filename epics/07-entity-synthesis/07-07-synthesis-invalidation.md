---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-07] synthesis: port cache invalidation on suppression (lcm_cache_leaf_refs)'
labels: 'port, epic-07'
---

## Source (TypeScript)

- File: `src/operator/purge.ts` (lines 346–352 — the explicit DELETE)
- File: `src/tools/lcm-synthesize-around-tool.ts` (line 1396 — the post-synthesis leaf-ref populate)
- Lines: ~40 LOC across the two sites
- Function(s)/class(es): `invalidate_caches_for_suppressed_leaves(conn, leaf_ids: Iterable[str]) → int` (returns deleted-row count), `record_cache_leaf_refs(conn, cache_id: str, leaf_ids: Iterable[str]) → None` (best-effort INSERT OR IGNORE per leaf)

## Target (Python)

- File: `src/lossless_hermes/synthesis/invalidation.py` (new file; co-located with `cache.py` from 07-06)
- Estimated LOC: ~80

## What this issue covers

When a leaf gets soft-purged (`summaries.suppressed_at = datetime('now')`), the purge path explicitly invalidates dependent cache rows. The `lcm_cache_leaf_refs` table is the lookup index: it has `ON DELETE CASCADE` in both directions, but **cascade only fires on hard `DELETE summaries`, not on soft suppression** (where the summary row stays put with `suppressed_at` set).

Two halves to this issue:

1. **Post-synthesis leaf-ref populate** — `record_cache_leaf_refs(conn, cache_id, leaf_ids)`:
   ```sql
   INSERT OR IGNORE INTO lcm_cache_leaf_refs (cache_id, leaf_summary_id) VALUES (?, ?);
   -- run once per leaf in the source set
   ```
   Best-effort: a single INSERT failure is logged but does NOT fail the surrounding synthesis. Worst case the cache row survives a later suppression and the operator audit catches it (rare).

2. **Suppression-time explicit DELETE** — `invalidate_caches_for_suppressed_leaves(conn, leaf_ids)`:
   ```sql
   DELETE FROM lcm_synthesis_cache
    WHERE cache_id IN (
      SELECT DISTINCT cache_id FROM lcm_cache_leaf_refs
       WHERE leaf_summary_id IN (?, ?, ...)
    );
   ```
   Called from the purge path (`operator/purge.py`, ported in Epic 06 or doctor-ops epic) immediately after the `UPDATE summaries SET suppressed_at = datetime('now')` write. Returns the row count for telemetry.

The Final.review.3 Loop 2 Leak 2.5 fix landed this: post-suppression cache reads were surfacing PII that was baked into the synthesis before suppression, because cascade did not fire on the soft-purge path.

Schema (created in Epic 01-06; cross-check):

```sql
CREATE TABLE lcm_cache_leaf_refs (
  cache_id         TEXT NOT NULL REFERENCES lcm_synthesis_cache(cache_id) ON DELETE CASCADE,
  leaf_summary_id  TEXT NOT NULL REFERENCES summaries(summary_id) ON DELETE CASCADE,
  PRIMARY KEY (cache_id, leaf_summary_id)
);
CREATE INDEX lcm_cache_leaf_refs_by_leaf_idx ON lcm_cache_leaf_refs (leaf_summary_id);
```

The `by_leaf_idx` index is load-bearing — the suppression DELETE filters by `leaf_summary_id IN (...)` and would full-scan without it.

## Dependencies

- Depends on: 07-06 (cache row write path that creates the `cache_id` this refs), Epic 01-06 (`lcm_cache_leaf_refs` table + `by_leaf_idx`), Epic 06 / doctor-ops (`purge.py` is the caller of `invalidate_caches_for_suppressed_leaves`)
- Blocks: nothing in this epic (terminal for the synthesis half)

## Acceptance criteria

- [ ] `record_cache_leaf_refs(conn, cache_id, leaf_ids)` runs `INSERT OR IGNORE` per leaf; per-leaf failures logged at warn but do NOT raise
- [ ] `invalidate_caches_for_suppressed_leaves(conn, leaf_ids)` is a single-statement DELETE with the cache_id subselect (NOT a Python loop that issues N DELETEs)
- [ ] Both functions are sync (ADR-017); no `await`
- [ ] The DELETE runs as part of the suppression transaction in the purge path (caller-owned tx; do NOT BEGIN/COMMIT inside)
- [ ] Return value is the row count from `cursor.rowcount` (caller emits telemetry)
- [ ] An inline `# LCM Final.review.3 Loop 2 Leak 2.5 (2026-04-08): explicit DELETE not FK cascade — soft suppression leaves summaries row in place.` comment on the DELETE
- [ ] Soft-suppress regression test: insert leaf, synthesize, suppress leaf, assert cache row deleted; assert summaries row still exists with `suppressed_at` set
- [ ] Hard-DELETE-summaries test: cache row also deleted via FK cascade (no Python code needed for this; just verifies the DDL constraint)
- [ ] `pytest tests/synthesis/test_invalidation.py` passes
- [ ] No new mypy errors with strict mode

## Tests to port

| Source | LOC | Cases |
|---|---:|---|
| `test/purge-soft-suppression.test.ts` (relevant subset) | — | (1) suppress a leaf → cache rows deleted; (2) cascade on hard DELETE summaries still works; (3) bulk suppression (N=100 leaves) DELETEs all dependent cache rows in one statement |
| `test/lcm-synthesize-around-tool.test.ts` (relevant subset) | — | (4) post-synthesis leaf-ref populate happens for every leaf in source set; (5) INSERT OR IGNORE makes the populate idempotent on retry |
| New tests this issue adds | — | (6) `record_cache_leaf_refs` best-effort: synthetic per-leaf INSERT failure does not raise; surrounding synthesis returns success |

## Estimated effort

**2–3 hours.** Two SQL statements, two thin Python wrappers, two regression tests. Most cost is the integration test that exercises the full ingest → synthesize → suppress → read flow.

## Confidence

**95%.** Residual risk:

- **Atomicity of suppression + invalidation.** The DELETE must run in the same transaction as the `suppressed_at` UPDATE — otherwise a crash between them leaves cache rows pointing at a suppressed leaf, and the next cache read surfaces PII. The caller (purge path) owns the transaction; this issue's only responsibility is to NOT open its own tx. Caught by a crash-injection test in Epic 06.
