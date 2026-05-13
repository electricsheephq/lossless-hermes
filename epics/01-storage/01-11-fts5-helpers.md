---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-01] storage: port FTS5 + scope + parse-utc helpers → store/'
labels: 'port, epic-01-storage'
---

## Source (TypeScript)

Five small pure-function modules:

| TS file | LOC | Notes |
|---|---:|---|
| `src/store/fts5-sanitize.ts` | 50 | `sanitizeFts5Query(raw) -> string` — wrap user tokens in `"..."` so FTS5 operators don't fire. |
| `src/store/full-text-sort.ts` | 21 | `buildFtsOrderBy(sort, created_at_expr) -> string`. Constant `AGE_DECAY_RATE = 0.001`. BM25 + recency hybrid ORDER BY builder. |
| `src/store/full-text-fallback.ts` | 84 | `containsCjk(text) -> bool`, `buildLikeSearchPlan(column, query) -> {terms, where, args}`, `createFallbackSnippet(content, terms) -> string`. |
| `src/store/parse-utc-timestamp.ts` | 26 | `parseUtcTimestamp(raw)`, `parseUtcTimestampOrNull(raw)`. SQLite `datetime('now')` reinterpretation as UTC. |
| `src/store/conversation-scope.ts` | 34 | `appendConversationScopeConstraint({where, args, column_expr, conversation_id?, conversation_ids?})`. Pure SQL-fragment mutator. |

Plus `src/store/index.ts` (44 LOC) — re-export barrel.

## Target (Python)

| Python file | LOC est |
|---|---:|
| `src/lossless_hermes/store/fts5_sanitize.py` | ~60 |
| `src/lossless_hermes/store/full_text_sort.py` | ~30 |
| `src/lossless_hermes/store/full_text_fallback.py` | ~100 |
| `src/lossless_hermes/store/parse_utc_timestamp.py` | ~30 |
| `src/lossless_hermes/store/conversation_scope.py` | ~45 |
| `src/lossless_hermes/store/__init__.py` | ~30 |

Total ~295 LOC.

## What this issue covers

Pure-function helpers consumed by the ConversationStore (#01-08) and SummaryStore (#01-09). **Phase 0 of the port order (per storage.md §9)** — no dependencies on anything else, parallel-portable.

### Each module

1. **`fts5_sanitize.py`** — `sanitize_fts5_query(raw: str) -> str`. Trivial regex tokenizer that wraps non-operator user tokens in `"..."` so boolean operators (`OR`, `AND`, `NOT`, `NEAR`), parentheses, quotes, carets, and phrases passed by users aren't interpreted as FTS5 syntax.

2. **`full_text_sort.py`** — `build_fts_order_by(sort: Literal["relevance","recency","hybrid"], created_at_expr: str) -> str`. Returns an SQL fragment. `AGE_DECAY_RATE = 0.001` constant. The hybrid formula combines `bm25(<table>)` with `(now - created_at) * AGE_DECAY_RATE`.

3. **`full_text_fallback.py`** — three pure functions:
   - `contains_cjk(text: str) -> bool` — same Unicode-block regex as SummaryStore (#01-09). Move to a shared module here.
   - `build_like_search_plan(column: str, query: str) -> LikeSearchPlan` — splits query on whitespace into terms, builds `WHERE column LIKE ? AND column LIKE ? ...` plus args. TypedDict / dataclass result.
   - `create_fallback_snippet(content: str, terms: list[str]) -> str` — returns a ~60-char window around the first matching term with `...` markers. Code-point-based slicing (not byte).

4. **`parse_utc_timestamp.py`** — `parse_utc_timestamp(raw: str) -> datetime` and `parse_utc_timestamp_or_null(raw: str | None) -> datetime | None`. Per storage.md §4.5: use `datetime.fromisoformat()` after replacing space with `T`; explicitly set `tzinfo=UTC`. Handles both SQLite's `'2026-05-13 12:34:56'` (space-separated, no Z) and ISO-formatted `'2026-05-13T12:34:56Z'` inputs.

5. **`conversation_scope.py`** — `append_conversation_scope_constraint(where_list: list[str], args_list: list[Any], column_expr: str, *, conversation_id: int | None = None, conversation_ids: Sequence[int] | None = None) -> None`. Mutates the lists in place — appends `column = ?` (single) or `column IN (?, ?, ...)` (multi). No return.

6. **`store/__init__.py`** — re-export barrel mirroring `src/store/index.ts`. Exports the stores (ConversationStore, SummaryStore, CompactionTelemetryStore, CompactionMaintenanceStore — these come from later issues) and the typed records / dataclasses. Per ADR-024 §"Open questions" #1: minimal barrel by default.

## Dependencies

- Depends on: nothing (Phase 0 leaves) other than #00-01 (scaffolding).
- Blocks: #01-08, #01-09 (stores import these helpers).

## Acceptance criteria

- [ ] `sanitize_fts5_query` passes all **17 cases** from `test/fts5-sanitize.test.ts` (storage.md §8 row 17 — boolean ops, NEAR, caret, quotes, phrases) → `tests/test_fts5_sanitize.py`.
- [ ] `build_fts_order_by("relevance", "created_at")` returns the bm25-only fragment; `("recency", ...)` returns recency-only; `("hybrid", ...)` returns the combined formula with `AGE_DECAY_RATE = 0.001`.
- [ ] `contains_cjk("hello")` is False; `contains_cjk("你好")` is True; CJK Unified, Compat, Kana, Hangul blocks all match.
- [ ] `build_like_search_plan("content", "foo bar")` returns plan with 2 LIKE clauses and 2 args (`%foo%`, `%bar%`).
- [ ] `create_fallback_snippet` returns a window with `...` markers; code-point-based slicing works on CJK + emoji content (no split surrogate pairs).
- [ ] `parse_utc_timestamp` passes all **5 cases** from `test/parse-utc-timestamp.test.ts` (storage.md §8 row 15 — UTC reinterpretation edge cases) → `tests/test_parse_utc_timestamp.py`.
- [ ] `append_conversation_scope_constraint` mutates lists in place; verified by asserting `where_list` and `args_list` contents before/after.
- [ ] `__init__.py` barrel exports all expected names (verified by `from lossless_hermes.store import *` returning the expected symbols).
- [ ] `pytest tests/test_fts5_sanitize.py tests/test_full_text_fallback.py tests/test_parse_utc_timestamp.py tests/test_conversation_scope.py tests/test_full_text_sort.py` passes (~30+ cases total across 5 files).
- [ ] `mypy --strict` passes.
- [ ] PR description cites LCM commit `1f07fbd` and lists the 5 TS source files.

## Estimated effort

**4 hours combined** (0.5–1.5 h each per storage.md §1 table).

## Confidence

**95%** — all pure functions, well-tested in TS. The only nontrivial port is the code-point-vs-byte snippet-offset semantics, addressed inline.
