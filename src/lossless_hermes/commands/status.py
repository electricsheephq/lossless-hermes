"""``/lcm status`` — engine health snapshot.

Ports the TS ``buildStatusText`` from ``lossless-claw/src/plugin/lcm-command.ts``
(case ``"status"`` in ``parseLcmCommand``). Epic 02 shipped a minimal status
block; Epic 08 will grow it to the full OpenClaw output per
``docs/porting-guides/plugin-glue.md`` line 426 once the operator helpers
land (``getLcmStatusStats``, ``getConversationStatusStats``).

At issue 08-01 the body is the same minimal block the Epic-02 dispatcher
returned — just moved out of the dispatcher class into a standalone
module that the new dispatch table can ``import_module`` lazily.

See:

* ``epics/08-cli-ops/08-01-slash-command-router.md`` — this issue.
* ``epics/02-engine-skeleton/02-10-slash-command-dispatcher.md`` —
  origin of the Epic-02 status body.
* ``docs/porting-guides/plugin-glue.md`` line 425 — Epic 08's planned
  full status body.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("lossless_hermes.commands.status")


def run(parsed: Any) -> str:
    """Render ``/lcm status``.

    Reads from :meth:`ContextEngine.get_status` (inherited on
    :class:`LCMEngine`) plus a few LCM-specific fields. The DB may not be
    open yet (heavy init defers to ``on_session_start`` per ADR-001) —
    we degrade gracefully when stores are ``None``.

    Args:
        parsed: The :class:`ParsedLcmCommand`. We read ``parsed.engine``
            (set by the dispatcher before invoking the handler).

    Returns:
        Multi-line status block ending with ``"  ok"``.
    """
    engine = getattr(parsed, "engine", None)

    try:
        status = engine.get_status() if engine is not None else {}
        if status is None:
            status = {}
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning("[lcm] engine.get_status() raised: %s", exc)
        status = {}

    db_open = getattr(engine, "_db", None) is not None
    conv_store = getattr(engine, "_conversation_store", None)

    lines = [
        "[lcm] status",
        f"  engine: {getattr(engine, 'name', '<unknown>')}",
        f"  db: {'open' if db_open else 'not opened (on_session_start pending)'}",
    ]

    if conv_store is not None:
        lines.append("  conversation_store: ready")
    else:
        lines.append("  conversation_store: not initialized")

    lines.append(f"  last_prompt_tokens: {status.get('last_prompt_tokens', 0)}")
    lines.append(f"  threshold_tokens: {status.get('threshold_tokens', 0)}")
    lines.append(f"  context_length: {status.get('context_length', 0)}")
    lines.append(f"  usage_percent: {status.get('usage_percent', 0):.1f}")
    lines.append(f"  compression_count: {status.get('compression_count', 0)}")
    lines.append("  ok")

    return "\n".join(lines)
