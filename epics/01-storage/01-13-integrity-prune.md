---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-01] storage: port integrity + prune + transaction-mutex → src/'
labels: 'port, epic-01-storage'
---

## Source (TypeScript)

Three top-level modules:

| TS file | LOC | Purpose |
|---|---:|---|
| `src/integrity.ts` | 600 | 8 integrity checks + metrics collector + repair plan |
| `src/prune.ts` | 392 | Age-based conversation pruning; dry-run / confirm / vacuum modes |
| `src/transaction-mutex.ts` | 202 | Per-DB async transaction lock + savepoint-based reentrancy + timeout |

## Target (Python)

| Python file | LOC est |
|---|---:|
| `src/lossless_hermes/integrity.py` | ~700 |
| `src/lossless_hermes/prune.py` | ~450 |
| `src/lossless_hermes/transaction_mutex.py` | ~240 |

Total ~1,390 LOC.

## What this issue covers

Three modules that round out the storage substrate. Per ADR-017 all three are **synchronous** — no `async` / `await`; the "mutex" is a `threading.RLock`-backed reentrant lock.

### `transaction_mutex.py`

Per storage.md §1 row 17 + ADR-017 consequences. Provides a per-DB-path mutex + savepoint-based reentrant transactions.

```python
@contextmanager
def with_database_transaction(conn, *, timeout: float = 30.0) -> Iterator[None]:
    """Acquire the per-DB lock + push a SAVEPOINT. Reentrant: nested calls
    on the same connection use SAVEPOINT depth tracking via contextvars.
    Per ADR-017: lock is threading.RLock, not asyncio.Lock.
    """
```

