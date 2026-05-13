---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-08] cli-ops: port /lcm rotate (force DB rotation)'
labels: 'port, epic-08-cli-ops'
---

## Source (TypeScript)

- File: `src/plugin/lcm-command.ts` (the `case "rotate"` body) + `src/engine.ts:rotateSessionStorageWithBackup`
- Lines: ~120 LOC inside lcm-command.ts + ~80 LOC inside engine.ts
- Function(s)/class(es): `case "rotate"` handler, `rotateSessionStorageWithBackup({ sessionId, backupLabel, lockTimeoutMs })`

## Target (Python)

- File: `src/lossless_hermes/plugin/commands/rotate.py` + `src/lossless_hermes/engine/lifecycle.py` (rotate method)
- Estimated LOC: ~150 (commands/rotate.py) + ~100 (engine method)

## What this issue covers

`/lcm rotate` — force a DB rotation if applicable. **Note:** in Hermes (per ADR-024 §"Consequences" line 189 + Epic 01 README line 95: "JSONL bootstrap, file-anchor checkpointing, session-file rollover" drop entirely), rotation is **SQLite-only**. The TS source rotates the session's JSONL transcript file; the Hermes port has no JSONL — there's no transcript file to rotate. What `/lcm rotate` becomes in the port:

1. **Backup the current DB** via `write_lcm_database_backup` (08-09) with label `"rotate"`.
2. **Force a fresh assemble snapshot** — clears `engine._previous_assembled_messages_by_conversation` for the current session, so the next assemble pass rebuilds from scratch (without the prefix-stability snapshot).
3. **Optionally run `PRAGMA wal_checkpoint(TRUNCATE)`** to compact the WAL sidecar.
4. **Write `state_meta.last_rotate_at = NOW()`** so `/lcm status` can show "last rotated N ago".

This is a meaningful operator action but a much narrower one than the TS rotation, which physically renamed the JSONL transcript file.

### Per plugin-glue.md §"/lcm slash commands — full inventory" line 427:

> `/lcm rotate` — Rotate the current session JSONL transcript (creates timestamped backup, replaces with bootstrap+fresh-tail). Calls `engine.rotateSessionStorageWithBackup` with 30s DB lock timeout.

The Hermes equivalent: "Create a timestamped backup of the lcm.db, clear the assemble snapshot cache for the current session, optionally compact the WAL." The 30s DB lock timeout applies to the backup step (`VACUUM INTO` holds the connection briefly).

### Algorithm

```python
def run_rotate(parsed: ParsedLcmCommand, *, engine: LcmContextEngine) -> str:
    session_id = engine.current_session_id
    if session_id is None:
        return "[lcm] rotate: no active session"

    # 1. Backup with label="rotate". 30s lock timeout per TS contract.
    try:
        backup_path = write_lcm_database_backup(
            engine.db,
            label="rotate",
            db_path=engine.db_path,
        )
    except LcmDatabaseBackupError as exc:
        return f"[lcm] rotate failed at backup step: {exc}"

    # 2. Clear assemble snapshot cache for this session (forces fresh rebuild next turn).
    engine.clear_assemble_snapshot(session_id)

    # 3. Optional WAL compaction. Best-effort.
    try:
        engine.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError:
        pass

    # 4. state_meta.last_rotate_at = now.
    engine.write_state_meta("last_rotate_at", datetime.utcnow().isoformat() + "Z")

    return (
        f"[lcm] rotate complete\n"
        f"Backup: {backup_path}\n"
        f"Snapshot cache cleared for session {session_id}\n"
        f"WAL compacted; state_meta.last_rotate_at updated"
    )
```

### Not owner-gated

Per plugin-glue.md line 427: rotate is NOT in the owner-gated list. It's safe for any agent to call — it creates a new file (read-only writer on existing data) and clears in-memory cache.

## Dependencies

- Depends on: #08-01 (dispatcher), #08-09 (DB backup primitive), Epic 02 (engine + `current_session_id`).
- Blocks: nothing.

## Acceptance criteria

- [ ] `run_rotate(parsed, engine) -> str` produces a backup, clears the assemble snapshot cache, optionally checkpoints WAL, and writes `state_meta.last_rotate_at`.
- [ ] No active session → returns `"[lcm] rotate: no active session"` and does nothing else.
- [ ] Backup failure → returns the error message; no partial state changes.
- [ ] `engine.clear_assemble_snapshot(session_id)` removes the per-session entry from `_previous_assembled_messages_by_conversation` (verified via direct dict inspection in test).
- [ ] WAL checkpoint failure is swallowed (best-effort).
- [ ] `state_meta.last_rotate_at` is ISO8601 UTC.
- [ ] No JSONL file is touched (no `.jsonl` file paths appear in the function body — invariant from ADR-024 / Epic 01 README).
- [ ] Not owner-gated (verified by `tests/commands/test_owner_gating.py` NOT being expected to reject `/lcm rotate`).
- [ ] TS test coverage: `test/lcm-command.test.ts::"/lcm rotate creates backup"` exists; port to `tests/commands/test_rotate.py`.
- [ ] **New test:** `tests/commands/test_rotate.py::test_no_active_session` — `engine.current_session_id=None` → no-op message.
- [ ] **New test:** `tests/commands/test_rotate.py::test_clears_assemble_snapshot` — seed `_previous_assembled_messages_by_conversation`, run rotate, assert cleared.
- [ ] **New test:** `tests/commands/test_rotate.py::test_state_meta_written` — confirms `last_rotate_at` row.
- [ ] **New test:** `tests/commands/test_rotate.py::test_no_jsonl_touched` — no file access outside the lcm.db file family (`grep -nr "\.jsonl" src/lossless_hermes/plugin/commands/rotate.py` returns 0 lines).
- [ ] Function signatures match the spec in [docs/porting-guides/plugin-glue.md](../../docs/porting-guides/plugin-glue.md) §"/lcm slash commands — full inventory" line 427.
- [ ] `pytest tests/commands/test_rotate.py` passes.
- [ ] No new mypy errors (`mypy --strict src/lossless_hermes/plugin/commands/rotate.py`).
- [ ] PR description cites LCM commit `1f07fbd` (pr-613 head) AND ADR-024 (JSONL drop).

## Estimated effort

**4 hours.**

## Confidence

**90%** — the Hermes-equivalent rotation is much simpler than the TS JSONL-rotate path (which is dropped per ADR-024). The 10% risk is operator-expectation drift: OpenClaw users who used `/lcm rotate` to start a fresh transcript will see different behavior. Documented in the README's upgrade-from-OpenClaw section.
