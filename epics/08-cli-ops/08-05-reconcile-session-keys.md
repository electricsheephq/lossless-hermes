---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-08] cli-ops: port reconcile-session-keys list + apply'
labels: 'port, epic-08-cli-ops'
---

## Source (TypeScript)

- File: `src/operator/reconcile-session-keys.ts`
- Lines: 301 LOC
- Function(s)/class(es): `reconcileSessionKeys(params): ReconcileResult`, `listLegacyCandidates(db)`, `ReconcileError`, internal `_validateMergeTargets`, `_rewriteSessionKey`

## Target (Python)

- File: `src/lossless_hermes/operator/reconcile.py`
- Estimated LOC: ~330

## What this issue covers

Session-key merge operator — fixes the "I have two `session_key`s for what should be one conversation" problem. Lossless-claw users hit this when an upstream Hermes change re-keys sessions (e.g., Telegram renaming a chat) or when an integration tool writes inconsistent session keys.

Two modes per plugin-glue.md §"/lcm slash commands — full inventory" lines 435–436:

1. **List mode:** `/lcm reconcile-session-keys --list-candidates`
   - Owner-gated (Wave-12 P1 fix per doctor-ops.md §"Operator gate" line 336 — listing exposes `session_key` + first-message previews across the entire conversation set).
   - `listLegacyCandidates(db)` reads `conversations` and groups by a similarity heuristic (sub-key match, common prefix). Returns top-N candidate groups with conversation counts, last-active timestamps, first-message preview (normalized to 120-char ellipsis-trimmed per `lcm-doctor-cleaners.ts:normalizeFirstMessagePreview`).

2. **Apply mode:** `/lcm reconcile-session-keys --apply --from k1,k2 --to k3 --reason "..." [--allow-main-session]`
   - Owner-gated.
   - Rewrites `conversations.session_key` from each `--from` key to the `--to` key.
   - Rewrites `summaries.session_key` (the v4.1 column added in `migration.ts`) for the same conversation IDs.
   - Writes an audit row to `lcm_session_key_audit` with `action='reconcile'`, `from_keys=<json>`, `to_key=<...>`, `reason=<reason>`, `affected_count=<count>`.
   - `--allow-main-session` is required to merge INTO a session_key matching `agent:main:thread:*` (safeguard against accidentally clobbering the operator's primary thread).
   - All steps run in one `BEGIN IMMEDIATE`.

Output format (apply mode, TS parity):

```
[lcm] reconcile-session-keys --apply
Merged 2 source keys → 1 target key:
  agent:telegram:chat:old-id  → agent:telegram:chat:new-id  (15 conversations, 1247 messages, 342 summaries)
  agent:telegram:chat:typo    → agent:telegram:chat:new-id  (1 conversation, 8 messages, 2 summaries)
Audit: id=lcm-skadt-9f8a, reason="@eva renamed Telegram group"
```

Output format (list mode):

```
[lcm] reconcile-session-keys candidates (top 10)
1. agent:telegram:chat:abc / agent:telegram:chat:xyz (similarity 0.92, 15+1 conversations)
   First message: "Hey what's up?..."
2. ...
```

Error cases (raise `ReconcileError`):
- `--from` is empty.
- `--to` is in `--from` (would self-merge to nowhere).
- `--to` matches `agent:main:thread:*` without `--allow-main-session`.
- Any `--from` key has zero conversations (probably a typo).

## Dependencies

- Depends on: #08-01 (dispatcher), Epic 01-06 (`lcm_session_key_audit` table), Epic 01-08/09 (`ConversationStore` + `SummaryStore`).
- Blocks: nothing.

## Acceptance criteria

- [ ] `list_legacy_candidates(db) -> list[ReconcileCandidate]` returns top-N similarity-grouped candidates with first-message previews.
- [ ] First-message preview normalization matches `lcm-doctor-cleaners.ts:normalizeFirstMessagePreview` exactly (256-char prefix, whitespace collapsed, then 120-char ellipsis trim).
- [ ] `reconcile_session_keys(params: ReconcileParams) -> ReconcileResult` rewrites both `conversations.session_key` and `summaries.session_key` in one `BEGIN IMMEDIATE`.
- [ ] Audit row is written with `action='reconcile'`, `from_keys=<JSON array>`, `to_key=<...>`, `reason=<text>`.
- [ ] `--allow-main-session` is required to merge into `agent:main:thread:*`.
- [ ] `ReconcileError` is raised on empty `--from`, self-merge, missing `--allow-main-session`, or zero-conversation `--from` key.
- [ ] Owner-gated dispatching (both list AND apply per Wave-12 P1; verified by `tests/commands/test_owner_gating.py` covering both).
- [ ] All TS test cases in `test/operator-reconcile-session-keys.test.ts` have ported pytest equivalents in `tests/operator/test_reconcile.py`.
- [ ] **New test:** `tests/operator/test_reconcile.py::test_first_message_preview_normalization` — fixture with whitespace + CJK + emoji confirms 120-char ellipsis trim.
- [ ] **New test:** `tests/operator/test_reconcile.py::test_main_session_guard` — merging into `agent:main:thread:foo` without flag raises.
- [ ] Function signatures match the spec in [docs/porting-guides/doctor-ops.md](../../docs/porting-guides/doctor-ops.md) §"Operator modules" line 309.
- [ ] `pytest tests/operator/test_reconcile.py` passes.
- [ ] No new mypy errors (`mypy --strict src/lossless_hermes/operator/reconcile.py`).
- [ ] PR description cites LCM commit `1f07fbd` (pr-613 head).

## Estimated effort

**6 hours.**

## Confidence

**92%** — pure SQL operator; well-specified contract; the only ambiguity is in the "similarity heuristic" for `listLegacyCandidates`, which the TS source defines explicitly (sub-key prefix match length + Jaccard on tokens).
