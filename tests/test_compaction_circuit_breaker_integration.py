"""Tests for the 04-07 circuit-breaker wiring on :class:`LCMEngine`.

Covers issue 04-07 acceptance criteria
(``epics/04-compaction/04-07-circuit-breaker-integration.md``):

* Helper methods on :class:`_CompactMixin`:

  - :meth:`LCMEngine._resolve_breaker_key` — produces
    ``f"{provider}::{model}"`` with ``"unknown"`` fallback for ``None``
    legs.
  - :meth:`LCMEngine._get_circuit_breaker_state` — alias over the
    shell's :meth:`_get_or_create_circuit_breaker`.
  - :meth:`LCMEngine._is_circuit_breaker_open` —
    :meth:`CircuitBreaker.is_open` wrapper; preserves the implicit
    half-open transition behavior.
  - :meth:`LCMEngine._record_compaction_auth_failure` /
    :meth:`LCMEngine._record_compaction_success` — wrappers that match
    the TS source's recordCompactionAuthFailure/Success surface.

* :meth:`LCMEngine.compact` — public entry that:

  - Short-circuits with ``reason="circuit breaker open"`` when the
    breaker is open.
  - Catches :class:`LcmProviderAuthError` from
    :meth:`_execute_compaction_core` and increments the breaker.
  - Honors a ``result.auth_failure=True`` flag from the core (same
    increment path).
  - Calls :meth:`_record_compaction_success` on a non-auth-failure
    success — resets the breaker fully.

* Half-open / cooldown behavior:

  - After cooldown elapses, the next compact() probe runs; success
    resets the breaker, failure re-opens with a fresh cooldown.
  - Per-(provider, model) scope — opening
    ``anthropic::claude-3-opus`` does NOT affect
    ``openai::gpt-4-turbo``.

The :meth:`_execute_compaction_core` default body raises
:class:`NotImplementedError` so 04-07 itself does not depend on a
fully-wired :class:`~lossless_hermes.compaction.CompactionEngine` —
tests subclass :class:`LCMEngine` and supply scripted results (the
same pattern :class:`_ScriptedEngine` uses in
:mod:`tests.test_compaction_anti_thrashing`).

### Source references

* TS source: ``lossless-claw/src/engine.ts`` (LCM commit ``1f07fbd``
  on branch ``pr-613``), lines 1782 (state field), 1963-2016
  (machine), 3376 / 3427-3429 / 3496-3498 / 6895 / 6976-6978 (call
  sites).
* Spec: ``epics/04-compaction/04-07-circuit-breaker-integration.md``.
* Porting guide: ``docs/porting-guides/engine.md`` §"Circuit-breaker
  logic".
* ADR-029 (Wave-N provenance — no Wave-N markers in the breaker
  itself; the integration is pre-Wave-N stable since LCM v3).
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import pytest

from lossless_hermes.compaction import CompactionResult
from lossless_hermes.db.config import LcmConfig
from lossless_hermes.engine import LCMEngine
from lossless_hermes.engine.circuit_breaker import CircuitBreaker
from lossless_hermes.summarize import LcmProviderAuthError


# ---------------------------------------------------------------------------
# Scripted engine — overrides :meth:`_execute_compaction_core` with a FIFO
# ---------------------------------------------------------------------------


class _ScriptedCompactEngine(LCMEngine):
    """An :class:`LCMEngine` with a scripted :meth:`_execute_compaction_core`.

    Each call pops the next scripted result OR raises if the next
    entry is an ``LcmProviderAuthError`` instance. Empty scripts raise
    :class:`AssertionError` so tests don't silently fall through to
    the base ``NotImplementedError`` body.

    Args:
        scripted_results: FIFO of (a) :class:`CompactionResult`
            instances, or (b) :class:`LcmProviderAuthError` instances
            (which are *raised* rather than returned), or (c)
            ``Exception`` subclasses that will be raised.
    """

    def __init__(
        self,
        *,
        scripted_results: list[CompactionResult | Exception] | None = None,
        config: LcmConfig | None = None,
    ) -> None:
        super().__init__(config=config)
        self.scripted_results: list[CompactionResult | Exception] = list(scripted_results or [])
        self.core_call_count: int = 0
        # Recorded args for assertion in tests.
        self.last_core_args: dict | None = None

    def _execute_compaction_core(
        self,
        *,
        conversation_id: int,
        token_budget: int,
        current_tokens: int,
        provider: Optional[str],
        model: Optional[str],
    ) -> CompactionResult:
        self.core_call_count += 1
        self.last_core_args = {
            "conversation_id": conversation_id,
            "token_budget": token_budget,
            "current_tokens": current_tokens,
            "provider": provider,
            "model": model,
        }
        assert self.scripted_results, (
            "ScriptedCompactEngine: ran out of scripted results "
            f"(core_call_count={self.core_call_count})"
        )
        next_result = self.scripted_results.pop(0)
        if isinstance(next_result, Exception):
            raise next_result
        return next_result


def _successful_result(
    *,
    tokens_before: int = 1000,
    tokens_after: int = 500,
    passes: int = 1,
) -> CompactionResult:
    """Convenience factory for a happy-path :class:`CompactionResult`."""
    return CompactionResult(
        action_taken=True,
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        created_summary_id="sum_test",
        condensed=False,
        level="normal",
        passes_completed=passes,
    )


# ---------------------------------------------------------------------------
# _resolve_breaker_key
# ---------------------------------------------------------------------------


def test_resolve_breaker_key_format() -> None:
    """Both legs present → ``f"{provider}::{model}"``."""
    engine = LCMEngine()
    assert engine._resolve_breaker_key("anthropic", "claude-3-opus") == "anthropic::claude-3-opus"


def test_resolve_breaker_key_none_provider_falls_back_to_unknown() -> None:
    """``provider=None`` → ``"unknown::<model>"``."""
    engine = LCMEngine()
    assert engine._resolve_breaker_key(None, "claude-3-opus") == "unknown::claude-3-opus"


def test_resolve_breaker_key_none_model_falls_back_to_unknown() -> None:
    """``model=None`` → ``"<provider>::unknown"``."""
    engine = LCMEngine()
    assert engine._resolve_breaker_key("anthropic", None) == "anthropic::unknown"


def test_resolve_breaker_key_both_none() -> None:
    """Both ``None`` → ``"unknown::unknown"``."""
    engine = LCMEngine()
    assert engine._resolve_breaker_key(None, None) == "unknown::unknown"


# ---------------------------------------------------------------------------
# Helper-method aliases over CircuitBreaker
# ---------------------------------------------------------------------------


def test_get_circuit_breaker_state_returns_breaker_instance() -> None:
    """``_get_circuit_breaker_state`` returns the CircuitBreaker for the key."""
    engine = LCMEngine()
    breaker = engine._get_circuit_breaker_state("anthropic::claude-3-opus")
    assert isinstance(breaker, CircuitBreaker)
    # Same instance is returned for the same key (identity stable).
    again = engine._get_circuit_breaker_state("anthropic::claude-3-opus")
    assert breaker is again


def test_get_circuit_breaker_state_applies_config_defaults() -> None:
    """Breaker inherits threshold + cooldown from LcmConfig.

    ``LcmConfig`` defaults: threshold=5, cooldown_ms=1_800_000 (30min).
    """
    engine = LCMEngine()
    breaker = engine._get_circuit_breaker_state("k1")
    assert breaker.threshold == 5
    assert breaker.cooldown_s == 1800.0


def test_get_circuit_breaker_state_respects_custom_config() -> None:
    """Custom config values flow through."""
    cfg = LcmConfig(circuit_breaker_threshold=3, circuit_breaker_cooldown_ms=10_000)
    engine = LCMEngine(config=cfg)
    breaker = engine._get_circuit_breaker_state("k1")
    assert breaker.threshold == 3
    assert breaker.cooldown_s == 10.0


def test_is_circuit_breaker_open_returns_false_for_fresh_key() -> None:
    """A never-failed key returns False — fresh breaker is closed."""
    engine = LCMEngine()
    assert engine._is_circuit_breaker_open("never-failed") is False


def test_record_auth_failure_increments_breaker() -> None:
    """``_record_compaction_auth_failure`` ticks the breaker.

    Test passes a low-threshold config so we can observe the open
    transition without 5 calls.
    """
    engine = LCMEngine(config=LcmConfig(circuit_breaker_threshold=2))
    key = "anthropic::claude-3-opus"
    engine._record_compaction_auth_failure(key)
    breaker = engine._get_circuit_breaker_state(key)
    assert breaker.failures == 1
    assert breaker.state == "closed"
    # 2nd failure opens.
    engine._record_compaction_auth_failure(key)
    assert breaker.failures == 2
    assert breaker.state == "open"
    assert engine._is_circuit_breaker_open(key) is True


def test_record_success_resets_breaker() -> None:
    """``_record_compaction_success`` resets failures + state."""
    engine = LCMEngine(config=LcmConfig(circuit_breaker_threshold=3))
    key = "anthropic::claude-3-opus"
    engine._record_compaction_auth_failure(key)
    engine._record_compaction_auth_failure(key)
    engine._record_compaction_success(key)
    breaker = engine._get_circuit_breaker_state(key)
    assert breaker.failures == 0
    assert breaker.state == "closed"
    assert breaker.open_since is None


def test_record_success_closes_open_breaker() -> None:
    """Success on an OPEN breaker still resets (matches TS unconditional reset)."""
    engine = LCMEngine(config=LcmConfig(circuit_breaker_threshold=2))
    key = "anthropic::claude-3-opus"
    engine._record_compaction_auth_failure(key)
    engine._record_compaction_auth_failure(key)
    assert engine._is_circuit_breaker_open(key) is True
    engine._record_compaction_success(key)
    assert engine._is_circuit_breaker_open(key) is False


# ---------------------------------------------------------------------------
# Core spec tests — translated from the issue's tests section
# ---------------------------------------------------------------------------


def test_breaker_stays_closed_below_threshold() -> None:
    """Spec: ``breaker stays closed below threshold``.

    2 failures with threshold=3 → still closed.
    """
    engine = LCMEngine(config=LcmConfig(circuit_breaker_threshold=3))
    key = engine._resolve_breaker_key("anthropic", "claude-3-opus")
    engine._record_compaction_auth_failure(key)
    engine._record_compaction_auth_failure(key)
    assert engine._is_circuit_breaker_open(key) is False
    assert engine._get_circuit_breaker_state(key).failures == 2


def test_breaker_opens_at_exactly_threshold() -> None:
    """Spec: ``breaker opens at exactly threshold``.

    3 failures with threshold=3 → open.
    """
    engine = LCMEngine(config=LcmConfig(circuit_breaker_threshold=3))
    key = engine._resolve_breaker_key("anthropic", "claude-3-opus")
    for _ in range(3):
        engine._record_compaction_auth_failure(key)
    assert engine._is_circuit_breaker_open(key) is True


def test_breaker_open_returns_no_op_compaction_result() -> None:
    """Spec: ``breaker open returns no-op CompactionResult with reason="circuit breaker open"``.

    Force the breaker open then call :meth:`compact` and verify the
    short-circuit shape.
    """
    cfg = LcmConfig(circuit_breaker_threshold=2)
    engine = _ScriptedCompactEngine(scripted_results=[], config=cfg)
    key = engine._resolve_breaker_key("anthropic", "claude-3-opus")
    engine._record_compaction_auth_failure(key)
    engine._record_compaction_auth_failure(key)
    assert engine._is_circuit_breaker_open(key) is True

    result = engine.compact(
        conversation_id=1,
        token_budget=100_000,
        current_tokens=80_000,
        provider="anthropic",
        model="claude-3-opus",
    )
    assert result.action_taken is False
    assert result.reason == "circuit breaker open"
    assert result.auth_failure is False
    # No core call was made — breaker short-circuited.
    assert engine.core_call_count == 0
    # tokens_before == tokens_after == current_tokens (no work).
    assert result.tokens_before == 80_000
    assert result.tokens_after == 80_000


def test_success_resets_failures_and_open_since() -> None:
    """Spec: ``success resets failures AND open_since``."""
    engine = LCMEngine(config=LcmConfig(circuit_breaker_threshold=2))
    key = engine._resolve_breaker_key("anthropic", "claude-3-opus")
    engine._record_compaction_auth_failure(key)
    engine._record_compaction_auth_failure(key)
    breaker = engine._get_circuit_breaker_state(key)
    assert breaker.open_since is not None
    assert breaker.failures == 2

    engine._record_compaction_success(key)
    assert breaker.failures == 0
    assert breaker.open_since is None
    assert breaker.state == "closed"


def test_breaker_half_opens_after_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec: ``breaker half-opens after cooldown``.

    Mock the monotonic clock; advance past cooldown_ms; verify
    :meth:`_is_circuit_breaker_open` returns False.
    """
    cfg = LcmConfig(circuit_breaker_threshold=1, circuit_breaker_cooldown_ms=10_000)
    engine = LCMEngine(config=cfg)
    key = engine._resolve_breaker_key("anthropic", "claude-3-opus")

    base = 1000.0
    monkeypatch.setattr(time, "monotonic", lambda: base)
    engine._record_compaction_auth_failure(key)
    assert engine._is_circuit_breaker_open(key) is True

    # Advance past cooldown.
    monkeypatch.setattr(time, "monotonic", lambda: base + 11.0)
    assert engine._is_circuit_breaker_open(key) is False
    # And the state transitioned to half_open (preserving the
    # failure counter — per the spec's "Don't reset the counter here"
    # invariant).
    breaker = engine._get_circuit_breaker_state(key)
    assert breaker.state == "half_open"
    assert breaker.failures == 1


