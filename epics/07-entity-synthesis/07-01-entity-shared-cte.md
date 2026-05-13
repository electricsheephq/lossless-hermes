---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-07] entity-shared-cte: port VISIBLE_MENTIONS_CTE helper'
labels: 'port, epic-07'
---

## Source (TypeScript)

- File: `src/tools/lcm-entity-shared.ts`
- Lines: 84 LOC
- Function(s)/class(es): module-level `VISIBLE_MENTIONS_CTE` constant + `entityAggCte({ includeFirstIn }) → string` helper

## Target (Python)

- File: `src/lossless_hermes/tools/entity_shared.py`
- Estimated LOC: ~90

## What this issue covers

Port the suppression-aware CTE pair that both `lcm-get-entity-tool.ts` and `lcm-search-entities-tool.ts` prepend to their queries. Wave-12 reviewer F4 + the 2026-05-08 architectural-decision methodology chose to **extract a shared helper** over merging the two tools, because byte-identical SQL maintained in two places is a parallel-edit drift hazard.

Two pieces:

1. **`VISIBLE_MENTIONS_CTE`** — module-level constant string, ported verbatim:
   ```sql
   WITH visible_mentions AS (
     SELECT m.entity_id, m.summary_id, m.surface_form, m.mentioned_at
       FROM lcm_entity_mentions m
       JOIN summaries s ON s.summary_id = m.summary_id
      WHERE s.suppressed_at IS NULL
   )
   ```

2. **`entity_agg_cte(*, include_first_in: bool) → str`** — builds the derived `entity_agg` CTE that recomputes:
   - `occ_count = COUNT(*)` over visible mentions
   - `first_at = MIN(mentioned_at)`, `last_at = MAX(mentioned_at)`
   - `first_in` (when `include_first_in=True`) = earliest visible `summary_id` per entity, ordered by `(mentioned_at ASC, summary_id ASC)`
   - `visible_surfaces = json_group_array(DISTINCT surface_form)`

The row-level `lcm_entities.occurrence_count` / `last_seen_at` columns are **producer-side counters** maintained by the worker (07-02). They do NOT decrement on suppression. This CTE is the read-side rectification — every `/lcm` entity read MUST go through it. Only operator-internal tooling that legitimately needs the raw counters reads `lcm_entities` directly.

## Dependencies

- Depends on: Epic 01-06 (`lcm_entities`, `lcm_entity_mentions`, `summaries` tables exist with `suppressed_at` column)
- Blocks: 07-02 (worker references the CTE in its read paths), Epic 06 tools (`get_entity`, `search_entities`) consume both exports

## Acceptance criteria

- [ ] `VISIBLE_MENTIONS_CTE` matches the TS source byte-for-byte (after whitespace normalization)
- [ ] `entity_agg_cte(include_first_in=True)` and `entity_agg_cte(include_first_in=False)` return the two variants the TS helper emits
- [ ] Module exports only these two names; no SQL execution at import time
- [ ] No mypy errors with strict mode
- [ ] Tests under `tests/tools/test_entity_shared.py` assert (a) string-equality with a vendored TS fixture, (b) both helper variants compose into a valid prepared statement against an empty schema

## Tests to port

| Source | Cases |
|---|---|
| `test/lcm-entity-shared.test.ts` (~50 LOC) | (1) CTE literal matches reference; (2) `entityAggCte({ includeFirstIn: true })` includes `first_in`; (3) `entityAggCte({ includeFirstIn: false })` omits `first_in`; (4) both compose with `VISIBLE_MENTIONS_CTE` to form executable SQL |

## Estimated effort

**1–2 hours** — pure string constant + string-builder helper. The cost is in writing the two-tool-compat fixtures and the parity test against the TS fixture.

## Confidence

**98%.** The TS source is two exports and an interpolation template. The only risk is a typo introducing a byte divergence — caught by the string-equality test against the vendored fixture.
