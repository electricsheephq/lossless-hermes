"""``/lcm help`` — list available /lcm subcommands.

Renders a markdown table of the 17 logical subcommands from
:data:`lossless_hermes.plugin.commands._SUBCOMMANDS`, with ``(admin)``
markers on owner-gated rows. Footer points at
``~/.hermes/config.yaml``'s ``allow_admin_from`` block per the issue
08-01 spec line 47.

See:

* ``epics/08-cli-ops/08-01-slash-command-router.md`` — this issue.
* ``docs/porting-guides/plugin-glue.md`` "/lcm slash commands" — the
  17-entry inventory the table renders.
* ``docs/adr/013-owner-gating.md`` — the gate is in config, not handler.
"""

from __future__ import annotations

from typing import Any


def run(parsed: Any) -> str:  # noqa: ARG001 — parsed unused; signature uniform
    """Render the ``/lcm help`` markdown table.

    Groups the inventory by destructiveness (admin / public), shows the
    canonical subcommand path, an ``(admin)`` suffix on owner-gated
    rows, and a one-line description. Footer cites ADR-013 and the
    ``allow_admin_from`` config knob.

    Args:
        parsed: Unused; signature parity with other handlers.

    Returns:
        Multi-line markdown string.
    """
    # Imported lazily to avoid a static import cycle with the dispatcher
    # (which also imports from this module).
    from lossless_hermes.plugin.commands import _SUBCOMMANDS

    lines = [
        "# /lcm subcommands",
        "",
        "| Subcommand | Description |",
        "| --- | --- |",
    ]
    for name, _handler, owner_gated, desc in _SUBCOMMANDS:
        marker = " *(admin)*" if owner_gated else ""
        lines.append(f"| `/lcm {name}`{marker} | {desc} |")
    lines.append("")
    lines.append(
        "Owner-gated subcommands marked *(admin)* are restricted by Hermes's "
        "`allow_admin_from` config in `~/.hermes/config.yaml`. See ADR-013."
    )
    return "\n".join(lines)
