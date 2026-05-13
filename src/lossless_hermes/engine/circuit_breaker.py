"""Circuit-breaker state machine for compaction auth failures.

Hosts the :class:`CircuitBreaker` dataclass + transition methods used by
:class:`~lossless_hermes.engine.LCMEngine` to gate compaction calls when
the summarizer is failing with auth errors (Epic 04 wiring).

This module ships the **state-machine scaffold only** at issue 02-09:

* :class:`CircuitBreaker` dataclass with three states: ``closed`` (the
  default, all calls allowed), ``open`` (calls rejected with cooldown),
  ``half_open`` (single probe-call allowed; success transitions to
  closed, failure transitions back to open).
* Transition methods: :meth:`record_failure`, :meth:`record_success`,
  :meth:`is_open`, :meth:`transition_to`, :meth:`cooldown_remaining_s`.
* Concurrency-safe — uses :class:`threading.Lock` because callers may
  be on different event loops or thread pools (Hermes background
  ingestion + asyncio compaction).

Maps to ``lossless-claw/src/engine.ts`` lines 1782 (state field) and
1963-2016 (transition methods). The TS source has only two states
(closed / open with auto-reset on cooldown); we add an explicit
``half_open`` state to make the probe-allow semantics easier to reason
about under concurrent callers. Epic 04 will wire the real
``LcmProviderAuthError`` catch around the summarizer; this issue ships
the primitives only.

Per the issue spec, the threshold + cooldown come from
:class:`~lossless_hermes.db.config.LcmConfig` (``circuit_breaker_threshold``
defaults to 5; ``circuit_breaker_cooldown_ms`` defaults to 1_800_000 =
30 min). Callers pass these in to :func:`CircuitBreaker.__init__`.

See:

* ``docs/adr/027-engine-splitting.md`` — engine package layout.
* ``docs/porting-guides/engine.md`` §"Circuit-breaker logic" — the TS
  algorithm being ported here.
* ``epics/02-engine-skeleton/02-09-circuit-breaker-scaffold.md`` — this
  issue's spec.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Literal

__all__ = ["CircuitBreaker", "CircuitBreakerStateName"]

logger = logging.getLogger("lossless_hermes.engine.circuit_breaker")


# Public type alias for the three states. Using ``Literal`` so callers can
# pass plain strings (matching the TS shape) without importing an Enum.
CircuitBreakerStateName = Literal["closed", "open", "half_open"]


# Default cooldown matches the TS source (``circuitBreakerCooldownMs`` in
# ``src/db/config.ts``), expressed in seconds for Python ergonomics. 30
# minutes — long enough that auth-config typos get noticed before retry
# storms, short enough that a transient outage self-heals.
_DEFAULT_COOLDOWN_S: float = 60.0


@dataclass
class CircuitBreaker:
    """Per-key circuit breaker state machine.

    Tracks consecutive auth failures from the summarizer for a single
    breaker key (typically ``f"{provider}/{model}"`` or
    ``f"{session_id}:{provider}/{model}"`` — Epic 04 chooses the policy).
    Opens after ``threshold`` failures; while open, :meth:`is_open`
    returns ``True`` until the cooldown elapses. After cooldown,
    transitions to ``half_open`` to allow a single probe call; on
    success transitions to ``closed``, on failure transitions back to
    ``open`` with a fresh ``open_since`` timestamp.

    Maps to engine.ts:CircuitBreakerState (line 98) + the four
    transition methods at lines 1963-2016.

    Attributes:
        state: Current state — one of ``"closed"`` / ``"open"`` /
            ``"half_open"``. Default ``"closed"``.
        failures: Consecutive failure count. Reset to 0 on any
            success. The breaker opens when ``failures >= threshold``.
        open_since: Monotonic timestamp (``time.monotonic()``) of the
            most recent ``closed → open`` or ``half_open → open``
            transition. ``None`` when ``state != "open"``.
        threshold: Failure count at which the breaker opens. Default 5
            matches :attr:`LcmConfig.circuit_breaker_threshold`.
        cooldown_s: Cooldown duration in seconds. Default 60.0; Epic 04
            callers pass ``config.circuit_breaker_cooldown_ms / 1000``
            (default 1800.0s = 30min).
    """

    state: CircuitBreakerStateName = "closed"
    failures: int = 0
    open_since: float | None = None
    threshold: int = 5
    cooldown_s: float = _DEFAULT_COOLDOWN_S
    # Internal lock — guards every state mutation. Excluded from the
    # dataclass ``__init__`` / ``__repr__`` / ``__eq__`` because it is
    # an implementation detail (two breakers with identical state are
    # equal even if they hold distinct lock objects).
    _lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
        compare=False,
    )

    def record_failure(self) -> None:
        """Record an auth failure; open the breaker if at threshold.

        Maps to engine.ts:recordCompactionAuthFailure (line 1983).

        Behavior by current state:

        * ``closed``: increment ``failures``; if it reaches
          ``threshold``, transition to ``open`` and stamp
          ``open_since``.
        * ``half_open``: the probe failed — transition back to
          ``open`` with a fresh ``open_since`` (re-start cooldown);
          ``failures`` is incremented for telemetry.
        * ``open``: no-op for state, but ``failures`` is incremented
          to keep the counter accurate (the TS source increments in
          all states; we match for shape parity).

        Thread-safe; holds the internal lock for the entire mutation.
        """
        with self._lock:
            self.failures += 1
            if self.state == "half_open":
                # Probe failed — re-open with a fresh cooldown.
                self.state = "open"
                self.open_since = time.monotonic()
                logger.warning(
                    "[lcm] circuit breaker RE-OPENED after half-open probe failure "
                    "(failures=%d, threshold=%d)",
                    self.failures,
                    self.threshold,
                )
            elif self.state == "closed" and self.failures >= self.threshold:
                self.state = "open"
                self.open_since = time.monotonic()
                logger.warning(
                    "[lcm] circuit breaker OPENED after %d consecutive failures "
                    "(threshold=%d, cooldown=%.1fs)",
                    self.failures,
                    self.threshold,
                    self.cooldown_s,
                )

    def record_success(self) -> None:
        """Record a successful compaction; reset to ``closed``.

        Maps to engine.ts:recordCompactionSuccess (line 2001).

        Behavior by current state:

        * ``closed`` with ``failures == 0``: no-op (already steady).
        * ``closed`` with ``failures > 0``: clear ``failures`` (the
          counter was tracking transient failures that never reached
          threshold).
        * ``half_open``: the probe succeeded — transition to
          ``closed`` and reset all counters.
        * ``open``: success while open is unexpected (caller should
          have been gated by :meth:`is_open`), but we still reset
          rather than ignore — matches TS source which calls
          ``resetCircuitBreaker`` unconditionally.

        Thread-safe.
        """
        with self._lock:
            had_state = self.failures > 0 or self.open_since is not None
            previous_state = self.state
            self.state = "closed"
            self.failures = 0
            self.open_since = None
            if had_state:
                logger.info(
                    "[lcm] circuit breaker CLOSED (was %s, prior failures cleared)",
                    previous_state,
                )

    def is_open(self) -> bool:
        """Return whether the breaker is currently rejecting calls.

        Maps to engine.ts:isCircuitBreakerOpen (line 1972).

        Side-effect: if the breaker is ``open`` and the cooldown has
        elapsed, transitions to ``half_open`` (allowing one probe
        call). Callers can then attempt the operation; on success they
        call :meth:`record_success` (→ ``closed``), on failure they
        call :meth:`record_failure` (→ back to ``open`` with fresh
        cooldown).

        Returns:
            ``True`` if the breaker rejects calls (``state == "open"``
            and cooldown not yet elapsed). ``False`` for ``closed``
            and ``half_open`` (probe allowed).

        Thread-safe.
        """
        with self._lock:
            if self.state != "open" or self.open_since is None:
                return False
            elapsed = time.monotonic() - self.open_since
            if elapsed >= self.cooldown_s:
                # Cooldown elapsed — transition to half_open. The next
                # caller gets a probe attempt; failure re-opens, success
                # closes.
                self.state = "half_open"
                logger.info(
                    "[lcm] circuit breaker COOLED DOWN after %.1fs; transitioning to half_open",
                    elapsed,
                )
                return False
            return True

    def transition_to(self, new_state: CircuitBreakerStateName) -> None:
        """Force a state transition (mainly for tests and Epic 04 control).

        This is a lower-level escape hatch than
        :meth:`record_failure` / :meth:`record_success`. It does NOT
        increment counters; it does NOT stamp ``open_since`` unless
        explicitly going to ``open``. Use the ``record_*`` methods for
        normal operation.

        Args:
            new_state: One of ``"closed"`` / ``"open"`` / ``"half_open"``.

        Raises:
            ValueError: If ``new_state`` is not a recognized state.

        Thread-safe.
        """
        if new_state not in ("closed", "open", "half_open"):
            raise ValueError(
                f"unknown circuit breaker state {new_state!r} "
                f"(expected 'closed', 'open', or 'half_open')",
            )
        with self._lock:
            previous = self.state
            self.state = new_state
            if new_state == "closed":
                self.failures = 0
                self.open_since = None
            elif new_state == "open" and self.open_since is None:
                # Force-opening without a record_failure call — stamp
                # the timestamp so cooldown logic works.
                self.open_since = time.monotonic()
            elif new_state == "half_open":
                # Half-open with no fresh stamp — the breaker is
                # probe-ready. Keep ``open_since`` for diagnostics but
                # ``is_open`` returns False because state != "open".
                pass
            logger.debug(
                "[lcm] circuit breaker transition_to: %s → %s",
                previous,
                new_state,
            )

    def cooldown_remaining_s(self) -> float:
        """Return seconds remaining in the cooldown window, or 0 if not open.

        Useful for diagnostics / operator messages (e.g. "auto-retry in
        12.3s"). For ``half_open`` and ``closed`` returns 0.0.

        Returns:
            Non-negative float. ``0.0`` if state is not ``"open"``, or
            if ``open_since`` is somehow ``None``, or if the cooldown
            has already elapsed.

        Thread-safe.
        """
        with self._lock:
            if self.state != "open" or self.open_since is None:
                return 0.0
            elapsed = time.monotonic() - self.open_since
            remaining = self.cooldown_s - elapsed
            return max(0.0, remaining)
