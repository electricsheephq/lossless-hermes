---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-05] db: open_db() factory loads sqlite-vec + apsw fallback'
labels: 'port, embeddings, db, vec0'
---

## Source (TypeScript)
- File: `lossless-claw/src/embeddings/store.ts` (lines 122-146 — `tryLoadSqliteVec`; lines 83-100 — `candidateVec0Paths` which Python drops per spike 001) plus the existing Hermes `src/lossless_hermes/db/connection.py` (created in Epic 01).
- Lines: ~25 (collapsed from ~50 TS LOC because Python's `sqlite_vec.load()` finds its own bundled extension).
- Function(s)/class(es): `tryLoadSqliteVec` plus connection-open. Python target: `open_db()` factory in `db/connection.py`.

## Target (Python)
- File: `src/lossless_hermes/db/connection.py` (extend the file created in Epic 01)
- Estimated LOC: ~80 new (factory + apsw branch + helper)

## Dependencies
- Depends on: Epic 01 (the file `src/lossless_hermes/db/connection.py` exists from Epic 01 with the basic `sqlite3.connect()` + PRAGMA setup).
- Blocks: #05-03 (vec0 store needs the load pattern); #05-07 (backfill needs an open connection with vec0 loaded); #05-08, #05-09 (semantic + hybrid search transitively).

## Acceptance criteria

- [ ] **`open_db(path, *, role="gateway") -> sqlite3.Connection`** factory:
  1. Opens stdlib `sqlite3.connect(path)`.
  2. Applies PRAGMAs (defined in Epic 01 — `journal_mode=WAL`, `synchronous=NORMAL`, etc.).
  3. **Sets `busy_timeout`** per ADR-018: `30_000` for `role="gateway"`, `5_000` for `role="worker"`. (Gateway always wins contention.)
  4. **`enable_load_extension(True)`** — fail loudly with a clear error if `AttributeError` (Apple system Python — see spike 001 §"Gotchas").
  5. **`sqlite_vec.load(conn)`** — bundled extension auto-discovered from the PyPI package.
  6. **`enable_load_extension(False)`** to tighten the attack surface after load (spike 001 recommendation).
  7. Returns the connection.
- [ ] **Apple-system-Python diagnostic:** if `enable_load_extension` raises `AttributeError`, the error message must be actionable: `"lossless-hermes requires a Python build with --enable-loadable-sqlite-extensions. /usr/bin/python3 on macOS is NOT supported. Install Homebrew Python (python@3.12 or newer) and re-run."`. Documented in CONTRIBUTING (Epic 00) but the runtime error must reproduce the guidance.
- [ ] **`apsw` fallback** (gated on `[apsw]` extra being installed per ADR-005):
  - Detect via `try: import apsw; HAS_APSW = True except ImportError: HAS_APSW = False`.
  - If `[apsw]` extra is installed AND the stdlib path fails with `OperationalError` (rare on supported platforms), fall through to `apsw.Connection(path)` with `conn.loadextension(sqlite_vec.loadable_path())`. apsw's API differs (`enableloadextension` not `enable_load_extension`, no PEP-249 cursor boilerplate, native autocommit) — isolate apsw-specific code behind a single `_open_with_apsw(path, role)` helper.
  - **Caller-facing API stays identical.** Both branches return a connection-like object that exposes `.execute`, `.executemany`, `.commit`, `.close`. Document the API surface in a `Connection` protocol or `typing.Protocol` so type-checkers see a uniform contract.
  - apsw is **opt-in only** — the default install path uses stdlib `sqlite3`. Don't pin `apsw` as a hard dep (it's in `[project.optional-dependencies].apsw` per Epic 00).
- [ ] **`try_load_sqlite_vec(conn, *, silent=False) -> bool`** (port of `store.ts:122-146`):
  ```python
  def try_load_sqlite_vec(conn, *, silent=False) -> bool:
      try:
          conn.enable_load_extension(True)
          sqlite_vec.load(conn)
          conn.enable_load_extension(False)
          return True
      except (AttributeError, sqlite3.OperationalError) as e:
          if not silent:
              logger.warning(f"[db.connection] failed to load sqlite-vec: {e}")
          return False
  ```
  Used by paths that want to gracefully degrade (e.g. `runSemanticSearch` raises `SemanticSearchUnavailableError` when this returns False — see #05-08).
- [ ] **`vec0_version(conn) -> str | None`** (port of `store.ts:152-159`): runs `SELECT vec_version()`; returns `None` on `OperationalError` (extension not loaded). Used by `/lcm health` and the `runSemanticSearch` precondition check.
- [ ] **The `candidateVec0Paths` TS function is dropped.** TS searched `~/.openclaw/extensions/node_modules/sqlite-vec-<platform>-<arch>/vec0.<ext>` because LCM bundles the binary with the plugin install. Python's `sqlite_vec.load(conn)` handles this internally via the PyPI wheel. Document the drop in a code comment so future contributors don't reintroduce the path search.
- [ ] **`mypy --strict` and `ty check`** pass — including the `Connection` protocol for the stdlib/apsw uniform surface.
- [ ] PR description references spike 001 and ADR-004/005.

## Tests (`tests/db/test_connection.py`)

- `open_db("test.db")` returns a connection with vec0 loaded; `vec0_version(conn)` returns a string.
- `open_db(..., role="gateway")` sets `busy_timeout=30000`; `role="worker"` sets `5000`.
- `try_load_sqlite_vec(conn)` returns True on a connection where the extension is loadable; False when not.
- `vec0_version(conn)` returns None when extension not loaded (uses a `:memory:` connection without `enable_load_extension`).
- **Apple-system-Python diagnostic:** mock `enable_load_extension` to raise `AttributeError`; assert `open_db` raises with the documented actionable message.
- **apsw fallback (gated on `HAS_APSW`):** `@pytest.mark.skipif(not HAS_APSW)` block:
  - `open_db(path)` with apsw extra → `apsw.Connection` (or compatible) with vec0 loaded.
  - `conn.execute("SELECT vec_version()")` returns the version row.
- Integration test (matrix cell): on a Linux `ubuntu-latest` GH Actions runner, `open_db` works without `[apsw]` extra — closing spike 001's risk #2 (Linux not first-hand tested).

## Estimated effort
2 hours

## Confidence
95% — spike 001 verified the load pattern on Homebrew Python 3.12.13 and 3.14.3 (both bundle SQLite 3.53.0 with `--enable-loadable-sqlite-extensions`). The `sqlite_vec.load()` PyPI auto-discovery removes the `candidateVec0Paths` complexity entirely. Residual 5%:
- Linux not locally validated in spike 001 (inferred from wheel availability + build defaults). CI matrix on `ubuntu-latest` closes this.
- apsw's API differs structurally (`enableloadextension` no underscore, no PEP-249 cursor). The protocol-based abstraction needs careful test coverage so the swap stays cheap. Spike 001 confirmed `apsw==3.53.1.0` ships full Linux + macOS wheels.

## Files to read before starting
- `docs/spike-results/001-sqlite-vec-python.md` (entire — 92 LOC, especially §"Recommended Python stack" and §"Gotchas")
- `docs/porting-guides/embeddings.md` §"Load pattern (Python, Spike 001 = PASS)" (lines 490-540)
- `docs/adr/004-sqlite3-backend.md` (stdlib primary; apsw opt-in)
- `docs/adr/005-python-version.md` (Python 3.11+; Apple system Python forbidden)
- TS source: `lossless-claw/src/embeddings/store.ts:83-146`
