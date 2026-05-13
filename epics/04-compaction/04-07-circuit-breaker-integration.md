---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-04] compaction: wire circuit-breaker fields into summarize.py'
labels: 'port'
---

## Source (TypeScript)
- File: `lossless-claw/src/engine.ts` (pr-613 `1f07fbd`)
- Lines:
  - Circuit-breaker state field: 1782 (`circuitBreakerStates: Map<string, CircuitBreakerState>`)
  - State machine: 1963–2016 (~54 LOC)
  - Configuration: `config.circuitBreakerThreshold`, `config.circuitBreakerCooldownMs` (defaults: threshold=3, cooldown=60_000 ms)
  - Integration sites:
    - `recordCompactionAuthFailure(breakerKey)` — opens breaker after N failures
    - `recordCompactionSuccess(breakerKey)` — resets breaker on any success
    - `isCircuitBreakerOpen(breakerKey)` — gate check before calling summarize
- Test: `test/circuit-breaker.test.ts`
- Function(s)/class(es): `LcmContextEngine.getCircuitBreakerState`, `recordCompactionAuthFailure`, `recordCompactionSuccess`, `isCircuitBreakerOpen`

## Target (Python)
- File: `src/lossless_hermes/engine/compact.py` (the `_CompactMixin` per ADR-027) — owns the state
- File: `src/lossless_hermes/summarize.py` — consumer; receives the gate state via the `LcmSummarizer` constructor or per-call argument
- Estimated LOC: ~80 (the state machine itself is small; integration is the volume)
- Methods on `_CompactMixin`:
  - `_get_circuit_breaker_state(breaker_key) → dict` — lazy-creates
  - `_record_compaction_auth_failure(breaker_key) → None`
  - `_record_compaction_success(breaker_key) → None`
  - `_is_circuit_breaker_open(breaker_key) → bool`
- State on `LCMEngine` shell (per ADR-027 §"All state lives on the shell class"):
  - `self._circuit_breaker_states: dict[str, dict]` initialized in `LCMEngine.__init__`

## Why the seam lives at the engine

Per `docs/porting-guides/engine.md` §"Circuit-breaker logic":

> State per `breakerKey` (provider/model scope): `{ failures: number, openSince: number | null }`. Opens after `circuitBreakerThreshold` consecutive auth failures from the summarizer; cooldown is `circuitBreakerCooldownMs`. While open, compaction is no-op (`reason: "circuit breaker open"`). On any success, reset.

The breaker is **per (provider, model) scope**, not per-conversation. It belongs on the engine because:

1. State must persist across many `_leaf_pass` and `_condensed_pass` invocations.
2. The same auth failure on `(anthropic, claude-3-opus)` should affect every conversation, not just the one that hit it.
3. The breaker is checked BEFORE the summarize call (gate), not inside it — so the engine is the natural call site.

## Algorithm

### State shape

```python
@dataclass
class CircuitBreakerState:
    failures: int = 0
    open_since: float | None = None  # epoch seconds, None when closed
```

### `_get_circuit_breaker_state(breaker_key)`

Lazy-create with `failures=0, open_since=None` if absent.

### `_record_compaction_auth_failure(breaker_key)`

```python
def _record_compaction_auth_failure(self, breaker_key: str) -> None:
    state = self._get_circuit_breaker_state(breaker_key)
    state["failures"] += 1
    if state["failures"] >= self.config.circuit_breaker_threshold:
        state["open_since"] = time.time()
        self._log.warn(f"circuit breaker opened for {breaker_key} after {state['failures']} consecutive auth failures")
```

### `_record_compaction_success(breaker_key)`

```python
def _record_compaction_success(self, breaker_key: str) -> None:
    state = self._get_circuit_breaker_state(breaker_key)
    if state["failures"] > 0 or state["open_since"] is not None:
        self._log.info(f"circuit breaker reset for {breaker_key}")
    state["failures"] = 0
    state["open_since"] = None
```

Reset on **any** success — half-open recovery is implicit (one successful call closes the breaker).

### `_is_circuit_breaker_open(breaker_key)`

```python
def _is_circuit_breaker_open(self, breaker_key: str) -> bool:
    state = self._get_circuit_breaker_state(breaker_key)
    if state["open_since"] is None:
        return False
    elapsed_ms = (time.time() - state["open_since"]) * 1000
    if elapsed_ms >= self.config.circuit_breaker_cooldown_ms:
        # Cooldown expired — let the next call probe (half-open). Don't reset
        # the counter here; reset happens on successful probe.
        return False
    return True
```

### Integration with compaction

Inside `_CompactMixin.compact()` (the public engine entry point):

