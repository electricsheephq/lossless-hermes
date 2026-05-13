---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-01] storage: port FTS5 virtual tables → db/migration.py (messages_fts, summaries_fts, summaries_fts_cjk)'
labels: 'port, epic-01-storage'
---

## Source (TypeScript)

- File: `src/db/migration.ts`
- Lines: subset around 1,199–1,260 covering the three FTS5 virtual tables + `shouldRecreateStandaloneFtsTable()` stale-schema detection.
- Function(s)/class(es): `ensureStandaloneFtsTable(db, name, schema)`, `shouldRecreateStandaloneFtsTable(db, name, schema)`, `dropFts5ShadowTables(db, name)`.

## Target (Python)

- File: `src/lossless_hermes/db/migration.py` (additive — extends the file from #01-04)
- Estimated LOC: ~300

## What this issue covers

The 3 FTS5 virtual tables LCM creates conditionally (per storage.md §2.2):

1. **`messages_fts`** — `CREATE VIRTUAL TABLE messages_fts USING fts5(content, tokenize='porter unicode61')`. Standalone (default content tracking). Seeded by `INSERT INTO messages_fts SELECT message_id, content FROM messages`. ConversationStore writes to it directly on every message insert/delete.
2. **`summaries_fts`** — `fts5(summary_id UNINDEXED, content, tokenize='porter unicode61')`. Standalone. Seeded by SELECT FROM summaries.
3. **`summaries_fts_cjk`** — **only created when `trigram_tokenizer_available` is True**. `fts5(summary_id UNINDEXED, content, tokenize='trigram')`. Used for CJK substring search via OR semantics. Gracefully skipped when trigram is missing.

### Stale-schema recreate

Per storage.md §2.2 last paragraph and §12 risk #6: when `shouldRecreateStandaloneFtsTable()` detects a stale schema marker (e.g. an old `content_rowid='summary_id'` config no longer compatible), DROP the FTS5 table + its 5 shadow tables (`_data`, `_idx`, `_content`, `_docsize`, `_config`), then CREATE fresh and re-seed.

The TS code matches substrings in `sqlite_master.sql` via `staleSchemaPatterns`. Port the same heuristic; Python sqlite3 returns the same CREATE-SQL text. Per spike-005 risk #4: confirm UNINDEXED semantics with trigram (it works; verify in tests).

### Verify spike 005 invariants

Per spike-005 §"LCM's FTS5 surface used":

- `tokenize='porter unicode61'` works on stdlib Python sqlite3 (verified on 3.12/3.13/3.14).
- `tokenize='trigram'` works on stdlib (verified).
- `UNINDEXED` column attribute compiles and is preserved on read.
- `bm25(<table>)` / `bm25(<table>, w1, w2)` / `rank` column / `snippet(...)` / `highlight(...)` all available.
- Sanity test: `tokenize='not_a_real_tokenizer'` raises `OperationalError: no such tokenizer:` — port this as a defensive test so a future probe regression is caught loudly.

### Out of scope

- ConversationStore / SummaryStore FTS write paths (#01-08, #01-09).
- FTS5 query sanitization helpers (#01-11).
- The CJK detection / segmentation code inside SummaryStore (#01-09 — `searchCjkTrigram`, `extractCjkSegments`).

## Dependencies

- Depends on: #01-03 (features probe — for the trigram branch), #01-04 (core tables — FTS5 seeds from `messages` and `summaries`).
- Blocks: #01-08 (ConversationStore writes to `messages_fts` on every insert), #01-09 (SummaryStore writes to `summaries_fts` and CJK).

## Acceptance criteria

- [ ] On a fresh DB with `fts5_available=True` and `trigram_tokenizer_available=True`, all 3 virtual tables are created and seeded from their parent tables.
- [ ] On a fresh DB with `trigram_tokenizer_available=False`, only `messages_fts` and `summaries_fts` are created — `summaries_fts_cjk` is gracefully skipped.
- [ ] On a DB with a stale-schema FTS5 table (e.g. created with the old `content_rowid='summary_id'` config), `shouldRecreateStandaloneFtsTable()` returns True, the 5 shadow tables are dropped, and the table is recreated + re-seeded.
- [ ] Sanity test: `tokenize='not_a_real_tokenizer'` raises `OperationalError` (defensive regression guard).
- [ ] `UNINDEXED` column attribute preserved on read: inserting `(summary_id='abc', content='hello world')` and SELECTing returns `summary_id='abc'`.
- [ ] `bm25(<table>)` ordering returns rows in descending relevance order on a 3-row fixture.
- [ ] `snippet(<table>, col, '', '', '...', 10)` returns a substring around the match.
- [ ] CJK trigram test: insert `{'summary_id': 'a', 'content': '你好世界'}` into `summaries_fts_cjk`; `MATCH '世界'` returns the row. Verified per spike-005 §"SQLite versions found" trigram column.
- [ ] Per `tests-and-config.md` line 494, the relevant subset of the 16-case `test/migration.test.ts` (FTS recreate, stale schema, missing-shadow-table cleanup) is ported in `tests/test_migration.py`.
- [ ] `pytest tests/test_migration.py::test_fts_*` passes.
- [ ] No new mypy errors.
- [ ] PR description cites LCM commit `1f07fbd` and `src/db/migration.ts:1199-1258`.

## Estimated effort

**4–6 hours.**

## Confidence

**95%** — spike-005 closes every uncertainty about FTS5+trigram in stdlib sqlite3. The stale-schema-recreate path is mechanical pattern-matching.
