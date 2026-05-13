---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-01] storage: port telemetry stores (compaction-telemetry + compaction-maintenance) → store/'
labels: 'port, epic-01-storage'
---

## Source (TypeScript)

- Files: `src/store/compaction-telemetry-store.ts` (204 LOC), `src/store/compaction-maintenance-store.ts` (219 LOC)
- Function(s)/class(es): `class CompactionTelemetryStore`, `class CompactionMaintenanceStore`.

## Target (Python)

- Files: `src/lossless_hermes/store/compaction_telemetry.py` (~230 LOC), `src/lossless_hermes/store/compaction_maintenance.py` (~250 LOC)

## What this issue covers

Two small, mostly-mechanical stores that wrap the two single-row-per-conversation state machines from §2.1 of storage.md.

### CompactionTelemetryStore (per storage.md §4.3)

Methods:

- `__init__(conn)`
- `with_transaction(fn)`
- `get_conversation_compaction_telemetry(conv_id) -> CompactionTelemetryRecord | None`
- `upsert_conversation_compaction_telemetry(input)` — UPSERT via `INSERT ... ON CONFLICT DO UPDATE`.

Pure CRUD around `conversation_compaction_telemetry` (created in #01-04). Mostly mechanical port. Watch CHECK constraints:

- `cache_state IN ('hot','cold','unknown')` — Python `Literal["hot","cold","unknown"]` type.
- `last_activity_band IN ('low','medium','high')` — Python `Literal["low","medium","high"]` type.

### CompactionMaintenanceStore (per storage.md §4.4)

Methods:

- `__init__(conn)`
- `with_transaction(fn)`
- `get_conversation_compaction_maintenance(conv_id) -> CompactionMaintenanceRecord | None`
- `request_proactive_compaction_debt({conv_id, reason, token_budget, current_token_count}) -> void` — sets `pending=1, requested_at=now, reason=...`.
- `mark_proactive_compaction_running(conv_id) -> bool` — atomic compare-and-set: `UPDATE ... SET running=1, last_started_at=now WHERE conv_id=? AND running=0 AND pending=1`. Returns True if the row was claimed.
- `mark_proactive_compaction_finished(conv_id, *, failure_summary=None)` — clears `running` and `pending` (or just `running` if `failure_summary` is set), sets `last_finished_at=now` and optionally `last_failure_summary`.

**Coalesced single-row state machine per conversation (no queue)** — per storage.md §4.4 last sentence. Two simultaneous `request_*` calls for the same conv just leave one debt; running is single-flight via the compare-and-set.

### Records

Use pydantic `BaseModel` or `@dataclass(frozen=True, slots=True)` — pick one and apply consistently across all stores. (The Epic 00 ADR on `TypedDict` vs Pydantic per ADR-024 §"Open questions" #1 settles this; default to Pydantic.)

## Dependencies

- Depends on: #01-01 (connection), #01-04 (tables created), #01-13 (transaction_mutex for `with_transaction`).
- Blocks: Epic 04 (compaction engine reads telemetry to decide cache-aware compaction timing; writes maintenance debt).

## Acceptance criteria

- [ ] Both stores implemented with the methods above.
- [ ] `upsert_conversation_compaction_telemetry` is idempotent — calling twice with the same input is equivalent to calling once.
- [ ] `mark_proactive_compaction_running` returns `False` when called on a row that's already running OR has no pending debt.
- [ ] `mark_proactive_compaction_running` returns `True` and updates `last_started_at` when claim succeeds.
- [ ] CHECK constraint violation: inserting `cache_state='lukewarm'` raises `IntegrityError`.
- [ ] Per `test/compaction-maintenance-store.test.ts` (storage.md §8 row 22) — 1 state-machine-flow case → `tests/test_compaction_maintenance.py`.
- [ ] **New tests** for CompactionTelemetryStore (the TS test set didn't include direct unit tests for this store; integration tests in `lcm-integration.test.ts` cover it. Port the relevant subset here — ~5 cases covering insert, upsert idempotency, get-null, get-existing, cache-state transitions.)
- [ ] `pytest tests/test_compaction_telemetry.py tests/test_compaction_maintenance.py` passes.
- [ ] `mypy --strict` passes.
- [ ] PR description cites LCM commit `1f07fbd` and the two TS source files.

## Estimated effort

**4 hours combined** (2 hours each per storage.md §1 table).

## Confidence

**95%** — small surface, pure CRUD, well-tested state-machine semantics from TS.
