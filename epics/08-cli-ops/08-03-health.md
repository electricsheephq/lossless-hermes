---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-08] cli-ops: port /lcm health detailed probe'
labels: 'port, epic-08-cli-ops'
---

## Source (TypeScript)

- File: `src/operator/health.ts`
- Lines: 442 LOC
- Function(s)/class(es): `getV41HealthSnapshot(db)`, `formatV41HealthSnapshot(snapshot)`, plus eight per-subsystem probes (`probeEmbeddings`, `probeWorkers`, `probeSynthesis`, `probeEval`, `probeSuppression`, `probeFtsIndexes`, `probeVec0Triggers`, `probeMigrationState`)

## Target (Python)

- File: `src/lossless_hermes/operator/health.py`
- Estimated LOC: ~480

## What this issue covers

The "detailed health probe" surfaced as `/lcm health` — distinct from `/lcm status` (08-02). Status is a per-call info snapshot; health is a v4.1-wide system probe that touches every subsystem (embeddings, workers, synthesis cache, eval rows, suppression accounting, FTS indexes, vec0 triggers, migration ledger). Tolerant of missing tables — per doctor-ops.md table line 308, "pure read-only, tolerant of missing tables." Never raises; missing surfaces report `kind: "unavailable"` rather than crashing the probe.

The eight probes (from `operator/health.ts`):

1. **Embeddings probe** — counts unembedded leaves (`summaries WHERE kind='leaf' AND embedded_at IS NULL AND suppressed_at IS NULL`); reports active embedding model (`lcm_embedding_profile` table); reports backfill lag (mean age of unembedded leaves).
2. **Workers probe** — `getWorkerStatusSnapshot(db)` (08-10); cross-references each held lock against `lcm_worker_lock` rows.
3. **Synthesis probe** — counts active synthesis caches, recently invalidated caches (last 24h), failed dispatches; reads from `lcm_synthesis_cache` + `lcm_synthesis_audit`.
4. **Eval probe** — reads `lcm_eval_run` rows; reports last run's `recall_at_k`, drift vs baseline.
5. **Suppression probe** — counts suppressed summaries by reason; flags condensed summaries with `contains_suppressed_leaves=1` awaiting idle rebuild.
6. **FTS indexes probe** — verifies `messages_fts`, `summaries_fts`, `summaries_fts_cjk` exist and have correct row counts vs source tables.
7. **Vec0 triggers probe** — verifies `lcm_embed_suppress_<slug>` triggers exist for every registered embedding model (`SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'lcm_embed_suppress_%'`).
8. **Migration state probe** — reads `lcm_migration_state` and reports any versioned backfill at `algorithm_version < 1`.

Output format (TS parity, plain text designed for chat rendering):

```
[lcm] Health (v4.1)

Embeddings: voyage-3 (1247 embedded, 18 unembedded, mean lag 4.2 min)
Workers: 3 healthy, 0 stale, 0 expired
  - embedding-backfill: held by host=eva.local, started 2 min ago, heartbeat OK
  - entity-extraction: held by host=eva.local, started 45s ago, heartbeat OK
  - condensation-maintenance: idle (last tick 1 min ago)
Synthesis: 412 active, 8 invalidated, 0 failed
Eval: last run 2 days ago, recall@5=0.84, drift -0.02 vs baseline
Suppression: 23 leaves suppressed (12 user-purge, 11 doctor-clean), 4 condensed awaiting rebuild
FTS indexes: all 3 healthy
Vec0 triggers: 2 models registered, 4 triggers OK
Migrations: all backfills at algorithm_version >= 1
```

When a probe is unavailable (table doesn't exist), print `<subsystem>: <unavailable: table 'lcm_eval_run' does not exist>`.

## Dependencies

- Depends on: #08-01 (dispatcher), Epic 05 (worker infra — needed for the workers probe), Epic 01-06 (v4.1 sidecar tables for synthesis/eval probes).
- Blocks: nothing — read-only.

## Acceptance criteria

- [ ] `get_v41_health_snapshot(db) -> V41HealthSnapshot` returns a pydantic model with one field per probe; each field is either `Healthy`, `Degraded(reason)`, or `Unavailable(reason)`.
- [ ] Every probe handles missing tables via `has_table(db, name)` guard, never raises.
- [ ] `format_v41_health_snapshot(snapshot) -> str` matches TS `formatV41HealthSnapshot` line-for-line modulo whitespace.
- [ ] Workers probe shows held-by host, age, heartbeat status (live read from `lcm_worker_lock`).
- [ ] Suppression probe reports suppressed-leaf count grouped by `suppress_reason` (per doctor-ops.md §"Schema additions to support suppression" line 296).
- [ ] All TS test cases in `test/operator-health.test.ts` have ported pytest equivalents in `tests/operator/test_health.py`.
- [ ] **New test:** `tests/operator/test_health.py::test_missing_table_unavailable` — drops `lcm_eval_run` and confirms probe reports `Unavailable` without raising.
- [ ] **New test:** `tests/operator/test_health.py::test_workers_held_by_other_host` — seeds a held lock row from a hypothetical second host and confirms it's surfaced.
- [ ] Function signatures match the spec in [docs/porting-guides/doctor-ops.md](../../docs/porting-guides/doctor-ops.md) §"Operator modules" line 308.
- [ ] `pytest tests/operator/test_health.py` passes.
- [ ] No new mypy errors (`mypy --strict src/lossless_hermes/operator/health.py`).
- [ ] PR description cites LCM commit `1f07fbd` (pr-613 head).

## Estimated effort

**6 hours.**

## Confidence

**90%** — TS has a tight test suite (`test/operator-health.test.ts`). 10% risk is in the workers probe: cross-process lock reads depend on Epic 05's `lcm_worker_lock` schema landing first.
