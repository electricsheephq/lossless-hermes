"""``/lcm worker`` status + tick stubs.

Stub for issue 08-NN (worker bodies). Issue 08-01 ships only the router.

Maps to TS cases ``"worker_status"`` and ``"worker_tick_backfill"`` in
``lossless-claw/src/plugin/lcm-command.ts``.
"""

from __future__ import annotations

from typing import Any


def run_status(parsed: Any) -> str:  # noqa: ARG001 — stub
    """``/lcm worker`` / ``/lcm worker status`` stub."""
    return (
        "/lcm worker: subcommand not yet implemented (Epic 08). "
        "Run /lcm help for the full subcommand inventory."
    )


def run_tick_backfill(parsed: Any) -> str:  # noqa: ARG001 — stub
    """``/lcm worker tick embedding-backfill`` (owner-gated) stub."""
    return (
        "/lcm worker tick embedding-backfill: subcommand not yet implemented "
        "(Epic 08). Run /lcm help for the full subcommand inventory."
    )