def test_half_open_probe_failure_re_opens_breaker_with_fresh_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec: ``half-open probe failure re-opens breaker with fresh cooldown``.

    Per the spec's "Half-open semantics — explicit": if the probe
    fails, ``_record_compaction_auth_failure`` increments failures
    (already at threshold) and updates ``open_since`` to the new time.
    Effectively the breaker re-opens with a fresh cooldown window.
    Failure counter is NOT reset on probe-fail.
    """
    cfg = LcmConfig(circuit_breaker_threshold=1, circuit_breaker_cooldown_ms=10_000)
    engine = LCMEngine(config=cfg)
    key = engine._resolve_breaker_key("anthropic", "claude-3-opus")

    base = 1000.0
    monkeypatch.setattr(time, "monotonic", lambda: base)
    engine._record_compaction_auth_failure(key)
    first_open_since = engine._get_circuit_breaker_state(key).open_since

    # Advance past cooldown — triggers half_open on the next is_open call.
    monkeypatch.setattr(time, "monotonic", lambda: base + 11.0)
    assert engine._is_circuit_breaker_open(key) is False
    assert engine._get_circuit_breaker_state(key).state == "half_open"

    # Probe-fail at a new monotonic time.
    monkeypatch.setattr(time, "monotonic", lambda: base + 12.0)
    engine._record_compaction_auth_failure(key)
    breaker = engine._get_circuit_breaker_state(key)
    assert breaker.state == "open"
    # Failure counter incremented (NOT reset).
    assert breaker.failures == 2
    # Fresh open_since.
    assert breaker.open_since is not None
    assert breaker.open_since != first_open_since
    assert engine._is_circuit_breaker_open(key) is True


def test_half_open_probe_success_resets_breaker_fully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec: ``half-open probe success resets breaker fully``."""
    cfg = LcmConfig(circuit_breaker_threshold=1, circuit_breaker_cooldown_ms=10_000)
    engine = LCMEngine(config=cfg)
    key = engine._resolve_breaker_key("anthropic", "claude-3-opus")

    base = 1000.0
    monkeypatch.setattr(time, "monotonic", lambda: base)
    engine._record_compaction_auth_failure(key)
    monkeypatch.setattr(time, "monotonic", lambda: base + 11.0)
    engine._is_circuit_breaker_open(key)  # half_open transition

    # Probe-success.
    engine._record_compaction_success(key)
    breaker = engine._get_circuit_breaker_state(key)
    assert breaker.state == "closed"
    assert breaker.failures == 0
    assert breaker.open_since is None


