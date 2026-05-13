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

**Issue 03-10 (Epic 03):** the ``_on_pre_llm_call`` body now returns
``{"context": LOSSLESS_RECALL_POLICY_PROMPT}`` so Hermes appends the
LCM recall-policy text to every turn's user-message content per
ADR-014. The TS source (``lossless-claw/src/plugin/index.ts:2395``)
uses ``prependSystemContext`` for the same text — Hermes deliberately
diverges to preserve the Anthropic prompt cache. The user-voice
rewording lives in :mod:`lossless_hermes.recall_policy`.

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

from lossless_hermes.recall_policy import LOSSLESS_RECALL_POLICY_PROMPT

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
    ) -> Optional[Dict[str, str]]:
        """``pre_llm_call`` Hermes hook — recall-policy injection (issue 03-10).

        Per ADR-014: every user turn the engine returns a dict
        ``{"context": LOSSLESS_RECALL_POLICY_PROMPT}`` whose text Hermes
        appends to the current turn's user-message content (NOT the
        system prompt, to preserve the Anthropic prompt cache). The
        upstream TS source (``lossless-claw/src/plugin/index.ts:2395``)
        uses ``prependSystemContext`` for the same text — Hermes
        deliberately diverges because mutating the system prompt every
        turn invalidates the cache prefix; user-message-position
        injection keeps system prompt + tools stable across turns.

        **Idempotency / no double-inject:** Hermes's plugin-context
        plumbing (``hermes_cli/plugins.py:invoke_hook``) is what builds
        the joined ``_plugin_user_context`` from every hook callback's
        return — the engine does not see the in-flight user-message
        content before injection. So this hook is stateless w.r.t.
        prior injection and always returns the same payload. If the
        agent already has the policy text in the conversation
        (e.g. multi-plugin overlap), it appears as duplicate user-
        message context, but Hermes does not call this hook with the
        already-built user message, so the engine has no way to detect
        and skip — defensive idempotency lives at the Hermes plumbing
        layer, not here.

        **Sync, not async:** Hermes's ``invoke_hook`` does
        ``ret = cb(**kwargs)`` with no ``await``; ``async def`` would
        return a coroutine that Hermes treats as the injection context
        (``str(coroutine)`` would be appended verbatim). Per PR #34's
        async-to-sync conversion this method stays ``def``.

        **First-turn parity:** the hook fires on every turn including
        the first. Per ``docs/spike-results/002-hermes-pre-llm-call.md``
        line 38 ("All non-None returns are concatenated"), Hermes does
        not gate the injection on ``is_first_turn`` — the policy text
        ships every turn so the agent retains it after compaction
        windows or long histories that may drop earlier turns.

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
            ``{"context": LOSSLESS_RECALL_POLICY_PROMPT}`` — Hermes
            appends the ``context`` value to the user-message content
            for this turn. Never returns ``None`` — the policy text is
            unconditional per ADR-014 §Decision.
        """
        # Debug breadcrumb so operators scanning logs can see the hook
        # fire each turn (the policy injection itself is invisible in
        # any visible log unless the user is dumping /messages).
        history_len = len(conversation_history) if conversation_history else 0
        logger.debug(
            "[lcm] pre_llm_call inject-policy session=%s history_len=%d first_turn=%s",
            session_id,
            history_len,
            is_first_turn,
        )
        return {"context": LOSSLESS_RECALL_POLICY_PROMPT}
