"""Tests for the :class:`CircuitBreaker` state machine (issue 02-09).

Covers the additive surface added by issue 02-09:

* The :class:`~lossless_hermes.engine.circuit_breaker.CircuitBreaker`
  dataclass — three states (closed / open / half_open) + four
  transition methods (:meth:`record_failure`, :meth:`record_success`,
  :meth:`is_open`, :meth:`transition_to`) and the
  :meth:`cooldown_remaining_s` diagnostic.
* The :meth:`LCMEngine._get_or_create_circuit_breaker` shell helper
  that wires config-driven threshold/cooldown defaults for Epic 04
  callers.

The state machine matches the engine.ts shape (lines 1782, 1963-2016)
but the real auth-failure wiring lives in Epic 04 (depends on
``summarize.py`` + ``LcmProviderAuthError``). This issue ships
primitives only.

See:

* ``epics/02-engine-skeleton/02-09-circuit-breaker-scaffold.md`` —
  this issue's spec.
* ``docs/porting-guides/engine.md`` §"Circuit-breaker logic" — TS
  algorithm being ported.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import List

import pytest

from lossless_hermes.db.config import LcmConfig
from lossless_hermes.engine import LCMEngine
from lossless_hermes.engine.circuit_breaker import CircuitBreaker


# ---------------------------------------------------------------------------
# Construction defaults
# ---------------------------------------------------------------------------


def test_initial_state_is_closed() -> None:
    """A fresh breaker is closed, with zero failures and no open_since.

    AC: ``engine.get_circuit_breaker_state("k1")`` returns a fresh state.
    """
    breaker = CircuitBreaker()
    assert breaker.state == "closed"
    assert breaker.failures == 0
    assert breaker.open_since is None
    assert breaker.is_open() is False


def test_default_threshold_and_cooldown() -> None:
    """Default threshold/cooldown are sensible.

    Defaults are 5 / 60.0s — the shell helper overrides cooldown with
    ``config.circuit_breaker_cooldown_ms / 1000`` (default 1800s). The
    standalone defaults are smaller so direct callers / tests don't
    have to wait minutes.
    """
    breaker = CircuitBreaker()
    assert breaker.threshold == 5
    assert breaker.cooldown_s == 60.0


# ---------------------------------------------------------------------------
# Threshold transitions
# ---------------------------------------------------------------------------


def test_below_threshold_stays_closed() -> None:
    """N-1 failures keep the breaker closed."""
    breaker = CircuitBreaker(threshold=3)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == "closed"
    assert breaker.failures == 2
    assert breaker.is_open() is False


def test_opens_at_threshold() -> None:
    """N failures open the breaker."""
    breaker = CircuitBreaker(threshold=3)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == "open"
    assert breaker.failures == 3
    assert breaker.open_since is not None
    assert breaker.is_open() is True


def test_threshold_config_respected() -> None:
    """A higher threshold delays opening.

    Set ``threshold=5``; 4 failures don't open, 5th does.
    """
    breaker = CircuitBreaker(threshold=5)
    for _ in range(4):
        breaker.record_failure()
    assert breaker.is_open() is False
    breaker.record_failure()
    assert breaker.is_open() is True


# ---------------------------------------------------------------------------
# Success / reset transitions
# ---------------------------------------------------------------------------


def test_success_resets_partial_failures() -> None:
    """A success while below threshold clears the failure counter."""
    breaker = CircuitBreaker(threshold=3)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    assert breaker.failures == 0
    assert breaker.state == "closed"
    assert breaker.open_since is None


def test_success_closes_open_breaker() -> None:
    """A success while open closes the breaker (defensive — usually gated).

    The TS source's ``recordCompactionSuccess`` resets unconditionally;
    we match that shape.
    """
    breaker = CircuitBreaker(threshold=2)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == "open"
    breaker.record_success()
    assert breaker.state == "closed"
    assert breaker.failures == 0
    assert breaker.open_since is None


# ---------------------------------------------------------------------------
# Cooldown / half-open / probe semantics
# ---------------------------------------------------------------------------


def test_cooldown_auto_transitions_to_half_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Once cooldown elapses, ``is_open`` returns False and state is half_open.

    The TS source auto-resets to closed on cooldown elapse. We make
    the intermediate ``half_open`` state explicit so a probe-call can
    confirm the upstream is healthy before fully closing.
    """
    breaker = CircuitBreaker(threshold=1, cooldown_s=10.0)
    base = 1000.0
    monkeypatch.setattr(time, "monotonic", lambda: base)
    breaker.record_failure()
    assert breaker.is_open() is True

    # Advance time past cooldown.
    monkeypatch.setattr(time, "monotonic", lambda: base + 11.0)
    assert breaker.is_open() is False
    assert breaker.state == "half_open"


