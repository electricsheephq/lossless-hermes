---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-02] engine: port circuit-breaker state machine scaffold (no auth-failure handling yet)'
labels: 'port, epic-02'
---

## Source (TypeScript)
- File: `src/engine.ts`
- Lines:
  - `circuitBreakerStates: Map<string, CircuitBreakerState>` (1782) — field declaration
  - `getCircuitBreakerState`, `recordCompactionAuthFailure`, `recordCompactionSuccess`, `isCircuitBreakerOpen` (1963–2016) — the state machine methods
- Function(s)/class(es): all circuit-breaker primitives

## Target (Python)
- File: `src/lossless_hermes/engine/__init__.py` (state machine methods on the shell class) + `src/lossless_hermes/types.py` (`CircuitBreakerState` dataclass)
- Estimated LOC: ~120

## Summary

Port the circuit-breaker state machine **scaffold only** — the state transition primitives without the auth-failure handling (which depends on `summarize.py` and the LCM `LcmProviderAuthError` exception type, both Epic 04).

Per `docs/porting-guides/engine.md` "Circuit-breaker logic" (lines 237–242): per-`breakerKey` state of `{failures: number, openSince: number | null}`. Opens after `circuitBreakerThreshold` consecutive auth failures from the summarizer; cooldown is `circuitBreakerCooldownMs`. While open, compaction returns no-op with `reason: "circuit breaker open"`. On any success, reset.

This issue ships:
- `CircuitBreakerState` dataclass.
- `get_circuit_breaker_state(breaker_key)` — read-only accessor.
- `record_compaction_auth_failure(breaker_key)` — increments failures, opens if at threshold.
- `record_compaction_success(breaker_key)` — resets state to closed.
- `is_circuit_breaker_open(breaker_key)` — read + auto-close-on-cooldown-expired.

