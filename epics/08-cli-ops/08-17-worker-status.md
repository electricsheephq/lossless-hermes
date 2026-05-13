---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-08] cli-ops: /lcm worker status (open) + /lcm worker tick (gated)'
labels: 'port, epic-08-cli-ops'
---

## Source (TypeScript)

- File: `src/plugin/lcm-command.ts` (the `case "worker"` body)
- Lines: ~150 LOC across `case "worker"` + sub-cases for `status` and `tick`
- Function(s)/class(es): `case "worker status"` handler, `case "worker tick embedding-backfill"` handler. Both delegate to `src/operator/worker-orchestrator.ts` surfaces (ported in 08-10).

## Target (Python)

- File: `src/lossless_hermes/plugin/commands/worker.py`
- Estimated LOC: ~170

## What this issue covers

Two worker subcommands sharing a parent dispatch:

1. **`/lcm worker` (no args) or `/lcm worker status`** — read-only worker status snapshot. **NOT owner-gated.** Anyone can read.
2. **`/lcm worker tick embedding-backfill`** — force one tick of the embedding-backfill worker. **OWNER-GATED** (per plugin-glue.md line 430: "200 paid Voyage embeddings per call. Owner-gated because of paid quota burn.").

Both delegate to `worker_orchestrator.py` from 08-10 (`get_worker_status_snapshot` and `tick_embedding_backfill` respectively).

### Status output (TS parity)

`/lcm worker status` renders the snapshot as text:

```
[lcm] Workers
embedding-backfill:    held by eva.local (worker=wk-7f3a, started 2 min ago, heartbeat 30s ago)
entity-extraction:     idle (last tick 45 s ago)
condensation-maintenance: held by eva.local (worker=wk-1c2d, started 8 min ago, heartbeat STALE — 4 min old)

Pending:
  embedding-backfill: 18 leaves awaiting embedding (mean age 4.2 min)
  entity-extraction: 0
  condensation-maintenance: 2 conversations awaiting rebuild
```

Stale workers (last_heartbeat_at older than ttl) are flagged with `STALE`. The Snapshot's `stale: True` field drives this.

### Tick output

`/lcm worker tick embedding-backfill` calls `tick_embedding_backfill(deps)` and renders:

```
[lcm] worker tick embedding-backfill
Processed: 200 embeddings (Voyage calls: 200; estimated cost: $0.024)
Remaining queue: 47 leaves
Tick latency: 14.2 s
```

If no work (queue empty):

```
[lcm] worker tick embedding-backfill
Skipped: queue is empty (no unembedded leaves)
```

If peer holds the lock:

```
[lcm] worker tick embedding-backfill
Skipped: embedding-backfill lock held by host=eva.local since 2 min ago
```

### Owner-gating

Per plugin-glue.md §"/lcm slash commands — full inventory" lines 429–430:

- `/lcm worker` / `/lcm worker status` — **NOT owner-gated**.
- `/lcm worker tick embedding-backfill` — **OWNER-GATED**.

Per ADR-013, the dispatcher (08-01) sees `/lcm worker tick embedding-backfill` and the `slash_access` gate enforces upstream. This handler does NOT re-check.

### Future tick kinds

The TS source has only `worker tick embedding-backfill` as the supported `/lcm worker tick` subcommand. The Python port should structure the dispatch table so additional tick kinds (e.g. `worker tick entity-extraction`, `worker tick condensation-maintenance`) can be added without code changes to 08-01 — drive from a `WORKER_JOB_KINDS` registry (Epic 05 dependency).

## Dependencies

- Depends on: #08-01 (dispatcher), #08-10 (`worker_orchestrator.py` provides `get_worker_status_snapshot` + `tick_embedding_backfill`).
- Blocks: nothing.

## Acceptance criteria

- [ ] `run_status(parsed)` returns the rendered status table (matches TS snapshot test line-for-line modulo whitespace).
- [ ] Stale workers (last_heartbeat_at older than ttl) are flagged `STALE`.
- [ ] `run_tick_backfill(parsed)` calls `tick_embedding_backfill(deps)` and renders the processed/skipped/lock-held variants correctly.
- [ ] Unknown tick kind (e.g. `worker tick foo`) returns `[lcm] worker tick: unknown kind 'foo'. Valid kinds: embedding-backfill`.
- [ ] No per-handler owner check (ADR-013 invariant — `grep -n "is_owner" src/lossless_hermes/plugin/commands/worker.py` returns 0 lines).
- [ ] Dispatcher table (08-01) marks `worker tick embedding-backfill` as owner-gated (verified by `tests/commands/test_owner_gating.py`).
- [ ] All TS test cases in `test/lcm-command.test.ts::"/lcm worker*"` have ported pytest equivalents in `tests/commands/test_worker.py`.
- [ ] **New test:** `tests/commands/test_worker.py::test_status_renders_stale` — seeded lock with `last_heartbeat_at` older than ttl, output contains `STALE`.
- [ ] **New test:** `tests/commands/test_worker.py::test_tick_lock_held_by_peer` — `acquire_lock` returns None, output reports `Skipped: ... lock held by host=...`.
- [ ] **New test:** `tests/commands/test_worker.py::test_tick_empty_queue` — no work, output reports `Skipped: queue is empty`.
- [ ] **New test:** `tests/commands/test_worker.py::test_tick_processes_count` — 250 unembedded leaves, output shows `Processed: 200 embeddings`.
- [ ] Function signatures match the spec in [docs/porting-guides/plugin-glue.md](../../docs/porting-guides/plugin-glue.md) §"/lcm slash commands — full inventory" lines 429–430.
- [ ] `pytest tests/commands/test_worker.py` passes.
- [ ] No new mypy errors (`mypy --strict src/lossless_hermes/plugin/commands/worker.py`).
- [ ] PR description cites LCM commit `1f07fbd` (pr-613 head).

## Estimated effort

**4 hours.**

## Confidence

**92%** — thin wrapper over 08-10's orchestrator surfaces; rendering logic is straightforward; owner-gating is dispatcher-level per ADR-013.