def test_half_open_success_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A success in half_open transitions to closed and clears state."""
    breaker = CircuitBreaker(threshold=1, cooldown_s=10.0)
    base = 1000.0
    monkeypatch.setattr(time, "monotonic", lambda: base)
    breaker.record_failure()
    monkeypatch.setattr(time, "monotonic", lambda: base + 11.0)
    breaker.is_open()  # transitions to half_open
    assert breaker.state == "half_open"

    breaker.record_success()
    assert breaker.state == "closed"
    assert breaker.failures == 0
    assert breaker.open_since is None


def test_half_open_failure_reopens(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure in half_open transitions back to open with fresh cooldown."""
    breaker = CircuitBreaker(threshold=1, cooldown_s=10.0)
    base = 1000.0
    monkeypatch.setattr(time, "monotonic", lambda: base)
    breaker.record_failure()
    first_open_since = breaker.open_since

    monkeypatch.setattr(time, "monotonic", lambda: base + 11.0)
    breaker.is_open()  # transitions to half_open
    assert breaker.state == "half_open"

    # Probe fails — re-open with fresh stamp.
    monkeypatch.setattr(time, "monotonic", lambda: base + 12.0)
    breaker.record_failure()
    assert breaker.state == "open"
    assert breaker.open_since is not None
    assert breaker.open_since != first_open_since
    assert breaker.is_open() is True


# ---------------------------------------------------------------------------
# cooldown_remaining_s diagnostic
# ---------------------------------------------------------------------------


def test_cooldown_remaining_s_when_closed() -> None:
    """Closed breaker reports 0 remaining."""
    breaker = CircuitBreaker()
    assert breaker.cooldown_remaining_s() == 0.0


def test_cooldown_remaining_s_counts_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """Open breaker reports the cooldown remaining, then 0 after elapse."""
    breaker = CircuitBreaker(threshold=1, cooldown_s=10.0)
    base = 1000.0
    monkeypatch.setattr(time, "monotonic", lambda: base)
    breaker.record_failure()
    assert breaker.cooldown_remaining_s() == pytest.approx(10.0, abs=0.01)

    monkeypatch.setattr(time, "monotonic", lambda: base + 4.0)
    assert breaker.cooldown_remaining_s() == pytest.approx(6.0, abs=0.01)

    monkeypatch.setattr(time, "monotonic", lambda: base + 11.0)
    # Past cooldown — clamped to 0.0 (not negative). Note: this read
    # does not auto-transition to half_open; only is_open() does.
    assert breaker.cooldown_remaining_s() == 0.0


# ---------------------------------------------------------------------------
# transition_to escape hatch
# ---------------------------------------------------------------------------


def test_transition_to_open_stamps_timestamp() -> None:
    """Forcing to ``open`` from closed stamps ``open_since``."""
    breaker = CircuitBreaker(threshold=10, cooldown_s=10.0)
    breaker.transition_to("open")
    assert breaker.state == "open"
    assert breaker.open_since is not None
    # is_open returns True because state == 'open' and within cooldown.
    assert breaker.is_open() is True


def test_transition_to_closed_clears_state() -> None:
    """Forcing to ``closed`` clears failures + open_since."""
    breaker = CircuitBreaker(threshold=2)
    breaker.record_failure()
    breaker.record_failure()  # opens
    breaker.transition_to("closed")
    assert breaker.state == "closed"
    assert breaker.failures == 0
    assert breaker.open_since is None


def test_transition_to_half_open_keeps_history() -> None:
    """Forcing to ``half_open`` keeps the failure counter for diagnostics."""
    breaker = CircuitBreaker(threshold=3)
    breaker.record_failure()
    breaker.record_failure()
    breaker.transition_to("half_open")
    assert breaker.state == "half_open"
    assert breaker.failures == 2  # preserved
    # is_open returns False (probe-allowed).
    assert breaker.is_open() is False


def test_transition_to_rejects_invalid_state() -> None:
    """Unknown state name raises ValueError."""
    breaker = CircuitBreaker()
    with pytest.raises(ValueError, match="unknown circuit breaker state"):
        breaker.transition_to("not-a-state")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Logging behavior
# ---------------------------------------------------------------------------


def test_logs_open_warning(caplog: pytest.LogCaptureFixture) -> None:
    """Opening emits a WARNING with key metrics."""
    breaker = CircuitBreaker(threshold=2, cooldown_s=10.0)
    with caplog.at_level(logging.WARNING, logger="lossless_hermes.engine.circuit_breaker"):
        breaker.record_failure()
        breaker.record_failure()
    assert any("OPENED" in rec.message for rec in caplog.records)