```python
def compact(self, *, conversation_id, token_budget, ...) -> CompactionResult:
    breaker_key = self._resolve_breaker_key(provider, model)
    if self._is_circuit_breaker_open(breaker_key):
        return CompactionResult(
            action_taken=False,
            tokens_before=current_tokens,
            tokens_after=current_tokens,
            reason="circuit breaker open",
        )
    try:
        result = self._execute_compaction_core(...)
    except LcmProviderAuthError:
        self._record_compaction_auth_failure(breaker_key)
        return CompactionResult(action_taken=False, auth_failure=True, ...)
    self._record_compaction_success(breaker_key)
    return result
```

The summarizer itself does NOT touch the breaker — it raises `LcmProviderAuthError` (issue 04-06) and the engine wraps the call.

### Breaker key resolution

```python
def _resolve_breaker_key(self, provider: str | None, model: str | None) -> str:
    return f"{provider or 'unknown'}::{model or 'unknown'}"
```

TS uses the same `provider::model` format. Per-conversation breakers (`breaker_key = conversation_id`) are NOT used — failures are provider-wide.

## Wave-N fixes

No specific Wave-N fix in the breaker code itself, but the breaker exists because of historical incidents:

```python
# Compaction circuit breaker: opens after N consecutive auth failures on the
# same (provider, model) scope. Prevents retry-storm during provider outages
# from exhausting backoff budgets across conversations.
# Original: lossless-claw/src/engine.ts:1782 (state), 1963-2016 (machine).
```

## Half-open semantics — explicit

The TS implementation is "open after N failures, closed on any success, half-open after cooldown expires (return False to allow probe)". Port these semantics exactly. The probe call IS a normal compaction call — there is no special "half-open" code path.

If the probe fails again, `_record_compaction_auth_failure` increments `failures` (already at threshold) and updates `open_since` to the new time. Effectively the breaker re-opens with a fresh cooldown window.

## Dependencies
- Depends on: Epic 02 (`LCMEngine.__init__` initializes `self._circuit_breaker_states`; `config.circuit_breaker_threshold` and `config.circuit_breaker_cooldown_ms` are config fields)
- Depends on: Issue 04-06 (`LcmProviderAuthError` is the trigger; this issue wires it into the breaker)
- Blocks: Issue 04-08 (breaker open/close state is part of compaction telemetry)

## Acceptance criteria
- [ ] `self._circuit_breaker_states: dict[str, dict]` initialized empty in `LCMEngine.__init__`
- [ ] `_get_circuit_breaker_state` lazy-creates with `{failures: 0, open_since: None}`
- [ ] `_record_compaction_auth_failure` increments + opens at threshold (default 3)
- [ ] `_record_compaction_success` resets failures AND open_since on ANY success
- [ ] `_is_circuit_breaker_open` respects cooldown_ms (default 60_000) — returns False after elapsed > cooldown
- [ ] After cooldown expires, `_is_circuit_breaker_open` returns False (half-open) WITHOUT resetting the failure counter (reset happens only on successful probe)
- [ ] `compact()` returns `CompactionResult(action_taken=False, reason="circuit breaker open")` when breaker open
- [ ] `compact()` catches `LcmProviderAuthError` and calls `_record_compaction_auth_failure(breaker_key)`
- [ ] `compact()` calls `_record_compaction_success(breaker_key)` on non-exception path
- [ ] Breaker key format: `f"{provider}::{model}"` matching TS
- [ ] Inline comment present per ADR-029 format
- [ ] All TS unit tests in `test/circuit-breaker.test.ts` ported
- [ ] PR description cites LCM commit SHA `1f07fbd`

## Tests

Port from `test/circuit-breaker.test.ts`:

- `breaker stays closed below threshold` (2 failures with threshold=3 → still closed)
- `breaker opens at exactly threshold` (3 failures → open)
- `breaker open returns no-op CompactionResult with reason="circuit breaker open"`
- `success resets failures AND open_since`
- `breaker half-opens after cooldown` (mock clock; advance cooldown_ms → `_is_circuit_breaker_open` returns False)
- `half-open probe failure re-opens breaker with fresh cooldown` (no reset of failure counter on probe-fail)
- `half-open probe success resets breaker fully`
- `breaker is per-provider-model scope` (failures on `(anthropic, opus)` don't affect `(openai, gpt-4)`)
- `breaker key resolution handles missing provider/model` (`unknown::model` or `provider::unknown`)
- `compact() with closed breaker calls _execute_compaction_core` (happy path)
- `compact() catches LcmProviderAuthError and increments breaker`

## Estimated effort
6–8 hours

## Confidence
90% — the state machine is small and well-tested in TS. Risk is in placing the breaker check at the right scope (engine, not summarizer) and ensuring the half-open semantics don't accidentally reset the failure counter.
