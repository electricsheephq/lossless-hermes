"""Compaction methods for :class:`~lossless_hermes.engine.LCMEngine`.

Hosts the ``compress`` overflow-recovery entry point + the
``execute_compaction_core`` / ``compact_until_under`` /
``evaluate_incremental_compaction`` state-machine helpers per ADR-027
§Decision "Package structure" — the ``compact.py`` sub-module of
``src/lossless_hermes/engine/``.

This file is a **skeleton at issue 02-01**: the mixin class is declared
with method signatures and a passthrough ``compress`` (and
``should_compress`` predicate) so the existing 00-06 no-op behavior is
preserved. Full bodies land in Epic 04 (compaction algorithm).

Maps to ``engine.ts`` ``compact()`` / ``executeCompactionCore`` (lines
7185-7243, 3344-3528) and ``evaluateIncrementalCompaction``.

Mixin contract (per ADR-027 §Consequences "All state lives on the shell
class"):

* No state owned here. Methods read/write
  ``self._circuit_breakers``, ``self._compaction_telemetry_store``,
  ``self._compaction_maintenance_store``, etc. exclusively via the
  shell class's attributes declared in :meth:`LCMEngine.__init__`.
* No cross-mixin imports. If compaction work needs assemble behavior,
  it goes through ``self.assemble(...)`` (MRO resolves to
  :class:`_AssembleMixin`).

Why ``compress`` and ``should_compress`` have bodies (not stubs):

* These are required ABC methods on :class:`ContextEngine` — they MUST
  be callable on a freshly-constructed engine. The 00-06 no-op passes
  every input through unchanged and ``should_compress`` returns
  ``False``; Epic 04 replaces the body with the real compaction
  algorithm. Keeping the no-op bodies here (rather than the shell
  class) means the algorithm-fill in Epic 04 changes one file.

See:

* ``docs/adr/024-project-layout.md`` — engine/ package placement.
* ``docs/adr/027-engine-splitting.md`` — mixin pattern decisions.
* ``docs/porting-guides/engine.md`` §"compact" — the TS algorithm that
  fills the heavy bodies in Epic 04.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    pass


class _CompactMixin:
    """Compaction handlers for :class:`LCMEngine`.

    Skeleton at 02-01 — ``compress`` is a passthrough (no-op) and
    ``should_compress`` always returns ``False`` (matches 00-06 v0
    behavior). The state-machine helpers (``_execute_compaction_core``,
    ``_evaluate_incremental_compaction``, etc.) land in Epic 04.

    Maps to engine.ts ``compact()`` / ``executeCompactionCore`` (lines
    7185-7243, 3344-3528) and ``evaluateIncrementalCompaction``.

    The mixin is on :class:`LCMEngine`'s MRO at issue 02-01 so Epic 04
    can replace the ``compress`` body with the real compaction
    algorithm without touching :class:`LCMEngine` itself.
    """

    def should_compress(self, prompt_tokens: Optional[int] = None) -> bool:
        """Return ``False`` unconditionally for the 02-01 skeleton.

        LCM's compaction decision is driven by ``post_llm_call`` ingest +
        per-turn evaluation, not by Hermes's threshold gate (ADR-009 +
        ADR-010). The Hermes ``compress()`` path is the **overflow-
        recovery** entry point — it fires only when ``should_compress``
        returns ``True``. At 02-01 we always return ``False`` because
        the real engine is not yet wired up (Epic 03 ingest + Epic 04
        compaction); raw messages pass through unchanged.

        Args:
            prompt_tokens: Optional explicit token count. Ignored at 02-01.

        Returns:
            Always ``False`` at 02-01.
        """
        return False

    def compress(
        self,
        messages: List[Dict[str, Any]],
        current_tokens: Optional[int] = None,
        focus_topic: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return ``messages`` unchanged (no-op passthrough at 02-01).

        Maps to engine.ts ``compact()`` / ``executeCompactionCore``
        (lines 7185-7243, 3344-3528) which port in Epic 04. The 02-01
        skeleton preserves the ABC contract while no real compaction
        runs, so the rest of the plugin (entry point, config loader,
        hooks) continues to exercise correctly.

        Args:
            messages: The conversation message list. Returned verbatim.
            current_tokens: Ignored at 02-01.
            focus_topic: Ignored at 02-01.

        Returns:
            The input ``messages`` list, unchanged.
        """
        return messages
