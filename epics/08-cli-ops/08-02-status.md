---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-08] cli-ops: port /lcm status'
labels: 'port, epic-08-cli-ops'
---

## Source (TypeScript)

- File: `src/plugin/lcm-command.ts` (the `case "status"` body + `buildStatusText` helper)
- Lines: ~120 LOC inside the dispatcher (out of 2884 total)
- Function(s)/class(es): `buildStatusText({ ctx, db, config })`, plus three helpers it calls — `getCurrentConversationStats`, `getStoredVsSourceTokens`, `formatLastMaintainTelemetry`

## Target (Python)

- File: `src/lossless_hermes/plugin/commands/status.py`
- Estimated LOC: ~140

## What this issue covers

The non-destructive, info-level snapshot for the current LCM state. Bare `/lcm` also routes here per 08-01. The output is plain text designed for chat rendering — multiple lines, leading section headers, no tables.

The TS handler reads from `db` (open `DatabaseSync` connection) and `ctx.sessionId` / `ctx.sessionKey` (to find the "current conversation" for the per-conversation snapshot). The port uses `shared.engine.current_session_id` (engine-tracked via `on_session_start`) per plugin-glue.md §"Per-subcommand translation table" line 650, falling back to "no active conversation" when None.

Output structure (TS parity):

```
[lcm] Status

Database: /Users/.../lossless-hermes/lcm.db
DB size: 12.4 MB (rotated 2025-05-12 18:32 UTC; last backup 1 day ago)

Conversations: 247 (12 active, 235 archived)
Messages: 14,892
Summaries: 3,471 (2,108 leaf, 1,363 condensed; 0 suppressed)

Current conversation (id=42, session_key=agent:main:thread:xyz):
  Context tokens: 18,432 / 200,000 (9.2%)
  Compression ratio: 4.2x (76,818 source → 18,432 context)
  Pyramid depth: 3
  Fresh tail count: 8

Stored tokens (all-conversations): 4,827,431
Source tokens (all-conversations): 19,201,883 (4.0x stored:source)

Last maintain: 2025-05-12 18:30 UTC, mode=cache-aware-deferred, sweeps=2
```

Field semantics (from TS `lcm-command.ts:buildStatusText`):
- DB size — `os.path.getsize(db_path)` formatted as MB/GB.
- Last rotate — read `state_meta.last_rotate_at`.
- Last backup — read the most-recent file under `<db_path>.*.bak` by mtime; if none, print `"never"`.
- Conversation counts — `SELECT count(*), sum(case when active=1 then 1 else 0 end), sum(case when active=0 then 1 else 0 end) FROM conversations`.
- Summary counts include `suppressed_at IS NOT NULL` in the "suppressed" slot (per doctor-ops.md §"Read paths that filter `suppressed_at IS NULL`" — status is one of the few read surfaces that COUNTS suppressed rows for operator visibility).
- "Current conversation" block omitted entirely if `current_session_id is None` (CLI before first message; gateway with no active conversation).
- Compression ratio — `summed source_message_token_count / context_tokens` per the v4.1 column added in `migration.ts`.
- Last maintain — read `lcm_compaction_telemetry` ORDER BY `created_at DESC LIMIT 1`.

## Dependencies

- Depends on: #08-01 (dispatcher), Epic 02 (engine + `current_session_id` accessor), Epic 01-09 (`SummaryStore.count_by_kind`), Epic 04-08 (`compaction_telemetry_store.read_last`).
- Blocks: nothing in Epic 08 — `status` is a read-only command.

## Acceptance criteria

- [ ] Output format matches the TS snapshot test (`test/lcm-command.test.ts::"/lcm status renders all sections"`) line-for-line modulo whitespace.
- [ ] `current_session_id is None` causes the "Current conversation" block to be omitted entirely (no "id=None" or empty fields).
- [ ] DB size formatted as MB/GB with one decimal place; matches `format_bytes` from TS `lcm-command.ts:formatBytes`.
- [ ] "Last backup" reads the newest file matching `<db_path>.*.bak`; absence prints `"never"`.
- [ ] Suppressed-summary count is shown alongside leaf/condensed even when zero (operator visibility).
- [ ] All TS test cases in `test/lcm-command.test.ts::status*` have ported pytest equivalents in `tests/commands/test_status.py` — snapshot-style assertions per plugin-glue.md §"Test inventory" line 591.
- [ ] **New test:** `tests/commands/test_status.py::test_no_active_conversation` — engine with `current_session_id=None` returns valid output minus the per-conversation block.
- [ ] **New test:** `tests/commands/test_status.py::test_last_backup_when_none_exist` — empty DB dir prints `"never"` not a stack trace.
- [ ] Function signatures match the spec in [docs/porting-guides/plugin-glue.md](../../docs/porting-guides/plugin-glue.md) §"/lcm slash commands — full inventory" line 425.
- [ ] `pytest tests/commands/test_status.py` passes.
- [ ] No new mypy errors (`mypy --strict src/lossless_hermes/plugin/commands/status.py`).
- [ ] PR description cites LCM commit `1f07fbd` (pr-613 head).

## Estimated effort

**4 hours.**

## Confidence

**95%** — read-only over already-ported stores; output format is a snapshot test.
