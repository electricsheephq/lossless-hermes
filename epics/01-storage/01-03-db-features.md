---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-01] storage: port db/features.ts → db/features.py (FTS5 + trigram probes)'
labels: 'port, epic-01-storage'
---

## Source (TypeScript)

- File: `src/db/features.ts`
- Lines: ~61 LOC
- Function(s)/class(es): `probeFts5Available(db)`, `probeTrigramTokenizerAvailable(db)`, `getDbFeatures(db)` (cached per-conn).

## Target (Python)

- File: `src/lossless_hermes/db/features.py`
- Estimated LOC: ~80

## What this issue covers

Runtime feature probes for FTS5 + trigram tokenizer (per `docs/porting-guides/storage.md` §1 row 4 and spike-005 §"Coordination with sqlite-vec"). Both probes attempt a `CREATE VIRTUAL TABLE ... USING fts5(...)` against a temporary table inside a SAVEPOINT, catch `OperationalError`, then `ROLLBACK TO SAVEPOINT` to leave no schema residue.

The probe result is cached per-connection in a module-level `WeakKeyDictionary[Connection, DbFeatures]`. This matches the TS pattern (one probe per `Connection` instance, even though TS uses a `WeakMap`).

```python
@dataclass(frozen=True, slots=True)
class DbFeatures:
    fts5_available: bool
    trigram_tokenizer_available: bool


def get_db_features(conn: sqlite3.Connection | apsw.Connection) -> DbFeatures: ...
```

### Probe specifics

- **FTS5 probe:** `CREATE VIRTUAL TABLE _probe_fts5_<random> USING fts5(content, tokenize='porter unicode61')` inside a SAVEPOINT, then `DROP` + `ROLLBACK TO`. Per spike-005 §"SQLite versions found": this succeeds on every Python 3.11+ on macOS/Linux.
- **Trigram probe:** Same pattern but `tokenize='trigram'`. Per spike-005: sanity-checked via `tokenize='not_a_real_tokenizer'` failing (so the trigram-success answer is genuine, not a no-op). Per storage.md §12 risk #4: SQLite 3.34+ (Dec 2020) is the floor — Python 3.11 ships 3.39, so we are well above. Defensive: if the probe fails, set `trigram_tokenizer_available = False` and skip the `summaries_fts_cjk` table at migration time (graceful degrade).

### Why the random suffix on the probe table name

If a parallel test fixture runs against the same in-memory DB, two concurrent probes must not collide. Use `secrets.token_hex(4)` for the suffix.

## Dependencies

- Depends on: #01-01 (DB connection — probe needs an open `Connection`).
- Blocks: #01-04 (migration uses `fts5_available` to decide whether to CREATE `messages_fts`), #01-05 (FTS5 tables), #01-08 / #01-09 (stores branch on `fts5_available` and `trigram_tokenizer_available`).

## Acceptance criteria

- [ ] `get_db_features(conn) -> DbFeatures` matches the TS interface shape.
- [ ] First call performs both probes inside a SAVEPOINT; subsequent calls return the cached value without re-probing (verified via `pytest`-level call counter).
- [ ] FTS5 probe leaves zero residue in `sqlite_master` (verified before/after).
- [ ] Trigram probe likewise.
- [ ] **Negative test:** mock `conn.execute` to raise `OperationalError("no such tokenizer: trigram")` on the trigram probe; assert `trigram_tokenizer_available is False` and that migration would skip the CJK table.
- [ ] **Positive test:** on Homebrew Python 3.12+, assert both fields are `True`.
- [ ] Cache is a `WeakKeyDictionary` so it doesn't leak across closed connections (verified by closing the conn and asserting the cache entry is gone after the connection is GC'd).
- [ ] `pytest tests/test_db_features.py` passes on macOS Homebrew Python 3.12 and Ubuntu 3.13 CI runners.
- [ ] No new mypy errors.
- [ ] PR description cites LCM commit `1f07fbd` and `src/db/features.ts:33-38` for the probe pattern.

## Estimated effort

**2 hours.**

## Confidence

**98%** — spike-005 validated every interpreter we target. The only residual risk is custom-compiled Python with FTS5 disabled (storage.md §12 risk #4) — which the graceful-degrade path already handles.
