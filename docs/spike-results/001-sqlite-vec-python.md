# Spike 001: sqlite-vec Python loading

**Status:** PASS
**Date:** 2026-05-13
**Confidence:** 95%
**Decision impact:** ADR-XXX (to be linked) — SQLite driver selection for the LCM port

## Question
Can we use sqlite-vec from Python on macOS/Linux to host LCM's vec0 tables?

## Method

1. Probed three Python installs on this macOS arm64 box: Homebrew `python@3.14` (3.14.3), Homebrew `python@3.12` (3.12.13), and the macOS system `/usr/bin/python3` (3.9.6).
2. Tested whether each `sqlite3.Connection` exposes `enable_load_extension`.
3. Created two venvs on LEXAR (`.venv-spike1` on 3.14, `.venv-spike1-312` on 3.12), pip-installed `sqlite-vec` + `httpx`.
4. Ran the canonical load + KNN test verbatim from the spike brief.
5. Tested the polymorphic LCM shape: `vec0(embedding float[1024], +embedded_id text, embedded_kind text, suppressed integer)` — partition column, two auxiliary columns, and a metadata column — with MATCH + WHERE filter.
6. Verified FTS5 trigram tokenizer co-exists in the same connection.
7. Exercised the int64-binding edge case: stored `2**62` in a column and read it back.
8. Benchmarked 1k inserts and 100 KNN queries over `float[384]` using both the JSON-string and raw-bytes (`sqlite_vec.serialize_float32`) binding paths.
9. Verified PyPI wheel coverage for `sqlite-vec` 0.1.9 across `macosx_11_0_arm64`, `manylinux2014_x86_64`, and `manylinux2014_aarch64` for Python 3.10/3.11/3.12/3.13/3.14 (wheels are pure-Python `py3-none-*` so the cpXY tag isn't even relevant — same wheel for every 3.x).
10. Cross-checked `pysqlite3-binary` and `apsw` wheel availability on PyPI; installed and load-tested `apsw` as the canonical fallback.

## Findings

- **Stock Python sqlite3 on macOS (Homebrew):** Works flawlessly. `python@3.12.13` and `python@3.14.3` both ship with `--enable-loadable-sqlite-extensions` and bundle SQLite 3.53.0. `enable_load_extension(True)` returns silently; `sqlite_vec.load(conn)` succeeds; `vec_version()` reports `v0.1.9`.
- **Stock Python sqlite3 on macOS (system `/usr/bin/python3` 3.9.6):** **FAILS.** Apple's system Python is built **without** loadable-extension support: `AttributeError: 'sqlite3.Connection' object has no attribute 'enable_load_extension'`. Never let a dev rely on `/usr/bin/python3`.
- **Stock Python sqlite3 on Linux (inferred from wheel availability + Python build defaults):** Works on any distro Python 3.10+ from python.org installers, deadsnakes PPA, manylinux base images, or `pyenv` builds — all of which enable the extension flag by default. The only Linux failure mode is custom-built Python with `--disable-loadable-sqlite-extensions`, which is rare.
- **python.org Python installers (macOS .pkg):** Enable loadable extensions since 3.6 per their build script. Not locally tested (no install on this box) — see remaining-risk section.
- **pysqlite3-binary on macOS:** **No macOS wheels exist.** Latest version `0.5.4.post2` ships only `manylinux2014_x86_64` wheels (cp38 through cp314). On macOS, `pip install pysqlite3-binary` errors with `No matching distribution found`. This rules it out as the primary recommendation for a cross-platform team.
- **apsw on macOS:** Works as a fallback. `apsw 3.53.1.0` ships `macosx_11_0_arm64`, `macosx_10_9_x86_64` (and `macosx_10_13_x86_64` from cp312), and full Linux manylinux coverage for Python 3.10/3.11/3.12/3.13. Bundles its own statically-linked SQLite (3.53.1 — one patch ahead of Homebrew's). API differs from stdlib (`enableloadextension` not `enable_load_extension`, no PEP 249 `cursor()` boilerplate needed, native autocommit semantics).
- **vec0 virtual table create:** Works on both stdlib `sqlite3` and `apsw` paths.
- **KNN query:** `WHERE embedding MATCH '[...]' ORDER BY distance LIMIT k` returns sorted `(rowid, distance)` rows. Confirmed correctness with hand-picked vectors.
- **Auxiliary/metadata column polymorphic shape:** `vec0(embedding float[1024], +embedded_id text, embedded_kind text, suppressed integer)` creates and queries cleanly. Mixed MATCH + equality filter (`AND embedded_kind = 'note'`) returns the expected row. The `+` prefix (auxiliary, stored uncompressed alongside) and bare-name (partition / metadata) syntax both work as documented.
- **FTS5 trigram tokenizer co-existence:** `CREATE VIRTUAL TABLE f USING fts5(content, tokenize='trigram')` succeeds in the same connection that loaded vec0. Sub-string match (`MATCH 'sqli'`) returns the trigram-indexed row. Both extensions live happily side-by-side.
- **INTEGER/INT64 binding:** Python's stdlib `sqlite3` round-trips `2**62` (`4611686018427387904`) without truncation. **No Node BigInt/Number split exists here** — Python `int` is arbitrary-precision, and the C-level bindings use `sqlite3_bind_int64` for any int that doesn't fit in 32 bits. The LCM-Node gotcha (silent loss of precision past `2**53`) does not have a Python analog. Caveat: if you ingest IDs from a Node service via JSON, JSON itself truncates at `2**53` — that's a wire-format issue, not a driver issue.

## Recommended Python stack

- **Package:** `sqlite-vec` (PyPI) loaded via stdlib `sqlite3`.
- **Version pin:** `sqlite-vec==0.1.9` (latest, no newer version available; the package is at 0.1.x and hasn't broken API in this minor series).
- **Python:** 3.11+ recommended; 3.10 known-good; 3.12 is the production sweet spot. Avoid system `/usr/bin/python3` on macOS unconditionally.
- **SQLite floor:** 3.41+ recommended for FTS5 trigram + vec0 compatibility. Homebrew Python 3.12/3.13/3.14 all ship 3.53.0 which is well above the floor.
- **Load pattern:**
  ```python
  import sqlite3
  import sqlite_vec

  def open_lcm_db(path: str) -> sqlite3.Connection:
      conn = sqlite3.connect(path)
      conn.enable_load_extension(True)
      sqlite_vec.load(conn)
      conn.enable_load_extension(False)  # tighten attack surface after load
      return conn
  ```
- **Binding pattern (preferred — raw bytes, ~2.3x faster on insert than JSON):**
  ```python
  vec = sqlite_vec.serialize_float32([0.1, 0.2, 0.3, ...])  # returns bytes, len = 4*dim
  conn.execute("INSERT INTO v(rowid, embedding) VALUES (?, ?)", (rowid, vec))
  ```
- **Fallback driver if a stdlib problem surfaces in CI or container builds:** `apsw==3.53.1.0`. Same vec0 capability via `conn.loadextension(sqlite_vec.loadable_path())`. Note that apsw's API is NOT PEP-249 compatible — code that switches from stdlib to apsw is a rewrite of the connection layer, not a drop-in. Keep the connection-open logic isolated behind a function so the swap stays cheap.

### Gotchas

- **Apple system Python is poisoned.** `/usr/bin/python3` (3.9.6 on this box) has no `enable_load_extension` attribute. Any contributor `which python3`-ing into the system Python will hit `AttributeError`, not a clean error. Document the Homebrew (or pyenv/uv-managed) Python requirement loudly in the README and CONTRIBUTING.
- **`pysqlite3-binary` is Linux-only.** Don't put it in `pyproject.toml` as a hard dep — it'll break every Mac dev. If you ever need a newer SQLite than the stdlib ships, prefer `apsw` (cross-platform) or document an opt-in Linux extra.
- **Don't leave `enable_load_extension(True)` on permanently.** Best-practice is enable → load → disable, both because it shrinks the SQL-injection blast radius and because some hosting environments (e.g. running under a SIP-restricted Python) refuse to keep the flag on across commits.
- **JSON encoding for vectors is fine but slower.** Use `sqlite_vec.serialize_float32` for hot inserts. Both forms work as MATCH inputs.
- **Connection-per-thread.** Python's stdlib `sqlite3` enforces single-thread by default; if LCM hosts a request-per-thread server, pass `check_same_thread=False` (with your own locking) or open one connection per thread. apsw is more permissive but still recommends per-thread connections.

## Performance sanity

Measured on M-series MacBook Pro, Python 3.12.13, Homebrew, in-memory database:

| Operation                             | Latency (ms)      |
|---------------------------------------|-------------------|
| `vec_load` + create polymorphic table | <1                |
| Insert 1000 × `float[384]` (JSON)     | 17.4 ms (0.017/row) |
| Insert 1000 × `float[384]` (bytes)    | 7.7 ms (0.008/row) |
| KNN top-10 over 1000 vecs (JSON)      | 0.08 ms/query     |
| KNN top-10 over 1000 vecs (bytes)     | 0.11 ms/query     |
| FTS5 trigram MATCH over 2 rows        | <1                |

Numbers are in-memory; on-disk WAL-mode will be ~2-5x slower for inserts and near-identical for reads.

## Remaining 5% risk

1. **python.org Python installer not locally validated.** Not installed on this box. The python.org build scripts (publicly visible in `cpython` repo `Mac/BuildScript/build-installer.py`) explicitly pass `--enable-loadable-sqlite-extensions`, so we know it works in principle, but no first-hand confirmation. Mitigation: pin to "Homebrew, pyenv, or uv-managed Python" in CONTRIBUTING and avoid the question.
2. **Linux not tested first-hand.** Confirmed via wheel availability (`manylinux_2_17_x86_64`, `manylinux_2_17_aarch64`) and the fact that every mainstream Python 3.10+ build on Linux ships with extension loading enabled. A clean CI run on `ubuntu-latest` would close this — recommend adding it to the GH Actions matrix in the next PR.
3. **`sqlite-vec` is still 0.1.x.** Pre-1.0 means API breakage is theoretically possible. Strict version pin protects us; budget for one upgrade cycle per minor bump.
4. **No concurrency stress test.** A single in-process connection was used. Multi-connection WAL semantics with vec0 (specifically: does vec0 honor SQLite's locking model under writer + reader contention?) is documented as supported by the sqlite-vec README but not exercised here. Recommend a follow-up spike if LCM intends high-concurrency writes.
5. **No persistence/upgrade test.** The polymorphic-shape table was created in-memory and torn down. We didn't validate that a vec0 schema survives a `VACUUM`, a `pragma user_version` bump, or a sqlite-vec extension version upgrade. Not blocking for the port (LCM has its own migration story), but worth a sub-spike before production.
