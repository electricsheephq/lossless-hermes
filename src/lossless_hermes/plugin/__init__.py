"""Hermes plugin glue — slash command dispatcher and (future) hook adapters.

This package hosts the Hermes-facing plugin glue: the ``/lcm`` slash command
dispatcher (:mod:`lossless_hermes.plugin.commands`) and, in later epics, the
``pre_llm_call`` / ``post_llm_call`` hook adapters.

The boundary between :mod:`lossless_hermes.engine` and this package is
deliberate: engine code is Hermes-agnostic (it only depends on the
:class:`ContextEngine` ABC), while everything under :mod:`lossless_hermes.plugin`
is the "translation layer" between Hermes's plugin API and engine methods.
ADR-024 §"Project layout" pins this split.

At issue 02-10 only the slash command dispatcher lives here. Epic 03 adds
hook adapters; Epic 08 fills in real subcommand bodies.

See:

* ``docs/adr/013-owner-gating.md`` — handlers receive only ``raw_args``;
  owner-gating is upstream via ``gateway/slash_access``.
* ``docs/adr/024-project-layout.md`` — package layout decisions.
* ``docs/reference/hermes-hooks.md`` — Hermes plugin SDK surface.
* ``epics/02-engine-skeleton/02-10-slash-command-dispatcher.md`` — this issue.
"""

from __future__ import annotations

from lossless_hermes.plugin.commands import LcmCommandDispatcher

__all__ = ["LcmCommandDispatcher"]
