"""Tests for :meth:`LCMEngine.should_compress` (issue 02-05).

Covers the conventional threshold gate + anti-thrashing back-off body
that 02-05 layers onto the 02-01 skeleton. Specifically:

* **Threshold gate.** ``should_compress`` returns ``False`` when
  ``self.threshold_tokens == 0`` (never set — :meth:`update_model`
  hasn't fired) regardless of how high ``prompt_tokens`` is. Otherwise
  it returns ``True`` when ``observed >= threshold_tokens`` and
  ``False`` when below.
* **Default token source.** When ``prompt_tokens`` is ``None``,
  ``should_compress`` reads ``self.last_prompt_tokens``.
* **Anti-thrashing back-off.** Maintains a
  ``_compression_history: deque[tuple[int, int]]`` of
  ``(before, after)`` token counts. When the most recent N entries are
  all "ineffective" (saved <10% of pre-compression tokens),
  ``should_compress`` returns ``False`` even at over-threshold prompts.
  At 02-05 ``compress`` is still a passthrough, so every recorded entry
  is by definition ineffective; the back-off thus trips after two
  consecutive ``compress`` calls and clears once an effective entry
  appears.
* **History tracking.** :meth:`compress` appends one
  ``(before, after)`` tuple per call. ``before`` is the explicit
  ``current_tokens`` argument when provided, else
  ``self.last_prompt_tokens``. ``after`` equals ``before`` at 02-05
  (passthrough; Epic 04 overwrites the body with the real algorithm).

The 00-06 + 02-01 regression tests (``test_engine_noop.py``,
``test_engine_skeleton.py``) still hold — they exercise the
default-state behavior (``threshold_tokens=0``) which the threshold
gate guards correctly.

See:

* ``epics/02-engine-skeleton/02-05-should-compress.md`` — this issue's AC.
* ``docs/adr/010-pre-assembly-vs-compress-path.md`` — once preassemble
  lands, ``should_compress`` flips to always-False (compaction runs
  via the always-on assembly hook + deferred debt queue). 02-05 ships
  the conventional path; 03/04 revisit when spike 002 results land.
* ``agent/context_compressor.py:493-513`` — Hermes's own
  ``should_compress``, the closer kin to this body.
"""

from __future__ import annotations

from collections import deque

import pytest

from lossless_hermes.engine import LCMEngine
from lossless_hermes.engine.compact import (
    INEFFECTIVE_RUN_LENGTH,
    INEFFECTIVE_SAVINGS_THRESHOLD,
    _is_ineffective,
)


# ---------------------------------------------------------------------------
# Threshold gate — basic above / below / boundary
# ---------------------------------------------------------------------------


def test_returns_true_when_over_threshold() -> None:
    """AC: ``should_compress(prompt_tokens=100k)`` returns ``True`` when
    ``threshold_tokens=80k``."""
    engine = LCMEngine()
    engine.threshold_tokens = 80_000
    assert engine.should_compress(prompt_tokens=100_000) is True


def test_returns_false_when_under_threshold() -> None:
    """AC: ``should_compress(prompt_tokens=50k)`` returns ``False`` when
    ``threshold_tokens=80k``."""
    engine = LCMEngine()
    engine.threshold_tokens = 80_000
    assert engine.should_compress(prompt_tokens=50_000) is False


def test_returns_true_at_exact_threshold() -> None:
    """Boundary: ``observed == threshold`` engages compaction.

    Hermes ``context_compressor.py`` uses ``tokens < threshold`` (not
    ``<=``), so equal-to-threshold returns True. We match the parity.
    """
    engine = LCMEngine()
    engine.threshold_tokens = 80_000
    assert engine.should_compress(prompt_tokens=80_000) is True


def test_returns_false_just_below_threshold() -> None:
    """Boundary: one token below threshold returns False."""
    engine = LCMEngine()
    engine.threshold_tokens = 80_000
    assert engine.should_compress(prompt_tokens=79_999) is False


# ---------------------------------------------------------------------------
# Threshold gate — never-set protection (the 00-06 regression invariant)
# ---------------------------------------------------------------------------