def test_breaker_is_per_provider_model_scope() -> None:
    """Spec: ``breaker is per-provider-model scope``.

    Failures on ``(anthropic, opus)`` don't affect ``(openai, gpt-4)``.
    """
    engine = LCMEngine(config=LcmConfig(circuit_breaker_threshold=2))
    key_a = engine._resolve_breaker_key("anthropic", "claude-3-opus")
    key_b = engine._resolve_breaker_key("openai", "gpt-4-turbo")

    engine._record_compaction_auth_failure(key_a)
    engine._record_compaction_auth_failure(key_a)
    assert engine._is_circuit_breaker_open(key_a) is True
    # The other scope is unaffected.
    assert engine._is_circuit_breaker_open(key_b) is False
    assert engine._get_circuit_breaker_state(key_b).failures == 0


def test_breaker_key_resolution_handles_missing_provider_or_model() -> None:
    """Spec: ``breaker key resolution handles missing provider/model``."""
    engine = LCMEngine()
    assert engine._resolve_breaker_key(None, "claude") == "unknown::claude"
    assert engine._resolve_breaker_key("anthropic", None) == "anthropic::unknown"
    assert engine._resolve_breaker_key(None, None) == "unknown::unknown"


# ---------------------------------------------------------------------------
# compact() — happy path / auth-failure-via-exception / auth-via-flag
# ---------------------------------------------------------------------------


