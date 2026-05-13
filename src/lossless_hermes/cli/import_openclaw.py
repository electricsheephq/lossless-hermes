"""``/lcm import-openclaw`` and ``hermes lcm import-openclaw`` stub.

Stub for issue 08-NN (import-openclaw body). Issue 08-01 ships only the
router; this module is referenced from the dispatch table.

The real body will import an OpenClaw LCM snapshot (SQLite + JSONL
transcripts) into the Hermes-hosted database, mapping session_keys per
the configured policy.
"""

from __future__ import annotations

from typing import Any


def run_slash(parsed: Any) -> str:  # noqa: ARG001 — stub
    """``/lcm import-openclaw`` (owner-gated) stub. Real body lands with 08-NN."""
    return (
        "/lcm import-openclaw: subcommand not yet implemented (Epic 08). "
        "Run /lcm help for the full subcommand inventory."
    )