What it does NOT ship (Epic 04):
- Integration with `compact()` body (the no-op `compress()` from issue 02-06 doesn't make summarizer calls; nothing to fail).
- `LcmProviderAuthError` recognition (the exception type lands with `summarize.py`).
- Per-session/per-provider breaker key resolution policy (the simple key string is fine for the scaffold; Epic 04 may want richer keys like `f"{session_id}:{provider}/{model}"`).

## Implementation

```python
# src/lossless_hermes/types.py — already partially defined per issue 02-02

from dataclasses import dataclass
from typing import Optional


@dataclass
class CircuitBreakerState:
    """Per-key auth-failure breaker state.

    Maps to engine.ts:CircuitBreakerState struct around line 1782.
    """
    failures: int = 0
    open_since: Optional[float] = None  # monotonic timestamp; None when closed
```

```python
# src/lossless_hermes/engine/__init__.py

import time
import logging
from typing import Optional

from ..types import CircuitBreakerState

logger = logging.getLogger("lcm.engine.circuit_breaker")


# In _init_state_fields (issue 02-02):
# self._circuit_breaker_states: dict[str, CircuitBreakerState] = {}


def get_circuit_breaker_state(self, breaker_key: str) -> CircuitBreakerState:
    """Get-or-create the breaker state for a key.

    Maps to engine.ts:getCircuitBreakerState (line ~1963).
    """
    state = self._circuit_breaker_states.get(breaker_key)
    if state is None:
        state = CircuitBreakerState()
        self._circuit_breaker_states[breaker_key] = state
    return state


def record_compaction_auth_failure(self, breaker_key: str) -> None:
    """Increment failures; open the breaker if at threshold.

    Maps to engine.ts:recordCompactionAuthFailure. Epic 02: the wiring exists
    but no caller invokes it yet (compress() is no-op). Epic 04's
    executeCompactionCore catches LcmProviderAuthError and calls this.
    """
    state = self.get_circuit_breaker_state(breaker_key)
    state.failures += 1
    if state.failures >= self.config.circuit_breaker_threshold and state.open_since is None:
        state.open_since = time.monotonic()
        logger.warning(
            "[lcm] circuit breaker OPENED for key=%s after %d failures",
            breaker_key, state.failures,
        )


def record_compaction_success(self, breaker_key: str) -> None:
    """Reset state on any successful compaction.

    Maps to engine.ts:recordCompactionSuccess.
    """
    state = self._circuit_breaker_states.get(breaker_key)
    if state is None:
        return
    if state.failures > 0 or state.open_since is not None:
        logger.info(
            "[lcm] circuit breaker CLOSED for key=%s (was failures=%d, open=%s)",
            breaker_key, state.failures, state.open_since is not None,
        )
    state.failures = 0
    state.open_since = None


def is_circuit_breaker_open(self, breaker_key: str) -> bool:
    """Check whether the breaker is currently open. Auto-closes if cooldown expired.

    Maps to engine.ts:isCircuitBreakerOpen.
    """
    state = self._circuit_breaker_states.get(breaker_key)
    if state is None or state.open_since is None:
        return False

    cooldown_s = self.config.circuit_breaker_cooldown_ms / 1000.0
    elapsed = time.monotonic() - state.open_since

    if elapsed >= cooldown_s:
        # Auto-close after cooldown
        logger.info(
            "[lcm] circuit breaker COOLED DOWN for key=%s after %.1fs",
            breaker_key, elapsed,
        )
        state.failures = 0
        state.open_since = None
        return False

    return True
```

## Dependencies
- Depends on: 02-01 (engine shell), 02-02 (state field declaration), config has `circuit_breaker_threshold` and `circuit_breaker_cooldown_ms` defaults
- Blocks: Epic 04 (integrates the breaker with the real `compress()` body and `LcmProviderAuthError` exception catch)

## Acceptance criteria
- [ ] `engine.get_circuit_breaker_state("k1")` returns a fresh `CircuitBreakerState(failures=0, open_since=None)`
- [ ] After `circuit_breaker_threshold` (default 3) calls to `record_compaction_auth_failure("k1")`, `is_circuit_breaker_open("k1")` returns `True`
- [ ] `record_compaction_success("k1")` resets state; `is_circuit_breaker_open("k1")` returns `False`
- [ ] After cooldown elapses (mock `time.monotonic`), `is_circuit_breaker_open("k1")` returns `False` and state is reset
- [ ] Multiple keys are independent: opening `k1` does not open `k2`
- [ ] `pytest tests/test_circuit_breaker.py` passes
- [ ] No call site in Epic 02 invokes these methods (verified by grep — search blocks issue 02-06's no-op compress); Epic 04 is the consumer

## Tests
- `tests/test_circuit_breaker.py::test_initial_state` — assert fresh state is closed with failures=0
- `tests/test_circuit_breaker.py::test_open_at_threshold` — call `record_compaction_auth_failure` `circuit_breaker_threshold` times; assert open
- `tests/test_circuit_breaker.py::test_below_threshold_stays_closed` — call N-1 times; assert closed
- `tests/test_circuit_breaker.py::test_success_resets` — open the breaker; call `record_compaction_success`; assert closed
- `tests/test_circuit_breaker.py::test_cooldown_auto_close` — monkeypatch `time.monotonic`; advance past `circuit_breaker_cooldown_ms`; assert `is_open` returns False and state is reset
- `tests/test_circuit_breaker.py::test_multiple_keys_independent` — open `k1`; assert `k2` stays closed
- `tests/test_circuit_breaker.py::test_threshold_config_respected` — set `circuit_breaker_threshold=5` in config; assert 4 failures don't open; 5th does
- `tests/test_circuit_breaker.py::test_log_messages` — capture logs; assert "OPENED" / "CLOSED" / "COOLED DOWN" messages at the right transitions

## Estimated effort
8 hours

## Confidence
90% — the state machine itself is mechanical. The minor uncertainty is around breaker_key resolution policy in Epic 04 (will it be `f"{provider}/{model}"` or `f"{session_id}:{provider}/{model}"`?). This issue ships the primitives with the key as opaque string so Epic 04 can choose its own policy.
