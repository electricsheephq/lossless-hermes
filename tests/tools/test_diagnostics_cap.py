"""Tests for the ADR-035 diagnostic output-cap helper.

Exercises :mod:`lossless_hermes.tools._diagnostics` —
:func:`cap_diagnostic_text` and the cap constants. ADR-035 §Consequences
makes the tool-variant output cap a **mandatory** caveat; this module
pins that the cap actually bounds the output and that the bypass /
truncation behaviour is correct.

Test inventory:

* Short text (within budget) passes through unchanged.
* Over-budget text is truncated so the result is ``<= char_cap``.
* The truncated result carries the cap-tail naming the ``/lcm`` slash
  command.
* Truncation prefers a clean line boundary when one exists.
* A single very long line (no newline) hard-cuts at the char boundary.
* The boundary case (length exactly at the cap) does not truncate.

See:

* ``docs/adr/035-lcm-status-doctor-model-tools.md`` §Consequences — the
  mandatory output-cap caveat.
"""

from __future__ import annotations

from lossless_hermes.tools._diagnostics import (
    DIAGNOSTIC_DOCTOR_FINDING_CAP,
    DIAGNOSTIC_TOOL_OUTPUT_CHAR_CAP,
    cap_diagnostic_text,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_cap_constants_are_positive_ints() -> None:
    """The cap constants are positive ints — a non-positive cap would
    make every tool result empty."""
    assert isinstance(DIAGNOSTIC_TOOL_OUTPUT_CHAR_CAP, int)
    assert DIAGNOSTIC_TOOL_OUTPUT_CHAR_CAP > 0
    assert isinstance(DIAGNOSTIC_DOCTOR_FINDING_CAP, int)
    assert DIAGNOSTIC_DOCTOR_FINDING_CAP > 0


def test_char_cap_is_about_1_5k_tokens() -> None:
    """The char cap is ~1.5K tokens at the project's 4-chars/token rate.

    ADR-035 §"Open questions" row 1 names ~1.5K tokens as the proposed
    starting point for ``lcm_status``. Pin that the constant matches —
    if someone retunes it, this test forces a deliberate update.
    """
    approx_tokens = DIAGNOSTIC_TOOL_OUTPUT_CHAR_CAP / 4
    assert 1_000 <= approx_tokens <= 2_000


# ---------------------------------------------------------------------------
# cap_diagnostic_text — pass-through
# ---------------------------------------------------------------------------


def test_short_text_passes_through_unchanged() -> None:
    """Text within budget is returned byte-identical."""
    text = "verdict: healthy\nall good"
    assert cap_diagnostic_text(text, slash_command="status") == text


def test_text_exactly_at_cap_is_not_truncated() -> None:
    """A string whose length equals the cap is within budget (``<=``)."""
    text = "x" * DIAGNOSTIC_TOOL_OUTPUT_CHAR_CAP
    assert cap_diagnostic_text(text, slash_command="doctor") == text


# ---------------------------------------------------------------------------
# cap_diagnostic_text — truncation
# ---------------------------------------------------------------------------


def test_over_budget_text_is_capped_to_char_cap() -> None:
    """An over-budget report is truncated so the result is ``<= char_cap``."""
    text = "y" * (DIAGNOSTIC_TOOL_OUTPUT_CHAR_CAP * 3)
    capped = cap_diagnostic_text(text, slash_command="status")
    assert len(capped) <= DIAGNOSTIC_TOOL_OUTPUT_CHAR_CAP
    assert len(capped) < len(text)


def test_capped_text_carries_the_slash_command_tail() -> None:
    """The cap-tail names the ``/lcm <slash_command>`` for the full report."""
    text = "z" * (DIAGNOSTIC_TOOL_OUTPUT_CHAR_CAP * 2)
    capped_status = cap_diagnostic_text(text, slash_command="status")
    capped_doctor = cap_diagnostic_text(text, slash_command="doctor")
    assert "/lcm status" in capped_status
    assert "/lcm doctor" in capped_doctor
    assert "output capped" in capped_status


def test_truncation_prefers_a_line_boundary() -> None:
    """When a newline exists in range, truncation trims back to it.

    The body of the capped output should not end mid-line — it ends at
    the last newline before the budget.
    """
    # Many short lines — well over the cap in total.
    line = "a line of diagnostic output here\n"
    text = line * (DIAGNOSTIC_TOOL_OUTPUT_CHAR_CAP // len(line) + 50)
    capped = cap_diagnostic_text(text, slash_command="doctor")
    # The portion before the cap-tail ends at a line boundary: split off
    # the tail (begins with "\n... [output capped") and check the body.
    body = capped.split("\n... [output capped", 1)[0]
    assert body.endswith("diagnostic output here")


def test_single_long_line_hard_cuts_at_char_boundary() -> None:
    """A single newline-free line still respects the char cap.

    There's no line boundary to trim to, so the helper hard-cuts — the
    result must still be ``<= char_cap`` (the contract holds even for
    pathological input).
    """
    text = "q" * (DIAGNOSTIC_TOOL_OUTPUT_CHAR_CAP * 4)  # no newlines at all
    capped = cap_diagnostic_text(text, slash_command="status")
    assert len(capped) <= DIAGNOSTIC_TOOL_OUTPUT_CHAR_CAP
    assert capped.endswith("the full uncapped report]")


def test_custom_char_cap_is_respected() -> None:
    """An explicit ``char_cap`` overrides the default and is honoured."""
    text = "w" * 5_000
    capped = cap_diagnostic_text(text, slash_command="doctor", char_cap=500)
    assert len(capped) <= 500
