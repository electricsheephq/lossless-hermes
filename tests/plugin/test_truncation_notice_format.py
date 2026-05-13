"""Tests pinning the truncation-notice regex per Wave-12 N3 retro.

The truncation-notice prose is part of the **agent-facing contract**.
Two regex pins must stay in lockstep:

1. The runtime prose emitted by :func:`result_budget.truncation_notice`.
2. The agent's documented expectation (regex
   ``r"truncated at ~\\d+ tokens to protect agent context"``) — embedded
   in tool description strings so agents can regex-match "did this
   tool truncate?".

The TS source carries this requirement in
``test/v41-tool-budget-guardrail.test.ts`` and
``test/v41-adversarial-output-bounds.test.ts``. The Python port
mirrors the pin here so cosmetic edits to the format string fail loudly.

References:

* ADR-029 §"Known Wave-N fixes" Wave-12 N3 row.
* :mod:`lossless_hermes.plugin.result_budget` — implementation.
* Issue spec: ``epics/06-tools/06-03-runwithtokengate-middleware.md``.
"""

from __future__ import annotations

import re

import pytest

from lossless_hermes.plugin import result_budget


# The single load-bearing regex. If this breaks the agent contract is
# also broken — coordinate any format change with the agent description
# strings (``LCM_GREP_SCHEMA["description"]`` etc.).
TRUNCATION_REGEX = re.compile(r"truncated at ~\d+ tokens to protect agent context")


@pytest.fixture(autouse=True)
def _reset_budget_state() -> "object":
    """Reset module-level bindings between tests."""
    result_budget.__reset_result_budget_for_testing()
    yield
    result_budget.__reset_result_budget_for_testing()


class TestTruncationNoticeFormatPin:
    """The truncation-notice regex is pinned and load-bearing."""

    def test_regex_matches_default_notice(self) -> None:
        """The regex matches the notice at the default cap (10000 tokens)."""
        notice = result_budget.truncation_notice("narrow query, lower limit")
        assert TRUNCATION_REGEX.search(notice) is not None

    def test_regex_matches_at_raised_cap(self) -> None:
        """The regex matches when ``apply_result_budget_config`` raises the cap."""
        result_budget.apply_result_budget_config(50_000)
        notice = result_budget.truncation_notice("test reason")
        assert TRUNCATION_REGEX.search(notice) is not None
        match = re.search(r"~(\d+) tokens", notice)
        assert match is not None
        assert int(match.group(1)) == 50_000

    def test_regex_matches_at_floor(self) -> None:
        """The regex matches when the cap is clamped to the floor."""
        result_budget.apply_result_budget_config(500)  # below floor
        notice = result_budget.truncation_notice("anything")
        assert TRUNCATION_REGEX.search(notice) is not None
        match = re.search(r"~(\d+) tokens", notice)
        assert match is not None
        assert int(match.group(1)) == 2_000  # floor

    def test_format_constant_carries_template(self) -> None:
        """The :data:`TRUNCATION_NOTICE_FORMAT` constant has the literal substring."""
        # The pin is on the LITERAL substring (without the digit count) so
        # the constant itself can be regex-checked at lint time. CI will
        # eventually wave-n-grep for the substring to detect drift.
        assert "truncated at ~{tokens} tokens to protect agent context" in (
            result_budget.TRUNCATION_NOTICE_FORMAT
        )

    def test_format_constant_has_reason_placeholder(self) -> None:
        """The format-string carries a ``{reason}`` placeholder for callers."""
        assert "{reason}" in result_budget.TRUNCATION_NOTICE_FORMAT

    def test_format_constant_has_operator_hint(self) -> None:
        """The format-string includes the operator-tunable env knob name."""
        assert "LCM_TOOL_RESULT_TOKEN_BUDGET" in result_budget.TRUNCATION_NOTICE_FORMAT

    def test_notice_reason_substituted(self) -> None:
        """The reason hint is substituted verbatim into the output."""
        notice = result_budget.truncation_notice("custom reason text")
        assert "custom reason text" in notice
        # The template emits "<regex> — <reason>;" so the reason is between
        # the long-dash and a semicolon. Spot-check the structure.
        assert " — custom reason text;" in notice

    def test_notice_is_markdown_italic(self) -> None:
        """The notice is wrapped in ``*(...)*`` — markdown italic emphasis.

        Tool output is rendered as markdown by the agent; the italic
        emphasis flags the line as a system-side note rather than tool
        content. Cosmetic but pinned for the agent UX contract.
        """
        notice = result_budget.truncation_notice("reason")
        assert notice.startswith("*(")
        assert notice.endswith(")*")
