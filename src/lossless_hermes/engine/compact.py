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

    ### Issue 03-09 extension — ADR-010 Option A experimental path

    When the engine is configured with
    ``experimental_always_on_via_compress: true`` AND the upstream
    Hermes lacks the ``preassemble`` ABC method (PR #24949 still in
    review), this mixin's :meth:`should_compress` returns ``True``
    every turn and :meth:`compress` runs the LCM assembly substitution
    body in place of the overflow-recovery path. Routes the substituted
    message list through ``run_agent.py:10264`` which REPLACES the
    live list. The side effects (session-ID rotation per turn, memory
    provider re-extraction, compression-count warnings, log spam) are
    documented in ADR-010 §"Option A" and surfaced to operators via
    the rate-limited per-turn warning. NOT FOR PRODUCTION.

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
    context_length: int
    _compression_history: Deque[Tuple[int, int]]
    # 03-09 — ADR-010 Option A flags. Initialized in
    # :meth:`LCMEngine.__init__`.
    _has_preassemble: bool
    _experimental_always_on_via_compress: bool

    # ------------------------------------------------------------------
    # Sibling-mixin contract (type-only declarations)
    # ------------------------------------------------------------------
    # Per ADR-027 §Consequences "No cross-mixin imports", a mixin that
    # needs behavior owned by a sibling MIXin reaches it via ``self.X``
    # — Python's MRO resolves to the appropriate ``_FooMixin`` body at
    # call time. ``ty`` doesn't follow the MRO across sibling mixins, so
    # we re-declare the expected signatures here (TYPE_CHECKING gated)
    # so the type-checker can resolve ``self._assemble(...)`` /
    # ``self._infer_session_id(...)`` /
    # ``self._emit_experimental_warning_if_due()`` calls against the
    # documented contract. Bodies live in
    # :class:`_AssembleMixin` (sibling on the MRO).
    if TYPE_CHECKING:

        def _assemble(
            self,
            session_id: str,
            messages: List[Dict[str, Any]],
            token_budget: int,
            prompt: Optional[str] = None,
        ) -> List[Dict[str, Any]]: ...

        def _infer_session_id(
            self,
            messages: Optional[List[Dict[str, Any]]],
        ) -> str: ...

        def _emit_experimental_warning_if_due(self) -> bool: ...

    def should_compress(self, prompt_tokens: Optional[int] = None) -> bool:
        """Return ``True`` if compaction should fire this turn.

        Per ``docs/reference/hermes-hooks.md`` line 51, called from
        ``run_agent.py:14841`` after each turn's API call. When ``True``,
        the host fires :meth:`compress`. Maps to engine.ts
        ``evaluateIncrementalCompaction`` (lines 2824-3002) — the full
        cache-aware state machine lands in Epic 04. Issue 02-05 ships the
        **conventional threshold gate** + a simple anti-thrashing back-off.

        ### 03-09 extension — ADR-010 Option A force-true path

        When the **experimental** path is active
        (``_experimental_always_on_via_compress=True`` AND
        ``_has_preassemble=False``), this method returns ``True`` on
        EVERY turn, regardless of token count. That routes
        substitution through ``run_agent.py:10264``'s
        ``messages = compress(messages, ...)`` call site — the only
        Hermes-side hook that REPLACES the live message list.

        Algorithm (production / disabled mode):

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
        (Hermes PR #24949 — patch #1 in ADR-015), the ``compress``
        path becomes overflow-recovery only and the always-on
        substitution moves to ``preassemble``. The experimental flag
        becomes inert once Hermes has the ABC method (the
        ``_has_preassemble`` check below short-circuits the force-true
        return).

        Args:
            prompt_tokens: Optional explicit token count. When ``None``
                falls back to ``self.last_prompt_tokens``. Default
                ``None`` matches the ABC signature.

        Returns:
            ``True`` if either (a) the experimental fallback is active
            and Hermes lacks ``preassemble``, OR (b) both gates pass
            (over-threshold + not in anti-thrashing back-off);
            ``False`` otherwise.
        """
        # ── 03-09 ADR-010 Option A force-true path ────────────────
        # When the experimental fallback is wired AND Hermes lacks the
        # preassemble ABC method, every turn fires compress() — which
        # in this mixin's compress() body runs the assembler. The
        # _has_preassemble gate means: if Hermes ever upgrades to a
        # version with preassemble, the production path takes over
        # automatically and the experimental flag becomes inert.
        # No matter how high or low the token count is, return True.
        if self._experimental_always_on_via_compress and not self._has_preassemble:
            return True

        # ── Production / disabled mode: conventional threshold gate ──
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
        """Compaction / always-on substitution entry point.

        Maps to engine.ts ``compact()`` / ``executeCompactionCore``
        (lines 7185-7243, 3344-3528).

        ### 03-09 — three branches

        1. **Experimental always-on substitution** (Option A) — when
           ``_experimental_always_on_via_compress`` is ``True`` AND
           ``_has_preassemble`` is ``False``, compress runs the LCM
           assembly substitution body (:meth:`_AssembleMixin._assemble`)
           instead of overflow-recovery. Fires every turn (paired with
           ``should_compress=True`` above). Emits the rate-limited
           per-turn warning. NOT FOR PRODUCTION (ADR-010 §"Option A"
           side effects).

        2. **Overflow-recovery / Epic 04** — when the experimental
           flag is OFF OR Hermes has ``preassemble``, fall through to
           the 02-06 passthrough body. Epic 04 fills this in with the
           real compaction algorithm.

        Per ``docs/reference/hermes-hooks.md`` "Required class-level
        state" — ``compression_count`` is read at ``run_agent.py:10377``
        for the per-turn display, so we increment it on every entry
        regardless of which branch ran. Same logic as 02-06.

        Per the 02-06 anti-thrashing contract, every entry appends a
        ``(before, after)`` tuple to ``self._compression_history`` so
        :meth:`should_compress`'s back-off has data to consult.
        Branch 1 (experimental) reports the assembled token count as
        ``after``; the back-off is moot in that path because
        :meth:`should_compress` short-circuits to True anyway, but
        keeping the history consistent across branches simplifies
        debugging.

        Args:
            messages: The conversation message list. The
                experimental branch returns the substituted list; the
                fallthrough path returns ``messages`` unchanged.
            current_tokens: Pre-compression token estimate from the
                host. Used as the ``before`` value in the history
                tuple. When ``None`` falls back to
                ``self.last_prompt_tokens``.
            focus_topic: Ignored at 03-09 (Epic 04 will forward to the
                guided-compression path).

        Returns:
            * Experimental branch: the substituted message list from
              :meth:`_AssembleMixin._assemble`.
            * Fallthrough: the input ``messages`` list, unchanged.
        """
        # ── 03-09 — Experimental always-on substitution (Option A) ──
        # When the experimental flag is set AND Hermes lacks the
        # preassemble ABC method, run the substitution body. This is
        # called every turn (because should_compress short-circuited to
        # True above). The substituted list replaces the live messages
        # at run_agent.py:10264.
        if self._experimental_always_on_via_compress and not self._has_preassemble:
            # Emit the rate-limited warning so operators know the
            # experimental path is firing. Per ADR-010 §"Path 2" the
            # warning fires once at engine init AND once per minute
            # thereafter. This is the per-turn half.
            self._emit_experimental_warning_if_due()

            # Resolve the session_id from the messages list (or the
            # cached most-recent session). When no session_id can be
            # inferred, ``_assemble`` is unreachable from this path —
            # graceful no-op + return live messages.
            session_id = self._infer_session_id(messages)
            if not session_id:
                logger.debug(
                    "[lcm] compress (experimental): no session_id "
                    "inferred — returning messages unchanged"
                )
                before = current_tokens if current_tokens is not None else self.last_prompt_tokens
                self._compression_history.append((before, before))
                self.compression_count += 1
                return messages

            # Resolve the budget. ``context_length`` (set by
            # update_model) is the natural choice; fall back to
            # ``last_prompt_tokens`` (current pressure) when both
            # context_length is 0 and current_tokens is positive,
            # ultimately to 128_000 if nothing is set.
            if self.context_length and self.context_length > 0:
                effective_budget = int(self.context_length)
            elif current_tokens and current_tokens > 0:
                effective_budget = int(current_tokens)
            elif self.last_prompt_tokens > 0:
                effective_budget = int(self.last_prompt_tokens)
            else:
                effective_budget = 128_000

            # Run the substitution body. Note: ``self._assemble`` is
            # provided by :class:`_AssembleMixin` (sibling on the MRO).
            # The call is sync (PR #34 sync conversion); the per-
            # session lock is acquired inside _assemble.
            substituted = self._assemble(
                session_id=session_id,
                messages=messages,
                token_budget=effective_budget,
                prompt=focus_topic,
            )

            # Anti-thrashing telemetry — track the assembled token
            # count as ``after``. The savings ratio is informational
            # only here (should_compress force-trues on the next call
            # regardless of history), but keeping the history filled
            # gives Epic 04 / debugging a consistent timeline.
            before = current_tokens if current_tokens is not None else self.last_prompt_tokens
            # Rough token estimate of the substituted list. We use the
            # message count × an average token cost. The TS source
            # tracks the assembler's estimated_tokens for this; we
            # don't have it without re-running estimate_tokens on
            # every block here, so use a conservative approximation
            # — the anti-thrash gate is bypassed in this branch anyway.
            after = before  # Approximate; not load-bearing.
            self._compression_history.append((before, after))
            self.compression_count += 1
            logger.debug(
                "[lcm] compress (experimental always-on substitution): "
                "session=%s budget=%d input_msgs=%d output_msgs=%d",
                session_id,
                effective_budget,
                len(messages),
                len(substituted),
            )
            return substituted

        # ── 02-06 fallthrough — overflow-recovery passthrough ──────
        # Anti-thrashing telemetry. The host passes ``current_tokens``
        # as the pre-compression count; we fall back to
        # ``last_prompt_tokens`` when absent (e.g., a manual ``/compress``
        # invocation without a fresh API response). At 02-06 the body is
        # a passthrough so ``after == before``; every entry will be
        # "ineffective" by definition and the gate will trip after
        # ``INEFFECTIVE_RUN_LENGTH`` compress() calls.
        before = current_tokens if current_tokens is not None else self.last_prompt_tokens
        after = before  # Passthrough — no compaction at 02-06 / 03-09 (Epic 04 fills in).
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