def test_compact_with_closed_breaker_calls_execute_compaction_core() -> None:
    """Spec: ``compact() with closed breaker calls _execute_compaction_core``.

    Happy path — the scripted engine returns a successful result and
    the engine.compact wrapper passes it through with the breaker
    recorded as success.
    """
    cfg = LcmConfig(circuit_breaker_threshold=3)
    engine = _ScriptedCompactEngine(
        scripted_results=[_successful_result(tokens_before=1000, tokens_after=400)],
        config=cfg,
    )
    result = engine.compact(
        conversation_id=42,
        token_budget=100_000,
        current_tokens=80_000,
        provider="anthropic",
        model="claude-3-opus",
    )
    assert engine.core_call_count == 1
    assert result.action_taken is True
    assert result.tokens_before == 1000
    assert result.tokens_after == 400
    assert result.auth_failure is False
    assert result.reason is None  # success path doesn't synthesize a reason

    # Core received the forwarded args.
    assert engine.last_core_args == {
        "conversation_id": 42,
        "token_budget": 100_000,
        "current_tokens": 80_000,
        "provider": "anthropic",
        "model": "claude-3-opus",
    }

    # Success was recorded — breaker is closed with zero failures.
    key = engine._resolve_breaker_key("anthropic", "claude-3-opus")
    breaker = engine._get_circuit_breaker_state(key)
    assert breaker.failures == 0
    assert breaker.state == "closed"


