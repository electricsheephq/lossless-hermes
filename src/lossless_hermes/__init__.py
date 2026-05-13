"""Lossless Context Management plugin for hermes-agent.

This module is the Hermes plugin entry point. Operators install this package
into the same Python environment as hermes-agent; Hermes discovers it at
startup by iterating ``importlib.metadata.entry_points(group="hermes_agent.plugins")``
and invoking the :func:`register` callable below with a ``PluginContext``.

At **issue 02-07** the wiring registers the no-op :class:`LCMEngine`,
the ``/lcm`` slash command (per issue 02-10), AND all four Hermes hooks
that the engine needs to be a real plugin: ``post_llm_call`` (per-turn
ingest seam, ADR-009), ``pre_llm_call`` (recall-policy injection,
ADR-014), ``on_session_end`` (per-turn defense-in-depth, ADR-009
Consequences), and ``subagent_stop`` (forward-compat seam for Epic 06
per ADR-012). The hook bodies themselves are no-op stubs at 02-07 —
Epic 03 (ingest, assemble) and Epic 06 (subagent context-sharing) fill
in the real behavior without touching :func:`register`.

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
* ``docs/adr/009-per-message-ingest.md`` — ``post_llm_call`` as the
  per-turn ingest seam.
* ``docs/adr/010-always-on-assembly-emulation.md`` — ``pre_llm_call``
  as the always-on assembly substitution seam.
* ``docs/adr/012-subagent-context-sharing.md`` — v1 defers subagent
  context-sharing to v2; ``subagent_stop`` hook is a forward-compat seam.
* ``docs/adr/014-recall-policy-injection.md`` — user-message-position
  injection of policy text (preserves prompt cache).
* ``docs/adr/024-project-layout.md`` — ``src/lossless_hermes/`` layout.
* ``docs/reference/hermes-hooks.md`` — VALID_HOOKS table + per-hook
  kwargs and dispatch sites.
* ``epics/02-engine-skeleton/02-07-hook-registrations.md`` — this
  issue's acceptance criteria.
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
    The 02-07 body:

    1. Verifies Hermes is on the import path (defensive against direct-
       invocation in a Hermes-less env per ADR-007 §Consequences).
    2. Loads the operator config from ``~/.hermes/config.yaml`` via
       :func:`lossless_hermes.db.config.load_config`.
    3. Constructs an :class:`LCMEngine` (no DB open, no migrations — those
       land in :meth:`LCMEngine.on_session_start` per ADR-001).
    4. Registers the engine via :meth:`PluginContext.register_context_engine`.
    5. **Registers the four Hermes hooks** the engine needs to operate:
       ``post_llm_call`` (per-turn ingest, ADR-009),
       ``pre_llm_call`` (recall-policy injection, ADR-014),
       ``on_session_end`` (per-turn defense-in-depth, ADR-009 Consequences),
       and ``subagent_stop`` (Epic 06 forward-compat per ADR-012).
    6. Registers the ``/lcm`` slash command (issue 02-10).
    7. Emits an info-level log line for observability.

    The hook bodies are no-op stubs at 02-07 (debug-log + return ``None``
    or ``None``-equivalent). Epic 03 fills in the real ingest /
    assemble paths; Epic 06 fills in the subagent_stop behavior. Per
    ADR-001 §Invariant "the package's top-level
    ``lossless_hermes:register`` callable must remain stable across
    versions" — wiring all four hooks here means Epic 03 / Epic 06
    patches only have to fill the hook bodies, not edit
    :func:`register`.

    Args:
        ctx: The Hermes ``PluginContext`` instance, providing the
            registration methods enumerated in
            ``hermes_cli/plugins.py:287-665``. Methods used at 02-07
            are ``register_context_engine``, ``register_hook`` (×4),
            and ``register_command``.

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

    # Hook registrations (issue 02-07). The four hooks listed in
    # docs/reference/hermes-hooks.md "Where LCM hooks land" table —
    # every hook the engine needs to be a real Hermes plugin. The hook
    # bodies are no-op stubs at 02-07 (debug-log only); Epic 03 / 06
    # fills in the real behavior without touching this register call.
    #
    # Per `hermes_cli/plugins.py:603-618`, ``register_hook(name, cb)``:
    # unknown hook names produce a warning but are still stored
    # (forward-compat); known names from VALID_HOOKS are appended to
    # the per-hook callback list.
    ctx.register_hook("post_llm_call", engine._on_post_llm_call)
    ctx.register_hook("pre_llm_call", engine._on_pre_llm_call)
    ctx.register_hook("on_session_end", engine._on_session_end_hook)
    ctx.register_hook("subagent_stop", engine._on_subagent_stop)

    # Slash command registration (issue 08-01 ships the router with the
    # full 17-subcommand dispatch table; issues 08-02..08-15 fill in the
    # handler bodies). Per ADR-013, owner-gating is upstream of the
    # handler — the dispatcher receives only ``raw_args``.
    #
    # We register two slash command names that point at the same handler
    # closure (``dispatcher.handle``):
    #
    # * ``/lcm``       — the more-typed name, used in operator docs.
    # * ``/lossless``  — alias matching OpenClaw's primary surface name
    #                    (``nativeNames.default: "lossless"`` in the TS).
    #
    # Per plugin-glue.md line 446 Hermes's ``register_command`` doesn't
    # accept aliases natively, so the second registration is the documented
    # workaround. Both names appear in ``hermes plugins list`` and the
    # Telegram menu unless ``gateway/telegram_bot.py:telegram_bot_commands``
    # is patched to hide one — out of scope for this issue.
    from lossless_hermes.plugin import LcmCommandDispatcher

    dispatcher = LcmCommandDispatcher(engine)
    for command_name in ("lcm", "lossless"):
        ctx.register_command(
            command_name,
            dispatcher.handle,
            description="LCM subsystem control (status, help, …)",
            args_hint="<subcommand>",
        )

    # Per ADR-013 §Consequences + plugin-glue.md §"Remaining 5% risk" #4:
    # if running in gateway mode and the operator hasn't set
    # ``allow_admin_from`` for any platform, destructive /lcm subcommands
    # are exposed to every allowed user. Emit a single WARNING-level
    # banner so the misconfiguration is operator-visible.
    _maybe_warn_unguarded_destructive_commands()

    _log.info(
        "lossless-hermes plugin loaded (engine=%s, hooks=4, /lcm + /lossless registered)",
        engine.name,
    )


def _maybe_warn_unguarded_destructive_commands() -> None:
    """Emit a startup WARNING if gateway mode + no ``allow_admin_from``.

    Per ADR-013 §Consequences and plugin-glue.md §"Remaining 5% risk"
    item #4: a misconfigured ``allow_admin_from`` (empty / unset) leaves
    destructive /lcm subcommands open to any DM-allowed user. The warning
    is the operator-noticeable signal.

    The check inspects the loaded gateway config (if any). When the
    config is absent (CLI-only mode), this is a no-op — CLI is implicitly
    single-user-owner per ADR-013 §Consequences.

    Implementation note: Hermes's ``cfg_get`` reads the *plugin* config;
    gateway config lives in a separate file (``gateway/run.py:944
    _load_gateway_config``). We attempt the lookup but tolerate failure
    so this never blocks plugin load.
    """
    try:
        from gateway.config import load_gateway_config  # type: ignore[import-not-found]
    except ImportError:
        # CLI-only Hermes install — no gateway code on path. Safe default:
        # don't warn (CLI doesn't expose multi-user surface).
        return

    try:
        gateway_config = load_gateway_config()
    except Exception:  # noqa: BLE001 — defensive against unloadable config
        return

    platforms = getattr(gateway_config, "platforms", None) or {}
    if not platforms:
        # No platforms configured → no multi-user exposure. Skip warning.
        return

    unguarded = []
    for platform_name, pcfg in platforms.items():
        extra = getattr(pcfg, "extra", None) or {}
        if not isinstance(extra, dict):
            continue
        if not extra.get("allow_admin_from") and not extra.get("group_allow_admin_from"):
            unguarded.append(platform_name)

    if unguarded:
        _log.warning(
            "[lcm] WARNING: destructive /lcm subcommands (purge, doctor apply, "
            "doctor clean, reconcile-session-keys, worker tick, eval, "
            "import-openclaw) are exposed to all users on platforms %s — "
            "set allow_admin_from in ~/.hermes/config.yaml to restrict. "
            "See ADR-013 §Consequences.",
            unguarded,
        )