def test_logs_closed_info(caplog: pytest.LogCaptureFixture) -> None:
    """Closing-after-failure emits an INFO."""
    breaker = CircuitBreaker(threshold=2)
    breaker.record_failure()
    with caplog.at_level(logging.INFO, logger="lossless_hermes.engine.circuit_breaker"):
        breaker.record_success()
    assert any("CLOSED" in rec.message for rec in caplog.records)


def test_logs_cooled_down_info(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cooldown-elapse emits an INFO when is_open() transitions to half_open."""
    breaker = CircuitBreaker(threshold=1, cooldown_s=10.0)
    base = 1000.0
    monkeypatch.setattr(time, "monotonic", lambda: base)
    breaker.record_failure()
    monkeypatch.setattr(time, "monotonic", lambda: base + 11.0)
    with caplog.at_level(logging.INFO, logger="lossless_hermes.engine.circuit_breaker"):
        breaker.is_open()
    assert any("COOLED DOWN" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_concurrent_record_failure_is_threadsafe() -> None:
    """Concurrent ``record_failure`` calls produce a consistent count.

    Without the lock, the ``failures += 1`` increment would race; under
    contention CPython's GIL releases between bytecode ops so the
    read/write split is observably non-atomic without explicit locking.
    With the lock, N threads * M increments yields exactly N*M.
    """
    breaker = CircuitBreaker(threshold=10_000, cooldown_s=60.0)
    n_threads = 8
    per_thread = 1_000

    def worker() -> None:
        for _ in range(per_thread):
            breaker.record_failure()

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert breaker.failures == n_threads * per_thread


def test_concurrent_mixed_calls_keep_state_consistent() -> None:
    """Concurrent record_failure + record_success keep state coherent.

    Property: at the end of the run, ``failures`` and ``open_since``
    agree with ``state`` (closed → both clear; open → failures >=
    threshold and open_since set).
    """
    breaker = CircuitBreaker(threshold=5, cooldown_s=60.0)
    stop = threading.Event()

    def fail_worker() -> None:
        while not stop.is_set():
            breaker.record_failure()

    def succeed_worker() -> None:
        while not stop.is_set():
            breaker.record_success()

    threads = [
        threading.Thread(target=fail_worker),
        threading.Thread(target=fail_worker),
        threading.Thread(target=succeed_worker),
    ]
    for t in threads:
        t.start()
    time.sleep(0.05)
    stop.set()
    for t in threads:
        t.join()

    # Invariant check — state must agree with bookkeeping fields.
    # ``state == "closed"`` only guarantees "not open"; the breaker is
    # closed both at ``failures == 0`` (fresh / just-reset) and at
    # ``0 < failures < threshold`` (transient failures that have not
    # yet crossed the trip line). Only ``record_success`` zeros
    # ``failures``, so we cannot assert ``failures == 0`` here — a
    # ``record_failure`` may be the last interleaved op. The accurate
    # invariant is: while closed, the breaker has not yet tripped, so
    # ``failures < threshold`` and ``open_since`` is unset.
    if breaker.state == "closed":
        assert breaker.failures < breaker.threshold
        assert breaker.open_since is None
    elif breaker.state == "open":
        # Could have been opened then partially decremented by a success
        # (record_success resets to closed). So observing "open" here
        # means the last mutation was a record_failure crossing the
        # threshold.
        assert breaker.failures >= breaker.threshold
        assert breaker.open_since is not None
    # half_open is unreachable from these mutations (only is_open()
    # triggers it), so the test does not assert that branch.


# ---------------------------------------------------------------------------
# Shell integration: _get_or_create_circuit_breaker
# ---------------------------------------------------------------------------


def test_shell_circuit_breakers_typing_preserved() -> None:
    """02-01's invariant: ``_circuit_breakers`` is an empty dict on init.

    02-09 refines the value type to :class:`CircuitBreaker` but does
    not change the empty-on-init shape.
    """
    engine = LCMEngine()
    assert engine._circuit_breakers == {}
    assert isinstance(engine._circuit_breakers, dict)


def test_get_or_create_returns_circuit_breaker_instance() -> None:
    """The shell helper returns a CircuitBreaker, stored in _circuit_breakers."""
    engine = LCMEngine()
    breaker = engine._get_or_create_circuit_breaker("provider/model")
    assert isinstance(breaker, CircuitBreaker)
    assert engine._circuit_breakers["provider/model"] is breaker


def test_get_or_create_is_idempotent() -> None:
    """Same key returns the same instance across calls."""
    engine = LCMEngine()
    a = engine._get_or_create_circuit_breaker("k1")
    b = engine._get_or_create_circuit_breaker("k1")
    assert a is b


def test_get_or_create_applies_config_defaults() -> None:
    """Breaker inherits threshold + cooldown from LcmConfig.

    Default config has ``circuit_breaker_threshold=5`` and
    ``circuit_breaker_cooldown_ms=1_800_000``. The helper converts ms
    → seconds.
    """
    engine = LCMEngine()
    breaker = engine._get_or_create_circuit_breaker("k1")
    assert breaker.threshold == 5
    assert breaker.cooldown_s == 1800.0


def test_get_or_create_respects_custom_config() -> None:
    """Custom config values flow through to the breaker."""
    config = LcmConfig(circuit_breaker_threshold=3, circuit_breaker_cooldown_ms=10_000)
    engine = LCMEngine(config=config)
    breaker = engine._get_or_create_circuit_breaker("k1")
    assert breaker.threshold == 3
    assert breaker.cooldown_s == 10.0


def test_multiple_keys_independent() -> None:
    """Opening one key does not affect another.

    AC from spec: "Multiple keys are independent: opening k1 does not
    open k2."
    """
    engine = LCMEngine(config=LcmConfig(circuit_breaker_threshold=2))
    b1 = engine._get_or_create_circuit_breaker("k1")
    b2 = engine._get_or_create_circuit_breaker("k2")
    b1.record_failure()
    b1.record_failure()
    assert b1.is_open() is True
    assert b2.is_open() is False
    assert b2.failures == 0


# ---------------------------------------------------------------------------
# Acceptance criteria sweep — matches the spec line-by-line
# ---------------------------------------------------------------------------


def test_ac_fresh_state_is_closed_with_failures_zero() -> None:
    """AC: ``engine.get_circuit_breaker_state("k1")`` returns fresh state."""
    engine = LCMEngine()
    breaker = engine._get_or_create_circuit_breaker("k1")
    assert breaker.failures == 0
    assert breaker.open_since is None
    assert breaker.state == "closed"


def test_ac_threshold_calls_open_breaker() -> None:
    """AC: After threshold calls, ``is_open`` returns True."""
    engine = LCMEngine(config=LcmConfig(circuit_breaker_threshold=3))
    breaker = engine._get_or_create_circuit_breaker("k1")
    for _ in range(3):
        breaker.record_failure()
    assert breaker.is_open() is True


def test_ac_success_resets_state() -> None:
    """AC: record_success resets state to closed."""
    engine = LCMEngine(config=LcmConfig(circuit_breaker_threshold=2))
    breaker = engine._get_or_create_circuit_breaker("k1")
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.is_open() is True
    breaker.record_success()
    assert breaker.is_open() is False
    assert breaker.failures == 0


def test_ac_cooldown_auto_close(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC: After cooldown, ``is_open`` returns False (transitions to half_open).

    The TS source resets to closed; our half_open intermediate still
    returns False from ``is_open`` — the AC is "rejects no more
    calls", which matches.
    """
    engine = LCMEngine(config=LcmConfig(circuit_breaker_cooldown_ms=10_000))
    breaker = engine._get_or_create_circuit_breaker("k1")
    base = 1000.0
    monkeypatch.setattr(time, "monotonic", lambda: base)
    # Default threshold is 5
    for _ in range(5):
        breaker.record_failure()
    assert breaker.is_open() is True

    monkeypatch.setattr(time, "monotonic", lambda: base + 11.0)
    assert breaker.is_open() is False


def test_ac_no_caller_invokes_in_epic_02() -> None:
    """AC: Epic 02 does not invoke the breaker — Epic 04 is the consumer.

    Verified by grepping src/ for callers of ``record_failure`` /
    ``record_success`` / ``is_open`` / ``_get_or_create_circuit_breaker``.
    The only call sites should be tests and the breaker module itself.

    We also explicitly check that the no-op ``compress`` method does
    not touch the breaker by calling it and asserting the breaker dict
    stays empty.
    """
    engine = LCMEngine()
    # Trigger compress on an empty conversation — should be no-op.
    result = engine.compress(messages=[])
    # ``compress`` returns the messages unchanged at 02-01.
    assert result == []
    # No breaker was created.
    assert engine._circuit_breakers == {}


# ---------------------------------------------------------------------------
# Telemetry hook (failure counter exposed for logging)
# ---------------------------------------------------------------------------


def test_failure_counter_visible_after_each_call() -> None:
    """``failures`` is read-after-write visible (useful for logs).

    Epic 04 will surface this in compaction telemetry — verify the
    field is observable on a single thread (the threadsafe test
    confirms the multi-thread case).
    """
    breaker = CircuitBreaker(threshold=10)
    observations: List[int] = []
    for i in range(5):
        breaker.record_failure()
        observations.append(breaker.failures)
    assert observations == [1, 2, 3, 4, 5]
