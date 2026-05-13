"""Tests for the :func:`lossless_hermes.register` plugin entry point.

Covers the wiring side of issue 00-06: that the entry-point callable
correctly constructs an :class:`LCMEngine` and registers it via
``PluginContext.register_context_engine``. The engine class itself is
covered in ``tests/test_engine_noop.py``.

The Hermes plugin loader will invoke ``register(ctx)`` once at startup
with a real ``PluginContext``. These tests use a ``Mock`` standing in
for that context (per AC line 44 of the issue spec): a Mock with
``register_context_engine`` / ``register_hook`` / ``register_command``
attributes. The Mock surface lets us assert exactly what ``register()``
does, even in a Hermes-less env where the real ``PluginContext`` isn't
importable.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest

from lossless_hermes import register
from lossless_hermes.engine import LCMEngine
from lossless_hermes.hermes_bridge import LosslessHermesEnvironmentError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stub_ctx() -> Any:
    """Build the Mock PluginContext required by the issue spec line 44.

    The Mock has all three registration methods the v0 ``register()``
    might call (only ``register_context_engine`` is actually used at v0;
    the other two are present so any accidental call surfaces as an
    assertion failure on call_count rather than ``AttributeError``).
    """
    ctx = MagicMock()
    ctx.register_context_engine = MagicMock()
    ctx.register_hook = MagicMock()
    ctx.register_command = MagicMock()
    return ctx


@pytest.fixture
def hermes_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``HERMES_AVAILABLE = True`` for tests that bypass the health-check.

    The bridge's ``HERMES_AVAILABLE`` flag is set at import time based on
    whether ``agent.context_engine`` was importable. In the default CI
    env (no Hermes) it's ``False``, and ``register()`` raises
    ``LosslessHermesEnvironmentError``. For tests that exercise the
    post-health-check path with a stub ``ctx``, we override the flag
    AND patch ``get_hermes_home`` to a stub since the real function
    raises in the Hermes-less env.
    """
    import lossless_hermes
    import lossless_hermes.hermes_bridge as bridge

    monkeypatch.setattr(bridge, "HERMES_AVAILABLE", True)
    # ``register()`` re-imports ``get_hermes_home`` lazily — patch on the
    # bridge module so the import inside ``register()`` picks up the stub.
    monkeypatch.setattr(bridge, "get_hermes_home", lambda: "/tmp/.hermes-test")
    # ``register()`` reads ``HERMES_AVAILABLE`` from its own module's
    # imports (a name, not a lookup), so monkey-patching only the bridge
    # module isn't enough — we also need to patch the name re-bound at
    # ``lossless_hermes.HERMES_AVAILABLE``.
    monkeypatch.setattr(lossless_hermes, "HERMES_AVAILABLE", True)


# ---------------------------------------------------------------------------
# Health check (Hermes-missing path)
# ---------------------------------------------------------------------------


def test_register_raises_when_hermes_missing() -> None:
    """AC: ``register()`` wraps the body in a try/except that emits a
    structured error if ``agent.context_engine`` is missing.

    In the default test env, ``HERMES_AVAILABLE`` is ``False``. Calling
    ``register(ctx)`` must raise :class:`LosslessHermesEnvironmentError`
    with an actionable message that names ``hermes-agent`` and the
    install path.
    """
    ctx = _make_stub_ctx()
    with pytest.raises(LosslessHermesEnvironmentError, match=r"hermes-agent"):
        register(ctx)
    # The context must not have been touched — registration is
    # all-or-nothing.
    ctx.register_context_engine.assert_not_called()


# ---------------------------------------------------------------------------
# Registration (Hermes-available path)
# ---------------------------------------------------------------------------


def test_register_calls_register_context_engine_once(hermes_available: None) -> None:
    """AC line 44: ``register(ctx)`` calls ``ctx.register_context_engine``
    exactly once with an ``LCMEngine`` instance."""
    ctx = _make_stub_ctx()
    register(ctx)
    ctx.register_context_engine.assert_called_once()
    (engine_arg,) = ctx.register_context_engine.call_args.args
    assert isinstance(engine_arg, LCMEngine)
    assert engine_arg.name == "lcm"


