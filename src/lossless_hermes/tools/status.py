"""``lcm_status`` — read-only, model-callable LCM health snapshot tool.

Per [ADR-035](../../docs/adr/035-lcm-status-doctor-model-tools.md), this
module exposes the ``/lcm status`` health surface as a **model-callable
agent tool** so the model can self-diagnose mid-turn — observe context
pressure, global counts, and the current-conversation snapshot directly
after a weird recall miss, instead of inferring LCM's state from
symptoms.

### What it wraps — and what it does NOT add

The handler is a **thin wrapper**. It delegates the entire health
snapshot to :func:`lossless_hermes.commands.status.run` — the exact same
function the ``/lcm status`` slash handler calls. ADR-035 §Consequences
makes this an invariant:

> The tool handlers add **no** diagnostic logic of their own. They
> delegate to the existing command bodies.

No new SQL, no new probes, no new render path *for the data*. The tool
diverges from the slash command at exactly one point: **output size**.

### The output cap (ADR-035 mandatory caveat)

The slash variant renders an unbounded multi-section markdown report —
an operator's terminal can take it. A tool result lands in the model's
context window and is paid for in tokens. ADR-035 §Consequences makes
capping mandatory:

> The **tool** variant MUST cap or summarize its output to a bounded
> size so a diagnostic call does not blow the turn's tool-result
> budget.

:func:`handle_lcm_status` renders the full status text via
:func:`commands.status.run`, then passes it through
:func:`lossless_hermes.tools._diagnostics.cap_diagnostic_text` — capping
to :data:`~lossless_hermes.tools._diagnostics.DIAGNOSTIC_TOOL_OUTPUT_CHAR_CAP`
(~1.5K tokens, the ADR §"Open questions" row-1 starting point) with a
tail pointing the operator at the uncapped ``/lcm status`` slash
command. ``commands.status.run``'s output is dominated by the
highest-signal sections (Plugin / Global / Current conversation /
Maintenance); the cap keeps them and trims any overflow at a clean line
boundary.

### Read-only ⇒ no owner gate

A status snapshot mutates nothing. Per [ADR-013](../../docs/adr/013-owner-gating.md)
the owner gate exists for *destructive* ``/lcm`` subcommands
(``doctor apply`` / ``purge`` / ``reconcile``); it does not participate
here. ``handle_lcm_status`` runs unconditionally — there is no policy
probe.

### Handler signature

Per the dispatch contract (``docs/porting-guides/tools.md`` §"dispatch
table" / :data:`lossless_hermes.engine.TOOL_DISPATCH`), every handler is
called ``handler(args, **kwargs)`` and returns a JSON string. ``args``
is empty for this tool (:data:`LCM_STATUS_SCHEMA` declares no
parameters). The handler reads the engine off the ``ctx`` kwarg — the
same :class:`~lossless_hermes.engine.LCMEngine` the slash dispatcher
attaches to ``parsed.engine``.

See:

* [ADR-035](../../docs/adr/035-lcm-status-doctor-model-tools.md) — the
  decision record.
* ``commands/status.py`` — the wrapped command body.
* ``tools/_diagnostics.py`` — the shared output-cap helper.
* Issue [electricsheephq/lossless-hermes#135].
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, Final, Optional

from lossless_hermes.commands.status import run as _run_status
from lossless_hermes.tools import TOOL_SCHEMAS
from lossless_hermes.tools._common import tool_result
from lossless_hermes.tools._diagnostics import cap_diagnostic_text
from lossless_hermes.tools._typebox import object_schema, tool_schema

logger = logging.getLogger("lossless_hermes.tools.status")

__all__ = (
    "LCM_STATUS_DESCRIPTION",
    "LCM_STATUS_SCHEMA",
    "handle_lcm_status",
)


# ===========================================================================
# Schema — OpenAI function-call format
# ===========================================================================
#
# ADR-016 §Consequences requires tool-schema description prose to be
# verbatim from the TS source. ``lcm_status`` has NO TS source — it is a
# NEW tool introduced by ADR-035, not a port of an ``lcm-*-tool.ts``
# factory. So this description is original prose, written (per ADR-035
# §"Open questions" row 3) to make clear this is a *read-only
# self-diagnosis* tool — the model should reach for it after a recall
# miss, not at random.

LCM_STATUS_DESCRIPTION: Final[str] = (
    "Read-only self-diagnosis: snapshot LCM's own health for THIS conversation. "
    "Call this after a weird recall miss or a summary that reads thin, to tell "
    "apart 'my prompt is the problem' from 'LCM handed me a degraded context'. "
    "Returns a compact report: plugin/db config, global summary counts, the "
    "current conversation's message/summary/token counts, context compression "
    "ratio, and maintenance/cache state. Mutates nothing — no DB writes, no "
    "compaction, no owner permission needed. Output is capped to ~1.5K tokens; "
    "for the full uncapped report an operator can run the /lcm status slash "
    "command. Use lcm_doctor (the read-only integrity scan) when you suspect "
    "broken or orphaned summaries rather than context pressure."
)
"""Model-facing prose for ``lcm_status``. ADR-035 introduces this tool —
it has no TS source, so this is original prose (not a verbatim port).
Written per ADR-035 §"Open questions" row 3 to steer the model to reach
for it as a *read-only self-diagnosis* tool."""


LCM_STATUS_SCHEMA: Final[dict[str, Any]] = tool_schema(
    name="lcm_status",
    description=LCM_STATUS_DESCRIPTION,
    # ADR-035 §Consequences: "Both schemas take no parameters." A status
    # snapshot operates on the engine's current conversation + DB — there
    # is nothing for the model to supply. ``object_schema()`` with no
    # kwargs yields ``{"type": "object", "properties": {}, "required": []}``.
    parameters=object_schema(),
)
"""OpenAI-function-call schema for ``lcm_status``. Empty-parameter object
per ADR-035 §Consequences — the tool needs no model-supplied input."""


# Register at module import time per the TOOL_SCHEMAS contract documented
# in tools/__init__.py. tools/__init__.py imports this module AFTER
# TOOL_SCHEMAS is defined, so the append target exists.
TOOL_SCHEMAS.append(LCM_STATUS_SCHEMA)


# ===========================================================================
# Handler
# ===========================================================================


def handle_lcm_status(
    args: dict[str, Any],
    *,
    ctx: Optional[Any] = None,
    **_kwargs: Any,
) -> str:
    """Handle an ``lcm_status`` tool call — read-only LCM health snapshot.

    Delegates the entire snapshot to
    :func:`lossless_hermes.commands.status.run` (the ``/lcm status``
    command body), then caps the rendered text to the tool-result
    budget. Adds no diagnostic logic — see the module docstring's
    "what it wraps" section and ADR-035's delegation invariant.

    ``lcm_status`` is **not** owner-gated (ADR-013 gates destructive
    subcommands only; a snapshot mutates nothing) and is **not** in
    :data:`lossless_hermes.plugin.needs_compact_gate.TOKEN_GATE_TOOLS`
    (a diagnostic must stay callable when context is near-full — that
    is precisely when the model needs to self-diagnose).

    Args:
        args: The tool-call ``arguments`` dict. ``lcm_status`` declares
            no parameters (:data:`LCM_STATUS_SCHEMA`), so this is
            expected to be empty; any keys are ignored.
        ctx: The :class:`~lossless_hermes.engine.LCMEngine` (passed by
            the dispatch layer as the ``ctx`` kwarg). When ``None`` —
            the plugin is still booting and no engine is wired — the
            handler returns a structured ``engine-unavailable`` payload
            rather than raising.
        **_kwargs: Other dispatch kwargs (``session_key``,
            ``runtime_context``, ...) — unused; a status snapshot reads
            only the engine.

    Returns:
        A JSON string (per :func:`tool_result`). The inner payload is:

        * ``{"ok": False, "reason": "engine-unavailable", "note": ...}``
          when ``ctx`` is ``None``.
        * ``{"ok": True, "report": <capped status text>, "capped": <bool>}``
          on success. ``report`` is the rendered ``/lcm status`` text
          capped to the tool-result budget; ``capped`` is ``True`` when
          truncation occurred (the operator can run ``/lcm status`` for
          the uncapped version).
        * ``{"ok": False, "reason": "exception", "note": ...}`` if the
          delegated command body raises unexpectedly (it is written not
          to, but the handler must never propagate a stack trace to the
          dispatcher).
    """
    del args  # lcm_status takes no parameters (empty-param schema).

    if ctx is None:
        return tool_result({
            "ok": False,
            "reason": "engine-unavailable",
            "note": (
                "LCM engine is not available. The plugin may still be "
                "initializing — try again on the next turn."
            ),
        })

    # The command body reads everything off ``parsed.engine`` via
    # getattr — it never inspects the rest of the ParsedLcmCommand. A
    # SimpleNamespace carrying just ``engine`` is the minimal shape that
    # satisfies it (the same stand-in tests/commands/test_status.py uses).
    parsed = SimpleNamespace(engine=ctx)

    try:
        full_text = _run_status(parsed)
    except Exception as exc:  # noqa: BLE001 — handler must return text, not raise
        # commands.status.run is written to catch its own DB errors and
        # return a one-line failure string. A raise here is an
        # unexpected programmer error; surface it as a structured
        # failure rather than letting a stack trace escape dispatch.
        logger.warning("[lcm] lcm_status tool: status body raised", exc_info=True)
        return tool_result({
            "ok": False,
            "reason": "exception",
            "note": (
                f"lcm_status failed: {exc}. Check the gateway log; "
                "the /lcm status slash command may still work."
            ),
        })

    # ADR-035 mandatory caveat — cap the tool-variant output. The slash
    # command leaves the report unbounded; the tool variant must not.
    capped_text = cap_diagnostic_text(full_text, slash_command="status")
    return tool_result({
        "ok": True,
        "report": capped_text,
        "capped": capped_text != full_text,
    })
