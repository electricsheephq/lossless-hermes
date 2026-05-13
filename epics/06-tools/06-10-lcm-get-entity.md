---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-06] tools: port lcm_get_entity tool'
labels: 'port, tool'
---

## Source (TypeScript)
- File: `src/tools/lcm-get-entity-tool.ts`
- Lines: 1–342
- Function(s)/class(es): `createLcmGetEntityTool` factory, schema (lines 39–67), `VISIBLE_MENTIONS_CTE` + `entityAggCte({includeFirstIn: true})` query path.

## Target (Python)
- File: `src/lossless_hermes/tools/get_entity.py`
- Estimated LOC: ~320 LOC.

## Dependencies
- Depends on: #06-02, #06-03, #06-04, Epic 01 storage (`lcm_entities`, `lcm_entity_mentions`, `summaries` tables), a shared `entity_shared.py` module (see "Sub-deliverable" below).
- Blocks: Epic 09 eval (entity-anchored recall tests).

## Sub-deliverable
- [ ] **`src/lossless_hermes/tools/entity_shared.py`** (84 TS LOC → ~80 Python LOC) — exports the SQL fragments shared with `lcm_search_entities`:
  - `VISIBLE_MENTIONS_CTE` — `WITH visible_mentions AS (SELECT m.entity_id, m.summary_id, m.surface_form, m.mentioned_at FROM lcm_entity_mentions m JOIN summaries s ON s.summary_id = m.summary_id WHERE s.suppressed_at IS NULL)`.
  - `entity_agg_cte(include_first_in: bool) -> str` — builds the `, entity_agg AS (SELECT vm.entity_id, COUNT(*) AS occ_count, MIN(...) AS first_at, MAX(...) AS last_at, [first_in subquery,] json_group_array(DISTINCT vm.surface_form) AS visible_surfaces FROM visible_mentions vm GROUP BY vm.entity_id)` clause.
  - Both are pure SQL templates with no SQL-injection surface. **Wave-12 P1 fix:** aggregates recompute from UNSUPPRESSED mentions only — this is the load-bearing reason for `VISIBLE_MENTIONS_CTE` joining on `summaries.suppressed_at IS NULL`.

## Acceptance criteria
- [ ] `LCM_GET_ENTITY_SCHEMA` dict — **description string verbatim** from `lcm-get-entity-tool.ts:123–136` (tools.md lines 401–402). The PRIMARY-for-Type-D routing prose + fallback-to-hybrid hint are load-bearing.
- [ ] Handler validates `name` (non-empty after strip).
- [ ] Resolves `effective_session_key` — param wins, else `input.sessionKey`. If neither → error.
- [ ] Optional `entity_type` filter (case-folded).
- [ ] **Lookup entity row** via `VISIBLE_MENTIONS_CTE` + `entity_agg_cte(include_first_in=True)`:
  - **Wave-12 P1 inline comment** at the CTE site:
    ```python
    # LCM Wave-12 P1: aggregates recompute from UNSUPPRESSED mentions only
    # to prevent suppressed-mention data leaking via aggregate columns.
    # Original: lossless-claw/src/tools/lcm-get-entity-tool.ts (uses lcm-entity-shared.ts CTE).
    ```
- [ ] **If not found:** returns `{"found": False, "fallback_suggestions": [3 suggestions]}` — concrete:
  1. Try `lcm_search_entities` with `mode='prefix'`.
  2. Try `lcm_grep` with `mode='hybrid'`.
  3. Try `lcm_grep` with `mode='verbatim'`.

  **Critical UX detail — keep verbatim in the port** per tools.md line 429.
- [ ] **Existence-probing defense:** the `not found` payload is INTENTIONALLY indistinguishable from "all mentions suppressed" — same shape, no leakage. Pin this with a test.
- [ ] **Mention list:** `SELECT m.* FROM lcm_entity_mentions m JOIN summaries s ON s.summary_id = m.summary_id WHERE m.entity_id=? AND s.suppressed_at IS NULL ORDER BY m.mentioned_at DESC LIMIT ?`.
- [ ] Render as markdown with metadata + mention list. Strip canonical form from `alternateSurfaces` display.
- [ ] **Token-gate estimator** (#06-03): `250 + mentionLimit * 110` chars.
- [ ] PR description cites the LCM commit SHA being ported.

## Tests
- Mirror `lcm-get-entity-tool.test.ts` 1:1 in `tests/tools/test_lcm_get_entity.py` (~480 TS LOC → ~380 pytest LOC):
  - Entity exists with mentions → markdown with metadata + ordered mention list.
  - Entity exists but ALL mentions are suppressed → returns `{found: False, fallback_suggestions: [...]}` (identical to entity-not-found shape — pin this).
  - Entity does not exist → `{found: False, fallback_suggestions: [...]}` with the 3 concrete suggestions.
  - `entityType` filter restricts results.
  - Case-folding: `name="Foo"` matches canonical "foo" (`COLLATE NOCASE`).
  - `alternateSurfaces` display strips canonical form.
  - `mentionLimit` caps results (test 100 boundary).
  - `mentioned_at DESC` ordering verified.
  - **Wave-12 P1 regression:** seed 1 entity with 5 mentions, suppress 3 of them; assert `occurrence_count = 2` (not 5).

## Estimated effort
**6 hours** — 3h port + entity_shared.py shared module, 3h tests.

## Confidence
**95%** — DB-only, well-specified, clear test surface. 5% on getting the `json_group_array(DISTINCT ...)` semantics right in sqlite3 (some versions of SQLite handle the DISTINCT differently — test against the production SQLite version per Epic 01).

## References
- [`docs/porting-guides/tools.md`](../../docs/porting-guides/tools.md) "lcm_get_entity" section (lines 396–440) + "lcm-entity-shared.ts" section (lines 582–588).
- [ADR-029](../../docs/adr/029-wave-fix-provenance.md) — Wave-12 P1 inline-comment format.
- TS test fixture: `test/lcm-get-entity-tool.test.ts` (480 LOC).
