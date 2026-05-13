# ADR-004: Python sqlite3 backend

**Status:** Accepted
**Date:** 2026-05-13
**Confidence:** 95%
**Supersedes:** —
**Superseded by:** —

## Context

LCM requires a Python SQLite driver capable of:

1. Loading the `sqlite-vec` extension to host `vec0` virtual tables (KNN over float32 embeddings).
2. Running FTS5 + trigram tokenizer queries in the same connection (`messages_fts`, `summaries_fts`, `summaries_fts_cjk`).
3. Running on macOS arm64, macOS x86_64, and Linux x86_64/arm64 — the platforms Hermes itself supports.
4. Round-tripping `int64` values without silent truncation (LCM IDs are arbitrary-precision).
5. Surviving WAL-mode concurrency on a single `lcm.db` file (ADR-003).

Three Python SQLite stacks are real options:

- **Stock `import sqlite3`** (Python stdlib).
- **`pysqlite3-binary`** (PyPI; bundles a newer SQLite than the host).
- **`apsw`** (PyPI; non-PEP-249 API; bundles its own SQLite).

## Options considered

### Option A: Stdlib `import sqlite3` (primary) + `apsw==3.53.1.0` documented as opt-in `[apsw]` extra

- Description: use the Python standard library `sqlite3` module via `sqlite_vec.load(conn)`. Provide `apsw` as a fallback for environments where stdlib `sqlite3` lacks `enable_load_extension`. Keep `open_lcm_db()` as a single function with a switch.
- Pros:
  - **Zero install footprint.** Stdlib `sqlite3` is bundled with every supported Python build.
  - **Proven working.** Spike 001 PASS — Homebrew Python 3.12.13 and 3.14.3 both successfully load `sqlite-vec`, create `vec0` tables, run KNN queries, and host FTS5 + trigram in the same connection.
  - **FTS5 + trigram in stdlib.** Spike 005 PASS — every Python ≥ 3.11 ships SQLite ≥ 3.39, well above the SQLite 3.34 floor for trigram. Spike 005 §"SQLite versions found" confirms 3.51 / 3.53 on every tested interpreter.
  - **Hermes itself uses stdlib `sqlite3`.** `hermes_state.py` runs `tokenize='trigram'` on stdlib `sqlite3` in production (`/Volumes/LEXAR/Claude/hermes-agent/hermes_state.py:253-306`, spike 005 §Method step 7). Using the same driver removes a class of integration risk.
  - **PEP-249 compatible.** Standard cursor / connection / context-manager semantics. Code is portable across `apsw` fallback later if `open_db()` is the only switch point.
  - **`apsw` fallback is real.** Spike 001 confirmed `apsw==3.53.1.0` works as a cross-platform fallback (`macosx_11_0_arm64`, `manylinux2014_*` wheels for Python 3.10-3.13). API is non-PEP-249, so isolate behind `open_lcm_db()` to keep the swap cheap.
  - **Int64 binding is correct.** Stdlib `sqlite3` uses `sqlite3_bind_int64` for any int that doesn't fit in 32 bits; spike 001 confirmed round-trip of `2**62` without truncation. There is no Node-style BigInt/Number split in Python — `int` is arbitrary-precision (spike 001 §Findings, INTEGER/INT64 row).
- Cons:
  - **Apple `/usr/bin/python3` is unsupported.** Spike 001 confirmed: system Python (3.9.6) lacks `enable_load_extension` — `sqlite_vec.load()` raises `AttributeError`. Operators must install a non-system Python.
  - **Pre-1.0 `sqlite-vec` (0.1.9).** Minor bumps could break the API surface; exact pin protects us per bump.
- Evidence cited:
  - Spike 001 §Findings — stdlib `sqlite3` loads `vec0` on Homebrew Python 3.12 and 3.14; Apple system Python fails.
  - Spike 005 §"SQLite versions found" — every tested interpreter has FTS5 + trigram via stdlib.
  - Spike 005 §Method step 7 — Hermes's `hermes_state.py` already uses stdlib `sqlite3` with `tokenize='trigram'` in production.
  - `dependencies.md:14,29` — `sqlite-vec==0.1.9` and `apsw==3.53.1.0` pins.
  - Spike 001 §"Gotchas" — non-PEP-249 caveat and `open_lcm_db()` isolation pattern.

### Option B: `pysqlite3-binary` as primary driver

- Description: pin `pysqlite3-binary==0.5.4.post2` as a hard dep; bundle a newer SQLite than the host's.
- Pros:
  - Bundles SQLite ≥ 3.46 regardless of host stdlib version.
- Cons:
  - **No macOS wheels exist.** Spike 001 §Findings confirmed `pysqlite3-binary 0.5.4.post2` ships `manylinux2014_x86_64`-only wheels (cp38-cp314). On macOS, `pip install pysqlite3-binary` errors with `No matching distribution found`. This breaks every Mac contributor — instant deal-breaker.
  - We don't have a "need newer SQLite" problem. Homebrew Python 3.12/3.13/3.14 already ship 3.53.0 — five major releases past the FTS5-trigram floor (3.34 from Dec 2020).
- Evidence cited:
  - Spike 001 §Findings → "pysqlite3-binary on macOS: No macOS wheels exist".
  - `dependencies.md:30` — explicitly NOT pinned, NOT recommended.

### Option C: `apsw` as primary driver

