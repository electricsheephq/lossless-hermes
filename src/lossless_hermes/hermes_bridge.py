"""Hermes plugin-SDK seam — the only file in lossless-hermes that imports
from Hermes's namespace.

Replaces ``src/openclaw-bridge.ts`` from lossless-claw (26 LOC, DROPPED — see
``docs/reference/lcm-source-map.md`` line 216). Every other module in this
package MUST import Hermes-side symbols via this bridge, so future Hermes
ABC churn touches one file, not 50 (ADR-024 §"Hermes bridge", lines 171-173).

Imports are guarded with a structured fallback: ``import lossless_hermes``
succeeds in a Hermes-less env, but the first call into any re-exported
symbol — and ``register()`` itself — raises
:class:`LosslessHermesEnvironmentError` with an actionable message. This
matches ADR-007 §Consequences "Startup health-check required" and
ADR-007 §Decision "Hermes is host-installed, not pinned".

See:
* ADR-024 §"Hermes bridge" — why this file exists.
* ADR-007 §Decision — why Hermes is not in ``pyproject.toml`` deps.
* ADR-001 — entry-point distribution model.
* ``docs/reference/hermes-hooks.md`` — canonical list of what Hermes exposes.
"""

from __future__ import annotations

from typing import Any, Dict, List


class LosslessHermesEnvironmentError(ImportError):
    """Raised when lossless-hermes is loaded without hermes-agent on the path."""


# Hermes has no named ``AgentMessage`` type; messages are plain ``Dict[str, Any]``
# in OpenAI chat-completions shape (verified 2026-05-13 against
# /Volumes/LEXAR/Claude/hermes-agent/agent/context_engine.py). We name it so
# lossless-hermes call sites can document intent.
AgentMessage = Dict[str, Any]
AgentMessages = List[AgentMessage]

_MISSING_HERMES_MSG = (
    "lossless-hermes was loaded in an environment without hermes-agent on the "
    "import path. Install Hermes first — see "
    "https://github.com/NousResearch/hermes-agent#install — then "
    "`pip install lossless-hermes` into the same Python environment."
)

try:
    from agent.context_engine import ContextEngine  # type: ignore[import-not-found]
    from hermes_cli.config import cfg_get, load_config  # type: ignore[import-not-found]
    from hermes_cli.plugins import PluginContext  # type: ignore[import-not-found]
    from hermes_constants import get_hermes_home  # type: ignore[import-not-found]

    HERMES_AVAILABLE = True
except ImportError as _exc:  # pragma: no cover — covered by subprocess test
    HERMES_AVAILABLE = False
    # Capture the underlying ImportError in a stable name. The ``except`` clause's
    # ``_exc`` binding is cleared by Python when the block ends, so closures that
    # reference it would see NameError at call time.
    _IMPORT_CAUSE: ImportError = _exc

    def _missing(*_: Any, **__: Any) -> Any:
        raise LosslessHermesEnvironmentError(_MISSING_HERMES_MSG) from _IMPORT_CAUSE

    # Stubs: any access raises. Used as classes for isinstance compatibility.
    ContextEngine = type("ContextEngine", (), {"__init__": _missing})
    PluginContext = type("PluginContext", (), {"__init__": _missing})
    load_config = _missing
    cfg_get = _missing
    get_hermes_home = _missing


__all__ = [
    "AgentMessage",
    "AgentMessages",
    "ContextEngine",
    "HERMES_AVAILABLE",
    "LosslessHermesEnvironmentError",
    "PluginContext",
    "cfg_get",
    "get_hermes_home",
    "load_config",
]