def test_returns_false_when_threshold_tokens_unset() -> None:
    """AC: ``threshold_tokens == 0`` (never set — :meth:`update_model`
    hasn't fired) returns ``False`` even at very high prompt_tokens.

    This is the critical guard against compaction firing on a freshly-
    constructed engine before the host has wired the model context
    length. Mirrors the 00-06 regression test
    ``test_should_compress_returns_false_for_huge_token_count``.
    """
    engine = LCMEngine()
    assert engine.threshold_tokens == 0
    assert engine.should_compress(prompt_tokens=999_999_999) is False


def test_returns_false_at_threshold_zero_and_prompt_zero() -> None:
    """Defensive: both threshold and prompt_tokens zero returns False."""
    engine = LCMEngine()
    assert engine.should_compress(prompt_tokens=0) is False


def test_returns_false_at_negative_threshold() -> None:
    """Defensive: a negative threshold (shouldn't happen but...) returns False.

    Guards against ``int()`` underflow in a future ``update_model``
    implementation. The ``threshold_tokens <= 0`` guard catches all
    non-positive values uniformly.
    """
    engine = LCMEngine()
    engine.threshold_tokens = -1
    assert engine.should_compress(prompt_tokens=999_999) is False


# ---------------------------------------------------------------------------
# Default token source — falls back to last_prompt_tokens
# ---------------------------------------------------------------------------


def test_no_arg_uses_last_prompt_tokens_when_over_threshold() -> None:
    """AC: ``should_compress()`` with no arg falls back to
    ``self.last_prompt_tokens``."""
    engine = LCMEngine()
    engine.threshold_tokens = 80_000
    engine.last_prompt_tokens = 100_000
    assert engine.should_compress() is True


def test_no_arg_uses_last_prompt_tokens_when_under_threshold() -> None:
    """``should_compress()`` reads ``last_prompt_tokens`` even when
    that value is below threshold."""
    engine = LCMEngine()
    engine.threshold_tokens = 80_000
    engine.last_prompt_tokens = 50_000
    assert engine.should_compress() is False


def test_explicit_arg_overrides_last_prompt_tokens() -> None:
    """When ``prompt_tokens`` is passed explicitly it wins over the
    instance attribute — the host can override on a per-call basis."""
    engine = LCMEngine()
    engine.threshold_tokens = 80_000
    engine.last_prompt_tokens = 999_999  # would trigger
    # Explicit below-threshold arg → False
    assert engine.should_compress(prompt_tokens=10_000) is False


def test_zero_prompt_tokens_is_explicit_not_fallback() -> None:
    """``prompt_tokens=0`` is a valid explicit value (below threshold) —
    must NOT silently fall back to ``last_prompt_tokens``.

    The ABC signature uses ``prompt_tokens=None`` as the sentinel, so
    ``0`` is a real value. This test guards against a buggy
    ``if not prompt_tokens:`` fallback.
    """
    engine = LCMEngine()
    engine.threshold_tokens = 80_000
    engine.last_prompt_tokens = 999_999  # would trigger
    assert engine.should_compress(prompt_tokens=0) is False


# ---------------------------------------------------------------------------
# History tracking — compress() appends (before, after) tuples
# ---------------------------------------------------------------------------


def test_compress_appends_history_with_current_tokens() -> None:
    """``compress(messages, current_tokens=N)`` records ``(N, N)`` at 02-05
    (passthrough — ``after`` equals ``before``)."""
    engine = LCMEngine()
    assert len(engine._compression_history) == 0
    engine.compress([{"role": "user", "content": "hi"}], current_tokens=90_000)
    assert len(engine._compression_history) == 1
    assert engine._compression_history[-1] == (90_000, 90_000)


def test_compress_falls_back_to_last_prompt_tokens() -> None:
    """When ``current_tokens`` is ``None``, ``compress`` reads
    ``last_prompt_tokens`` for the ``before`` value."""
    engine = LCMEngine()
    engine.last_prompt_tokens = 75_000
    engine.compress([{"role": "user", "content": "hi"}])
    assert engine._compression_history[-1] == (75_000, 75_000)


