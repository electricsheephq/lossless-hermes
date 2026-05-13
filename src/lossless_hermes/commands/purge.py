"""``/lcm purge`` stub.

Stub for issue 08-NN (purge body). Issue 08-01 ships only the router.

Maps to TS case ``"purge"`` in ``lossless-claw/src/plugin/lcm-command.ts``.
"""

from __future__ import annotations

from typing import Any


def run(parsed: Any) -> str:  # noqa: ARG001 — stub
    """``/lcm purge`` (owner-gated) stub. Real body lands with issue 08-NN."""
    return (
        "/lcm purge: subcommand not yet implemented (Epic 08). "
        "Run /lcm help for the full subcommand inventory."
    )
