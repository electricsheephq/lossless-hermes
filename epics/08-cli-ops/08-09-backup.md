---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-08] cli-ops: port lcm-db-backup.ts (VACUUM INTO primitive)'
labels: 'port, epic-08-cli-ops'
---

## Source (TypeScript)

- File: `src/plugin/lcm-db-backup.ts`
- Lines: 82 LOC
- Function(s)/class(es): `writeLcmDatabaseBackup(db, options)`, `buildLcmDatabaseBackupPath(databasePath, label?)`, `LcmDatabaseBackupError`

## Target (Python)

- File: `src/lossless_hermes/plugin/db_backup.py`
- Estimated LOC: ~100

## What this issue covers

The backup primitive consumed by three callers:

1. `/lcm backup` (08-01 dispatch → this module's `write_lcm_database_backup` directly).
2. `/lcm rotate` (08-16) — backs up before performing the rotation.
3. `applyDoctorCleaners` (08-08) — backs up BEFORE the destructive BEGIN IMMEDIATE.

### Algorithm

`writeLcmDatabaseBackup(db, options)` produces a fresh, independent SQLite file at `<db_path>.<timestamp>-<rand>.bak` containing the entire database, using SQLite's `VACUUM INTO` (not `.backup()` API — the TS source uses `VACUUM INTO` for atomicity and the result file is fully compacted with no WAL).

Python equivalent: stdlib `sqlite3` supports `VACUUM INTO 'path'` as a regular SQL statement. The implementation:

```python
def write_lcm_database_backup(db: Connection, *, label: str | None = None, db_path: str | Path) -> Path:
    backup_path = build_lcm_database_backup_path(db_path, label=label)
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    # VACUUM INTO requires no active transaction. The transaction-mutex
    # guarantees this — backup is called via withDatabaseTransaction(...,
    # "BEGIN") at the OUTER level, then the mutex releases before the
    # VACUUM INTO body. Defensive: ROLLBACK any in-flight transaction.
    db.execute("ROLLBACK")  # no-op if no txn
    db.execute(f"VACUUM INTO {sqlite_quote_string(str(backup_path))}")
    return backup_path
```

**Note on the original TS comment:** the task brief mentions "Uses SQLite `.backup()` API" but the TS source actually uses `VACUUM INTO`. The Python port follows the TS source (VACUUM INTO), not the brief. `.backup()` is a separate (also-supported) primitive but produces a file with WAL state — `VACUUM INTO` is preferred because the resulting file is portable and immediately usable as a fresh DB.

### Path construction (`buildLcmDatabaseBackupPath`)

Format: `<db_path>.<YYYY-MM-DDTHHMMSS>-<rand6>.bak` (or `<db_path>.<label>.<timestamp>-<rand>.bak` if `label` is provided).

Example:
- `/Users/eva/.hermes/lossless-hermes/lcm.db` + `label=None` → `/Users/eva/.hermes/lossless-hermes/lcm.db.2026-05-13T143055-a3f9b2.bak`
- `/Users/eva/.hermes/lossless-hermes/lcm.db` + `label="doctor-cleaners"` → `/Users/eva/.hermes/lossless-hermes/lcm.db.doctor-cleaners.2026-05-13T143055-a3f9b2.bak`

The 6-char random suffix prevents collisions on subsecond consecutive backups. Use `secrets.token_hex(3)`.

### Error handling

`LcmDatabaseBackupError` is raised if:
- `db_path` is `:memory:` (in-memory DB has no file to back up).
- `VACUUM INTO` fails (e.g., destination disk full, permission denied).
- Destination directory cannot be created.

The `applyDoctorCleaners` caller (08-08) intercepts `LcmDatabaseBackupError` and converts it to `{"kind": "unavailable", "reason": "..."}` per the unavailable-reason contract.

## Dependencies

- Depends on: #08-01 (dispatcher), Epic 01-01 (DB connection layer — the `Connection` type).
- Blocks: #08-08 (doctor cleaners need the mandatory backup), #08-16 (rotate uses this).

## Acceptance criteria

- [ ] `write_lcm_database_backup(db, *, label=None, db_path) -> Path` writes a fresh SQLite file via `VACUUM INTO` and returns the absolute path.
- [ ] The backup file is a valid SQLite database (verified by opening with `sqlite3.connect()` and running `PRAGMA integrity_check`).
- [ ] The backup file is fully compacted (no WAL sidecar, no `-shm` / `-wal` files alongside it).
- [ ] `build_lcm_database_backup_path(db_path, label=None)` returns the timestamp+rand path; with `label`, embeds it before the timestamp.
- [ ] Subsecond consecutive backups produce distinct paths (random suffix collision check).
- [ ] In-memory DBs (`:memory:`) raise `LcmDatabaseBackupError`.
- [ ] Permission denied / disk full surfaces as `LcmDatabaseBackupError` with the underlying `OSError` chained.
- [ ] Defensive `ROLLBACK` is a no-op when no transaction is active (test with both states).
- [ ] No active transaction state remains on the DB connection after `write_lcm_database_backup` returns (validated by `db.in_transaction is False`).
- [ ] TS source has no dedicated test for `lcm-db-backup.ts` (it's exercised via `test/v41-data-cleanup.test.ts` and `test/lcm-command.test.ts`).
- [ ] **New test:** `tests/plugin/test_db_backup.py::test_vacuum_into_produces_valid_db` — backup file passes `PRAGMA integrity_check`.
- [ ] **New test:** `tests/plugin/test_db_backup.py::test_subsecond_unique_paths` — two backups within 100ms produce different paths.
- [ ] **New test:** `tests/plugin/test_db_backup.py::test_in_memory_raises` — `:memory:` DB raises `LcmDatabaseBackupError`.
- [ ] **New test:** `tests/plugin/test_db_backup.py::test_label_in_path` — `label="doctor-cleaners"` appears in path before timestamp.
- [ ] Function signatures match the spec in [docs/porting-guides/plugin-glue.md](../../docs/porting-guides/plugin-glue.md) §"/lcm slash commands — full inventory" line 427 ("`VACUUM INTO` to `<db>.<timestamp>-<rand>.bak`").
- [ ] `pytest tests/plugin/test_db_backup.py` passes.
- [ ] No new mypy errors (`mypy --strict src/lossless_hermes/plugin/db_backup.py`).
- [ ] PR description cites LCM commit `1f07fbd` (pr-613 head).

## Estimated effort

**4 hours.**

## Confidence

**95%** — small file, well-understood SQLite primitive (`VACUUM INTO` is in stdlib `sqlite3`). The only uncertainty is the path-construction format (timestamp + random suffix); validated by the TS source.