def test_compress_history_is_deque_with_maxlen() -> None:
    """``_compression_history`` is a bounded deque (memory invariant)."""
    engine = LCMEngine()
    assert isinstance(engine._compression_history, deque)
    assert engine._compression_history.maxlen is not None
    assert engine._compression_history.maxlen >= INEFFECTIVE_RUN_LENGTH


def test_compress_history_preserves_passthrough() -> None:
    """Sanity: history tracking does not perturb the passthrough contract
    — :meth:`compress` still returns ``messages`` unchanged at 02-05."""
    engine = LCMEngine()
    msgs = [{"role": "user", "content": "hi"}]
    result = engine.compress(msgs, current_tokens=10_000)
    assert result is msgs


# ---------------------------------------------------------------------------
# Anti-thrashing back-off — engages after N ineffective compressions
# ---------------------------------------------------------------------------


def test_first_compression_returns_true_with_empty_history() -> None:
    """With no prior compressions, an over-threshold prompt triggers."""
    engine = LCMEngine()
    engine.threshold_tokens = 80_000
    assert len(engine._compression_history) == 0
    assert engine.should_compress(prompt_tokens=100_000) is True


def test_second_compression_returns_true_with_one_ineffective() -> None:
    """A single ineffective entry does NOT trigger the back-off — the gate
    requires ``INEFFECTIVE_RUN_LENGTH`` consecutive ineffective entries."""
    assert INEFFECTIVE_RUN_LENGTH >= 2, "test assumes >= 2"
    engine = LCMEngine()
    engine.threshold_tokens = 80_000
    # Simulate one ineffective compress() call (passthrough).
    engine.compress([], current_tokens=100_000)
    assert engine.should_compress(prompt_tokens=100_000) is True


def test_backoff_kicks_in_after_two_ineffective_compressions() -> None:
    """AC: After two consecutive ineffective compressions, the next
    ``should_compress`` returns ``False`` even at over-threshold prompts.

    At 02-05 every ``compress`` call is ineffective (passthrough); two
    calls is exactly the trip count for the default
    ``INEFFECTIVE_RUN_LENGTH=2``. Epic 04's real algorithm will produce
    mostly-effective entries and only trip back-off when the algorithm
    legitimately can't compact further.
    """
    engine = LCMEngine()
    engine.threshold_tokens = 80_000
    for _ in range(INEFFECTIVE_RUN_LENGTH):
        engine.compress([], current_tokens=100_000)
    # Back-off engaged — over-threshold prompt no longer triggers.
    assert engine.should_compress(prompt_tokens=100_000) is False


def test_backoff_clears_after_effective_compression() -> None:
    """When the run of ineffective compressions is broken by an effective
    one (savings >= ``INEFFECTIVE_SAVINGS_THRESHOLD``), the back-off lifts
    and the next over-threshold prompt triggers again.

    Manually inject an "effective" entry into the history because at
    02-05 ``compress`` only produces passthrough entries. Epic 04's real
    algorithm will produce these naturally.
    """
    engine = LCMEngine()
    engine.threshold_tokens = 80_000
    # Two ineffective entries → back-off should engage.
    for _ in range(INEFFECTIVE_RUN_LENGTH):
        engine.compress([], current_tokens=100_000)
    assert engine.should_compress(prompt_tokens=100_000) is False
    # Inject one effective entry — savings = 50%.
    engine._compression_history.append((100_000, 50_000))
    # The last ``INEFFECTIVE_RUN_LENGTH`` entries no longer all
    # ineffective; back-off lifts.
    assert engine.should_compress(prompt_tokens=100_000) is True


def test_backoff_clears_under_threshold_still_returns_false() -> None:
    """When the prompt is also under-threshold, the threshold gate fires
    first — the back-off check is moot but the result is consistent
    (False)."""
    engine = LCMEngine()
    engine.threshold_tokens = 80_000
    for _ in range(INEFFECTIVE_RUN_LENGTH):
        engine.compress([], current_tokens=100_000)
    assert engine.should_compress(prompt_tokens=50_000) is False


