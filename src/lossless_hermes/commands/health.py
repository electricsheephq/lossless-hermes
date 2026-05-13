"""``/lcm health`` — v4.1 health snapshot.

Stub for issue 08-NN (health body). Issue 08-01 ships only the router.

Maps to TS ``buildHealthText`` in
``lossless-claw/src/plugin/lcm-command.ts`` (case ``"health"``).
"""

from __future__ import annotations

from typing import Any


def run(parsed: Any) -> str:  # noqa: ARG001 — stub
    """``/lcm health`` stub. Real body lands with issue 08-NN."""
    return (
        "/lcm health: subcommand not yet implemented (Epic 08). "
        "Run /lcm help for the full subcommand inventory."
    )
