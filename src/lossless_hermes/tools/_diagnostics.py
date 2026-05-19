"""Shared output-capping helper for the read-only diagnostic tools.

Per [ADR-035](../../docs/adr/035-lcm-status-doctor-model-tools.md), the
``lcm_status`` and ``lcm_doctor`` model-callable tools wrap the existing
read-only command bodies. The slash-command variants render an
**unbounded** report (an operator's terminal can take it); the **tool**
variants MUST cap their output so a diagnostic call does not blow the
turn's tool-result budget.

This module owns the one load-bearing caveat from ADR-035 §Consequences:

> Tool-variant output is capped/summarized — mandatory. [...] cap
> ``lcm_status`` to its highest-signal sections [...] and cap
> ``lcm_doctor`` to a bounded count of findings plus a "+N more — run
> ``/lcm doctor`` for the full scan" tail.

What this module owns
---------------------

* :data:`DIAGNOSTIC_TOOL_OUTPUT_CHAR_CAP` — the byte/char budget the
  tool variants cap to. Distinct from
  :data:`lossless_hermes.plugin.result_budget.MAX_RESULT_CHARS` (the
  general per-tool truncation cap, 40K chars default): a diagnostic
  call is a *self-check*, not a content read, so it gets a much
  tighter budget — the model should pay a small, fixed context price
  to observe LCM's state, not a 10K-token slice.
* :func:`cap_diagnostic_text` — truncate a rendered diagnostic report
  to the cap, appending a one-line ``"... output capped"`` tail that
  points the operator at the uncapped ``/lcm`` slash command.

Why a fixed char cap, not a token estimate
------------------------------------------

The general tool surface uses ``estimate_result_tokens`` +
``MAX_RESULT_TOKENS``. The diagnostic tools deliberately use a simpler
fixed char cap because (a) the output is structured text we render
ourselves (not arbitrary user content), so ``len(str)`` is a faithful
proxy, and (b) the cap is small and fixed — a token estimate would add
a dependency for no precision gain at this size. The
``CHARS_PER_TOKEN`` ratio (4) makes the cap easy to reason about:
:data:`DIAGNOSTIC_TOOL_OUTPUT_CHAR_CAP` of 6000 chars is ~1.5K tokens —
the ADR §"Open questions" row-1 proposed starting point for
``lcm_status``.

References
----------

* [ADR-035 §Consequences](../../docs/adr/035-lcm-status-doctor-model-tools.md)
  — the mandatory output-cap caveat.
* [ADR-035 §"Open questions" row 1] — proposed ~1.5K-token starting
  point for ``lcm_status``; ~20-finding cap for ``lcm_doctor``.
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# Output budget
# ---------------------------------------------------------------------------
#
# ADR-035 §"Open questions" row 1 names ~1.5K tokens as the proposed
# starting point for the lcm_status tool variant. At the project's
# CHARS_PER_TOKEN=4 estimate (result_budget.py), 1.5K tokens ≈ 6000
# chars. We use that as the shared char cap for both diagnostic tools:
# lcm_status caps its rendered sections to it, and lcm_doctor caps its
# finding digest both by a finding-count cap (below) AND this char cap
# as a backstop.

DIAGNOSTIC_TOOL_OUTPUT_CHAR_CAP: Final[int] = 6_000
"""Hard char cap on a diagnostic tool's rendered output.

~1.5K tokens at the project's 4-chars/token estimate — the ADR-035
§"Open questions" row-1 proposed starting point for ``lcm_status``.
The tool variant is a self-diagnosis probe, so it gets a much tighter
budget than the general 40K-char per-tool truncation cap."""

DIAGNOSTIC_DOCTOR_FINDING_CAP: Final[int] = 20
"""Max number of per-summary doctor findings the ``lcm_doctor`` tool
variant enumerates inline.

ADR-035 §"Open questions" row 1: "≤ ~20 findings for ``lcm_doctor``".
Beyond this the tool emits a ``"+N more"`` tail pointing at the
uncapped ``/lcm doctor`` slash command."""

_CAP_TAIL: Final[str] = (
    "\n... [output capped at ~{tokens}K tokens for the tool result budget — "
    "run `/lcm {command}` for the full uncapped report]"
)
"""Tail appended when :func:`cap_diagnostic_text` truncates. The
``{command}`` placeholder is the slash subcommand (``status`` /
``doctor``) the operator can run for the unbounded output."""


def cap_diagnostic_text(
    text: str,
    *,
    slash_command: str,
    char_cap: int = DIAGNOSTIC_TOOL_OUTPUT_CHAR_CAP,
) -> str:
    """Cap a rendered diagnostic report to the tool-result budget.

    If ``text`` is at or under ``char_cap``, it is returned unchanged.
    Otherwise it is truncated to fit ``char_cap`` *including* the
    appended cap-tail, so the returned string is never longer than
    ``char_cap``. The tail names the ``/lcm <slash_command>`` the
    operator can run for the full uncapped output (per ADR-035 — the
    slash variant stays unbounded).

    The truncation prefers a clean line boundary: it trims back to the
    last newline before the budget so the capped report does not end
    mid-line. If there is no newline in range (a single very long
    line), it hard-cuts at the char boundary.

    Args:
        text: The fully rendered diagnostic report (the same text the
            slash command would emit).
        slash_command: The ``/lcm`` subcommand name — ``"status"`` or
            ``"doctor"`` — surfaced in the cap-tail so the operator
            knows which command yields the uncapped report.
        char_cap: The char budget. Defaults to
            :data:`DIAGNOSTIC_TOOL_OUTPUT_CHAR_CAP`. The returned
            string (body + tail) never exceeds this.

    Returns:
        ``text`` unchanged when within budget; otherwise a truncated
        prefix plus the one-line cap-tail, total length ``<= char_cap``.

    Examples:
        >>> cap_diagnostic_text("short report", slash_command="status")
        'short report'
        >>> capped = cap_diagnostic_text("x" * 10000, slash_command="doctor")
        >>> len(capped) <= DIAGNOSTIC_TOOL_OUTPUT_CHAR_CAP
        True
        >>> "run `/lcm doctor`" in capped
        True
    """
    if len(text) <= char_cap:
        return text

    # Render the tail first so we know exactly how much room the body
    # gets. ~1.5K-token figure is char_cap / 4000 rounded to 1 decimal.
    tail = _CAP_TAIL.format(
        tokens=round(char_cap / 4_000, 1),
        command=slash_command,
    )
    body_budget = char_cap - len(tail)
    if body_budget <= 0:
        # Pathological: char_cap smaller than the tail itself. Return a
        # hard-cut of the tail so the contract (<= char_cap) still holds.
        return tail[:char_cap]

    body = text[:body_budget]
    # Trim back to the last line boundary so the report doesn't end
    # mid-line. Only do this if a newline exists in the kept region —
    # otherwise (one giant line) keep the hard char cut.
    last_newline = body.rfind("\n")
    if last_newline > 0:
        body = body[:last_newline]
    return body + tail
