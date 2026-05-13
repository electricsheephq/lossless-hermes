---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-01] storage: port db/connection.ts → db/connection.py'
labels: 'port, epic-01-storage'
---

## Source (TypeScript)

- File: `src/db/connection.ts`
- Lines: ~170 LOC
- Function(s)/class(es): `configureConnection(db)`, `openConnection(path, opts)`, connection registry (`connectionsByPath`, `connectionIndex`), `closeConnection(db)`, `closeConnectionsForPath(path)`, `assertForeignKeysEnabled(db)`

## Target (Python)

- File: `src/lossless_hermes/db/connection.py`
- Estimated LOC: ~220

## What this issue covers

The single sanctioned connection factory per ADR-004 invariant. Encapsulates:

1. `sqlite3.connect(path, check_same_thread=False)` (with own locking) or `apsw.Connection(path)` switched by config (per ADR-004 consequence "apsw is an opt-in `[apsw]` extra").
2. `conn.enable_load_extension(True)` → `sqlite_vec.load(conn)` → `conn.enable_load_extension(False)` (per spike-001 §"Load pattern").
3. PRAGMAs applied in exact order (per `docs/porting-guides/storage.md` §3):

   | PRAGMA | Value |
   |---|---|
   | `journal_mode` | `WAL` |
   | `busy_timeout` | `30000` (30 s — production saw OOM at 5 s default) |
   | `foreign_keys` | `ON` |
   | `assert_foreign_keys_enabled` | runtime assertion via `PRAGMA foreign_keys` readback |
   | `cache_size` | `-65536` (64 MB) |
   | `synchronous` | `NORMAL` |
   | `temp_store` | `MEMORY` |

4. **WAL-on-network-filesystem fallback** (per storage.md §10.4) — refactor or copy `apply_wal_with_fallback()` from hermes-agent's `hermes_state.py` lines 40–60. If the upstream refactor lands, import from a shared `db_utils.py`; otherwise inline the function locally.

5. Connection registry — module-level `dict[(path, thread_id), Connection]` guarded by `threading.Lock` (per ADR-007). Provides `close_lcm_connection(path)` to close all threads' connections for a path (used by test fixtures).

6. **`PRAGMA optimize` on close** — best-effort, swallow `OperationalError` (TS does the same; storage.md §3 last line).

7. **Apple system Python guard** — `__init__.py`-level probe per ADR-004 consequence: if `conn.enable_load_extension` is missing, raise an actionable error pointing at `docs/CONTRIBUTING.md` ("install Homebrew Python, pyenv, uv, or python.org Python").

## Dependencies

- Depends on: #00-01 (scaffolding — pyproject.toml + package layout from Epic 00) — must be merged first.
- Blocks: #01-03 (features probe needs an open conn), #01-04 (migration uses connection factory), #01-08 / #01-09 (stores).

## Acceptance criteria

- [ ] `open_lcm_db(path: str | Path, *, driver: Literal["sqlite3", "apsw"] = "sqlite3") -> Connection` matches the spike-001 load pattern.
- [ ] All 7 PRAGMAs are applied in the exact order documented in storage.md §3.
- [ ] `assert_foreign_keys_enabled(conn)` runs after the `foreign_keys=ON` PRAGMA and raises if the readback is 0.
- [ ] All 4 TS test cases in `test/db-connection.test.ts` have ported pytest equivalents in `tests/test_db_connection.py` (path helper purity).
- [ ] **New test:** `test_apple_system_python_guard` — patches `Connection.enable_load_extension` to be missing, asserts an actionable `RuntimeError` is raised.
- [ ] **New test:** `test_wal_fallback_on_nfs` — mounts a tmpfs filesystem that rejects WAL, asserts the connection downgrades to `DELETE` journal mode without crashing (mirror hermes-agent `hermes_state.py` test pattern).
- [ ] Function signatures match the spec in [docs/porting-guides/storage.md](../../docs/porting-guides/storage.md) §3.
- [ ] `pytest tests/test_db_connection.py` passes on macOS Homebrew Python 3.12 and on `ubuntu-latest` GH Actions runner.
- [ ] No new mypy errors (`mypy --strict src/lossless_hermes/db/connection.py`).
- [ ] PR description cites LCM commit `1f07fbd` (pr-613 head).

## Estimated effort

**4–6 hours.**

## Confidence

**95%** — spike-001 fully validated the load pattern; the only uncertainty is WAL-fallback refactor coordination with hermes-agent maintainers (storage.md §12 risk #8, mitigation = inline copy if upstream refactor stalls).
