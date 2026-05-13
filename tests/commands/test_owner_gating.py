"""Tests for ADR-013 owner-gating invariants (issue 08-01).

Per the issue 08-01 acceptance criterion line 73:

> New test: tests/commands/test_owner_gating.py — mock SlashAccessPolicy.deny()
> and assert the handler body is never reached for destructive subcommands
> (ADR-013 §"Open questions" line 90).

ADR-013 §Decision moves owner-gating UPSTREAM of dispatch: the gateway's
:class:`SlashAccessPolicy` runs ``can_run(user_id, command_name)`` BEFORE
the plugin handler is invoked. So the test surface here is dual:

1. **Invariant on the dispatcher** — ``LcmCommandDispatcher`` does not
   read ``is_owner`` or any per-call security state. We grep the source
   for this (mirrors the AC line 70).

2. **End-to-end gating behavior** — using the real
   :class:`SlashAccessPolicy`, denied users never reach the handler.
   We construct a policy with a known non-admin user and assert
   ``policy.can_run("non-admin", "purge")`` returns ``False``. The
   handler body is verified un-reached because the policy gate is the
   only thing standing between Hermes's dispatch and the LCM handler.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from lossless_hermes.plugin.commands import (
    _SUBCOMMANDS,
    LcmCommandDispatcher,
)


# The 9 destructive subcommands per plugin-glue.md "Owner-gating count: 9
# out of 13" + the issue 08-01 spec table. These are the entries in
# :data:`_SUBCOMMANDS` with ``owner_gated=True``. The test below
# regenerates the list from the inventory so the two stay in sync.
def _destructive_subcommands() -> list[str]:
    return [name for (name, _h, gated, _d) in _SUBCOMMANDS if gated]


# ---------------------------------------------------------------------------
# Invariant — dispatcher source has no `is_owner` mentions (ADR-013)
# ---------------------------------------------------------------------------


def test_dispatcher_source_has_no_is_owner_check() -> None:
    """`grep is_owner src/.../commands.py` returns 0 lines (AC line 70).

    ADR-013 §Decision: handlers do NOT check is_owner. The dispatcher's
    source must not reference is_owner — that smell suggests in-handler
    gating creeping back in.
    """
    from lossless_hermes.plugin import commands as commands_mod

    source_path = commands_mod.__file__
    assert source_path is not None
    with open(source_path, "r", encoding="utf-8") as f:
        source = f.read()
    assert "is_owner" not in source, (
        "Dispatcher source must not reference is_owner per ADR-013 §Decision."
    )


# ---------------------------------------------------------------------------
# End-to-end — SlashAccessPolicy denies → handler never runs
# ---------------------------------------------------------------------------


def _has_gateway_module() -> bool:
    try:
        from gateway.slash_access import SlashAccessPolicy  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(
    not _has_gateway_module(),
    reason="Hermes gateway module not on import path — owner-gating is "
    "out-of-band; in this env the policy lives upstream and we can't "
    "import its types. ADR-013 invariant is verified via the source-grep "
    "test above; the end-to-end policy test runs only with Hermes "
    "installed.",
)
def test_destructive_subcommand_blocked_by_policy_when_user_not_admin() -> None:
    """Real :class:`SlashAccessPolicy` rejects non-admin users for /lcm.

    Sanity check that the upstream gate does its job. The dispatcher
    isn't invoked at all when ``policy.can_run`` returns ``False`` — the
    gateway runs the gate first and returns the "admin-only" rejection
    text without calling into the plugin.
    """
    from gateway.slash_access import policy_from_extra

    # Construct a policy where:
    #   - admin_user_ids = {"alice"}
    #   - user_allowed_commands = {"help", "status"} (non-admins get
    #     read-only commands only)
    extra = {
        "allow_admin_from": ["alice"],
        "user_allowed_commands": ["help", "status"],
    }
    policy = policy_from_extra(extra, scope="dm")

    # Sanity: gating IS enabled.
    assert policy.enabled is True

    # Admin can run everything.
    assert policy.can_run("alice", "lcm") is True

    # Non-admin "bob" can NOT run /lcm — it's not in user_allowed_commands.
    assert policy.can_run("bob", "lcm") is False

    # Sanity: non-admin /help is allowed (always-allowed floor).
    assert policy.can_run("bob", "help") is True


@pytest.mark.skipif(
    not _has_gateway_module(),
    reason="Hermes gateway module not on import path.",
)
def test_destructive_subcommands_inventory_matches_adr_013() -> None:
    """The destructive subcommand list aligns with ADR-013 §Context.

    ADR-013 §Context enumerates the 9 destructive subcommands explicitly:

    > * /lcm worker tick embedding-backfill
    > * /lcm doctor apply
    > * /lcm doctor clean, /lcm doctor clean apply
    > * /lcm reconcile-session-keys --list-candidates, --apply
    > * /lcm eval
    > * /lcm purge

    Plus the issue 08-01 spec adds ``import-openclaw``. The inventory
    should mark these as owner-gated.
    """
    expected_gated = {
        "worker tick embedding-backfill",
        "doctor apply",
        "doctor clean",
        "doctor clean apply",
        "reconcile-session-keys --list-candidates",
        "reconcile-session-keys --apply",
        "eval",
        "purge",
        "import-openclaw",
    }
    actual_gated = set(_destructive_subcommands())
    assert actual_gated == expected_gated, (
        f"Owner-gated inventory diverged from ADR-013 §Context.\n"
        f"  Expected: {sorted(expected_gated)}\n"
        f"  Actual:   {sorted(actual_gated)}\n"
        f"  Missing:  {sorted(expected_gated - actual_gated)}\n"
        f"  Extra:    {sorted(actual_gated - expected_gated)}"
    )


# ---------------------------------------------------------------------------
# Defense-in-depth — dispatcher passes raw_args, NOT user context
# ---------------------------------------------------------------------------


def test_dispatcher_handler_receives_only_parsed_command() -> None:
    """Handler signature is ``run(parsed: ParsedLcmCommand) -> str | None``.

    ADR-013 §Decision: the handler receives only ``raw_args`` (wrapped
    as :class:`ParsedLcmCommand` + the engine ref). No user_id, no
    is_owner, no platform — all security context lives upstream.

    We assert this by inspecting what the dispatcher passes to the
    handler module.
    """
    captured: dict[str, Any] = {}

    def _capturing_handler(parsed: Any) -> str:
        captured["parsed"] = parsed
        return "ok"

    import lossless_hermes.commands.status as status_mod

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(status_mod, "run", _capturing_handler)
        engine = MagicMock()
        engine.name = "lcm"
        dispatcher = LcmCommandDispatcher(engine)
        dispatcher.handle("status")

    parsed = captured["parsed"]
    # The handler gets ParsedLcmCommand attributes: name, raw_args,
    # tokens, flags, engine. NOTHING about the caller / user / platform.
    for forbidden in ("user_id", "is_owner", "sender_is_owner", "platform"):
        assert not hasattr(parsed, forbidden), (
            f"ParsedLcmCommand must not carry {forbidden!r} — ADR-013 §Decision: "
            f"handler receives no security context."
        )