- Description: pin `apsw==3.53.1.0` as a hard dep; use `conn.loadextension(sqlite_vec.loadable_path())` for vec0.
- Pros:
  - Cross-platform wheels: macOS arm64 + x86_64 + Linux manylinux2014 across Python 3.10-3.13.
  - Bundles its own statically-linked SQLite (3.53.1 — one patch ahead of Homebrew's).
  - Permissive concurrency model (more flexible than stdlib's `check_same_thread`).
- Cons:
  - **Not PEP-249.** The API differs from stdlib in load-bearing ways: `enableloadextension` (no underscore), no cursor boilerplate, native autocommit semantics, no `executemany` return semantics. Code that targets `apsw` cannot trivially swap to stdlib later.
  - **No drop-in.** Switching from `apsw` to stdlib (or vice-versa) means rewriting the connection layer, not just the open function.
  - Larger install footprint (the wheel is ~4 MB vs zero).
  - Solves a problem we don't have — stdlib `sqlite3` already works on 100% of supported platforms (per spike 001 + spike 005).
- Evidence cited:
  - Spike 001 §Findings → "apsw on macOS" (works, but non-PEP-249).
  - Spike 001 §"Recommended Python stack" — apsw documented as fallback, not primary.

## Decision

Chosen: **Option A — stock `import sqlite3` (stdlib) as primary; `apsw==3.53.1.0` documented as opt-in `[apsw]` extra fallback.**

Connection-open logic is isolated behind a single `open_lcm_db()` factory so the apsw swap stays cheap when needed.

## Rationale

Spike 001 and spike 005 are unambiguous: stdlib `sqlite3` covers 100% of our supported platform matrix when the host uses a non-system Python (Homebrew, pyenv, uv-managed, or python.org installer). Hermes itself runs FTS5 + trigram on stdlib `sqlite3` in production (`hermes_state.py:253-306`, spike 005 §Method step 7), so we are sharing a driver path that the host platform has already proven.

`pysqlite3-binary` (Option B) is structurally disqualified — no macOS wheels exist (spike 001), and we don't have the "newer SQLite than the host" problem it solves.

`apsw` (Option C) is a real cross-platform alternative, but it solves a problem we don't have. Worse, its non-PEP-249 API makes it non-drop-in — adopting it as primary now would force a more invasive rewrite than keeping it as an opt-in fallback. Spike 001's recommendation is to keep `open_lcm_db()` isolated as a single function so the swap stays cheap.

Apple's `/usr/bin/python3` is poisoned (spike 001 §Findings — `AttributeError: enable_load_extension`) regardless of which option we pick. This isn't a sqlite3 backend question; it's a "use a real Python" question, and the README must say so loudly.

## Consequences

- **Apple `/usr/bin/python3` is UNSUPPORTED.** Documented in README and CONTRIBUTING: "use Homebrew Python, pyenv, uv-managed Python, or the python.org installer. Do not use `/usr/bin/python3`." A startup probe in `lossless_hermes/__init__.py` raises an actionable error if `enable_load_extension` is missing on the current connection (spike 001 §"Gotchas").
- **`open_lcm_db()` is the only sanctioned connection factory.** It encapsulates: open → `enable_load_extension(True)` → `sqlite_vec.load(conn)` → `enable_load_extension(False)` → PRAGMA tunings → return conn (spike 001 §"Load pattern"). Nothing else opens `lcm.db`.
- **`apsw` is an opt-in `[apsw]` extra.** `pip install lossless-hermes[apsw]` installs `apsw==3.53.1.0`. `open_lcm_db()` checks a config flag (`storage.driver: apsw`) to decide which path to take.
- **Vector binding uses `sqlite_vec.serialize_float32(...)`.** ~2.3× faster on insert than JSON-string per spike 001 §"Performance sanity". Both paths work; bytes is preferred on the hot ingest path.
- **`enable_load_extension(False)` is called after load.** Tightens the attack surface and matches the spike-001 recommended pattern.
- **Connection-per-thread.** Stdlib `sqlite3` enforces single-thread by default; LCM passes `check_same_thread=False` plus its own lock, OR opens one connection per worker thread (spike 001 §"Gotchas"). Decision is operator-controlled via config; default is per-thread connections.
- **Precluded:** no `pysqlite3-binary` anywhere (would silently break Mac contributors). No mixing of stdlib + apsw connections within the same process — pick one driver per process lifetime via config.
- **Invariant:** every SQLite import path lives behind `open_lcm_db()`. No raw `sqlite3.connect()` calls scattered through the codebase.

## Open questions / 5% uncertainty

1. **python.org Python installer not locally validated.** Spike 001 §"Remaining 5% risk" item 1 — strong inference (CPython build script enables `--enable-loadable-sqlite-extensions`), but not first-hand tested on this box. Mitigation: CI matrix in the first PR adds `macos-latest` with the python.org installer.
2. **Linux not first-hand tested.** Spike 001 §"Remaining 5% risk" item 2 — strong inference from wheel coverage + Hermes's own production Linux usage of stdlib sqlite3. Mitigation: GH Actions matrix adds `ubuntu-latest` and `python:3.11-slim` / `python:3.13-alpine`.
3. **`sqlite-vec` is still 0.1.x.** Spike 001 §"Remaining 5% risk" item 3 — pre-1.0 API breakage is theoretically possible. Mitigation: exact-pin `==0.1.9`; budget one upgrade cycle per minor bump.
4. **Multi-process WAL with vec0.** Spike 001 §"Remaining 5% risk" item 4 — vec0 + WAL multi-process behavior is documented as supported but not exercised. Mitigation: follow-up spike before high-concurrency production.
5. **`apsw` fallback path tested only on `vec0`, not on full LCM schema.** Spike 001 validated apsw for vec0 but not for the full 27-table LCM migration ladder. Mitigation: an `[apsw]` CI lane on first PR — runs the full migration suite under apsw.