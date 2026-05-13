---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-07] extraction: port entity-coreference worker (498 LOC)'
labels: 'port, epic-07, wave-1, wave-7, wave-10'
---

## Source (TypeScript)

- File: `src/extraction/entity-coreference.ts`
- Lines: 498 LOC
- Function(s)/class(es): `runCoreferenceTick(db, extractor, opts) → CoreferenceTickResult`, `countPendingExtractions(db, *, kind='entity') → int`, `surfaceHashForId(surface, maxBytes=16) → str`, `CoreferenceTickResult` / `CoreferenceTickOptions` / `ExtractedEntity` types

## Target (Python)

- File: `src/lossless_hermes/extraction/coreference.py`
- Estimated LOC: ~600 (includes the per-row SAVEPOINT scaffold + Wave-N comment overhead)

## What this issue covers

The full async tick body that drains `lcm_extraction_queue` once per call. The TS implementation is ~500 LOC of selector SQL, per-item SAVEPOINT discipline, race-safe upsert against the `lcm_entities` UNIQUE index, deterministic `mention_id` generation, and heartbeat-loss break. See `docs/porting-guides/entity-extraction.md` §"Dequeue + worker loop" for the load-bearing algorithm.

Three Wave-N fixes are non-negotiable (per ADR-029, every site carries an inline `# LCM Wave-N` comment):

- **Wave-1 (2025-11-08):** race-safe `INSERT OR IGNORE` against the `(session_key, canonical_text COLLATE NOCASE)` UNIQUE index. Two workers seeing the same canonical name simultaneously must not both abort their txn.
- **Wave-7 (2026-02-14):** **per-row SAVEPOINT** wrapping each `{surface, entityType}` resolution. A single bad surface (FK violation, encoding bomb) must not roll back the whole leaf's mentions. SAVEPOINT name = `coref_<idx>_<base36(now())>`; use `time.monotonic_ns()` not `time.time()` to avoid ms-resolution collisions on macOS.
- **Wave-10 (2026-03-22):** **`count_pending_extractions` filter parity** — the selector in `count_pending_extractions` MUST match `runCoreferenceTick`'s selector exactly (same `kind`, same `attempts < 5`, same `suppressed_at IS NULL`). Mismatch caused autostart to spin on rows the tick would never select.

Additional Wave-discipline this issue must preserve:

- **Wave-1 finding #7:** `occurrence_count` bumps ONLY on true new-mention insert (`changes > 0`), not unconditionally — idempotent re-runs must not double-count.
- **Wave-4 P0-1 heartbeat:** call `opts.on_item_heartbeat()` per iteration; on `False`, set `result.lock_lost_mid_tick = True` and `break` immediately. Do not try to recover the lock mid-tick.
- **FNV-1a 32-bit `surface_hash_for_id`:** port verbatim from the porting guide; do not substitute `hashlib.md5` or Python `hash()`. The mention_id format `men_<entity_id>_<leaf_id>_<surfaceHashForId(surface, 16)>` is load-bearing for idempotency.

## Dependencies

- Depends on: 07-01 (`VISIBLE_MENTIONS_CTE` constant), 07-03 (the `ExtractEntitiesFn` Protocol it consumes is defined alongside the LLM extractor), 07-04 (autostart depends on this), Epic 05 (worker-lock + worker-orchestrator infra it runs under), Epic 01-06 (`lcm_entities`, `lcm_entity_mentions`, `lcm_entity_type_registry`, `lcm_extraction_queue` schema)
- Blocks: 07-04 (extraction autostart wires this tick into the worker loop)

## Acceptance criteria

- [ ] Selector SQL is identical between `run_coreference_tick` and `count_pending_extractions` (Wave-10 P2)
- [ ] Each `{surface, entityType}` resolution is wrapped in a per-row SAVEPOINT (Wave-7); per-item failure does NOT abort the outer transaction
- [ ] `INSERT OR IGNORE` is used against `lcm_entities` (Wave-1); on lost-race re-SELECT, fall through cleanly
- [ ] `mention_id` is deterministic `men_<entity_id>_<leaf_id>_<surface_hash_for_id(surface, 16)>` with FNV-1a 32-bit hex hash
- [ ] `occurrence_count` bumps only on true new-mention insert (Wave-1 finding #7)
- [ ] `on_item_heartbeat()` returning `False` sets `lock_lost_mid_tick = True` and breaks the loop (Wave-4 P0-1)
- [ ] Extractor throws bump `attempts` + truncate `last_error` to 500 chars; do NOT mark queue row processed
- [ ] `MAX_ATTEMPTS = 5` constant; rows with `attempts >= 5` are dead-lettered (skipped by both selectors)
- [ ] `BEGIN IMMEDIATE` outer transaction per item; per-row SAVEPOINTs nest within
- [ ] Every Wave-N fix carries an inline `# LCM Wave-N (date): ...` comment per ADR-029
- [ ] `pytest tests/extraction/test_coreference.py` passes (all 9 test cases from `entity-coreference.test.ts`)
- [ ] No new mypy errors with strict mode

## Tests to port

| Source | LOC | Cases |
|---|---:|---|
| `test/entity-coreference.test.ts` | 236 | (1) happy path single leaf single entity; (2) cross-leaf coref via NOCASE; (3) multi-entity per leaf; (4) `lcm_entity_type_registry` bump on new-entity insert; (5) extractor-throws → retry until `attempts < 5`; (6) partial-batch resilience (one bad surface doesn't abort peers — Wave-7); (7) `perTickLimit` clamp; (8) suppressed-leaf skip; (9) empty extraction → queue marked done |
| `test/v41-wave10-reviewer-regressions.test.ts` (relevant subset) | — | `count_pending_extractions` returns the same set the next `run_coreference_tick` will draw |

## Estimated effort

**12–16 hours.** The selector SQL ports verbatim; the per-row SAVEPOINT discipline + heartbeat-loss-break + Wave-1 race recovery are the load-bearing detail and where most of the bug surface lives. Add ~2 h for the FNV-1a fixture parity test (a TS-produced reference mention_id table verified byte-identical).

## Confidence

**88%.** Two residual risks:

- **`on_item_heartbeat` async-vs-sync shape** — TS uses sync `() → boolean`. Python may need `async def` if the heartbeat itself goes through an `await`-able DB call. Mitigation: keep it sync at the signature level; have the autostart wire a sync wrapper.
- **SAVEPOINT-name millisecond collisions** — TS `Date.now().toString(36)` on ms resolution; macOS Python `time.time()` is sometimes ms-only. Use `time.monotonic_ns()` or include a per-tick counter token. Caught by partial-batch-resilience test (case 6) running fast loops.
