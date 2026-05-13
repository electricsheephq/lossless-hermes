"""Lossless Context Management plugin for hermes-agent.

This module is the Hermes plugin entry point. Operators install this package
into the same Python environment as hermes-agent; Hermes discovers it at
startup by iterating ``importlib.metadata.entry_points(group="hermes_agent.plugins")``
and invoking the ``register`` callable below with a ``PluginContext``.

The ``register`` implementation is intentionally a no-op stub for the
scaffolding milestone. The real engine wiring (context engine + hooks +
``/lcm`` slash command) lands in issue #00-06 — see
``epics/00-scaffolding/issues/00-06-engine-stub-and-registration.md``.

See:

* ``docs/adr/001-plugin-distribution-model.md`` — entry-point distribution
  decision.
* ``docs/adr/024-project-layout.md`` — ``src/lossless_hermes/`` layout.
"""


def register(ctx: object) -> None:
    """Hermes plugin entry point (stub).

    Hermes invokes this callable once at startup with a ``PluginContext``.
    The full registration (context engine, hooks, slash command) is
    implemented in issue #00-06. For now this is a no-op so the entry
    point resolves cleanly and ``pip install -e .`` produces a working
    (but functionally empty) plugin.

    Args:
        ctx: The Hermes ``PluginContext``. Ignored by this stub.
    """
    return None


__all__ = ["register"]
