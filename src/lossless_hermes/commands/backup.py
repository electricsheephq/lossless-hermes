"""``/lcm backup`` — VACUUM INTO a timestamped .bak file.

Stub for issue 08-NN (backup body). Issue 08-01 ships only the router;
this module is referenced from the dispatch table so the routing works,
and returns the "not yet implemented" message until 08-NN lands.

Maps to TS ``buildBackupText`` in
``lossless-claw/src/plugin/lcm-command.ts`` (case ``"backup"``).
"""

from __future__ import annotations

from typing import Any


def run(parsed: Any) -> str:  # noqa: ARG001 — stub
    """``/lcm backup`` stub. Real body lands with issue 08-NN."""
    return (
        "/lcm backup: subcommand not yet implemented (Epic 08). "
        "Run /lcm help for the full subcommand inventory."
    )
