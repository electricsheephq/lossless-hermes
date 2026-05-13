# Spike 005: Python sqlite3 FTS5 + trigram tokenizer

**Status:** PASS — stdlib `sqlite3` is sufficient. No `pysqlite3-binary` or `apsw` needed for the FTS5 + trigram surface LCM uses.
**Date:** 2026-05-13
**Confidence:** 95%
**Decision impact:** ADR-XXX — SQLite driver selection (composes with spike 001's recommendation; same stdlib `sqlite3` carries both vec0 and trigram-FTS5).

## Question
Can we use Python's stdlib `sqlite3` for FTS5 + trigram + bm25, or do we need `pysqlite3-binary` / `apsw`?

## Method

1. Probed four locally-available Python interpreters on macOS arm64 (Apple/Xcode `/usr/bin/python3` 3.9.6, Homebrew `python@3.12` 3.12.13, Homebrew `python@3.13` 3.13.12, Homebrew `python@3.14` 3.14.3).
2. For each, queried `sqlite3.sqlite_version` and `PRAGMA compile_options` to confirm FTS5 is compiled in.
3. Ran a sanity test (`tokenize='not_a_real_tokenizer'`) to verify `CREATE VIRTUAL TABLE` does NOT silently swallow bogus tokenizer names — i.e. when the trigram create succeeds, the tokenizer is genuinely there.
4. Exercised every FTS5 feature LCM uses against each interpreter: `CREATE VIRTUAL TABLE ... USING fts5(...)`, `MATCH`, `tokenize='porter unicode61'`, `tokenize='trigram'`, `bm25(table)`, `bm25(table, weight1, weight2, ...)`, `rank` column, `snippet(...)`, `highlight(...)`, `tokenize="unicode61 remove_diacritics 2"`, contentless tables (`content=''`).
5. Inspected the actual tokens produced by the trigram tokenizer via `fts5vocab` to verify it is generating 3-grams (not just accepting the keyword and behaving as a no-op).
6. Cross-checked LCM's `src/store/conversation-store.ts`, `src/store/summary-store.ts`, and `src/db/migration.ts` for the exact FTS5 syntax in use, plus `src/db/features.ts` for the runtime probe LCM does today.
7. Verified that hermes-agent's own `hermes_state.py` (`/Volumes/LEXAR/Claude/hermes-agent/hermes_state.py:253-306`) already declares `CREATE VIRTUAL TABLE ... USING fts5(content)` and `... USING fts5(content, tokenize='trigram')` plus the bookkeeping triggers, and uses **`import sqlite3`** (stdlib) — confirming Hermes itself already runs on whichever Python's stdlib `sqlite3` is available.
8. Cross-referenced `pyproject.toml` (`requires-python = ">=3.11"`) and the Python→bundled-SQLite version map (Python 3.11→SQLite 3.39, 3.12→3.43, 3.13→3.45, 3.14→3.49+) against the SQLite version floor for trigram (3.34, 2020).

## SQLite versions found

| Python | SQLite version | FTS5 | porter+unicode61 | trigram | bm25 | rank | snippet | highlight | remove_diacritics |
|---|---|---|---|---|---|---|---|---|---|
| `/usr/bin/python3` (Apple) 3.9.6 | **3.51.0** | YES | OK | OK (CJK + Latin) | OK | OK | OK | OK | OK |
| Homebrew `python@3.12` 3.12.13 | **3.53.0** | YES | OK | OK | OK | OK | OK | OK | OK |
| Homebrew `python@3.13` 3.13.12 | **3.53.0** | YES | OK | OK | OK | OK | OK | OK | OK |
| Homebrew `python@3.14` 3.14.3 | **3.53.0** | YES | OK | OK | OK | OK | OK | OK | OK |

Sanity check passed: `tokenize='not_a_real_tokenizer'` raises `OperationalError: no such tokenizer: not_a_real_tokenizer`. So when `tokenize='trigram'` succeeds, the tokenizer is genuinely registered. `fts5vocab` confirms it emits actual 3-grams (' br', 'bro', 'row', 'own', 'wn ', ...) — not a no-op.

`pysqlite3-binary` and `apsw` were **not** installed for this spike, because the stdlib answer is unambiguously yes. Spike 001 already validated `apsw` as a fallback for the vec0 surface; it would carry the same trigram capability since it bundles SQLite 3.53.1.

## LCM's FTS5 surface used

Each row maps a feature in LCM's TypeScript code to the macOS stdlib `sqlite3` result above.

| Feature | Used in | Available in stdlib? |
|---|---|---|
| `CREATE VIRTUAL TABLE ... USING fts5(content, tokenize='porter unicode61')` | `src/db/migration.ts:1199` (`messages_fts`), `:1217` (`summaries_fts`) | YES |
| `CREATE VIRTUAL TABLE ... USING fts5(..., tokenize='trigram')` | `src/db/migration.ts:1248` (`summaries_fts_cjk`) | YES |
| `<table> MATCH ?` | `src/store/conversation-store.ts:891`, `src/store/summary-store.ts:1180`, `:1342` | YES |
| `bm25(<table>)` / `bm25(<table>, w1, w2, ...)` ordering | `rank` column used implicitly via `ORDER BY rank` in store queries; bm25(...) tested directly | YES |
| `snippet(<table>, col, '', '', '...', N)` | `src/store/conversation-store.ts:917`, `src/store/summary-store.ts:1209`, `:1372` | YES |
| `highlight(<table>, col, '<b>', '</b>')` | Not used by stores directly today, but trivially available | YES |
| Contentless / external-content tables | LCM uses standalone (default content) tables, not external-content; see `ensureStandaloneFtsTable` calls. Contentless is tested and works regardless. | YES |
| `UNINDEXED` column attribute | `src/db/migration.ts:1218`, `:1249` (`summary_id UNINDEXED`) | YES (works under stdlib) |
| Runtime feature probe (`features.ts`) | `src/db/features.ts:33-38` probes trigram availability before creating `summaries_fts_cjk` | Python port should mirror this — see below |

The Python port should keep LCM's runtime probe (the equivalent of `src/db/features.ts`) so that if the database is ever opened with a Python build that lacks the trigram tokenizer (custom-compiled Python with FTS5-only build, or some odd container image), the CJK table is skipped gracefully rather than crashing migration.

## Recommendation

- **Python sqlite3 backend:** **stock stdlib `sqlite3`.** All FTS5 features LCM uses are present in every mainstream Python build from Python 3.11 onward, on macOS and on Linux. The SQLite versions Python bundles (3.39+ at the minimum-supported Python) are well above the SQLite 3.34 floor where the trigram tokenizer was introduced (Dec 2020).
- **Reason:** Concretely tested on Python 3.9.6 (Apple system), 3.12.13, 3.13.12, and 3.14.3 — every interpreter ships SQLite ≥ 3.51 with FTS5 + porter + unicode61 + trigram + bm25 + rank + snippet + highlight + unicode61 options all working. The trigram tokenizer is genuine (sanity-checked against bogus-name rejection and against `fts5vocab` output). LCM's specific syntax (`tokenize='porter unicode61'`, `tokenize='trigram'`, `bm25(table, w...)`, `snippet(...)`, `UNINDEXED` columns) all execute cleanly. Importantly, **hermes-agent already runs FTS5 + trigram on stdlib `sqlite3`** in `hermes_state.py` — the upstream platform LCM is being ported into has already proven this works in production.
- **Cross-platform notes:**
  - **macOS Homebrew Python (`python@3.11+`):** Confirmed working. Default recommendation.
  - **macOS `/usr/bin/python3` (Apple system Python):** FTS5 + trigram work, BUT this Python is poisoned for the LCM port for a different reason — spike 001 found `enable_load_extension` is missing on system Python, so sqlite-vec won't load. Avoid system Python regardless of FTS5 status.
  - **python.org Python installers (macOS .pkg):** Not locally tested. Their build script enables FTS5 by default (the CPython build defaults include `--enable-fts5` since 3.7), so they are expected to work, but treat as unverified.
  - **Linux (Ubuntu/Debian/RHEL/Alpine):** Not locally tested. Every mainstream `python:3.11-slim`, `python:3.12-bookworm`, `python:3.13-alpine`, `manylinux_2_28` Docker image bundles SQLite ≥ 3.40 with FTS5+trigram by virtue of the CPython source's bundled SQLite (when CPython is built `--with-sqlite-builtin`) or the platform's libsqlite3 (manylinux/Debian ship ≥ 3.40 since ~2023). Close this by adding `ubuntu-latest` + `python:3.11-slim` + `python:3.13-alpine` to the GH Actions matrix at first PR.
  - **Custom-compiled Python:** A Python built with `--with-sqlite3-disable-feature=fts5` or against an ancient libsqlite3 (< 3.34) could fail. This is a niche we can document but should not try to support — fall through to the runtime feature probe and skip the CJK table.
- **No need for `pysqlite3-binary`:** It only solves "newer SQLite than your distro ships" — we don't have that problem. It also has no macOS wheel (per spike 001), so adding it would break Mac contributors.
- **No need for `apsw`:** Keep as a documented fallback (per spike 001) for the vec0 path, but FTS5 alone never requires it.

## Coordination with sqlite-vec (spike 001)

Spike 001 confirmed that vec0 and trigram-FTS5 **co-exist in the same stdlib `sqlite3` connection** (see spike-001 §Findings → "FTS5 trigram tokenizer co-existence"). One opened connection on Homebrew Python 3.12 / 3.14 carries:

- Loaded `sqlite_vec` extension → `vec0` virtual tables work.
- FTS5 (always present, no extension load needed) → `messages_fts` (porter unicode61), `summaries_fts` (porter unicode61), `summaries_fts_cjk` (trigram) all work.

So the port can keep LCM's "everything in one SQLite file, opened on one connection" design unchanged. No connection-splitting, no driver-juggling.

The runtime feature probe at `src/db/features.ts` should be ported as-is — it does two things (probe FTS5, probe trigram) and gracefully degrades to "skip the CJK table" if trigram is missing. The Python equivalent is a ~25-line module.

## Remaining 5% risk

1. **Linux not tested first-hand.** Strong inference (Hermes' own `hermes_state.py` runs `tokenize='trigram'` against stdlib `sqlite3` in production on whatever Python users install, including Linux; CPython's bundled SQLite has had trigram since 2021; every `python:3.11-slim`+ image and every `manylinux_2_28` image ships ≥ SQLite 3.40 ≥ Python ≥ 3.11). Mitigation: add an `ubuntu-latest` + `python:3.13` GH Actions job in the first PR.
2. **python.org Python installer not locally validated.** Same situation as spike 001 — not installed locally; CPython build defaults enable FTS5; assumed working.
3. **Custom Python builds with FTS5 disabled.** Documented as "use the runtime probe and skip CJK". Not a port blocker.
4. **`UNINDEXED` semantics with trigram.** LCM uses `summary_id UNINDEXED` on `summaries_fts_cjk` (see `migration.ts:1248-1258`). Tested implicitly (CREATE succeeds, `summary_id` is preserved on read), but not exhaustively against the full LCM query set. Low risk — `UNINDEXED` is a documented FTS5 column attribute orthogonal to tokenizer choice. Worth confirming when the migration test suite is ported.
5. **No concurrency / WAL stress test for FTS5 writes.** LCM uses standalone FTS tables (not external-content), so writes are direct INSERTs against the FTS5 table — same lock contention model as ordinary writes, which spike 001 didn't stress either. Recommend a multi-process write benchmark before claiming production-grade performance.
