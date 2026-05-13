"""``/lcm doctor`` and ``/lcm doctor {apply,clean,clean apply}`` stubs.

Stub for issue 08-NN (doctor bodies). Issue 08-01 ships only the router;
this module is referenced from the dispatch table so routing works.

Maps to TS cases ``"doctor"`` and ``"doctor_cleaners"`` in
``lossless-claw/src/plugin/lcm-command.ts``.
"""

from __future__ import annotations

from typing import Any


def run_scan(parsed: Any) -> str:  # noqa: ARG001 — stub
    """``/lcm doctor`` (scan, read-only) stub."""
    return (
        "/lcm doctor: subcommand not yet implemented (Epic 08). "
        "Run /lcm help for the full subcommand inventory."
    )


def run_apply(parsed: Any) -> str:  # noqa: ARG001 — stub
    """``/lcm doctor apply`` (owner-gated) stub."""
    return (
        "/lcm doctor apply: subcommand not yet implemented (Epic 08). "
        "Run /lcm help for the full subcommand inventory."
    )


def run_cleaners_scan(parsed: Any) -> str:  # noqa: ARG001 — stub
    """``/lcm doctor clean`` (owner-gated read-only) stub."""
    return (
        "/lcm doctor clean: subcommand not yet implemented (Epic 08). "
        "Run /lcm help for the full subcommand inventory."
    )


def run_cleaners_apply(parsed: Any) -> str:  # noqa: ARG001 — stub
    """``/lcm doctor clean apply`` (owner-gated destructive) stub."""
    return (
        "/lcm doctor clean apply: subcommand not yet implemented (Epic 08). "
        "Run /lcm help for the full subcommand inventory."
    )
