"""Smoke tests for ``lossless_hermes.hermes_bridge``.

Covers:
1. The Hermes-available path — re-exports work and resolve to the live
   Hermes ABC / config functions (skipped via :func:`pytest.importorskip`
   when Hermes is not installed in the test env, per the issue spec's
   acceptance criterion).
2. The Hermes-missing fallback — the bridge imports cleanly even when
   Hermes is unavailable, but calling any re-exported symbol raises
   :class:`LosslessHermesEnvironmentError`.

The fallback case is exercised by re-importing the bridge in a subprocess
with the ``agent`` / ``hermes_cli`` / ``hermes_constants`` modules masked
in ``sys.modules`` so the ``try/except ImportError`` block fires.

See ``epics/00-scaffolding/issues/00-05-hermes-bridge-stub.md`` for the
full acceptance list and ADR-007 for the import-guard rationale.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest


def test_bridge_module_importable() -> None:
    """The bridge module always imports — no Hermes dependency at import time."""
    import lossless_hermes.hermes_bridge as bridge

    # The public surface advertised in __all__ must be present even when
    # Hermes is unavailable (they would be stubs in that case).
    for name in (
        "AgentMessage",
        "AgentMessages",
        "ContextEngine",
        "HERMES_AVAILABLE",
        "LosslessHermesEnvironmentError",
        "PluginContext",
        "cfg_get",
        "get_hermes_home",
        "load_config",
    ):
        assert hasattr(bridge, name), f"hermes_bridge is missing {name!r}"


def test_re_exports_match_hermes_when_available() -> None:
    """When Hermes is on the path, the re-exports must be the real symbols."""
    pytest.importorskip(
        "agent.context_engine",
        reason="hermes-agent not on PYTHONPATH — fallback covered by separate test",
    )

    from agent.context_engine import ContextEngine as HermesContextEngine
    from hermes_cli.config import cfg_get as hermes_cfg_get
    from hermes_cli.config import load_config as hermes_load_config
    from hermes_cli.plugins import PluginContext as HermesPluginContext
    from hermes_constants import get_hermes_home as hermes_get_hermes_home

    from lossless_hermes import hermes_bridge

    assert hermes_bridge.HERMES_AVAILABLE is True
    assert hermes_bridge.ContextEngine is HermesContextEngine
    assert hermes_bridge.PluginContext is HermesPluginContext
    assert hermes_bridge.load_config is hermes_load_config
    assert hermes_bridge.cfg_get is hermes_cfg_get
    assert hermes_bridge.get_hermes_home is hermes_get_hermes_home
    # Sanity — name lookups are not Any-typed
    assert hermes_bridge.ContextEngine.__name__ == "ContextEngine"


def test_fallback_raises_when_hermes_missing() -> None:
    """In a Hermes-less env, the module imports but stubs raise on use.

    Exercised in a subprocess so we don't pollute the parent interpreter's
    ``sys.modules``. The child masks every hermes-side top-level package
    with ``None`` before importing the bridge — that forces the
    ``except ImportError`` branch.
    """
    script = textwrap.dedent(
        """
        import sys
        # Mask hermes-agent submodules so the bridge's try/except ImportError fires.
        for mod in ("agent", "agent.context_engine", "hermes_cli",
                    "hermes_cli.config", "hermes_cli.plugins", "hermes_constants"):
            sys.modules[mod] = None

        from lossless_hermes.hermes_bridge import (
            ContextEngine,
            HERMES_AVAILABLE,
            LosslessHermesEnvironmentError,
            cfg_get,
            get_hermes_home,
            load_config,
        )

        assert HERMES_AVAILABLE is False, "expected HERMES_AVAILABLE=False"

        failures = []
        # Each stub must raise LosslessHermesEnvironmentError on use.
        for label, call in (
            ("ContextEngine()", lambda: ContextEngine()),
            ("load_config()", lambda: load_config()),
            ("cfg_get({}, 'x')", lambda: cfg_get({}, "x")),
            ("get_hermes_home()", lambda: get_hermes_home()),
        ):
            try:
                call()
            except LosslessHermesEnvironmentError:
                pass
            except Exception as exc:
                failures.append(f"{label}: wrong exception {type(exc).__name__}: {exc}")
            else:
                failures.append(f"{label}: did not raise")

        if failures:
            for f in failures:
                print("FAIL:", f)
            sys.exit(1)
        print("OK")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"subprocess failed (rc={result.returncode}):\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "OK" in result.stdout