Implementation notes (per storage.md §1 row 17 + §10 risk #2):
- Module-level `dict[Connection, threading.RLock]` for per-conn locks (or per-path if we make connection-per-thread the rule).
- `contextvars.ContextVar` tracks savepoint depth for the current thread's call stack. Nested entry → `SAVEPOINT sp_<depth>`; exit → `RELEASE sp_<depth>` on success, `ROLLBACK TO sp_<depth>` on exception.
- Timeout: if lock acquisition fails within `timeout` seconds, raise `TransactionMutexTimeout`.

### `integrity.py`

Per storage.md §1 row 14. **8 integrity checks** (per TS source — enumerate in source; e.g. orphaned context_items, dangling FK references, missing identity_hash, summary cycle detection, FTS5 row-count drift, message_parts ordinal gaps, suppressed-without-marker, etc.).

```python
@dataclass(frozen=True, slots=True)
class IntegrityCheckResult:
    name: str
    status: Literal["pass", "fail", "warn"]
    detail: str | None
    counts: dict[str, int]


def run_integrity_checks(conn) -> list[IntegrityCheckResult]: ...


def build_repair_plan(results: list[IntegrityCheckResult]) -> RepairPlan: ...
```

- Pure read-only checks; no writes.
- `RepairPlan` lists candidate SQL statements with cost estimate; caller (the doctor in Epic 08) decides whether to apply.

### `prune.py`

Per storage.md §1 row 15. Age-based conversation pruning.

Methods:
- `parse_duration(raw: str) -> timedelta` — `'30d'`, `'12h'`, `'7d3h'` etc.
- `find_prune_candidates(conn, *, older_than: timedelta) -> list[PruneCandidate]`
- `prune_conversations(conn, *, conversation_ids: list[int], dry_run: bool, vacuum: bool) -> PruneResult`
- `vacuum_database(conn) -> VacuumResult`

Behavior:
- **Dry run** — return what would be deleted, no DB writes.
- **Confirm mode** — delete in batches (1000 conversations per transaction) so a kill mid-prune leaves a recoverable state.
- **VACUUM** — runs after delete to reclaim space; this is a serialized full-DB lock so must happen after the delete transactions close.
- FK CASCADE drives the actual delete chain — only DELETE FROM conversations is needed; everything else cascades.

## Dependencies

- Depends on: #01-01 (connection), #01-04 / #01-05 / #01-06 (tables — integrity checks scan them, prune CASCADEs through them), #01-08 / #01-09 (stores — integrity uses some store helpers).
- Blocks: Epic 08 (doctor + ops surfaces use integrity + prune).

### Internal ordering inside this issue

Per storage.md §9 phases 2 and 6:
1. `transaction_mutex.py` first (phase 2 — required by stores). **Actually #01-08 / #01-09 depend on this — confirm ordering in PR comments.**
2. `prune.py` second (phase 6).
3. `integrity.py` last (phase 6 — depends on conv + summary stores).

## Acceptance criteria

### transaction_mutex

- [ ] `with_database_transaction(conn)` acquires the per-conn lock, pushes a SAVEPOINT, releases on exit.
- [ ] Nested re-entry on the same thread + same conn uses incrementing SAVEPOINT names (`sp_0`, `sp_1`, ...).
- [ ] Exception inside the block ROLLBACK TO the current SAVEPOINT and re-raises.
- [ ] Timeout: a second thread trying to enter a held lock raises `TransactionMutexTimeout` after `timeout` seconds.
- [ ] All 8 `it`-blocks in `test/transaction-mutex.test.ts` (storage.md §8 row 20) ported to `tests/test_transaction_mutex.py`:
  - Lock serialization
  - Nested savepoint
  - Cross-store reuse
  - **10-way concurrent stress test** (storage.md §12 risk #1 — verify Python sync version handles the same load).
  - Other 4 cases per the TS source enumeration.

### integrity

- [ ] All 8 checks implemented per TS source enumeration.
- [ ] `run_integrity_checks(conn)` returns a list of `IntegrityCheckResult`; pass / fail / warn statuses correctly assigned.
- [ ] `build_repair_plan(results)` returns SQL candidates with cost estimates; no writes performed.
- [ ] Test fixture with one orphaned context_items row produces a `fail` result for the `orphan_context_items` check.
- [ ] Test fixture with summary cycle (A → B → A) produces a `fail` result for the `summary_cycle_detection` check.
- [ ] TS test cases (no dedicated `integrity.test.ts` in storage.md §8 — coverage is via `lcm-integration.test.ts`) — port the integrity-relevant subset to `tests/test_integrity.py`.

### prune

- [ ] `parse_duration` handles `'30d'`, `'12h'`, `'7d3h'`, `'30m'` and raises `ValueError` on bad input.
- [ ] All **18 cases** from `test/prune.test.ts` (storage.md §8 row 19 — parseDuration; dry-run; batch deletion; VACUUM; cascade behavior) ported to `tests/test_prune.py`.
- [ ] Batch deletion: 5000 conversations prune cleanly in batches of 1000.
- [ ] VACUUM runs after delete; verified by querying `pragma_page_count` before and after.
- [ ] Dry-run is read-only (verified by checking row counts before and after a dry-run pass — unchanged).
- [ ] FK CASCADE verified: pruning a conversation removes corresponding rows from `messages`, `message_parts`, `summaries`, `summary_messages`, `summary_parents`, `context_items`, `large_files`, `conversation_bootstrap_state`, `conversation_compaction_telemetry`, `conversation_compaction_maintenance`.

### Common

- [ ] `pytest tests/test_transaction_mutex.py tests/test_integrity.py tests/test_prune.py` passes (~30+ cases total).
- [ ] `mypy --strict` passes.
- [ ] PR description cites LCM commit `1f07fbd` and the three TS source files.

## Estimated effort

**13–18 hours combined** (5 h prune + 4–6 h mutex + 6–8 h integrity per storage.md §1 table).

## Confidence

**90%** — TS sources are well-structured. Residual risk: (a) the `AsyncLocalStorage → contextvars` translation for savepoint depth (storage.md §12 risk #2 — verified by the 10-way stress test); (b) 10-way concurrent transaction stress on Python sync `threading.RLock` has different contention shape than TS asyncio (storage.md §12 risk #1 — needs the same test); (c) `PRAGMA optimize` on connection close (storage.md §12 risk #3).
