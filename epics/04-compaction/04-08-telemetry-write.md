---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-04] compaction: port compactionTelemetryStore writes + CompactionResult'
labels: 'port'
---

## Source (TypeScript)
- File: `lossless-claw/src/compaction.ts` (pr-613 `1f07fbd`)
- Lines:
  - `persistCompactionEvents`: 1754–1812 (~59 LOC) — called after each phase
  - `persistCompactionEvent`: 1815–1830 (~16 LOC) — single-event write
  - `CompactionResult` type definition: 11–61 (the dataclass header)
- Companion store: `lossless-claw/src/store/compaction-telemetry-store.ts`
- Function(s)/class(es): `CompactionEngine._persistCompactionEvents`, `_persistCompactionEvent`, dataclass `CompactionResult`

## Target (Python)
- File: `src/lossless_hermes/compaction.py` (write methods)
- File: `src/lossless_hermes/store/compaction_telemetry.py` (store — separate Epic 01 dependency)
- Estimated LOC: ~80 (the write paths are thin; most volume is the CompactionResult shape)
- Dataclass: `CompactionResult` (already declared in earlier issues; this issue finalizes it)
- Methods on `CompactionEngine`:
  - `_persist_compaction_events(conversation_id, results: list[dict]) → None`
  - `_persist_compaction_event(conversation_id, result: dict) → None`

## `CompactionResult` finalized shape

Per `docs/porting-guides/assembler-compaction.md` §"Public surface":

```python
@dataclass(slots=True)
class CompactionResult:
    action_taken: bool
    tokens_before: int
    tokens_after: int
    created_summary_id: str | None = None
    condensed: bool = False
    level: CompactionLevel | None = None  # "normal" | "aggressive" | "fallback" | "capped"
    auth_failure: bool = False
    reason: str | None = None
    # Phase aggregation (for compactFullSweep): per-phase counts + summary ids
    phase_results: list["CompactionResult"] = field(default_factory=list)
```

`CompactionLevel = Literal["normal", "aggressive", "fallback", "capped"]`

Fields semantics:
- `action_taken=True` ↔ a summary was inserted; `False` ↔ no-op (under threshold, no eligible chunk, breaker open, auth failure)
- `tokens_before`/`tokens_after`: live `getContextTokenCount` before and after the operation
- `created_summary_id`: present when action_taken=True
- `condensed`: True for `_condensed_pass`, False for `_leaf_pass`
- `level`: which escalation step succeeded (or `"capped"` if hard-cap kicked in)
- `auth_failure`: True when summarizer raised `LcmProviderAuthError` and the operation was skipped to preserve DAG integrity
- `reason`: human-readable why-not-compacted string (`"under threshold"`, `"no eligible chunk"`, `"circuit breaker open"`, `"auth failure"`)
- `phase_results`: nested per-phase for `compactFullSweep` aggregation (phase-1 leaves, phase-2 condensed)

## `_persist_compaction_event(conversation_id, result)`

Per the porting guide §"Telemetry write paths":

> `persistCompactionEvent` (lines 1815–1830) — currently **only logs** via `this.log.info`. Despite the name, NO row is currently written to a synthetic chat message. (Earlier versions of LCM appended a synthetic assistant message describing the compaction; that was removed to avoid history pollution.)
>
> The summary write itself (in `leafPass`/`condensedPass` transactions) is the canonical persistence point.

So `_persist_compaction_event` is **a structured log call**, not a DB write. Port faithfully:

```python
def _persist_compaction_event(self, conversation_id: int, result: dict) -> None:
    self._log.info(
        "compaction_event",
        extra={
            "conversation_id": conversation_id,
            "action_taken": result.action_taken,
            "tokens_before": result.tokens_before,
            "tokens_after": result.tokens_after,
            "delta": result.tokens_before - result.tokens_after,
            "level": result.level,
            "condensed": result.condensed,
            "auth_failure": result.auth_failure,
            "created_summary_id": result.created_summary_id,
            "reason": result.reason,
        },
    )
```

(Use Python `logging` extra-kwargs convention or your structured-logger of choice — see ADR for log format if one exists, or default to JSON-serializable extras.)

## `_persist_compaction_events(conversation_id, results)`

Iterates `_persist_compaction_event` for each non-None result. Called from `compactFullSweep` after each phase:

```python
def _persist_compaction_events(self, conversation_id: int, results: list[dict | None]) -> None:
    for r in results:
        if r is not None:
            self._persist_compaction_event(conversation_id, r)
```

