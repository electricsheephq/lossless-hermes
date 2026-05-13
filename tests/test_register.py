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


def test_register_does_not_call_register_hook_at_v0(hermes_available: None) -> None:
    """AC line 40: hooks are deferred to Epic 03 — v0 must not register them."""
    ctx = _make_stub_ctx()
    register(ctx)
    ctx.register_hook.assert_not_called()


def test_register_does_not_call_register_command_at_v0(hermes_available: None) -> None:
    """AC line 41: ``/lcm`` lands in Epic 08 — v0 must not register it."""
    ctx = _make_stub_ctx()
    register(ctx)
    ctx.register_command.assert_not_called()


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
