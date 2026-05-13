"""Dispatch-table tests for the ``/lcm`` slash command router (issue 08-01).

Ports the TS ``test/lcm-command.test.ts:__testing`` dispatcher-table tests
into pytest. The TS file isn't a single dedicated test (per plugin-glue.md
line 588: "LCM command tests are NOT separated into a single dedicated
file — the TS source has them inline in lcm-command.ts:__testing plus
various scenario tests"); this module reconstructs the routing invariants
into focused tests.

Per the issue 08-01 acceptance criteria (line 65 in the issue spec):

> All 17 dispatch keys are routed to the correct handler module (verified
> by mocking each handler to return a unique sentinel string and asserting
> the dispatcher returns it).

This file owns that criterion. Per-subcommand body tests live in
sibling files (``test_status_text.py``, ``test_purge.py``, etc.) added
by issues 08-02..08-15.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from lossless_hermes.plugin.commands import (
    _SUBCOMMANDS,
    LcmCommandDispatcher,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine() -> Any:
    """Minimal engine stub — dispatcher only reads ``name`` via handlers."""
    engine = MagicMock()
    engine.name = "lcm"
    return engine


def _mock_handler_module(
    monkeypatch: pytest.MonkeyPatch,
    module_path: str,
    function_name: str,
    sentinel: str,
) -> None:
    """Patch ``module_path.function_name`` to return ``sentinel``.

    Used by the "every dispatch key routes correctly" test below — each
    of the 17 entries is verified by replacing its handler with a unique
    sentinel-returning stub.
    """
    from importlib import import_module

    module = import_module(module_path)
    monkeypatch.setattr(module, function_name, lambda parsed: sentinel)


# ---------------------------------------------------------------------------
# AC line 65 — all 17 dispatch keys route to the correct handler module
# ---------------------------------------------------------------------------


def test_all_17_dispatch_keys_route_to_correct_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every entry in _SUBCOMMANDS routes to its declared handler.

    For each entry ``(canonical_path, handler_ref, ...)``:

    1. Replace the handler with a stub that returns a unique sentinel.
    2. Call ``dispatcher.handle(canonical_path)``.
    3. Assert the dispatcher returned the sentinel.

    A single failure pinpoints which entry has a mis-wired handler ref.
    """
    dispatcher = LcmCommandDispatcher(_make_engine())
    for idx, (path, handler_ref, _gated, _desc) in enumerate(_SUBCOMMANDS):
        sentinel = f"SENTINEL-{idx}-{path.replace(' ', '_')}"
        module_path, func_name = handler_ref.split(":", 1)
        _mock_handler_module(monkeypatch, module_path, func_name, sentinel)
        out = dispatcher.handle(path)
        assert out == sentinel, (
            f"dispatch for {path!r} did not return the stub sentinel "
            f"(expected {sentinel!r}, got {out!r})"
        )


def test_bare_lcm_routes_to_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/lcm` (no args) aliases to `/lcm status` per TS parity (spec line 47)."""
    dispatcher = LcmCommandDispatcher(_make_engine())
    _mock_handler_module(monkeypatch, "lossless_hermes.commands.status", "run", "STATUS_SENTINEL")
    assert dispatcher.handle("") == "STATUS_SENTINEL"
    assert dispatcher.handle("   ") == "STATUS_SENTINEL"


def test_unknown_subcommand_returns_help_pointer() -> None:
    """Truly unknown subcommands return the unknown-subcommand message."""
    dispatcher = LcmCommandDispatcher(_make_engine())
    out = dispatcher.handle("definitely-not-a-real-command")
    assert "unknown subcommand" in out
    assert "definitely-not-a-real-command" in out
    assert "/lcm help" in out


def test_handler_exception_caught_returns_failed_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Handler raising mid-flight returns ``/lcm <sub> failed: ...`` (no crash)."""
    dispatcher = LcmCommandDispatcher(_make_engine())
    import lossless_hermes.commands.status as status_mod

    def _broken(_parsed: Any) -> str:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(status_mod, "run", _broken)

    out = dispatcher.handle("status")
    assert "/lcm status failed" in out
    assert "kaboom" in out


def test_handler_returning_none_yields_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A handler returning ``None`` (allowed by the contract) yields ``""``.

    Hermes's ``register_command`` signature allows ``str | None``; the
    dispatcher coerces ``None`` to empty string so the Telegram / Slack
    bridge always gets a string.
    """
    dispatcher = LcmCommandDispatcher(_make_engine())
    import lossless_hermes.commands.status as status_mod

    monkeypatch.setattr(status_mod, "run", lambda parsed: None)
    out = dispatcher.handle("status")
    assert out == ""


def test_dispatcher_does_not_check_is_owner() -> None:
    """ADR-013 invariant — dispatcher never reads `is_owner`.

    Mirrors the acceptance criterion line 70:
    > No per-subcommand `is_owner` check in the dispatcher (ADR-013 invariant
    > — `grep -n "is_owner" src/lossless_hermes/plugin/commands.py` returns
    > 0 lines).

    We assert the source file itself has zero `is_owner` mentions.
    """
    from lossless_hermes.plugin import commands as commands_mod

    source_path = commands_mod.__file__
    assert source_path is not None
    with open(source_path, "r", encoding="utf-8") as f:
        source = f.read()
    # Per ADR-013, owner-gating is upstream of dispatch. The router must
    # not even mention is_owner — that's a smell of in-handler gating.
    assert "is_owner" not in source, (
        "Dispatcher source must not reference is_owner (ADR-013 §Decision)."
    )
