"""Ingest methods for :class:`~lossless_hermes.engine.LCMEngine`.

Hosts the ``_on_post_llm_call`` hook handler + ``ingest_single`` /
``ingest_batch`` helpers per ADR-027 §Decision "Package structure" —
the ``ingest.py`` sub-module of ``src/lossless_hermes/engine/``.

This file is a **skeleton at issue 02-01**: the mixin class is declared
with method signatures only; bodies land in Epic 03 (ingest + assemble
seam). Maps to ``engine.ts`` ``ingestSingle`` (lines 5899-6064),
``ingest`` (6066-6090), and ``ingestBatch`` (6092-6134).

**Issue 02-07** demotes the ``_on_post_llm_call`` stub from
:class:`NotImplementedError` to a silent no-op (with a debug breadcrumb)
so that :func:`lossless_hermes.register` can wire it as a Hermes
``post_llm_call`` hook callback without the agent loop crashing on every
turn. The real diff-and-ingest body still lands in Epic 03.

Mixin contract (per ADR-027 §Consequences "All state lives on the shell
class"):

* No state owned here. Methods read/write
  ``self._last_seen_message_idx``, ``self._conversation_store``,
  ``self._session_locks[session_id]`` exclusively via the shell class's
  attributes declared in :meth:`LCMEngine.__init__`.
* No cross-mixin imports. If ingest work needs assemble behavior, it
  goes through ``self.assemble(...)`` (MRO resolves to
  :class:`_AssembleMixin`).

See:

* ``docs/adr/009-per-message-ingest.md`` — ``post_llm_call`` as the
  per-turn ingest seam (the hook this stub handles).
* ``docs/adr/024-project-layout.md`` — engine/ package placement.
* ``docs/adr/027-engine-splitting.md`` — mixin pattern decisions.
* ``docs/porting-guides/engine.md`` §"ingest" — the TS algorithm that
  fills this stub in Epic 03.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    pass


logger = logging.getLogger("lossless_hermes.engine.ingest")


class _IngestMixin:
    """Ingest hook handlers for :class:`LCMEngine`.

    Skeleton at 02-01 — bodies land in Epic 03 (ingest + assemble seam).
    At 02-07 the ``_on_post_llm_call`` stub is demoted from
    :class:`NotImplementedError` to a no-op so :func:`register` can
    actually wire it through ``ctx.register_hook("post_llm_call", ...)``
    without the agent loop crashing every turn.

    Maps to engine.ts ``ingestSingle`` (lines 5899-6064), ``ingest``
    (6066-6090), ``ingestBatch`` (6092-6134), and the ``post_llm_call``
    hook handler per ADR-009.

    The mixin is on :class:`LCMEngine`'s MRO at issue 02-01 so Epic 03
    can land ``_on_post_llm_call`` + ``ingest_single`` + ``ingest_batch``
    bodies without touching :class:`LCMEngine` itself.
    """

    async def _on_post_llm_call(
        self,
        session_id: str = "",
        user_message: Any = None,
        assistant_response: str = "",
        conversation_history: Optional[List[Dict[str, Any]]] = None,
        model: str = "",
        platform: str = "",
        **kwargs: Any,
    ) -> None:
        """``post_llm_call`` Hermes hook — diff new messages + ingest. Body lands in Epic 03.

        Replaces engine.ts ``afterTurn()`` (lines 6473-6638) per ADR-009
        decision (post_llm_call as the per-turn ingest seam). Will diff
        ``conversation_history`` against
        ``self._last_seen_message_idx[session_id]`` and ingest each new
        message under ``self._session_locks[session_id]``.

        At 02-07 the body is a no-op (debug log only) so :func:`register`
        can wire it through ``ctx.register_hook("post_llm_call", ...)``
        without the agent loop crashing on every turn. The kwargs shape
        matches the documented ``post_llm_call`` signature per
        ``docs/reference/hermes-hooks.md`` line 92 — every kwarg is
        accepted (defaults provided + ``**kwargs`` catches forward-compat
        additions) and no exception fires.

        Args:
            session_id: The Hermes session identifier.
            user_message: The user's latest turn content. ``Any`` because
                Hermes may pass a string or a structured message dict.
            assistant_response: The assistant's latest response content.
            conversation_history: Full message history snapshot. ``None``
                is tolerated for forward-compat / partial-kwarg callers.
            model: The LLM model id.
            platform: The provider platform string.
            **kwargs: Forward-compat for future hook signature additions.
        """
        # No-op stub. Epic 03 replaces this body with the real diff-and-
        # ingest path. The debug log gives an operator scanning the logs
        # a breadcrumb that the hook fired with the expected kwargs.
        history_len = len(conversation_history) if conversation_history else 0
        logger.debug(
            "[lcm] post_llm_call session=%s history_len=%d (Epic 03 will diff and ingest)",
            session_id,
            history_len,
        )
