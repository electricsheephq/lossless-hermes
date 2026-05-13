---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-06] tools: port lcm_search_entities tool'
labels: 'port, tool'
---

## Source (TypeScript)
- File: `src/tools/lcm-search-entities-tool.ts`
- Lines: 1–377
- Function(s)/class(es): `createLcmSearchEntitiesTool` factory, schema (lines 43–83), `VISIBLE_MENTIONS_CTE` + `entityAggCte({includeFirstIn: false})` path, empty-result catalog probe (lines 286–303).

## Target (Python)
- File: `src/lossless_hermes/tools/search_entities.py`
- Estimated LOC: ~350 LOC.

## Dependencies
- Depends on: #06-02, #06-03, #06-04, **#06-10 (delivers `tools/entity_shared.py`)**, Epic 01 storage.
- Blocks: Epic 09 eval (entity-browse tests).

## Acceptance criteria
- [ ] `LCM_SEARCH_ENTITIES_SCHEMA` dict — **description string verbatim** from `lcm-search-entities-tool.ts:137–150` (tools.md lines 450–451). The three-modes prose ("browse by type", "fuzzy lookup", "catalog probe") is load-bearing UX routing copy.
- [ ] Handler validates: `query` required UNLESS `entityType` is provided (line 174 in TS).
- [ ] `escape_like(query)` for SQL LIKE: escapes `%`, `_`, `\` with `ESCAPE '\'`.
- [ ] Build SQL using `VISIBLE_MENTIONS_CTE` + `entity_agg_cte(include_first_in=False)` (shared with `get_entity` via `entity_shared.py`).
- [ ] **EXISTS guard** (defense in depth): mention with unsuppressed summary — even though `VISIBLE_MENTIONS_CTE` already filters, the outer query also enforces this.
- [ ] **Rank:** `ORDER BY ea.occ_count DESC, ea.last_at DESC LIMIT ?`.
- [ ] **`mode` enum:** `like` (default substring), `prefix`, `exact`.
- [ ] **Empty-result catalog probe** (TS lines 286–303): if zero results, run two cheap probes:
  - `SELECT EXISTS(SELECT 1 FROM lcm_entities WHERE session_key=? LIMIT 1)` — session-scoped.
  - `SELECT EXISTS(SELECT 1 FROM lcm_entities LIMIT 1)` — global.

  Map to `catalogStatus`:
  - `active` — query just didn't match.
  - `empty-for-session` — worker hasn't run on this session.
  - `empty-globally` — worker hasn't run on this DB at all.

  This is critical UX — the agent must know "entity doesn't exist" vs "worker hasn't run yet".
- [ ] **Token-gate estimator** (#06-03): `420 + limit * 85` chars.
- [ ] PR description cites the LCM commit SHA being ported.

## Tests
- Mirror `lcm-search-entities-tool.test.ts` 1:1 in `tests/tools/test_lcm_search_entities.py` (~394 TS LOC → ~310 pytest LOC):
  - `mode='like'` (default substring) matches mid-string.
  - `mode='prefix'` matches start only.
  - `mode='exact'` matches whole canonical name (case-folded).
  - `entityType` filter alone (catalog probe with empty query).
  - Both `query` and `entityType` missing → error.
  - `escape_like` handles `%`, `_`, `\` literally.
  - `catalogStatus == "active"` when query has zero matches but entities exist for session.
  - `catalogStatus == "empty-for-session"` when no entities for this session but some globally.
  - `catalogStatus == "empty-globally"` when no entities anywhere.
  - Rank order: occ_count DESC, last_at DESC.
  - `limit` clamp at 100 boundary.

## Estimated effort
**6 hours** — 3h port (shares CTE with get_entity, so most of the SQL work is done), 3h tests (the three-state catalogStatus matrix is the work).

## Confidence
**95%** — DB-only; the catalog probe is the only nontrivial bit. 5% on the `escape_like` edge cases (Python's `sqlite3` parameter binding may interact with the ESCAPE clause differently than `node:sqlite` — pin via tests).

## References
- [`docs/porting-guides/tools.md`](../../docs/porting-guides/tools.md) "lcm_search_entities" section (lines 444–490).
- TS test fixture: `test/lcm-search-entities-tool.test.ts` (394 LOC).
