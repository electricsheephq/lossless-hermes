"""Tests for the ``/lcm`` slash command dispatcher (issue 02-10).

Covers the dispatcher seam: subcommand routing, ``status`` body, ``help``
body, "not yet implemented" stubs for the 17 Epic-08/09 subcommands,
unknown-subcommand error, and the ``register()`` wiring that calls
``ctx.register_command("lcm", dispatcher.handle, ...)``.

Per ADR-013 the handler signature is ``(raw_args: str) -> str``; tests
exercise the dispatcher directly without any owner-context — Hermes's
upstream gate is out of scope here.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from lossless_hermes.plugin.commands import (
    _SUBCOMMAND_INVENTORY,
    LcmCommandDispatcher,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine(
    *,
    db_open: bool = False,
    name: str = "lcm",
    status: dict[str, Any] | None = None,
    has_conversation_store: bool = False,
) -> Any:
    """Build a minimal stub :class:`LCMEngine` for dispatcher tests.

    The dispatcher only reads engine state — never writes — so a Mock
    with the right attributes is sufficient. We avoid instantiating the
    real :class:`LCMEngine` to keep tests fast and isolated from
    sqlite/store machinery (those have their own test suites).

    Args:
        db_open: When ``True``, sets ``_db`` to a sentinel object so the
            "db: open" branch is exercised. Default ``False`` matches the
            pre-``on_session_start`` state.
        name: The engine name string. Defaults to ``"lcm"``.
        status: Override the ``get_status()`` return value. Defaults to
            the standard ABC dict with zeros.
        has_conversation_store: When ``True``, attaches a Mock store so
            the "conversation_store: ready" branch is exercised.
    """
    engine = MagicMock()
    engine.name = name
    engine._db = object() if db_open else None
    engine._conversation_store = MagicMock() if has_conversation_store else None
    engine.get_status.return_value = status or {
        "last_prompt_tokens": 0,
        "threshold_tokens": 0,
        "context_length": 0,
        "usage_percent": 0,
        "compression_count": 0,
    }
    return engine


# ---------------------------------------------------------------------------
# Basic routing — status / help / unknown / empty
# ---------------------------------------------------------------------------


def test_status_subcommand_returns_status_block() -> None:
    """`/lcm status` returns a status block containing engine name and 'ok'."""
    dispatcher = LcmCommandDispatcher(_make_engine())
    out = dispatcher.handle("status")
    assert "[lcm] status" in out
    assert "engine: lcm" in out
    assert "ok" in out


def test_empty_args_aliases_status() -> None:
    """Bare `/lcm` (no args) routes to status per Epic 02 spec."""
    dispatcher = LcmCommandDispatcher(_make_engine())
    out = dispatcher.handle("")
    # Same body as /lcm status
    assert "[lcm] status" in out
    assert "ok" in out


def test_whitespace_only_args_aliases_status() -> None:
    """Pure-whitespace `raw_args` (e.g. `/lcm  `) also aliases to status."""
    dispatcher = LcmCommandDispatcher(_make_engine())
    out = dispatcher.handle("   ")
    assert "[lcm] status" in out


def test_help_subcommand_returns_markdown_table() -> None:
    """`/lcm help` returns a markdown table of all 19 subcommands."""
    dispatcher = LcmCommandDispatcher(_make_engine())
    out = dispatcher.handle("help")
    # Markdown table header
    assert "| Subcommand | Target | Description |" in out
    assert "| --- | --- | --- |" in out
    # The two Epic-02 entries
    assert "/lcm status" in out
    assert "/lcm help" in out
    # A few Epic-08 entries
    assert "/lcm purge" in out
    assert "/lcm doctor" in out
    assert "/lcm worker tick" in out
    assert "/lcm db-backup" in out
    # Epic-09 entries
    assert "/lcm eval" in out
    # Owner-gating note references ADR-013
    assert "ADR-013" in out


def test_help_lists_all_19_subcommands() -> None:
    """Every entry in `_SUBCOMMAND_INVENTORY` shows up in /lcm help."""
    dispatcher = LcmCommandDispatcher(_make_engine())
    out = dispatcher.handle("help")
    for name, _, _ in _SUBCOMMAND_INVENTORY:
        assert f"/lcm {name}" in out, f"missing inventory entry {name!r} in /lcm help output"


def test_unknown_subcommand_returns_not_yet_implemented() -> None:
    """Every non-{status,help} subcommand returns 'not yet implemented (Epic 08)'.

    Per the Epic 02-10 scope, the dispatcher unifies the "known stub"
    and "unknown" paths — Epic 08 splits them when it adds the real
    bodies. At 02-10 both `purge` (inventory) and `nonsense` (not in
    inventory) report the same message: "not yet implemented (Epic 08)".
    """
    dispatcher = LcmCommandDispatcher(_make_engine())
    out = dispatcher.handle("nonsense")
    assert "not yet implemented" in out
    assert "Epic 08" in out
    assert "/lcm nonsense" in out


def test_unknown_cmd_returns_not_yet_implemented() -> None:
    """The user-facing `unknown_cmd` example from the issue spec works.

    Per the Epic 02-10 spec: "/lcm unknown_cmd returns the 'not yet
    implemented' message".
    """
    dispatcher = LcmCommandDispatcher(_make_engine())
    out = dispatcher.handle("unknown_cmd")
    assert "not yet implemented" in out
    assert "Epic 08" in out
    assert "/lcm unknown_cmd" in out


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
        ("reconcile-session-keys", "Epic 08"),
        ("db-backup", "Epic 08"),
        ("db-info", "Epic 08"),
        ("prompts", "Epic 08"),
        ("eval", "Epic 09"),
        ("eval-run", "Epic 09"),
    ],
)
def test_known_subcommand_returns_not_yet_implemented(subcommand: str, expected_epic: str) -> None:
    """Known subcommands stubbed for Epic 08/09 return the standard message."""
    dispatcher = LcmCommandDispatcher(_make_engine())
    out = dispatcher.handle(subcommand)
    assert "not yet implemented" in out
    assert expected_epic in out
    assert f"/lcm {subcommand}" in out


def test_nested_subcommand_returns_not_yet_implemented() -> None:
    """`/lcm doctor apply` routes via 'doctor' first token to the stub.

    At 02-10 the dispatcher does first-token routing only — nested
    subcommands collapse to their parent for the "not yet implemented"
    message. Epic 08 will implement inner-token dispatch inside each
    subcommand body.
    """
    dispatcher = LcmCommandDispatcher(_make_engine())
    out = dispatcher.handle("doctor apply")
    assert "not yet implemented" in out
    assert "Epic 08" in out
    # The dispatcher echoes back the parent token "doctor"
    assert "/lcm doctor" in out


def test_worker_tick_returns_not_yet_implemented() -> None:
    """`/lcm worker tick embedding-backfill` routes via 'worker' parent."""
    dispatcher = LcmCommandDispatcher(_make_engine())
    out = dispatcher.handle("worker tick embedding-backfill")
    assert "not yet implemented" in out
    assert "Epic 08" in out
    assert "/lcm worker" in out


# ---------------------------------------------------------------------------
# Status — engine state surfacing
# ---------------------------------------------------------------------------


def test_status_includes_token_state_from_get_status() -> None:
    """status reads engine.get_status() and surfaces the standard fields."""
    engine = _make_engine(
        status={
            "last_prompt_tokens": 1234,
            "threshold_tokens": 96000,
            "context_length": 128000,
            "usage_percent": 1.0,
            "compression_count": 3,
        }
    )
    dispatcher = LcmCommandDispatcher(engine)
    out = dispatcher.handle("status")
    assert "1234" in out
    assert "96000" in out
    assert "128000" in out
    assert "compression_count: 3" in out


def test_status_reports_db_not_opened_pre_session_start() -> None:
    """Without on_session_start the DB is None; status says so."""
    engine = _make_engine(db_open=False)
    dispatcher = LcmCommandDispatcher(engine)
    out = dispatcher.handle("status")
    assert "not opened" in out


def test_status_reports_db_open_post_session_start() -> None:
    """When _db is set, status says 'open'."""
    engine = _make_engine(db_open=True)
    dispatcher = LcmCommandDispatcher(engine)
    out = dispatcher.handle("status")
    assert "db: open" in out


def test_status_reports_conversation_store_ready() -> None:
    """When _conversation_store is set, status surfaces 'ready'."""
    engine = _make_engine(has_conversation_store=True)
    dispatcher = LcmCommandDispatcher(engine)
    out = dispatcher.handle("status")
    assert "conversation_store: ready" in out


def test_status_handles_get_status_exception() -> None:
    """If engine.get_status() raises, status still returns a useful block."""
    engine = _make_engine()
    engine.get_status.side_effect = RuntimeError("boom")
    dispatcher = LcmCommandDispatcher(engine)
    # Should NOT propagate; should return a degraded but well-formed block.
    out = dispatcher.handle("status")
    assert "[lcm] status" in out
    assert "engine: lcm" in out
    # Token fields default to 0 in the degraded path.
    assert "last_prompt_tokens: 0" in out


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
    assert "[lcm] status" in out
    assert "ok" in out


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


def test_handler_exception_caught_by_dispatcher() -> None:
    """A handler raising mid-flight returns `/lcm <sub> failed: ...` — no crash.

    We monkey-patch the status handler to raise and assert the dispatcher
    converts the exception into a user-visible failure message. This is
    the robustness contract — a buggy subcommand body must not crash
    the chat session.
    """
    dispatcher = LcmCommandDispatcher(_make_engine())

    def _broken_handler(_sub_args: str) -> str:
        raise RuntimeError("intentional test failure")

    # Override the exact-handler table so 'status' raises.
    dispatcher._exact_handlers = lambda: {"status": _broken_handler}  # type: ignore[method-assign]

    out = dispatcher.handle("status")
    assert "/lcm status failed" in out
    assert "intentional test failure" in out


# ---------------------------------------------------------------------------
# Wiring — register() calls ctx.register_command("lcm", ...)
# ---------------------------------------------------------------------------


def test_register_wires_lcm_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """``register(ctx)`` calls ``ctx.register_command("lcm", dispatcher.handle, …)``.

    Mirrors the acceptance criterion from the issue spec: the slash
    command MUST be registered exactly once with the ``lcm`` name and
    a callable handler.
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

    ctx.register_command.assert_called_once()
    call = ctx.register_command.call_args
    # First positional: the command name
    assert call.args[0] == "lcm"
    # Second positional: the bound handler
    handler = call.args[1]
    assert callable(handler)
    # kwargs: args_hint and (optionally) description
    assert call.kwargs.get("args_hint") == "<subcommand>"


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

    handler = ctx.register_command.call_args.args[1]
    # Calling the registered handler returns a real status block — the
    # dispatcher was constructed with the engine instance.
    out = handler("status")
    assert "[lcm] status" in out
    assert "engine: lcm" in out
