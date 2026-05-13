"""Lossless Context Management plugin for hermes-agent.

This module is the Hermes plugin entry point. Operators install this package
into the same Python environment as hermes-agent; Hermes discovers it at
startup by iterating ``importlib.metadata.entry_points(group="hermes_agent.plugins")``
and invoking the :func:`register` callable below with a ``PluginContext``.

The v0 wiring registers the no-op :class:`LCMEngine` only. The Hermes
hooks (``pre_llm_call`` / ``post_llm_call``) and the ``/lcm`` slash
command are deferred to later epics — see the inline ``TODO(epic-03)``
and ``TODO(epic-08)`` markers below.

### Distribution model

The :func:`register` callable is the entry-point binding declared in
``pyproject.toml`` (``[project.entry-points."hermes_agent.plugins"]
lossless-hermes = "lossless_hermes:register"``). The Hermes plugin loader
imports this module and calls :func:`register(ctx)` exactly once at
startup. Per ADR-001 §Invariant: "the package's top-level
``lossless_hermes:register`` callable must remain stable across
versions — it is the entry-point binding."

### Startup health-check (ADR-007 §Consequences)

When Hermes is **not** importable in the environment (the package was
installed in the wrong Python env, or Hermes is missing), the
``hermes_bridge`` module reports ``HERMES_AVAILABLE = False`` and
re-exports stubs. :func:`register` catches the resulting failure path
and emits a structured :class:`LosslessHermesEnvironmentError` so the
operator sees a clear, actionable message instead of an obscure
``ImportError`` traceback. Per ADR-007 §Consequences, this is the
startup health-check the plugin owes the user.

See:

* ``docs/adr/001-plugin-distribution-model.md`` — entry-point
  distribution decision and the ``register(ctx)`` contract.
* ``docs/adr/007-hermes-as-dependency.md`` — startup health-check
  rationale.
* ``docs/adr/024-project-layout.md`` — ``src/lossless_hermes/`` layout.
* ``docs/reference/hermes-hooks.md`` lines 256-326 — full worked example
  of ``register()`` plus the ``ContextEngine`` hook landing table.
* ``epics/00-scaffolding/issues/00-06-noop-engine.md`` — this issue's
  acceptance criteria.
"""

from __future__ import annotations

import logging
from typing import Any

from lossless_hermes.db.config import load_config
from lossless_hermes.engine import LCMEngine
from lossless_hermes.hermes_bridge import (
    HERMES_AVAILABLE,
    LosslessHermesEnvironmentError,
)

__all__ = ["register"]

_log = logging.getLogger("lossless_hermes")


def register(ctx: Any) -> None:
    """Hermes plugin entry point.

    Hermes invokes this callable once at startup with a ``PluginContext``.
    The v0 body:

    1. Verifies Hermes is on the import path (defensive against direct-
       invocation in a Hermes-less env per ADR-007 §Consequences).
    2. Loads the operator config from ``~/.hermes/config.yaml`` via
       :func:`lossless_hermes.db.config.load_config`.
    3. Constructs an :class:`LCMEngine` (no DB open, no migrations — those
       land in :meth:`LCMEngine.on_session_start` per ADR-001).
    4. Registers the engine via :meth:`PluginContext.register_context_engine`.
    5. Emits an info-level log line for observability.

    What is **not** registered at v0:

    * ``pre_llm_call`` / ``post_llm_call`` hooks (TODO epic-03 — the
      ingest + always-on-assembly seams land there per ADR-009 + ADR-010).
    * The ``/lcm`` slash command (TODO epic-08 — the 25 subcommands all
      land then).

    Args:
        ctx: The Hermes ``PluginContext`` instance, providing the
            registration methods enumerated in
            ``hermes_cli/plugins.py:287-665``. The only method used at
            v0 is ``register_context_engine``.

    Raises:
        LosslessHermesEnvironmentError: Hermes is not on the import
            path. The error message points the operator to the install
            docs. Per ADR-001 §Open Questions "Plugin discovery silently
            skips on import error" — without this guard the user sees a
            silent no-op, not an actionable failure.
    """
    # ADR-007 §Consequences "Startup health-check": fail loudly with an
    # actionable message if Hermes is missing. The bridge's
    # ``HERMES_AVAILABLE`` flag is the source of truth.
    if not HERMES_AVAILABLE:
        raise LosslessHermesEnvironmentError(
            "lossless-hermes is installed in an environment without "
            "hermes-agent on the import path. Install Hermes first — see "
            "https://github.com/NousResearch/hermes-agent#install — then "
            "`pip install lossless-hermes` into the same Python environment."
        )

    # Heavy init is forbidden in ``register()`` per ADR-001 §Consequences
    # ("Heavy init … belongs in ContextEngine.on_session_start"). Config
    # loading is the one exception — it's a single YAML file parse and
    # the validation result is part of the startup contract.
    config = load_config()

    # ``hermes_home`` resolves via the bridge re-export. We import lazily
    # so the function is testable without Hermes installed (the test
    # patches ``get_hermes_home`` or skips this block via a stub
    # ``PluginContext``).
    from lossless_hermes.hermes_bridge import get_hermes_home

    engine = LCMEngine(hermes_home=get_hermes_home(), config=config)
    ctx.register_context_engine(engine)

    # TODO(epic-03): wire ``pre_llm_call`` and ``post_llm_call`` hooks
    # once ``LCMEngine._on_pre_llm_call`` and ``_on_post_llm_call`` land.
    # ctx.register_hook("pre_llm_call", engine._on_pre_llm_call)
    # ctx.register_hook("post_llm_call", engine._on_post_llm_call)

    # TODO(epic-08): wire the ``/lcm`` slash command once the dispatcher
    # ports. The 25 subcommands all land in that epic.
    # ctx.register_command("lcm", lcm_dispatcher, description="LCM operator command")

    _log.info("lossless-hermes plugin loaded as no-op (engine=%s)", engine.name)
