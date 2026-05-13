"""Tests for the ``/lcm`` slash command dispatcher.

Originally written for issue 02-10 (Epic-02 stub dispatcher). Updated for
issue 08-01 (full 17-subcommand router) — see ``epics/08-cli-ops/08-01-
slash-command-router.md``. Per ADR-013 the handler signature is
``(raw_args: str) -> str``; tests exercise the dispatcher directly
without any owner-context — Hermes's upstream gate is out of scope here.

Covers:

* Subcommand routing (status, help, unknown).
* ``status`` body (port from Epic-02; lives in
  ``lossless_hermes.commands.status``).
* ``help`` body — markdown table of all 17 subcommands with ``(admin)``
  markers.
* "Not yet implemented" stubs for the 15 Epic-08/09 subcommands not yet
  wired.
* Argument parsing (shlex quoting, unbalanced quotes, etc.).
* ``register()`` wiring — registers both ``/lcm`` and ``/lossless`` per
  the alias requirement from plugin-glue.md line 446.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from lossless_hermes.plugin.commands import (
    _SUBCOMMAND_INVENTORY,
    _SUBCOMMANDS,
    LcmCommandDispatcher,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine(
    *,
    db_open: bool = False,
    name: str = "lcm",
    has_conversation_store: bool = False,
    current_session_id: str | None = None,
) -> Any:
    """Build a minimal stub :class:`LCMEngine` for dispatcher tests.

    The dispatcher only reads engine state — never writes — so a Mock
    with the right attributes is sufficient. We avoid instantiating the
    real :class:`LCMEngine` to keep tests fast and isolated from
    sqlite/store machinery (those have their own test suites).

    Args:
        db_open: When ``True``, sets ``_db`` to a sentinel object so the
            "db: open" branch is exercised. Default ``False`` matches the
            pre-``on_session_start`` state. NOTE: this is a sentinel
            object, not a real connection — status's DB-query path will
            raise if any test actually exercises it; tests that need a
            live DB use the dedicated ``test_status.py`` suite.
        name: The engine name string. Defaults to ``"lcm"``.
        has_conversation_store: When ``True``, attaches a Mock store so
            the "conversation_store: ready" branch is exercised.
        current_session_id: Per issue 08-02, the field that replaces
            the TS ``ctx.sessionId``. Default ``None`` matches the
            pre-on_session_start state — status omits the per-
            conversation block in this case.
    """
    engine = MagicMock()
    engine.name = name
    engine._db = object() if db_open else None
    engine._conversation_store = MagicMock() if has_conversation_store else None
    engine.current_session_id = current_session_id
    engine.config = SimpleNamespace(database_path="")
    engine._maintenance_store = None
    engine._telemetry_store = None
    return engine


# ---------------------------------------------------------------------------
# Basic routing — status / help / unknown / empty
# ---------------------------------------------------------------------------


def test_status_subcommand_returns_status_block() -> None:
    """`/lcm status` returns the issue 08-02 status block.

    Updated for issue 08-02 (full status body). The Epic-02 minimal
    body (``[lcm] status\n  engine: lcm\n  ...  ok``) was replaced
    with the markdown-formatted multi-section output per
    ``docs/porting-guides/plugin-glue.md`` line 425.
    """
    dispatcher = LcmCommandDispatcher(_make_engine())
    out = dispatcher.handle("status")
    # Header line — package name + version.
    assert "Lossless Hermes v" in out
    # Plugin section is always rendered.
    assert "**Plugin**" in out


def test_empty_args_aliases_status() -> None:
    """Bare `/lcm` (no args) routes to status per Epic 02 spec."""
    dispatcher = LcmCommandDispatcher(_make_engine())
    out = dispatcher.handle("")
    # Same body as /lcm status — Plugin section is the always-on marker.
    assert "**Plugin**" in out


def test_whitespace_only_args_aliases_status() -> None:
    """Pure-whitespace `raw_args` (e.g. `/lcm  `) also aliases to status."""
    dispatcher = LcmCommandDispatcher(_make_engine())
    out = dispatcher.handle("   ")
    assert "**Plugin**" in out


def test_help_subcommand_returns_markdown_table() -> None:
    """`/lcm help` returns a markdown table of all 17 subcommands."""
    dispatcher = LcmCommandDispatcher(_make_engine())
    out = dispatcher.handle("help")
    # Markdown table header (issue 08-01 dropped the Target column —
    # destructiveness is conveyed by the (admin) marker instead).
    assert "| Subcommand | Description |" in out
    assert "| --- | --- |" in out
    # The two always-on entries
    assert "/lcm status" in out
    assert "/lcm help" in out
    # A few Epic-08 entries
    assert "/lcm purge" in out
    assert "/lcm doctor" in out
    assert "/lcm worker tick" in out
    # Epic-09 entries
    assert "/lcm eval" in out
    # (admin) marker on owner-gated rows
    assert "(admin)" in out
    # Owner-gating note references ADR-013
    assert "ADR-013" in out


def test_help_lists_all_17_subcommands() -> None:
    """Every entry in `_SUBCOMMANDS` shows up in /lcm help."""
    dispatcher = LcmCommandDispatcher(_make_engine())
    out = dispatcher.handle("help")
    for name, _handler, _gated, _desc in _SUBCOMMANDS:
        assert f"/lcm {name}" in out, f"missing inventory entry {name!r} in /lcm help output"


def test_subcommand_inventory_has_17_entries() -> None:
    """The router inventory matches the spec — exactly 17 logical subcommands."""
    assert len(_SUBCOMMANDS) == 17, (
        f"expected 17 entries per plugin-glue.md /lcm slash commands inventory; "
        f"got {len(_SUBCOMMANDS)}"
    )


def test_unknown_subcommand_returns_unknown_message() -> None:
    """Truly unknown subcommands return the unknown-subcommand message.

    Per issue 08-01 the router distinguishes "known but not implemented"
    (returns Epic-NN stub) from "unknown subcommand" (returns help
    pointer). The "nonsense" / "unknown_cmd" inputs are not in
    :data:`_SUBCOMMANDS` so they hit the unknown branch.
    """
    dispatcher = LcmCommandDispatcher(_make_engine())
    out = dispatcher.handle("nonsense")
    assert "unknown subcommand" in out
    assert "nonsense" in out
    assert "/lcm help" in out


def test_unknown_cmd_returns_unknown_message() -> None:
    """The user-facing `unknown_cmd` example from the issue spec works."""
    dispatcher = LcmCommandDispatcher(_make_engine())
    out = dispatcher.handle("unknown_cmd")
    assert "unknown subcommand" in out
    assert "unknown_cmd" in out


# ---------------------------------------------------------------------------
# Stub subcommands — known-but-not-yet-implemented (Epic 08 / 09)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "subcommand,expected_epic",
    [
        ("purge", "Epic 08"),
        ("health", "Epic 08"),
        ("doctor", "Epic 08"),
        ("backup", "Epic 08"),
        ("rotate", "Epic 08"),
        ("eval", "Epic 09"),
        # Note: ``import-openclaw`` was previously stubbed; issue 08-15
        # wired the real body in ``lossless_hermes.cli.import_openclaw``.
        # The dedicated tests live in ``tests/cli/test_import_openclaw.py``.
    ],
)
def test_known_subcommand_returns_not_yet_implemented(subcommand: str, expected_epic: str) -> None:
    """Known subcommands stubbed for Epic 08/09 return the standard message."""
    dispatcher = LcmCommandDispatcher(_make_engine())
    out = dispatcher.handle(subcommand)
    assert "not yet implemented" in out
    assert expected_epic in out
    assert f"/lcm {subcommand}" in out


def test_nested_subcommand_routes_to_nested_handler() -> None:
    """`/lcm doctor apply` routes to ``doctor:run_apply`` not ``doctor:run_scan``.

    Issue 08-01 implements longest-prefix matching so multi-token
    subcommands route to dedicated handler functions. The
    ``doctor apply`` stub's "not yet implemented" message echoes the
    full canonical path.
    """
    dispatcher = LcmCommandDispatcher(_make_engine())
    out = dispatcher.handle("doctor apply")
    assert "not yet implemented" in out
    assert "/lcm doctor apply" in out


def test_worker_tick_routes_to_tick_handler() -> None:
    """`/lcm worker tick embedding-backfill` routes to ``worker:run_tick_backfill``."""
    dispatcher = LcmCommandDispatcher(_make_engine())
    out = dispatcher.handle("worker tick embedding-backfill")
    assert "not yet implemented" in out
    assert "/lcm worker tick embedding-backfill" in out


def test_doctor_clean_apply_routes_to_cleaners_apply() -> None:
    """`/lcm doctor clean apply` resolves the 3-token canonical path."""
    dispatcher = LcmCommandDispatcher(_make_engine())
    out = dispatcher.handle("doctor clean apply")
    assert "not yet implemented" in out
    assert "/lcm doctor clean apply" in out


def test_doctor_clean_routes_to_cleaners_scan() -> None:
    """`/lcm doctor clean` (no `apply`) resolves to the read-only listing handler."""
    dispatcher = LcmCommandDispatcher(_make_engine())
    out = dispatcher.handle("doctor clean")
    assert "not yet implemented" in out
    assert "/lcm doctor clean" in out


# ---------------------------------------------------------------------------
# Status — engine state surfacing
#
# Note: the rich status-body assertions (counts, suppression, compression
# ratio, maintenance section gating) live in ``tests/commands/test_status.py``
# which uses a real in-memory migrated DB. These dispatcher-level tests
# only confirm that the routing reaches the right module and that the
# pre-DB-open branch renders the expected hint.
# ---------------------------------------------------------------------------


def test_status_reports_db_not_opened_pre_session_start() -> None:
    """Without on_session_start the DB is None; status renders the hint section.

    Per issue 08-02 status body, the ``_db is None`` branch renders a
    ``**Status**`` section with ``db: not yet opened`` and a hint to
    trigger ``on_session_start``.
    """
    engine = _make_engine(db_open=False)
    dispatcher = LcmCommandDispatcher(engine)
    out = dispatcher.handle("status")
    assert "**Status**" in out
    assert "not yet opened" in out


# ---------------------------------------------------------------------------
# Argument parsing — shlex / quoting / nested args
# ---------------------------------------------------------------------------


def test_args_passed_to_handler() -> None:
    """`/lcm status --verbose` — status ignores args today but the wiring
    should let Epic 08 read them.

    We can't observe sub_args directly from the public surface, but we
    can confirm that args don't crash the dispatcher and the result is
    still a well-formed status block.
    """
    dispatcher = LcmCommandDispatcher(_make_engine())
    out = dispatcher.handle("status --verbose")
    # Issue 08-02 status block: Plugin section is the always-on marker.
    assert "**Plugin**" in out


def test_shlex_quoting_preserves_quoted_args() -> None:
    """`/lcm purge --reason "test with spaces"` — quoted args don't break routing.

    Smoke test for Epic 08's purge subcommand which will need
    ``--reason "..."``. At 02-10 it returns the Epic-08 stub; what we
    care about here is that the dispatcher routes to ``purge`` correctly
    despite the embedded spaces.
    """
    dispatcher = LcmCommandDispatcher(_make_engine())
    out = dispatcher.handle('purge --reason "test with spaces"')
    assert "/lcm purge" in out
    assert "not yet implemented" in out
    assert "Epic 08" in out


def test_unbalanced_quotes_returns_parse_error() -> None:
    """Unbalanced quotes return a parse error, not a stack trace."""
    dispatcher = LcmCommandDispatcher(_make_engine())
    out = dispatcher.handle('purge --reason "missing close')
    # shlex.split raises ValueError; we catch and return a friendly message.
    assert "argument parse error" in out
    assert "/lcm help" in out


# ---------------------------------------------------------------------------
# Exception robustness
# ---------------------------------------------------------------------------


def test_handler_exception_caught_by_dispatcher(monkeypatch: pytest.MonkeyPatch) -> None:
    """A handler raising mid-flight returns `/lcm <sub> failed: ...` — no crash.

    We monkey-patch the status handler module's ``run`` to raise and
    assert the dispatcher converts the exception into a user-visible
    failure message. This is the robustness contract — a buggy
    subcommand body must not crash the chat session.
    """
    dispatcher = LcmCommandDispatcher(_make_engine())

    def _broken_handler(_parsed: Any) -> str:
        raise RuntimeError("intentional test failure")

    # Patch the status module's run() so the lazy import resolves to a
    # broken handler. This mirrors the real failure path — the
    # dispatcher catches exceptions raised inside handler module code.
    import lossless_hermes.commands.status as status_mod

    monkeypatch.setattr(status_mod, "run", _broken_handler)

    out = dispatcher.handle("status")
    assert "/lcm status failed" in out
    assert "intentional test failure" in out


# ---------------------------------------------------------------------------
# Wiring — register() calls ctx.register_command("lcm", ...)
# ---------------------------------------------------------------------------


def test_register_wires_lcm_and_lossless_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    """``register(ctx)`` registers BOTH ``/lcm`` and ``/lossless``.

    Per issue 08-01 spec line 49 + plugin-glue.md line 446: Hermes's
    ``register_command`` doesn't accept aliases natively, so the second
    registration is the documented workaround. Both names point at the
    same dispatcher closure.
    """
    import lossless_hermes
    import lossless_hermes.hermes_bridge as bridge

    monkeypatch.setattr(bridge, "HERMES_AVAILABLE", True)
    monkeypatch.setattr(bridge, "get_hermes_home", lambda: "/tmp/.hermes-test")
    monkeypatch.setattr(lossless_hermes, "HERMES_AVAILABLE", True)

    ctx = MagicMock()
    ctx.register_context_engine = MagicMock()
    ctx.register_command = MagicMock()

    lossless_hermes.register(ctx)

    # Two registrations: /lcm and /lossless, in order.
    assert ctx.register_command.call_count == 2
    call_args_list = ctx.register_command.call_args_list
    names = [call.args[0] for call in call_args_list]
    assert names == ["lcm", "lossless"]
    # Both registrations get the same handler closure (same bound method
    # on the same dispatcher instance).
    handler_lcm = call_args_list[0].args[1]
    handler_lossless = call_args_list[1].args[1]
    assert handler_lcm == handler_lossless
    assert callable(handler_lcm)
    # kwargs: args_hint
    assert call_args_list[0].kwargs.get("args_hint") == "<subcommand>"
    assert call_args_list[1].kwargs.get("args_hint") == "<subcommand>"


def test_register_wires_dispatcher_with_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """The handler registered is a real `LcmCommandDispatcher.handle` bound method.

    Confirms the dispatcher is constructed with the engine — calling the
    handler should return the status block for the registered engine.
    """
    import lossless_hermes
    import lossless_hermes.hermes_bridge as bridge

    monkeypatch.setattr(bridge, "HERMES_AVAILABLE", True)
    monkeypatch.setattr(bridge, "get_hermes_home", lambda: "/tmp/.hermes-test")
    monkeypatch.setattr(lossless_hermes, "HERMES_AVAILABLE", True)

    ctx = MagicMock()
    ctx.register_context_engine = MagicMock()
    ctx.register_command = MagicMock()

    lossless_hermes.register(ctx)

    # Pull the /lcm handler (first registered).
    handler = ctx.register_command.call_args_list[0].args[1]
    # Calling the registered handler returns a real status block — the
    # dispatcher was constructed with the engine instance. Issue 08-02's
    # output format: markdown header + Plugin section.
    out = handler("status")
    assert "Lossless Hermes v" in out
    assert "**Plugin**" in out
