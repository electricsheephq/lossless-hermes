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

**Issue 04-07** wires the :class:`~lossless_hermes.engine.circuit_breaker.CircuitBreaker`
state machine (shipped in 02-09) into a new public :meth:`compact`
entry point. The breaker gates compaction at the ``(provider, model)``
scope: once :attr:`LcmConfig.circuit_breaker_threshold` consecutive
auth failures fire on a single ``(provider, model)`` pair, the breaker
opens; :meth:`compact` short-circuits with
``CompactionResult(reason="circuit breaker open")`` until the cooldown
elapses. The state-machine primitives themselves live on
:class:`~lossless_hermes.engine.circuit_breaker.CircuitBreaker`; this
mixin adds the thin wiring (``_resolve_breaker_key``,
``_is_circuit_breaker_open``, ``_record_compaction_auth_failure``,
``_record_compaction_success``) that the spec's pseudocode references.

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
* ``epics/04-compaction/04-07-circuit-breaker-integration.md`` —
  issue spec for the breaker wiring.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import TYPE_CHECKING, Any, Deque, Dict, List, Optional, Tuple

from lossless_hermes.compaction import CompactionResult
from lossless_hermes.summarize import LcmProviderAuthError

if TYPE_CHECKING:
    from lossless_hermes.engine.circuit_breaker import CircuitBreaker


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
    # 02-09 — Circuit-breaker state-machine map. Initialized in
    # :meth:`LCMEngine.__init__`. 04-07 reads via
    # :meth:`_get_circuit_breaker_state` (the configured-defaults
    # factory) to apply LcmConfig.circuit_breaker_threshold /
    # circuit_breaker_cooldown_ms.
    _circuit_breakers: Dict[str, "CircuitBreaker"]
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

        # 04-07 — the breaker-state factory lives on :class:`LCMEngine`
        # itself (engine/__init__.py:_get_or_create_circuit_breaker).
        # MRO resolves ``self._get_or_create_circuit_breaker(key)`` to
        # the shell's body at call time; we re-declare the signature
        # here so ``ty`` can resolve calls from inside this mixin
        # without crossing module boundaries.
        def _get_or_create_circuit_breaker(self, key: str) -> "CircuitBreaker": ...

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

    # ------------------------------------------------------------------
    # 04-07 — Circuit-breaker integration
    # ------------------------------------------------------------------
    #
    # The state machine itself lives on :class:`CircuitBreaker`
    # (engine/circuit_breaker.py, shipped in 02-09). Issue 04-07 adds
    # the thin wiring that connects the breaker to the compaction call
    # path:
    #
    #   * :meth:`_resolve_breaker_key` — produces the breaker scope
    #     string from ``(provider, model)``.
    #   * :meth:`_get_circuit_breaker_state` /
    #     :meth:`_is_circuit_breaker_open` /
    #     :meth:`_record_compaction_auth_failure` /
    #     :meth:`_record_compaction_success` — alias wrappers over the
    #     shell's :meth:`LCMEngine._get_or_create_circuit_breaker` +
    #     :class:`CircuitBreaker` instance methods, matching the API
    #     surface the issue spec's algorithm pseudocode references.
    #   * :meth:`compact` — public entry that gates on the breaker
    #     before invoking the compaction core. Returns
    #     ``CompactionResult(reason="circuit breaker open")`` when the
    #     breaker rejects.
    #   * :meth:`_execute_compaction_core` — subclass / test hook that
    #     :meth:`compact` delegates the actual sweep to. Default body
    #     raises ``NotImplementedError``; the production wiring (Epic
    #     04 wrap-up issue) will compose this with a
    #     :class:`~lossless_hermes.compaction.CompactionEngine` instance.
    #
    # The breaker is keyed at the ``(provider, model)`` scope so
    # consecutive auth failures on ``(anthropic, claude-3-opus)`` open
    # the breaker for EVERY conversation routed to that pair — not
    # just the conversation that hit it. This matches the TS source
    # (``engine.ts:3372`` ``breakerScope = sessionQueueKey`` was the
    # session-level scope in early LCM but the v4.1 release moved to
    # provider/model — see ``docs/porting-guides/engine.md`` §"Circuit
    # breaker logic").
    #
    # Maps to engine.ts circuit-breaker methods at lines 1963-2016 +
    # the call sites at 3376 / 3427-3429 / 3496-3498 / 6895 / 6976-6978.

    def _resolve_breaker_key(
        self,
        provider: str | None,
        model: str | None,
    ) -> str:
        """Return the breaker scope key for the given ``(provider, model)`` pair.

        Mirrors TS ``breakerScope`` resolution in
        ``engine.ts:resolveSummarize`` — the v4.1 source uses
        ``f"{provider}::{model}"`` so failures are pooled across
        conversations targeting the same model. ``None`` legs fall to
        the literal ``"unknown"`` so two distinct call sites with
        partial info still share a breaker (better than orphan keys
        per-call).

        Format: ``f"{provider or 'unknown'}::{model or 'unknown'}"``.

        Args:
            provider: Provider identifier (e.g. ``"anthropic"``,
                ``"openai"``). ``None`` falls back to ``"unknown"``.
            model: Model identifier (e.g. ``"claude-3-opus"``).
                ``None`` falls back to ``"unknown"``.

        Returns:
            The breaker key string. Same format as TS so logs / metrics
            line up across the two implementations.
        """
        return f"{provider or 'unknown'}::{model or 'unknown'}"

    def _get_circuit_breaker_state(self, breaker_key: str) -> "CircuitBreaker":
        """Return the breaker for ``breaker_key``, creating it on demand.

        Thin alias over :meth:`LCMEngine._get_or_create_circuit_breaker`
        — the spec's pseudocode references the method by the
        ``_get_circuit_breaker_state`` name; both call paths produce a
        :class:`CircuitBreaker` configured from
        :class:`LcmConfig.circuit_breaker_threshold` /
        :class:`LcmConfig.circuit_breaker_cooldown_ms`.

        Maps to engine.ts ``getCircuitBreakerState`` (line 1963).

        Args:
            breaker_key: Output of :meth:`_resolve_breaker_key` (typical
                caller). Any opaque non-empty string also works for
                tests.

        Returns:
            The :class:`CircuitBreaker` for ``breaker_key``. Same
            instance is returned for the same key across calls
            (identity stable).
        """
        return self._get_or_create_circuit_breaker(breaker_key)

    def _is_circuit_breaker_open(self, breaker_key: str) -> bool:
        """Return ``True`` if the breaker is currently rejecting calls.

        Maps to engine.ts ``isCircuitBreakerOpen`` (line 1972).

        Side-effect: when the underlying :class:`CircuitBreaker` is in
        the ``open`` state and the cooldown has elapsed, the read
        auto-transitions to ``half_open`` (allowing one probe call) —
        see :meth:`CircuitBreaker.is_open`. The failure counter is
        preserved across this transition; a successful probe in
        :meth:`compact` then closes the breaker, while a probe that
        also raises :class:`LcmProviderAuthError` re-opens with a
        fresh cooldown window. This matches the spec's "half-open
        semantics — explicit" section: there is no special-case
        half-open code path, just the same record-failure /
        record-success calls.

        Args:
            breaker_key: Output of :meth:`_resolve_breaker_key`.

        Returns:
            ``True`` when the breaker rejects the call;
            ``False`` for closed and half-open (probe allowed).
        """
        return self._get_circuit_breaker_state(breaker_key).is_open()

    def _record_compaction_auth_failure(self, breaker_key: str) -> None:
        """Record a provider-auth failure; opens the breaker at threshold.

        Maps to engine.ts ``recordCompactionAuthFailure`` (line 1983).

        Behavior (see :meth:`CircuitBreaker.record_failure` for the
        precise per-state branches):

        * ``closed`` — increment ``failures``; open if at threshold
          (default 5 per :class:`LcmConfig`).
        * ``half_open`` — probe failed, re-open with fresh cooldown.
        * ``open`` — already gated, but failures still ticks for
          telemetry.

        The shell's :meth:`LCMEngine._get_or_create_circuit_breaker`
        applies the config-driven threshold + cooldown the first time
        a key is seen, so a fresh-key call here always uses the
        configured values rather than the standalone defaults
        (``threshold=5``, ``cooldown_s=60.0``) on the dataclass.

        Args:
            breaker_key: Output of :meth:`_resolve_breaker_key`.
        """
        self._get_circuit_breaker_state(breaker_key).record_failure()

    def _record_compaction_success(self, breaker_key: str) -> None:
        """Record a successful compaction; resets the breaker.

        Maps to engine.ts ``recordCompactionSuccess`` (line 2001).

        Resets failures AND open_since on ANY success — half-open
        recovery is implicit (one successful probe closes the breaker).
        Matches the TS source's unconditional reset (``resetCircuitBreaker``
        on every success path) and the spec's "reset on **any**
        success" requirement.

        Args:
            breaker_key: Output of :meth:`_resolve_breaker_key`.
        """
        self._get_circuit_breaker_state(breaker_key).record_success()

    def _execute_compaction_core(
        self,
        *,
        conversation_id: int,
        token_budget: int,
        current_tokens: int,
        provider: str | None,
        model: str | None,
    ) -> CompactionResult:
        """Run the actual compaction sweep — subclass / wiring hook.

        04-07 ships this as a hook; the production wiring lives in a
        future Epic 04 wrap-up issue that composes
        :class:`~lossless_hermes.compaction.CompactionEngine` into
        :class:`LCMEngine` and delegates here to
        :meth:`CompactionEngine.compact_full_sweep`. Until that wiring
        lands, :meth:`compact` is testable end-to-end by subclassing
        :class:`LCMEngine` and overriding this method with scripted
        results — the same pattern :class:`_ScriptedEngine` uses in
        :mod:`tests.test_compaction_anti_thrashing`.

        Raising :class:`LcmProviderAuthError` from this method
        triggers the :meth:`compact` ``auth_failure`` branch (the
        breaker increments + returns ``auth_failure=True``).
        Returning a :class:`CompactionResult` with
        ``auth_failure=True`` is ALSO honored — the spec calls out
        both shapes (TS source signals via either an exception or the
        result flag depending on the call stack depth). The
        :meth:`compact` body uses the result flag for the catch-block
        path so both shapes are handled identically.

        Args:
            conversation_id: The conversation to compact.
            token_budget: The model's context window.
            current_tokens: The caller's observed live token count
                (used in the breaker-open short-circuit's
                ``tokens_before`` / ``tokens_after`` so callers can
                read a sensible value even when no work ran).
            provider: Optional provider identifier — forwarded to the
                core for telemetry / model resolution.
            model: Optional model identifier — likewise forwarded.

        Returns:
            A :class:`CompactionResult` describing what (if anything)
            the sweep did.

        Raises:
            NotImplementedError: 04-07 default body. The wrap-up
                issue replaces this with a real
                :class:`CompactionEngine` delegate.
            LcmProviderAuthError: Provider-auth signal from the
                summarizer (caught by :meth:`compact`).
        """
        del conversation_id, token_budget, current_tokens, provider, model
        raise NotImplementedError(
            "_execute_compaction_core is not yet wired (issue 04-07 ships the "
            "breaker integration; the CompactionEngine delegate lands in a "
            "follow-up Epic 04 wrap-up issue). Tests override this method to "
            "script results; production must compose a CompactionEngine."
        )

    def compact(
        self,
        *,
        conversation_id: int,
        token_budget: int,
        current_tokens: int,
        provider: str | None = None,
        model: str | None = None,
    ) -> CompactionResult:
        """Compaction entry point with circuit-breaker gating.

        Maps to engine.ts ``LcmContextEngine.compact`` (lines
        3344-3528) — specifically the breaker gate at line 3376 and
        the post-call ``recordCompactionAuthFailure`` /
        ``recordCompactionSuccess`` dispatch at lines 3427-3429 /
        3496-3498.

        Algorithm (per the issue spec's pseudocode):

        1. Resolve the breaker key from ``(provider, model)``.
        2. If the breaker is OPEN → return a no-op
           :class:`CompactionResult` with ``reason="circuit breaker
           open"``; ``tokens_before == tokens_after == current_tokens``
           so callers can read a sensible token figure without
           ambiguity.
        3. Otherwise call :meth:`_execute_compaction_core` (the
           subclass / wiring hook). Catch
           :class:`LcmProviderAuthError`:

           * On auth failure (either via exception OR via
             ``result.auth_failure=True``) → record the failure on the
             breaker and return ``CompactionResult(auth_failure=True,
             ...)``. The breaker increments and may open if at threshold.
           * On any non-auth success → record a breaker success
             (resets failures + clears open_since regardless of prior
             state) and return the core's result with the breaker
             telemetry attached.

        The half-open path falls out naturally: when the cooldown has
        elapsed, :meth:`_is_circuit_breaker_open` returns ``False``
        (transitioning the breaker to ``half_open`` as a side effect);
        the next call is a normal probe through this method's body.
        A successful probe resets the breaker; a failing probe
        re-opens it with a fresh ``open_since``. No special-case code
        path — same record-failure / record-success calls.

        Args:
            conversation_id: The conversation to compact. Forwarded to
                :meth:`_execute_compaction_core` for the sweep body.
            token_budget: The model's context window. Forwarded.
            current_tokens: The caller's observed live token count.
                Used only for the breaker-open short-circuit's
                ``tokens_before`` / ``tokens_after`` so callers reading
                the result don't see a stale or zero token count when
                no work ran.
            provider: Provider identifier — feeds
                :meth:`_resolve_breaker_key`. ``None`` falls back to
                ``"unknown"``.
            model: Model identifier — likewise. ``None`` falls back to
                ``"unknown"``.

        Returns:
            A :class:`CompactionResult` with one of three shapes:

            * **Breaker open** —
              ``action_taken=False, auth_failure=False,
              reason="circuit breaker open"``.
            * **Auth failure** — ``action_taken=False,
              auth_failure=True, reason="provider auth failure"``.
              Breaker incremented; may have opened.
            * **Successful sweep** — pass-through of
              :meth:`_execute_compaction_core`'s return value;
              breaker reset to ``closed``.

        Compaction circuit breaker: opens after N consecutive auth
        failures on the same ``(provider, model)`` scope. Prevents
        retry-storm during provider outages from exhausting backoff
        budgets across conversations.
        Original: lossless-claw/src/engine.ts:1782 (state), 1963-2016
        (machine), 3376/3427-3429/3496-3498/6895/6976-6978 (call sites).
        """
        breaker_key = self._resolve_breaker_key(provider, model)

        # Step 1: gate on the breaker. The is_open() call may transition
        # the breaker from open → half_open as a side effect when the
        # cooldown has elapsed; in that case it returns False and the
        # next compaction call becomes the half-open probe.
        if self._is_circuit_breaker_open(breaker_key):
            logger.info(
                "[lcm] compact short-circuit: breaker open for %s "
                "(skipping compaction; auto-retry when cooldown elapses)",
                breaker_key,
            )
            return CompactionResult(
                action_taken=False,
                tokens_before=current_tokens,
                tokens_after=current_tokens,
                created_summary_id=None,
                condensed=False,
                level=None,
                passes_completed=0,
                auth_failure=False,
                reason="circuit breaker open",
            )

        # Step 2: execute the core, catching auth failures from
        # :mod:`summarize` (the LcmProviderAuthError surface ported
        # forward-declared from issue 04-06). The TS source funnels both
        # exception-shaped and result-flag-shaped auth signals through
        # the same recordCompactionAuthFailure call; we mirror that by
        # catching the exception AND checking ``result.auth_failure``
        # on the non-exception path.
        try:
            result = self._execute_compaction_core(
                conversation_id=conversation_id,
                token_budget=token_budget,
                current_tokens=current_tokens,
                provider=provider,
                model=model,
            )
        except LcmProviderAuthError as exc:
            self._record_compaction_auth_failure(breaker_key)
            logger.warning(
                "[lcm] compact: provider auth failure for %s — breaker incremented (%s)",
                breaker_key,
                exc,
            )
            return CompactionResult(
                action_taken=False,
                tokens_before=current_tokens,
                tokens_after=current_tokens,
                created_summary_id=None,
                condensed=False,
                level=None,
                passes_completed=0,
                auth_failure=True,
                reason="provider auth failure",
            )

        # Step 3: post-call dispatch. Result-flag-shaped auth failure
        # path (the core caught the exception itself and propagated via
        # ``auth_failure=True``) MUST also increment the breaker —
        # otherwise an auth-handling subclass that swallows the
        # exception in favor of the flag could defeat the breaker. TS
        # source matches: engine.ts:3427-3429 checks ``sweepResult.authFailure``
        # to decide between recordCompactionAuthFailure and recordCompactionSuccess.
        if result.auth_failure:
            self._record_compaction_auth_failure(breaker_key)
            logger.warning(
                "[lcm] compact: core reported auth_failure for %s — breaker incremented",
                breaker_key,
            )
            # Preserve the core's token counts + passes_completed; only
            # synthesize the reason if the core didn't already set one.
            return CompactionResult(
                action_taken=result.action_taken,
                tokens_before=result.tokens_before,
                tokens_after=result.tokens_after,
                created_summary_id=result.created_summary_id,
                condensed=result.condensed,
                level=result.level,
                passes_completed=result.passes_completed,
                auth_failure=True,
                reason=result.reason or "provider auth failure",
            )

        # Non-exception, non-flag path — record success. Resets failures
        # AND open_since regardless of prior state (half-open probe
        # success → closed; closed-with-partial-failures → closed; etc.)
        # per the spec's "reset on **any** success" requirement.
        self._record_compaction_success(breaker_key)
        return result


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
