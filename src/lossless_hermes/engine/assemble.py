"""Assembly methods for :class:`~lossless_hermes.engine.LCMEngine`.

Hosts the per-turn assembly substitution + ``safe_fallback`` per
ADR-027 §Decision "Package structure" — the ``assemble.py`` sub-module
of ``src/lossless_hermes/engine/``.

This file is a **skeleton at issue 02-01**: the mixin class is declared
with method signatures only; bodies land in Epic 03 (ingest + assemble
seam). Maps to ``engine.ts`` ``assemble`` cluster (the ContextAssembler
collaborator and the LCM ``pre_llm_call`` always-on substitution per
ADR-010).

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

* ``docs/adr/024-project-layout.md`` — engine/ package placement.
* ``docs/adr/027-engine-splitting.md`` — mixin pattern decisions.
* ``docs/adr/010-always-on-assembly-emulation.md`` — the
  ``pre_llm_call`` substitution seam that fills these stubs in Epic 03.
* ``docs/porting-guides/engine.md`` §"assemble" + §"Always-on assembly
  problem" — TS algorithm + Python adaptation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List

if TYPE_CHECKING:
    pass


class _AssembleMixin:
    """Per-turn assembly + ``safe_fallback`` handlers for :class:`LCMEngine`.

    Skeleton at 02-01 — bodies land in Epic 03 (assemble seam).
    Maps to engine.ts ``assemble`` cluster + the ``pre_llm_call``
    substitution hook per ADR-010.

    The mixin is on :class:`LCMEngine`'s MRO at issue 02-01 so Epic 03
    can land ``_on_pre_llm_call`` + ``preassemble`` + ``safe_fallback``
    bodies without touching :class:`LCMEngine` itself.
    """

    async def _on_pre_llm_call(
        self,
        session_id: str,
        conversation_history: List[Dict[str, Any]],
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        """``pre_llm_call`` Hermes hook — always-on assembly substitution. Body lands in Epic 03.

        Per ADR-010: every turn the engine rewrites the prompt message
        list from the DAG via the assembler. Returns the assembled
        message list to substitute into the LLM call.

        Args:
            session_id: The Hermes session identifier.
            conversation_history: Full message history snapshot.
            **kwargs: Forward-compat for future hook signature additions.

        Returns:
            The assembled (per-turn-substituted) message list.

        Raises:
            NotImplementedError: Always at 02-01; body lands in Epic 03.
        """
        raise NotImplementedError("_on_pre_llm_call lands in Epic 03 (assemble seam)")