# ---------------------------------------------------------------------------
# _is_ineffective helper — savings-ratio classification
# ---------------------------------------------------------------------------


def test_is_ineffective_classifies_zero_savings_as_ineffective() -> None:
    """A passthrough (``after == before``) is ineffective."""
    assert _is_ineffective(100_000, 100_000) is True


def test_is_ineffective_classifies_50pct_savings_as_effective() -> None:
    """A 50% reduction is well over the 10% threshold — effective."""
    assert _is_ineffective(100_000, 50_000) is False


def test_is_ineffective_at_threshold_boundary() -> None:
    """Saving exactly ``INEFFECTIVE_SAVINGS_THRESHOLD`` rounds up: the
    helper uses ``<`` (strict), so exactly-10% savings is effective."""
    assert INEFFECTIVE_SAVINGS_THRESHOLD == 0.10
    # 100_000 -> 90_000 = exactly 10% savings = effective (not <10%).
    assert _is_ineffective(100_000, 90_000) is False


def test_is_ineffective_just_below_threshold_is_ineffective() -> None:
    """A 5% reduction is below the 10% threshold — ineffective."""
    # 100_000 -> 95_000 = 5% savings = ineffective.
    assert _is_ineffective(100_000, 95_000) is True


def test_is_ineffective_classifies_growth_as_ineffective() -> None:
    """A compression that *grew* the token count is trivially ineffective
    (defensive — should not happen in practice but guards against an
    upstream bug)."""
    assert _is_ineffective(100_000, 110_000) is True


def test_is_ineffective_with_zero_before_is_ineffective() -> None:
    """``before <= 0`` (no pre-compression count provided) is treated as
    ineffective so back-off doesn't enter an infinite-trigger loop on
    zero-info entries."""
    assert _is_ineffective(0, 0) is True
    assert _is_ineffective(0, 50_000) is True
    assert _is_ineffective(-1, 50_000) is True


# ---------------------------------------------------------------------------
# ADR-010 future-path placeholder
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason=(
        "ADR-010 always-on assembly — once the upstream preassemble patch "
        "lands (Hermes PR #24949), should_compress flips to always-False "
        "and compaction runs via the always-on assembly hook + deferred "
        "debt queue. Epic 03/04 revisit this contract once spike 002 "
        "results land."
    )
)
def test_lcm_always_on_future_path() -> None:
    """Placeholder for the ADR-010 contract flip.

    Reserved name so the future commit that lands the preassemble path
    has a labeled slot to flip behavior in. Skip-marked so CI does not
    fail; un-skip in the same commit that lands the new body.
    """
    engine = LCMEngine()
    # Future: even at over-threshold tokens with empty history, returns
    # False because compaction runs via assembly, not the threshold gate.
    engine.threshold_tokens = 80_000
    assert engine.should_compress(prompt_tokens=100_000) is False


# ---------------------------------------------------------------------------
# Integration: threshold + history together (the full state machine sketch)
# ---------------------------------------------------------------------------


def test_full_cycle_threshold_then_thrash_then_recover() -> None:
    """End-to-end: threshold trip, run-up to back-off, recover with an
    effective compression.

    Sequencing:

    1. Empty history + over-threshold prompt → True (first trigger).
    2. After 1 ineffective compress → True (run not long enough).
    3. After 2 ineffective compresses → False (back-off engaged).
    4. After an effective entry appears in history → True again.
    """
    engine = LCMEngine()
    engine.threshold_tokens = 80_000

    # Step 1: empty history.
    assert engine.should_compress(prompt_tokens=100_000) is True

    # Step 2: one passthrough call.
    engine.compress([], current_tokens=100_000)
    assert engine.should_compress(prompt_tokens=100_000) is True

    # Step 3: second passthrough call → back-off engages.
    engine.compress([], current_tokens=100_000)
    assert engine.should_compress(prompt_tokens=100_000) is False

    # Step 4: simulate an effective compression entering history.
    engine._compression_history.append((100_000, 30_000))  # 70% savings
    assert engine.should_compress(prompt_tokens=100_000) is True