## Compaction telemetry store updates

Distinct from the structured log calls above — the telemetry store (`compaction_telemetry_store.update_compaction_telemetry`) tracks **prompt-cache observations** and **retention windows** per conversation. The actual store I/O is owned by Epic 01 (storage); this issue is the consumer.

The compaction engine writes to the store at these moments:

1. **After successful `_leaf_pass`**: update `last_leaf_compaction_at`, `leaf_compaction_count += 1`.
2. **After successful `_condensed_pass`**: update `last_condensed_compaction_at`, `condensed_compaction_count += 1`.
3. **On auth failure**: update `last_auth_failure_at`, increment `consecutive_auth_failures`.

The exact field set is defined in `lossless-claw/src/store/compaction-telemetry-store.ts` — port that store in Epic 01. This issue is responsible only for the **call sites** in `compaction.py`:

```python
# After successful leaf pass:
self.compaction_telemetry_store.mark_leaf_compaction_success(
    conversation_id=conversation_id,
    at=now_ms(),
    summary_id=result.created_summary_id,
)

# After successful condensed pass:
self.compaction_telemetry_store.mark_condensed_compaction_success(...)

# After auth failure:
self.compaction_telemetry_store.mark_auth_failure(...)
```

If the telemetry store from Epic 01 is not yet ready when this issue starts, **stub the calls** — the structured-log path (above) is the load-bearing telemetry; the store is enhancement for cache-aware decision logic (Epic 02's `evaluate_incremental_compaction`).

## Dependencies
- Depends on: Issue 04-02 (`CompactionResult` is returned from leaf pass; needs the auth_failure + level fields wired)
- Depends on: Issue 04-03 (CompactionResult is returned from condensed pass too)
- Depends on: Epic 01 issue "compaction_telemetry_store implements mark_*_success/mark_auth_failure" (CAN BE STUBBED if not ready)
- Blocks: Epic 06 (`lcm_compact` tool reads `CompactionResult` shape to respond to agent)
- Blocks: Epic 02 (`evaluate_incremental_compaction` consumes telemetry store reads — but this issue is the WRITE path)

## Acceptance criteria
- [ ] `CompactionResult` dataclass has all fields per porting guide §"Public surface"
- [ ] `CompactionLevel` literal type matches `"normal" | "aggressive" | "fallback" | "capped"` exactly (no extras)
- [ ] `phase_results: list[CompactionResult]` field present for `compactFullSweep` aggregation
- [ ] `_persist_compaction_event` is a STRUCTURED LOG CALL — no DB row written for the event itself (per LCM's intentional removal of history pollution)
- [ ] Log event includes: `conversation_id`, `action_taken`, `tokens_before`, `tokens_after`, `delta`, `level`, `condensed`, `auth_failure`, `created_summary_id`, `reason`
- [ ] `_persist_compaction_events` iterates over a `list[dict | None]` and skips None entries
- [ ] Telemetry-store write call sites in place for: leaf success, condensed success, auth failure
- [ ] Telemetry-store calls are stubbed-safe (degrade gracefully if Epic 01 store is incomplete)
- [ ] PR description cites LCM commit SHA `1f07fbd`

## Tests

Port from `test/compaction-maintenance-store.test.ts`:

### `CompactionResult` shape

- `CompactionResult default values` (action_taken=False, no created_summary_id, condensed=False, auth_failure=False)
- `CompactionResult phase_results aggregation` (compactFullSweep returns parent with 2-element phase_results)
- `CompactionLevel literal accepts only 4 values` (mypy/pyright passes when assigning one of the 4; rejects "invalid")

### Persistence (log)

- `_persist_compaction_event emits structured log` (capture log records; assert extras present)
- `_persist_compaction_events skips None entries` (mixed list with one None → 1 log call, not 2)
- `_persist_compaction_event does NOT write a chat message row` (assert `conversation_store.create_message` not called)

### Telemetry store integration

- `successful _leaf_pass calls mark_leaf_compaction_success` (mock store; assert call args)
- `successful _condensed_pass calls mark_condensed_compaction_success`
- `auth-failed compaction calls mark_auth_failure`
- `circuit-breaker-open compaction does NOT call mark_*_success` (only auth_failure or no-op)

## Estimated effort
4–6 hours

## Confidence
95% — pure plumbing once `CompactionResult` is finalized. Telemetry store calls are stubbable.