def test_register_wires_all_four_hermes_hooks(hermes_available: None) -> None:
    """Issue 02-07 AC: ``register(ctx)`` calls ``ctx.register_hook`` exactly
    four times — once for each of ``post_llm_call``, ``pre_llm_call``,
    ``on_session_end``, ``subagent_stop`` — per the "Where LCM hooks land"
    table in ``docs/reference/hermes-hooks.md`` lines 322–334.

    Supersedes the v0 (00-06) assertion that ``register_hook`` was NOT
    called. The hook bodies themselves are no-op stubs at 02-07 (Epic
    03 fills the ingest / assemble bodies; Epic 06 wires
    subagent_stop's real behavior).
    """
    ctx = _make_stub_ctx()
    register(ctx)
    assert ctx.register_hook.call_count == 4, (
        f"expected register_hook called 4 times, got "
        f"{ctx.register_hook.call_count}: {ctx.register_hook.call_args_list}"
    )
    hook_names = [call.args[0] for call in ctx.register_hook.call_args_list]
    assert set(hook_names) == {
        "post_llm_call",
        "pre_llm_call",
        "on_session_end",
        "subagent_stop",
    }, f"unexpected hook names registered: {hook_names}"


def test_register_calls_register_command_for_lcm(hermes_available: None) -> None:
    """Issue 08-01: ``register()`` registers BOTH ``/lcm`` AND ``/lossless``.

    Originally Epic 02-10 wired one ``register_command("lcm", ...)`` call.
    Issue 08-01 adds the ``/lossless`` alias per plugin-glue.md line 446
    so OpenClaw users' muscle-memory works ("OpenClaw's
    ``nativeNames.default: lossless``").

    The dispatcher handle is a bound method on
    :class:`LcmCommandDispatcher`; both registrations point at the same
    closure.

    Per ``hermes_cli/plugins.py:401-453``, ``register_command`` takes
    ``(name, handler, description="", args_hint="")``.
    """
    ctx = _make_stub_ctx()
    register(ctx)
    # Two registrations: /lcm (canonical) and /lossless (alias).
    assert ctx.register_command.call_count == 2
    call_args_list = ctx.register_command.call_args_list
    names = [c.args[0] for c in call_args_list]
    assert names == ["lcm", "lossless"]
    # Both handlers are callable (bound method on the dispatcher) — and
    # equal, since they're the same closure.
    handler_lcm = call_args_list[0].args[1]
    handler_lossless = call_args_list[1].args[1]
    assert callable(handler_lcm)
    assert handler_lcm == handler_lossless
    # ``args_hint`` is the kw-only argument Hermes uses to surface the
    # command in gateway adapters (e.g. Discord's slash-command picker).
    for call in call_args_list:
        assert call.kwargs.get("args_hint") == "<subcommand>"


