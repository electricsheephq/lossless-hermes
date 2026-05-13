---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-06] tools: port lcm_grep regex + full_text + verbatim modes (Wave A)'
labels: 'port, tool, wave-a'
---

## Source (TypeScript)
- File: `src/tools/lcm-grep-tool.ts`
- Lines: 1–1179 (full file); Wave A subset is everything EXCEPT lines 474–760 (`runHybridSearch`) and the `semantic` dispatch branch around lines 291–351.
- Function(s)/class(es): `createLcmGrepTool`, schema (lines 43–125), regex/full_text/verbatim dispatch (lines 291–351 minus hybrid/semantic branches), local `sanitizeFts5Pattern` (lines 154–178) for the verbatim path.

## Target (Python)
- File: `src/lossless_hermes/tools/grep.py` — partial implementation in Wave A; hybrid + semantic land in #06-09.
- Estimated LOC: ~600 LOC for Wave A (modes regex + full_text + verbatim).

## Dependencies
- Depends on: #06-02, #06-03, #06-04, #06-05, Epic 01 retrieval (`retrieval.grep(...)`), Epic 01 `summaries_fts` / `messages_fts` tables + `sanitizeFts5Query` in `store/fts5-sanitize.ts`.
- Blocks: Epic 09 eval (grep is exercised heavily by the recall query set).

## Acceptance criteria
- [ ] `LCM_GREP_SCHEMA` dict at module top — **description string verbatim** from `lcm-grep-tool.ts:196–204` (tools.md lines 42–45). The 5-mode discriminator + `LCM_TOOL_RESULT_TOKEN_BUDGET` env name + `summaryKinds` filter prose are all load-bearing model-routing hints.
- [ ] `mode` enum includes all 5 values (`regex`, `full_text`, `hybrid`, `semantic`, `verbatim`) — Wave A only implements 3 modes but the schema advertises all 5. Hybrid + semantic dispatch returns:
  ```python
  {"error": "<mode> mode is not yet available in this build. Use mode='full_text' for keyword search."}
  ```
  until #06-09 lands. The error message must be **operator-facing helpful**, not a stack trace.
- [ ] **`regex` mode** — straight LIKE/REGEXP over `summaries.content` and/or `messages.content` via `retrieval.grep(...)`. Pure SQLite. Honors `scope` (`messages` | `summaries` | `both`, default `both`), `since`/`before`, `conversationId(s)`.
- [ ] **`full_text` mode** — FTS5 MATCH against `summaries_fts` / `messages_fts`. The store-layer `sanitize_fts5_query` already wraps problematic chars in phrase quotes — DO NOT re-sanitize. Supports `sort: relevance | hybrid | recency`.
- [ ] **`verbatim` mode** — **hard-capped at 20 rows.** Bypasses FTS for `LIKE` path, or uses FTS-with-phrase-quote-wrap via the local `sanitize_fts5_pattern` port (TS lines 154–178). Returns FULL untruncated message rows for citation. Honors `role` filter (`user | assistant | tool | system | all`). The 20-cap protects against blowing past `MAX_RESULT_CHARS`.
- [ ] **Empty pattern guard** — `pattern.strip() == ""` → `{"error": "`pattern` is required..."}` (TS line 234).
- [ ] **since >= before** → structured error.
- [ ] **Token-gate estimator** ([#06-03](06-03-runwithtokengate-middleware.md)):
  - `regex` / `full_text`: `200 + limit * 200` chars.
  - `verbatim`: `70 + min(20, limit) * 2400` chars (large because full message rows).
- [ ] PR description cites the LCM commit SHA being ported.

## Tests
- Mirror `lcm-grep-verbatim-mode.test.ts` 1:1 in `tests/tools/test_lcm_grep_wave_a.py` (~435 TS LOC → ~350 pytest LOC):
  - `regex` mode: literal pattern, regex pattern, `scope` variants, `since`/`before` filters, `conversationId` scoping.
  - `full_text` mode: simple keyword, multi-word AND default, quoted phrase preservation, `sort` variants.
  - `verbatim` mode: 20-cap enforcement, `role` filter (each of 5 values), `sanitize_fts5_pattern` edge cases (e.g. `"` in pattern, `*` in pattern, `(` in pattern), full-row untruncated output.
  - Empty pattern → error.
  - `since > before` → error.
  - Hybrid/semantic modes → `not yet available` error message (regression test that #06-09 deletes when it ships).
  - **Wave-12 N3 regression:** result-truncation regex matches the `MAX_RESULT_CHARS` overflow notice byte-identically.

## Estimated effort
**12 hours** — 5h port (schema + 3 modes + sanitize_fts5_pattern), 7h tests (the verbatim sanitizer cases + role-filter matrix are the bulk).

## Confidence
**92%** — well-specified; the SQLite FTS5 semantics in Python (`sqlite3` module) are identical to `node:sqlite`. 8% risk on the `sanitizeFts5Pattern` edge cases (some patterns that work in TS may need different escaping in Python's sqlite3 — pin via tests).

## References
- [`docs/porting-guides/tools.md`](../../docs/porting-guides/tools.md) "lcm_grep" section (lines 37–130).
- TS test fixture: `test/lcm-grep-verbatim-mode.test.ts` (435 LOC).