def test_compact_catches_lcm_provider_auth_error_and_increments_breaker() -> None:
    """Spec: ``compact() catches LcmProviderAuthError and increments breaker``.

    Exception-shaped auth failure path. The wrapper catches the
    exception, increments the breaker, and returns a result with
    ``auth_failure=True``.
    """
    cfg = LcmConfig(circuit_breaker_threshold=3)
    engine = _ScriptedCompactEngine(
        scripted_results=[LcmProviderAuthError("HTTP 401 from provider")],
        config=cfg,
    )

    result = engine.compact(
        conversation_id=1,
        token_budget=100_000,
        current_tokens=80_000,
        provider="anthropic",
        model="claude-3-opus",
    )
    assert engine.core_call_count == 1
    assert result.action_taken is False
    assert result.auth_failure is True
    assert result.reason == "provider auth failure"

    # Breaker incremented.
    key = engine._resolve_breaker_key("anthropic", "claude-3-opus")
    breaker = engine._get_circuit_breaker_state(key)
    assert breaker.failures == 1


def test_compact_honors_auth_failure_flag_on_result() -> None:
    """A core result with ``auth_failure=True`` also increments the breaker.

    Some call paths in TS catch the exception inside the core and
    propagate via the result flag. The :meth:`compact` body MUST treat
    both paths identically; this test asserts the flag-shaped path
    also ticks the breaker.
    """
    cfg = LcmConfig(circuit_breaker_threshold=3)
    auth_failure_result = CompactionResult(
        action_taken=False,
        tokens_before=80_000,
        tokens_after=80_000,
        created_summary_id=None,
        condensed=False,
        level=None,
        passes_completed=0,
        auth_failure=True,
    )
    engine = _ScriptedCompactEngine(scripted_results=[auth_failure_result], config=cfg)

    result = engine.compact(
        conversation_id=1,
        token_budget=100_000,
        current_tokens=80_000,
        provider="anthropic",
        model="claude-3-opus",
    )
    assert engine.core_call_count == 1
    assert result.auth_failure is True
    assert result.reason == "provider auth failure"

    # Breaker incremented despite no exception being raised.
    key = engine._resolve_breaker_key("anthropic", "claude-3-opus")
    breaker = engine._get_circuit_breaker_state(key)
    assert breaker.failures == 1


