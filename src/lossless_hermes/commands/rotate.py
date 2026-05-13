"""``/lcm rotate`` — rotate the current session JSONL transcript.

Stub for issue 08-NN (rotate body). Issue 08-01 ships only the router.

Maps to TS ``buildRotateText`` in
``lossless-claw/src/plugin/lcm-command.ts`` (case ``"rotate"``).
"""

from __future__ import annotations

from typing import Any


def run(parsed: Any) -> str:  # noqa: ARG001 — stub
    """``/lcm rotate`` stub. Real body lands with issue 08-NN."""
    return (
        "/lcm rotate: subcommand not yet implemented (Epic 08). "
        "Run /lcm help for the full subcommand inventory."
    )
