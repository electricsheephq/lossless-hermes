"""Compaction methods for :class:`~lossless_hermes.engine.LCMEngine`.

Hosts the ``compress`` overflow-recovery entry point + the
``execute_compaction_core`` / ``compact_until_under`` /
``evaluate_incremental_compaction`` state-machine helpers per ADR-027
§Decision "Package structure" — the ``compact.py`` sub-module of
``src/lossless_hermes/engine/``.

At issue **02-06** ``should_compress`` carries real logic — the
conventional threshold gate plus an anti-thrashing back-off — and
``compress`` is a refined passthrough that (a) appends to
``_compression_history`` for the anti-thrashing gate, (b) increments
``compression_count`` per ``run_agent.py``'s display contract, and
(c) emits a debug log so an operator scanning logs sees the no-op
fire. The real compaction algorithm lands in Epic 04.

Maps to ``engine.ts`` ``compact()`` / ``executeCompactionCore`` (lines
7185-7243, 3344-3528) and ``evaluateIncrementalCompaction`` (2824-3002).
Hermes's own ``should_compress`` (agent/context_compressor.py:493-513)
is the closer kin — same threshold gate + ineffective-compression count.

Mixin contract (per ADR-027 §Consequences "All state lives on the shell
class"):

* No state owned here. Methods read/write
  ``self._compression_history``, ``self._circuit_breakers``,
  ``self._compaction_telemetry_store``, ``self._compaction_maintenance_store``,
  ``self.compression_count`` etc. exclusively via the shell class's
  attributes declared in :meth:`LCMEngine.__init__`.
* No cross-mixin imports. If compaction work needs assemble behavior,
  it goes through ``self.assemble(...)`` (MRO resolves to
  :class:`_AssembleMixin`).

Why ``compress`` and ``should_compress`` have bodies (not stubs):

* These are required ABC methods on :class:`ContextEngine` — they MUST
  be callable on a freshly-constructed engine. The 02-06 ``compress``
  passes every input through unchanged but updates the counters/log;
  Epic 04 fills in the real compaction algorithm. Keeping the bodies
  here (rather than the shell class) means subsequent algorithm-fill
  issues touch one file.

See:

* ``docs/adr/024-project-layout.md`` — engine/ package placement.
* ``docs/adr/027-engine-splitting.md`` — mixin pattern decisions.
* ``docs/porting-guides/engine.md`` §"compact" — the TS algorithm that
  fills the heavy bodies in Epic 04.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import TYPE_CHECKING, Any, Deque, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    pass


logger = logging.getLogger("lossless_hermes.engine.compact")


# ---------------------------------------------------------------------------
# Anti-thrashing constants (Hermes parity)
# ---------------------------------------------------------------------------

# A compression that frees less than this fraction of the pre-compression
# token count is "ineffective" — it removed almost nothing and another
# trigger on the next turn is likely to also remove almost nothing.
# Hermes's ``context_compressor.py`` uses 10% (line 1538-1543); we match
# for parity. Tunable in Epic 04 when the real algorithm lands.
INEFFECTIVE_SAVINGS_THRESHOLD = 0.10

# When the most recent ``INEFFECTIVE_RUN_LENGTH`` entries in
# ``_compression_history`` were all ineffective, back off — return False
# from ``should_compress`` to break the hot-loop. Hermes uses 2
# (``_ineffective_compression_count >= 2``); matching the parity.
INEFFECTIVE_RUN_LENGTH = 2


class _CompactMixin:
    """Compaction handlers for :class:`LCMEngine`.

    At 02-05 ``should_compress`` ships real threshold + anti-thrashing
    logic; ``compress`` remains a passthrough that records
    ``(before, after)`` token counts into the shell's
    ``_compression_history`` deque so the anti-thrash gate has data to
    consult. The full compaction algorithm + state-machine helpers
    (``_execute_compaction_core``, ``_evaluate_incremental_compaction``,
    etc.) land in Epic 04.

    Maps to engine.ts ``compact()`` / ``executeCompactionCore`` (lines
    7185-7243, 3344-3528) and ``evaluateIncrementalCompaction`` (2824-
    3002). Hermes's own ``should_compress``
    (``agent/context_compressor.py:493-513``) is the closer kin — same
    threshold gate + ineffective-compression-count pattern.

    The mixin is on :class:`LCMEngine`'s MRO at issue 02-01 so Epic 04
    can replace the ``compress`` body with the real compaction
    algorithm without touching :class:`LCMEngine` itself.
    """

    # ------------------------------------------------------------------
    # Shell-state contract (type-only declarations, no runtime values)
    # ------------------------------------------------------------------
    # Per ADR-027 §Consequences "All state lives on the shell class",
    # these attributes are initialized by :meth:`LCMEngine.__init__`.
    # We re-declare them here as class-level annotations (no values) so
    # the ``ty`` type-checker knows the mixin's methods can rely on them
    # being present on ``self``. Annotations are PEP-563-deferred via
    # ``from __future__ import annotations``, so no runtime descriptor
    # is created — the values still come from the shell's ``__init__``.
    last_prompt_tokens: int
    threshold_tokens: int
    compression_count: int
    _compression_history: Deque[Tuple[int, int]]

    def should_compress(self, prompt_tokens: Optional[int] = None) -> bool:
        """Return ``True`` if compaction should fire this turn.

        Per ``docs/reference/hermes-hooks.md`` line 51, called from
        ``run_agent.py:14841`` after each turn's API call. When ``True``,
        the host fires :meth:`compress`. Maps to engine.ts
        ``evaluateIncrementalCompaction`` (lines 2824-3002) — the full
        cache-aware state machine lands in Epic 04. Issue 02-05 ships the
        **conventional threshold gate** + a simple anti-thrashing back-off.

        Algorithm:

        1. Resolve the observed token count: explicit ``prompt_tokens``
           arg if non-None, else ``self.last_prompt_tokens`` (set by
           :meth:`update_from_response`, landing in 02-04).
        2. **Threshold gate.** If ``self.threshold_tokens`` is 0 (never
           set — :meth:`update_model` hasn't fired) the gate returns
           ``False`` regardless of how high ``observed`` is. This guards
           the "default state" case: the engine must not fire compaction
           before the host wires the model context length. Otherwise the
           gate is ``observed >= self.threshold_tokens``.
        3. **Anti-thrashing back-off.** Inspect the last
           :data:`INEFFECTIVE_RUN_LENGTH` entries of
           ``self._compression_history`` (a ``deque[tuple[int, int]]`` of
           ``(before_tokens, after_tokens)`` pairs appended by
           :meth:`compress`). An entry is "ineffective" when its savings
           ratio ``(before - after) / before`` is less than
           :data:`INEFFECTIVE_SAVINGS_THRESHOLD` (10% — matches Hermes
           ``context_compressor.py`` line 1538-1543). If the most recent
           ``INEFFECTIVE_RUN_LENGTH`` entries are all ineffective, return
           ``False`` even at over-threshold tokens — the next pass would
           hot-loop with no real reduction.

        ADR-010 §Note — once the upstream ``preassemble`` patch lands
        (Hermes PR #24949 — patch #1 in ADR-015), LCM compaction will
        run via the always-on assembly hook + deferred debt queue, and
        ``should_compress`` will be redefined to always return ``False``
        (compaction no longer threshold-gated). Until then, the
        conventional threshold gate is the right shape.

        Args:
            prompt_tokens: Optional explicit token count. When ``None``
                falls back to ``self.last_prompt_tokens``. Default
                ``None`` matches the ABC signature.

        Returns:
            ``True`` if both gates pass (over-threshold + not in
            anti-thrashing back-off); ``False`` otherwise.
        """
        observed = prompt_tokens if prompt_tokens is not None else self.last_prompt_tokens

        # Threshold gate. ``threshold_tokens == 0`` means
        # ``update_model`` hasn't fired (or context_length was 0). In
        # both cases the engine has no notion of "full" yet, so
        # compaction must not fire — guard with explicit ``> 0`` rather
        # than the naive ``observed < threshold_tokens`` Hermes uses
        # (Hermes would return True at threshold=0 + any positive
        # token count, since ``positive < 0`` is False). The
        # 00-06 regression test ``test_should_compress_returns_false_
        # for_huge_token_count`` enforces this invariant.
        if self.threshold_tokens <= 0:
            return False
        if observed < self.threshold_tokens:
            return False

        # Anti-thrashing back-off. Look at the most recent
        # ``INEFFECTIVE_RUN_LENGTH`` entries; if all of them saved less
        # than ``INEFFECTIVE_SAVINGS_THRESHOLD`` of their pre-compression
        # token count, back off. The deque is appended by
        # :meth:`compress`; at 02-05 every entry is ineffective
        # (passthrough — ``after == before``), so two compress() calls
        # in a row will trip the back-off on the third should_compress.
        # Epic 04's real compaction will produce mostly-effective
        # entries and only trip back-off when the algorithm legitimately
        # can't bring tokens down (e.g., all messages are pinned).
        if len(self._compression_history) >= INEFFECTIVE_RUN_LENGTH:
            recent = list(self._compression_history)[-INEFFECTIVE_RUN_LENGTH:]
            if all(_is_ineffective(before, after) for before, after in recent):
                return False

        return True

    def compress(
        self,
        messages: List[Dict[str, Any]],
        current_tokens: Optional[int] = None,
        focus_topic: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return ``messages`` unchanged + track the result for anti-thrashing.

        Maps to engine.ts ``compact()`` / ``executeCompactionCore``
        (lines 7185-7243, 3344-3528) — the real algorithm ports in Epic
        04. At 02-06 the body is still a passthrough but it now (a)
        records a ``(before, after)`` tuple into
        ``self._compression_history`` so :meth:`should_compress` has
        data for its anti-thrashing gate, (b) increments
        ``self.compression_count`` so ``run_agent.py``'s display stays
        consistent (per ``docs/reference/hermes-hooks.md`` "Required
        class-level state" — ``compression_count`` is read at
        ``run_agent.py:10377``), and (c) emits a debug log so an
        operator scanning logs sees the no-op fire.

        Since 02-05 ``compress`` is a passthrough, ``after == before``
        for every entry — the savings ratio is 0% and the entry is
        always ineffective. That is intentional: two consecutive
        ``compress`` calls trip the anti-thrashing gate on the next
        ``should_compress`` and the hot-loop is broken. Epic 04's real
        algorithm will overwrite this body and produce mostly-effective
        entries that don't trip the gate.

        Args:
            messages: The conversation message list. Returned verbatim
                at 02-06.
            current_tokens: Pre-compression token estimate from the host.
                Used as the ``before`` value in the history tuple. When
                ``None`` falls back to ``self.last_prompt_tokens``.
            focus_topic: Ignored at 02-06; Epic 04 forwards to the
                guided-compression path.

        Returns:
            The input ``messages`` list, unchanged at 02-06.
        """
        # Anti-thrashing telemetry. The host passes ``current_tokens``
        # as the pre-compression count; we fall back to
        # ``last_prompt_tokens`` when absent (e.g., a manual ``/compress``
        # invocation without a fresh API response). At 02-06 the body is
        # a passthrough so ``after == before``; every entry will be
        # "ineffective" by definition and the gate will trip after
        # ``INEFFECTIVE_RUN_LENGTH`` compress() calls.
        before = current_tokens if current_tokens is not None else self.last_prompt_tokens
        after = before  # Passthrough — no compaction at 02-06.
        self._compression_history.append((before, after))

        # 02-06: increment compression_count per the hermes-hooks.md
        # "Required class-level state" contract — Hermes's
        # ``run_agent.py:10377`` reads this field for the per-turn
        # display. A "called but did nothing" still counts as one
        # compress invocation from the host's perspective; Epic 04 may
        # later split into separate counters for real vs. no-op runs.
        self.compression_count += 1

        # 02-06: debug breadcrumb so an operator scanning logs sees the
        # no-op fire. Useful while Epic 04 is still in flight (a real
        # compaction would produce richer telemetry); for now this
        # confirms the surface is reached.
        logger.debug(
            "[lcm] compress called (no-op): %d messages, current_tokens=%s, focus_topic=%s",
            len(messages),
            current_tokens,
            focus_topic,
        )

        return messages


def _is_ineffective(before: int, after: int) -> bool:
    """Return ``True`` if the compression saved less than
    :data:`INEFFECTIVE_SAVINGS_THRESHOLD` of its pre-compression tokens.

    Edge cases:

    * ``before <= 0`` — the host never provided a pre-compression count
      (or it was nonsense). Treat as ineffective so the back-off does
      not enter an infinite-trigger loop driven by zero-info entries.
    * ``after >= before`` — compression did not reduce token count.
      Ineffective.
    * Standard path — savings ratio ``(before - after) / before`` versus
      :data:`INEFFECTIVE_SAVINGS_THRESHOLD`.

    Hermes parity: ``context_compressor.py`` line 1538-1543 uses the
    same 10% threshold with ``savings_pct = saved / display_tokens * 100``
    and ``if savings_pct < 10: ineffective_count += 1``.

    Args:
        before: Pre-compression token count.
        after: Post-compression token count.

    Returns:
        ``True`` if the savings ratio is below the threshold.
    """
    if before <= 0:
        return True
    if after >= before:
        return True
    savings_ratio = (before - after) / before
    return savings_ratio < INEFFECTIVE_SAVINGS_THRESHOLD