def test_repeated_auth_failures_open_the_breaker() -> None:
    """N consecutive auth-failure call cycles open the breaker."""
    cfg = LcmConfig(circuit_breaker_threshold=3)
    engine = _ScriptedCompactEngine(
        scripted_results=[
            LcmProviderAuthError("attempt 1"),
            LcmProviderAuthError("attempt 2"),
            LcmProviderAuthError("attempt 3"),
        ],
        config=cfg,
    )

    for _ in range(3):
        engine.compact(
            conversation_id=1,
            token_budget=100_000,
            current_tokens=80_000,
            provider="anthropic",
            model="claude-3-opus",
        )

    key = engine._resolve_breaker_key("anthropic", "claude-3-opus")
    breaker = engine._get_circuit_breaker_state(key)
    assert breaker.failures == 3
    assert breaker.state == "open"
    assert engine._is_circuit_breaker_open(key) is True


def test_breaker_opens_then_short_circuits_subsequent_compact_calls() -> None:
    """After breaker opens, subsequent compact() calls do NOT invoke the core."""
    cfg = LcmConfig(circuit_breaker_threshold=2)
    engine = _ScriptedCompactEngine(
        scripted_results=[
            LcmProviderAuthError("attempt 1"),
            LcmProviderAuthError("attempt 2"),
            # No further scripted results; if the breaker fails to
            # gate, the next call will raise AssertionError from
            # _ScriptedCompactEngine.
        ],
        config=cfg,
    )

    # Trip the breaker open.
    for _ in range(2):
        engine.compact(
            conversation_id=1,
            token_budget=100_000,
            current_tokens=80_000,
            provider="anthropic",
            model="claude-3-opus",
        )

    # Next call should be short-circuited.
    result = engine.compact(
        conversation_id=1,
        token_budget=100_000,
        current_tokens=80_000,
        provider="anthropic",
        model="claude-3-opus",
    )
    assert result.reason == "circuit breaker open"
    # Core was NOT called a 3rd time.
    assert engine.core_call_count == 2