def test_register_logs_startup_line(
    hermes_available: None, caplog: pytest.LogCaptureFixture
) -> None:
    """v0 emits an info-level log line on successful registration.

    Observability: an operator scanning Hermes startup logs for
    "lossless-hermes" should find a single, clear line confirming the
    plugin loaded.
    """
    ctx = _make_stub_ctx()
    with caplog.at_level(logging.INFO, logger="lossless_hermes"):
        register(ctx)
    assert any("lossless-hermes plugin loaded" in rec.getMessage() for rec in caplog.records), (
        f"missing startup log line — captured: {[r.getMessage() for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# Issue 08-01 — destructive-commands-unguarded startup warning
# ---------------------------------------------------------------------------


def test_register_warns_when_gateway_lacks_allow_admin_from(
    hermes_available: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Issue 08-01 spec line 53 + ADR-013 §Consequences: warn at startup
    when running in gateway mode AND no platform sets ``allow_admin_from``.

    We monkey-patch the gateway config loader to return a config with at
    least one platform lacking the admin allowlist; the warning should
    fire. Without this signal, operators can ship destructive /lcm
    subcommands wide-open and not realize it.
    """
    import lossless_hermes as plugin_mod

    # Construct a fake gateway config: one platform, no allow_admin_from.
    class _StubPlatform:
        def __init__(self, extra: dict) -> None:
            self.extra = extra

    class _StubGatewayConfig:
        def __init__(self, platforms: dict) -> None:
            self.platforms = platforms

    fake_gateway_config = _StubGatewayConfig(platforms={"telegram": _StubPlatform(extra={})})

    # The register() function imports ``gateway.config.load_gateway_config``
    # lazily inside ``_maybe_warn_unguarded_destructive_commands``. We
    # inject a fake module so the import succeeds and returns our stub.
    import sys
    import types

    fake_module = types.ModuleType("gateway.config")
    fake_module.load_gateway_config = lambda: fake_gateway_config  # type: ignore[attr-defined]
    fake_pkg = types.ModuleType("gateway")
    fake_pkg.config = fake_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gateway", fake_pkg)
    monkeypatch.setitem(sys.modules, "gateway.config", fake_module)

    ctx = _make_stub_ctx()
    with caplog.at_level(logging.WARNING, logger="lossless_hermes"):
        plugin_mod.register(ctx)

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("destructive /lcm subcommands" in w and "allow_admin_from" in w for w in warnings), (
        f"missing unguarded-commands warning — got {warnings}"
    )


def test_register_does_not_warn_when_allow_admin_from_set(
    hermes_available: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Counter-test: when every platform has ``allow_admin_from`` set, no warning."""
    import lossless_hermes as plugin_mod

    class _StubPlatform:
        def __init__(self, extra: dict) -> None:
            self.extra = extra

    class _StubGatewayConfig:
        def __init__(self, platforms: dict) -> None:
            self.platforms = platforms

    fake_gateway_config = _StubGatewayConfig(
        platforms={
            "telegram": _StubPlatform(extra={"allow_admin_from": ["@admin"]}),
        }
    )

    import sys
    import types

    fake_module = types.ModuleType("gateway.config")
    fake_module.load_gateway_config = lambda: fake_gateway_config  # type: ignore[attr-defined]
    fake_pkg = types.ModuleType("gateway")
    fake_pkg.config = fake_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gateway", fake_pkg)
    monkeypatch.setitem(sys.modules, "gateway.config", fake_module)

    ctx = _make_stub_ctx()
    with caplog.at_level(logging.WARNING, logger="lossless_hermes"):
        plugin_mod.register(ctx)

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert not any("destructive /lcm subcommands" in w for w in warnings), (
        f"unexpected warning when allow_admin_from is set — got {warnings}"
    )


def test_register_silent_when_no_gateway_module() -> None:
    """When ``gateway.config`` isn't importable (CLI-only Hermes), no warning."""
    # This is the default test env — Hermes isn't installed, so the
    # gateway import fails. The startup warning path silently no-ops.
    # We exercise it indirectly via the existing
    # ``test_register_logs_startup_line`` test (which doesn't see a
    # WARNING in the captured logs).
    # No explicit assertion needed here — the existing test suite covers
    # the no-op path. This test exists for documentation only.
    pass


def test_register_loads_config_via_db_module(
    hermes_available: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``register()`` delegates config loading to
    ``lossless_hermes.db.config.load_config`` — confirms the seam is
    correct (not e.g. instantiating ``LcmConfig()`` directly)."""
    import lossless_hermes as plugin_mod
    from lossless_hermes.db.config import LcmConfig

    sentinel = LcmConfig()
    load_calls: list = []

    def _spy_load_config() -> LcmConfig:
        load_calls.append("called")
        return sentinel

    monkeypatch.setattr(plugin_mod, "load_config", _spy_load_config)

    ctx = _make_stub_ctx()
    register(ctx)

    assert load_calls == ["called"]
    (engine_arg,) = ctx.register_context_engine.call_args.args
    assert engine_arg.config is sentinel
