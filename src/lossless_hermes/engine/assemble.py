"""Assembly methods for :class:`~lossless_hermes.engine.LCMEngine`.

Hosts the per-turn assembly substitution + ``safe_fallback`` per
ADR-027 §Decision "Package structure" — the ``assemble.py`` sub-module
of ``src/lossless_hermes/engine/``.

This file is a **skeleton at issue 02-01**: the mixin class is declared
with method signatures only; bodies land in Epic 03 (ingest + assemble
seam). Maps to ``engine.ts`` ``assemble`` cluster (the ContextAssembler
collaborator and the LCM ``pre_llm_call`` always-on substitution per
ADR-010).

**Issue 02-07** demotes the ``_on_pre_llm_call`` stub from
:class:`NotImplementedError` to a silent no-op returning ``None`` so
that :func:`lossless_hermes.register` can wire it as a Hermes
``pre_llm_call`` hook callback without the agent loop crashing on every
turn. Per Hermes's hook contract (``hermes_cli/plugins.py``), a ``None``
return is a valid observer-only result that leaves the user message
unchanged; Epic 03 replaces the body with the real
``LOSSLESS_RECALL_POLICY_PROMPT`` injection per ADR-014.

**02-07 fix-forward (Epic 03 prep):** the hook is **synchronous**
(``def``, not ``async def``). Hermes's ``PluginManager.invoke_hook``
(``hermes_cli/plugins.py:1218-1232``) calls callbacks via
``ret = cb(**kwargs)`` with no ``await`` / ``asyncio.run`` — an
``async def`` callback would return a coroutine that Hermes would
treat as a non-``None`` return and append to the results list,
double-injecting context. Epic 03 returns the policy dict directly.

Mixin contract (per ADR-027 §Consequences "All state lives on the shell
class"):

* No state owned here. Methods read/write
  ``self._previous_assembled_messages_by_conversation``,
  ``self._summary_store``, etc. exclusively via the shell class's
  attributes declared in :meth:`LCMEngine.__init__`.
* No cross-mixin imports. If assemble work needs ingest behavior, it
  goes through ``self.ingest_batch(...)`` (MRO resolves to
  :class:`_IngestMixin`).

See:

* ``docs/adr/010-always-on-assembly-emulation.md`` — the
  ``pre_llm_call`` substitution seam that fills this stub in Epic 03.
* ``docs/adr/014-recall-policy-injection.md`` — user-message-position
  injection of the policy text (preserves Anthropic prompt cache).
* ``docs/adr/024-project-layout.md`` — engine/ package placement.
* ``docs/adr/027-engine-splitting.md`` — mixin pattern decisions.
* ``docs/porting-guides/engine.md`` §"assemble" + §"Always-on assembly
  problem" — TS algorithm + Python adaptation.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    pass


logger = logging.getLogger("lossless_hermes.engine.assemble")


class _AssembleMixin:
    """Per-turn assembly + ``safe_fallback`` handlers for :class:`LCMEngine`.

    Skeleton at 02-01 — bodies land in Epic 03 (assemble seam). At 02-07
    the ``_on_pre_llm_call`` stub is demoted from
    :class:`NotImplementedError` to a no-op returning ``None`` so
    :func:`register` can actually wire it through
    ``ctx.register_hook("pre_llm_call", ...)`` without the agent loop
    crashing every turn.

    Maps to engine.ts ``assemble`` cluster + the ``pre_llm_call``
    substitution hook per ADR-010.

    The mixin is on :class:`LCMEngine`'s MRO at issue 02-01 so Epic 03
    can land ``_on_pre_llm_call`` + ``preassemble`` + ``safe_fallback``
    bodies without touching :class:`LCMEngine` itself.
    """

    def _on_pre_llm_call(
        self,
        session_id: str = "",
        user_message: Any = None,
        conversation_history: Optional[List[Dict[str, Any]]] = None,
        is_first_turn: bool = False,
        model: str = "",
        platform: str = "",
        sender_id: str = "",
        **kwargs: Any,
    ) -> None:
        """``pre_llm_call`` Hermes hook — recall-policy injection (Epic 03).

        Per ADR-014: every user turn the engine returns a dict
        ``{"context": <reworded LOSSLESS_RECALL_POLICY_PROMPT>}`` whose
        text Hermes appends to the current turn's user message (NOT the
        system prompt, to preserve the Anthropic prompt cache).

        At 02-07 the body is a no-op returning ``None`` so
        :func:`register` can wire it via
        ``ctx.register_hook("pre_llm_call", ...)`` without the agent
        loop crashing every turn. Per Hermes's hook contract
        (``hermes_cli/plugins.py:1218-1232``) a ``None`` return is a
        valid observer-only result that leaves the user message
        unchanged. The kwargs shape matches the ``pre_llm_call``
        signature in ``docs/reference/hermes-hooks.md`` line 91 — every
        documented kwarg is accepted and no exception fires.

        **Sync, not async:** Hermes's ``invoke_hook`` does
        ``ret = cb(**kwargs)`` with no ``await``; ``async def`` would
        return a coroutine that Hermes treats as the injection context.
        Epic 03 stays sync and returns ``{"context": ...}`` directly.

        Args:
            session_id: The Hermes session identifier.
            user_message: The raw original user message (string or dict).
            conversation_history: Full message history snapshot. ``None``
                is tolerated for forward-compat / partial-kwarg callers.
            is_first_turn: Whether this is the first turn of the
                conversation.
            model: The LLM model id.
            platform: The provider platform string.
            sender_id: Gateway platform user id (empty in CLI).
            **kwargs: Forward-compat for future hook signature additions.

        Returns:
            ``None`` at 02-07 (no-op); Epic 03 returns
            ``{"context": <policy text>}`` per ADR-014.
        """
        # No-op stub. Epic 03 replaces this body with the real recall-
        # policy injection (returning ``{"context": ...}``). The debug
        # log gives an operator scanning logs a breadcrumb that the hook
        # fired with the expected kwargs.
        history_len = len(conversation_history) if conversation_history else 0
        logger.debug(
            "[lcm] pre_llm_call session=%s history_len=%d first_turn=%s "
            "(Epic 03 will inject recall-policy)",
            session_id,
            history_len,
            is_first_turn,
        )
        return None
