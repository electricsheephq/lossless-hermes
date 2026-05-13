---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-01] storage: port store/conversation-store.ts → store/conversation.py'
labels: 'port, epic-01-storage'
---

## Source (TypeScript)

- File: `src/store/conversation-store.ts`
- Lines: **1,071 LOC** (task brief said "1219" — the porting-guide and lcm-source-map both say 1,071; the file did not grow between the brief and verification. Use 1,071.)
- Function(s)/class(es): `class ConversationStore` — full public API per storage.md §4.1 table (29 methods including `searchMessages` and the FTS / LIKE / regex backends).

## Target (Python)

- File: `src/lossless_hermes/store/conversation.py`
- Estimated LOC: ~1,300

## What this issue covers

The conversation + message + message_parts CRUD layer plus the search dispatcher. Per ADR-017 the store is **synchronous** — plain `def` methods over `sqlite3.Connection`; no `async` / `await` / `aiosqlite`.

### Public surface (per storage.md §4.1)

29 methods total:

| Method | Notes |
|---|---|
| `__init__(conn, *, fts5_available: bool)` | Single conn; cache `fts5_available` (probed by #01-03). |
| `create_conversation(input) -> ConversationRecord` | Insert + return shaped record. |
| `get_conversation(id) -> ConversationRecord \| None` | By PK. |
| `get_conversation_by_session_id(session_id) -> ConversationRecord \| None` | Newest active. |
| `get_conversation_by_session_key(session_key) -> ConversationRecord \| None` | Active row matching partial UNIQUE index. |
| `get_conversation_family_ids({session_key OR session_id, include_archived})` | Cross-conv ID list. |
| `get_conversation_for_session({session_id, session_key})` | Resolve which conv to ingest into. |
| `list_active_conversations(limit=None)` | Recent active. |
| `get_or_create_conversation(input, opts)` | Atomic find-or-insert via UPSERT. |
| `mark_conversation_bootstrapped(id)` | Sets `bootstrapped_at`. |
| `archive_conversation(id)` | active=0 + archived_at. |
| `create_message(input) -> MessageRecord` | Auto-computes identity_hash via `build_message_identity_hash`; writes to `messages_fts`. |
| `create_messages_bulk(inputs) -> list[MessageRecord]` | Transactional batch. |
| `get_messages(conv_id, *, since=None, before=None, limit=None, offset=None)` | Range. |
| `get_last_message(conv_id)` | |
| `has_message(conv_id, identity_hash)` | Dedupe check — used by ingest. |
| `count_messages_by_identity(conv_id, identity_hash)` | |
| `get_message_by_id(message_id)` | |
| `create_message_parts(message_id, parts)` | Bulk insert of typed parts (12 part_types per storage.md §2.1). |
| `get_message_parts(message_id)` | |
| `get_message_count(conv_id)` | |
| `get_max_seq(conv_id)` | For next-seq derivation. |
| `delete_messages(message_ids) -> int` | Cascades to parts + FTS. |
| `search_messages(input) -> list[MessageSearchResult]` | Dispatcher for FTS / LIKE / regex. |
| `with_transaction(fn)` | Convenience wrapper via `transaction_mutex.with_database_transaction`. |
| `_index_message_for_full_text(message_id, content)` | FTS5 INSERT. |
| `_delete_message_from_full_text(message_id)` | FTS5 DELETE. |
| `_search_full_text(...)` | FTS5 backend. |
| `_search_like(...)` | LIKE backend. |
| `_search_regex(...)` | Regex backend (Python `re.search()`). |

### Dependencies on other modules

Uses `transaction_mutex` (#01-13), `conversation_scope` + `fts5_sanitize` + `full_text_fallback` + `full_text_sort` + `message_identity` + `parse_utc_timestamp` (all #01-11 + #01-07).

### TS-specific gotchas (per storage.md §4.1)

- **`BigInt → int`:** drop the defensive `Number(row.message_id)` casts; Python `int` is arbitrary-precision (spike-001 §"INTEGER/INT64").
- **`JSON.parse(metadata)`:** message_parts metadata blob — wrap `json.loads` in try/except and log a warning on invalid JSON (TS does the same).
- **Regex search path:** `re.search()` with TS's RegExp flags translated. Add a side-by-side parity test (storage.md §12 risk #7).
- **Snippet building:** TS uses byte offsets into UTF-16 strings; Python `str` slicing is code-point-based. **Both are equivalent for non-surrogate-pair content.** Add a CJK + emoji ZWJ family case to verify (parallel to spike-003 case #5).
- **`fts5Available` flag:** probed once per DB, cached on the store. Toggles which `search_*` backend runs.

## Dependencies

- Depends on: #01-01 (connection), #01-03 (features probe), #01-04 (core tables), #01-05 (`messages_fts`), #01-07 (message_identity), #01-11 (helpers — sanitize, sort, fallback, parse-utc, scope), #01-13 (transaction_mutex).
- Blocks: #01-09 (SummaryStore uses ConversationStore methods in some integration paths), Epic 02 (engine ingest).

## Acceptance criteria

- [ ] All 29 public/private methods implemented per the table above.
- [ ] `create_message` auto-computes `identity_hash = build_message_identity_hash(role, content)` and writes to `messages_fts` in the same transaction.
- [ ] `delete_messages` cascades to `message_parts` (via FK CASCADE) AND removes corresponding rows from `messages_fts` (manual DELETE — FTS5 standalone tables don't cascade).
- [ ] Per `test/lcm-integration.test.ts` (storage.md §8 row 21): ~25 storage-only cases ported to `tests/test_lcm_integration_storage.py` covering conversation lifecycle, message dedup via identity_hash, message_parts bulk insert, search dispatcher.
- [ ] Per `test/fts5-sanitize.test.ts` (17 cases) — indirect verification: search queries pass through the sanitizer before MATCH.
- [ ] Per `test/fts-fallback.test.ts` (6 cases): the 2 that are conversation-store-relevant ("ignores lcm_describe helper text", LIKE fallback path) port to `tests/test_fts_fallback.py`.
- [ ] **Regex parity test:** for each of 10 representative LCM regex patterns (collect from `src/store/conversation-store.ts:searchRegex`), assert Python `re.search()` returns the same match offsets as Node's `RegExp.exec()`. Use a subprocess to invoke Node if installed; skip if not.
- [ ] **CJK snippet byte-offset test:** insert a message with `"hello 你好 world"`, search for `你好`, assert the returned snippet correctly highlights the CJK characters without breaking surrogate pairs.
- [ ] **JSON.parse error handling:** insert a `message_parts.metadata` row with `"not valid json"`; `get_message_parts` returns the row with `metadata=None` and emits one warning (no raise).
- [ ] `with_transaction` semantics: re-entrant savepoint nesting works (verified by 3-deep nesting test).
- [ ] `pytest tests/test_conversation_store.py tests/test_lcm_integration_storage.py` passes.
- [ ] `mypy --strict` passes.
- [ ] PR description cites LCM commit `1f07fbd` and `src/store/conversation-store.ts`.

## Estimated effort

**18–22 hours.** Bulk is method-by-method translation; long tail is the search-dispatcher edge cases (regex parity, CJK snippet offsets) and integration-test partitioning.

## Confidence

**92%** — TS source is well-structured and stores are pure CRUD over SQL. Residual risk: (a) regex flag semantics drift between Node and Python (storage.md §12 risk #7 — mitigated by parity test); (b) integration-test partitioning from `test/lcm-integration.test.ts` requires care to keep the storage-only cases isolated from Epic 02's engine cases.
