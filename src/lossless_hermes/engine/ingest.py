"""Ingest methods for :class:`~lossless_hermes.engine.LCMEngine`.

Hosts the ``_on_post_llm_call`` hook handler + ``ingest_single`` /
``ingest_batch`` helpers per ADR-027 §Decision "Package structure" —
the ``ingest.py`` sub-module of ``src/lossless_hermes/engine/``.

This file is a **skeleton at issue 02-01**: the mixin class is declared
with method signatures only; bodies land in Epic 03 (ingest + assemble
seam). Maps to ``engine.ts`` ``ingestSingle`` (lines 5899-6064),
``ingest`` (6066-6090), and ``ingestBatch`` (6092-6134).

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

* ``docs/adr/024-project-layout.md`` — engine/ package placement.
* ``docs/adr/027-engine-splitting.md`` — mixin pattern decisions.
* ``docs/porting-guides/engine.md`` §"ingest" — the TS algorithm that
  fills these stubs in Epic 03.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List

if TYPE_CHECKING:
    pass


class _IngestMixin:
    """Ingest hook handlers for :class:`LCMEngine`.

    Skeleton at 02-01 — bodies land in Epic 03 (ingest + assemble seam).
    Maps to engine.ts ``ingestSingle`` (lines 5899-6064), ``ingest``
    (6066-6090), ``ingestBatch`` (6092-6134), and the ``post_llm_call``
    hook handler per ADR-009.

    The mixin is on :class:`LCMEngine`'s MRO at issue 02-01 so Epic 03
    can land ``_on_post_llm_call`` + ``ingest_single`` + ``ingest_batch``
    bodies without touching :class:`LCMEngine` itself.
    """

    async def _on_post_llm_call(
        self,
        session_id: str,
        user_message: str,
        assistant_response: str,
        conversation_history: List[Dict[str, Any]],
        model: str,
        platform: str,
        **kwargs: Any,
    ) -> None:
        """``post_llm_call`` Hermes hook — diff new messages + ingest. Body lands in Epic 03.

        Replaces engine.ts ``afterTurn()`` (lines 6473-6638) per ADR-009
        decision (post_llm_call as the per-turn ingest seam). Diffs
        ``conversation_history`` against
        ``self._last_seen_message_idx[session_id]`` and ingests each
        new message under ``self._session_locks[session_id]``.

        Args:
            session_id: The Hermes session identifier.
            user_message: The user's latest turn content.
            assistant_response: The assistant's latest response content.
            conversation_history: Full message history snapshot.
            model: The LLM model id.
            platform: The provider platform string.
            **kwargs: Forward-compat for future hook signature additions.

        Raises:
            NotImplementedError: Always at 02-01; body lands in Epic 03.
        """
        raise NotImplementedError("_on_post_llm_call lands in Epic 03 (ingest seam)")
