"""``/lcm reconcile-session-keys`` stubs.

Stub for issue 08-NN (reconcile body). Issue 08-01 ships only the router.

Maps to TS cases ``"reconcile_session_keys_list"`` and
``"reconcile_session_keys_apply"`` in
``lossless-claw/src/plugin/lcm-command.ts``.
"""

from __future__ import annotations

from typing import Any


def run_list(parsed: Any) -> str:  # noqa: ARG001 — stub
    """``/lcm reconcile-session-keys --list-candidates`` (owner-gated) stub."""
    return (
        "/lcm reconcile-session-keys --list-candidates: subcommand not yet "
        "implemented (Epic 08). Run /lcm help for the full subcommand inventory."
    )


def run_apply(parsed: Any) -> str:  # noqa: ARG001 — stub
    """``/lcm reconcile-session-keys --apply`` (owner-gated) stub.

    Real body lands with issue 08-NN. Requires ``--from k1,k2 --to k3
    --reason "..."`` per the TS source. Optional ``--allow-main-session``
    permits targeting Eva's primary thread.
    """
    return (
        "/lcm reconcile-session-keys --apply: subcommand not yet implemented "
        "(Epic 08). Run /lcm help for the full subcommand inventory."
    )