def test_compact_resets_breaker_after_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end half-open recovery: open → cooldown → probe success → closed."""
    cfg = LcmConfig(circuit_breaker_threshold=1, circuit_breaker_cooldown_ms=10_000)
    engine = _ScriptedCompactEngine(
        scripted_results=[
            LcmProviderAuthError("first failure"),
            _successful_result(tokens_before=1000, tokens_after=300),
        ],
        config=cfg,
    )

    base = 1000.0
    monkeypatch.setattr(time, "monotonic", lambda: base)
    # First call fails → breaker opens.
    engine.compact(
        conversation_id=1,
        token_budget=100_000,
        current_tokens=80_000,
        provider="anthropic",
        model="claude-3-opus",
    )
    key = engine._resolve_breaker_key("anthropic", "claude-3-opus")
    assert engine._is_circuit_breaker_open(key) is True

    # Advance time past cooldown — breaker auto-transitions to half_open.
    monkeypatch.setattr(time, "monotonic", lambda: base + 11.0)
    # Probe call succeeds.
    result = engine.compact(
        conversation_id=1,
        token_budget=100_000,
        current_tokens=80_000,
        provider="anthropic",
        model="claude-3-opus",
    )
    assert result.action_taken is True
    assert result.tokens_after == 300

    # Breaker reset.
    breaker = engine._get_circuit_breaker_state(key)
    assert breaker.state == "closed"
    assert breaker.failures == 0
    assert breaker.open_since is None


def test_compact_during_cooldown_does_not_call_core() -> None:
    """Within the cooldown window, compact() short-circuits — core untouched."""
    cfg = LcmConfig(circuit_breaker_threshold=1, circuit_breaker_cooldown_ms=60_000)
    engine = _ScriptedCompactEngine(
        scripted_results=[LcmProviderAuthError("trip")],
        config=cfg,
    )

    engine.compact(
        conversation_id=1,
        token_budget=100_000,
        current_tokens=80_000,
        provider="anthropic",
        model="claude-3-opus",
    )
    pre_count = engine.core_call_count
    assert engine.core_call_count == 1

    # Still within cooldown — second call short-circuits.
    result = engine.compact(
        conversation_id=1,
        token_budget=100_000,
        current_tokens=80_000,
        provider="anthropic",
        model="claude-3-opus",
    )
    assert result.reason == "circuit breaker open"
    assert engine.core_call_count == pre_count  # unchanged


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def test_compact_logs_breaker_open_short_circuit(caplog: pytest.LogCaptureFixture) -> None:
    """The breaker-open short-circuit emits an INFO log."""
    cfg = LcmConfig(circuit_breaker_threshold=1)
    engine = _ScriptedCompactEngine(
        scripted_results=[LcmProviderAuthError("trip")],
        config=cfg,
    )
    # Trip the breaker.
    engine.compact(
        conversation_id=1,
        token_budget=100_000,
        current_tokens=80_000,
        provider="anthropic",
        model="claude-3-opus",
    )
    # Second call should log the breaker-open short-circuit.
    with caplog.at_level(logging.INFO, logger="lossless_hermes.engine.compact"):
        engine.compact(
            conversation_id=1,
            token_budget=100_000,
            current_tokens=80_000,
            provider="anthropic",
            model="claude-3-opus",
        )
    assert any("breaker open for anthropic::claude-3-opus" in rec.message for rec in caplog.records)


def test_compact_logs_auth_failure(caplog: pytest.LogCaptureFixture) -> None:
    """Exception-shaped auth failure emits a WARNING log."""
    cfg = LcmConfig(circuit_breaker_threshold=10)  # Won't open
    engine = _ScriptedCompactEngine(
        scripted_results=[LcmProviderAuthError("HTTP 401 from provider")],
        config=cfg,
    )
    with caplog.at_level(logging.WARNING, logger="lossless_hermes.engine.compact"):
        engine.compact(
            conversation_id=1,
            token_budget=100_000,
            current_tokens=80_000,
            provider="anthropic",
            model="claude-3-opus",
        )
    assert any("provider auth failure" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Default _execute_compaction_core body
# ---------------------------------------------------------------------------


def test_default_execute_compaction_core_raises_notimplementederror() -> None:
    """The 04-07 default body raises NotImplementedError.

    The shell ships without a wired CompactionEngine — production
    wiring lives in a future Epic 04 wrap-up issue. Tests must
    override :meth:`_execute_compaction_core` to drive
    :meth:`compact`; the NotImplementedError is the loud signal that
    a caller forgot.
    """
    engine = LCMEngine()
    with pytest.raises(NotImplementedError, match="_execute_compaction_core is not yet wired"):
        engine.compact(
            conversation_id=1,
            token_budget=100_000,
            current_tokens=80_000,
            provider="anthropic",
            model="claude-3-opus",
        )


# ---------------------------------------------------------------------------
# Breaker key defaults — compact() works with None provider/model
# ---------------------------------------------------------------------------


def test_compact_works_with_none_provider_and_model() -> None:
    """``provider=None, model=None`` keys to ``unknown::unknown`` — still functional."""
    cfg = LcmConfig(circuit_breaker_threshold=3)
    engine = _ScriptedCompactEngine(
        scripted_results=[_successful_result()],
        config=cfg,
    )
    result = engine.compact(
        conversation_id=1,
        token_budget=100_000,
        current_tokens=80_000,
    )
    assert result.action_taken is True
    # Breaker on the "unknown::unknown" key was created + reset.
    assert "unknown::unknown" in engine._circuit_breakers
    breaker = engine._get_circuit_breaker_state("unknown::unknown")
    assert breaker.failures == 0
