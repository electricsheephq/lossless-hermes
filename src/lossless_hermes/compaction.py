"""Compaction engine — trigger evaluation + anti-thrashing guards.

Ports :class:`CompactionEngine` from
``lossless-claw/src/compaction.ts`` (LCM commit ``1f07fbd`` on branch
``pr-613``) to Python.

**Issues landed in this file:**

* **04-01** — trigger-evaluation foundation:
    * :meth:`CompactionEngine.evaluate` — context-level threshold
      trigger (TS lines 408-438).
    * :meth:`CompactionEngine.evaluate_leaf_trigger` — soft incremental
      leaf trigger (TS lines 447-459).
* **04-04** — the 3 anti-thrashing guards (this PR):
    * Guard 1 (Wave-12) — per-pass progress guard inside
      :meth:`CompactionEngine.compact_full_sweep` phase-1 + phase-2 loops
      (TS lines 705-712 + mirror 757-759).
    * Guard 2 — :meth:`CompactionEngine.compact_until_under` bail-out
      (TS line 849).
    * Guard 3 lives in :mod:`lossless_hermes.summarize` per issue 04-06
      (the ``_summarize_with_escalation`` cascade).

The :meth:`compact_full_sweep` + :meth:`compact_until_under` methods
landed by 04-04 are intentionally **skeletons**: they embody the guard
logic (and the surrounding control-flow scaffolding the guards need to
make sense) but defer the full leaf-pass / condensed-pass / chunk-
selection / persistence machinery to issues 04-02 and 04-03. Subclass
hooks (:meth:`CompactionEngine._run_leaf_pass`,
:meth:`CompactionEngine._run_condensed_pass`) let regression tests
drive the guards directly without a fully-migrated SQLite DB; 04-02 /
04-03 will fill in the production implementations of those hooks.

Subsequent issues 04-02..04-08 extend this class further with the
selection helpers, persistence calls, telemetry-decision logging, and
the cross-call anti-thrashing state.

### Why this lives at ``src/lossless_hermes/compaction.py`` (not the engine package)

Per ADR-024 §"Project layout" + ADR-027 §"Engine splitting", the
compaction algorithm is a *standalone* subsystem owned by
:class:`CompactionEngine`, not a mixin on
:class:`~lossless_hermes.engine.LCMEngine`. The TS source treats
``CompactionEngine`` as a peer of ``LcmContextEngine`` (the engine
holds a reference to a compaction engine instance; the compaction
engine reads/writes the same stores). Mirroring that split:

* ``src/lossless_hermes/compaction.py`` — :class:`CompactionEngine`
  + :class:`CompactionDecision` + :class:`CompactionConfig` + helpers.
* ``src/lossless_hermes/engine/compact.py`` — :class:`_CompactMixin`
  on the engine. **Calls into** :class:`CompactionEngine` once Epic 04
  lands; at 04-01 the mixin remains a passthrough + always-on-via-
  ``compress`` substitution (per ADR-010).

### Algorithm summary (per :doc:`porting-guides/assembler-compaction.md`
§"Trigger evaluation")

**``evaluate()``** — context-level threshold trigger:

1. ``stored_tokens = summary_store.get_context_token_count(conversation_id)``
   — running total persisted via context_items row arithmetic.
2. ``live_tokens = max(0, floor(observed_token_count))`` when the
   caller supplies a positive, finite observation; otherwise ``0``.
3. ``current_tokens = max(stored_tokens, live_tokens)`` — defensive
   max so a stale stored count (telemetry hasn't refreshed after
   ingest) does not under-trigger.
4. ``threshold = floor(config.context_threshold * token_budget)``.
5. Strict ``current_tokens > threshold`` decides. Reason is
   ``"threshold"`` when exceeded, ``"none"`` when not.

**``evaluate_leaf_trigger()``** — soft incremental trigger:

1. Resolve ``fresh_tail_ordinal`` from current context items
   (compaction's OWN walk, distinct from the assembler's — both look
   the same in this respect but compaction's helper takes
   :class:`~lossless_hermes.store.summary.ContextItemRecord` rows
   from the SummaryStore rather than already-resolved items, and
   reads the message token count from the ConversationStore via
   ``get_message_by_id``).
2. Sum raw-message tokens for items with ``ordinal <
   fresh_tail_ordinal``.
3. ``threshold = leaf_chunk_tokens_override or config.leaf_chunk_tokens``
   (default 20_000).
4. ``raw_tokens_outside_tail >= threshold`` decides (non-strict — soft
   trigger fires AT the boundary). Reason is ``"leaf-trigger"`` when
   exceeded, ``"below-leaf-trigger"`` when not.

### Sync / async (ADR-017)

All methods are sync (``def``, not ``async def``) per ADR-017 §"sync
stores everywhere". Stores are sync; the compaction engine reads from
stores; there is nothing to await. The TS source uses ``async`` only
because ``better-sqlite3``'s call sites in that branch were
async-wrapped — the underlying SQLite is synchronous.

See:

* ``docs/adr/017-sync-stores.md`` — all stores are sync.
* ``docs/adr/024-project-layout.md`` — top-level ``compaction.py``
  module placement (peer of ``assembler.py``).
* ``docs/adr/027-engine-splitting.md`` — engine mixin pattern.
* ``docs/adr/029-wave-fix-provenance.md`` — Wave-N comment format
  (PRESERVE markers on touched fix sites).
* ``docs/porting-guides/assembler-compaction.md`` §"Trigger
  evaluation" — algorithm walkthrough.
* ``lossless-claw/src/compaction.ts`` (LCM commit ``1f07fbd``,
  branch ``pr-613``) — TS source.

### Wave-N provenance

Issue 04-01 (trigger evaluation) is pre-Wave-N — the gate logic has
been stable since LCM v3. Issue 04-04 (this PR) introduces the
**Wave-12 per-pass progress guard** at the phase-1 + phase-2 loop
breakpoints inside :meth:`CompactionEngine.compact_full_sweep` (TS
lines 705-712 and the mirroring 757-759). Both sites carry the
inline ``# LCM Wave-12`` comment per ADR-029; ``grep -n "Wave-12"
src/lossless_hermes/compaction.py`` MUST find at least two hits.
Guard 2 (``compact_until_under`` bail-out) and Guard 3
(``_summarize_with_escalation`` escalation cascade, lives in
:mod:`lossless_hermes.summarize` per issue 04-06) are not flagged
Wave-N but are commented as "Anti-thrashing" intent so a future
contributor doesn't quietly drop them.

If a later issue ports additional Wave-N tagged TS lines into this
file (e.g. 04-02 may surface another Wave marker), that issue MUST
carry the ``# LCM Wave-N`` comment per ADR-029.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol

__all__ = [
    "CompactionConfig",
    "CompactionDecision",
    "CompactionEngine",
    "CompactionLevel",
    "CompactionReason",
    "CompactionResult",
    "CompactUntilUnderResult",
    "DEFAULT_LEAF_CHUNK_TOKENS",
    "EMPTY_FRESH_TAIL_ORDINAL",
    "LeafPassResult",
    "LeafTriggerReason",
    "LeafTriggerResult",
    "SummarizeFn",
]


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: TS ``DEFAULT_LEAF_CHUNK_TOKENS`` (compaction.ts line 180). The default
#: leaf-chunk size when neither :attr:`CompactionConfig.leaf_chunk_tokens`
#: nor the per-call override is set. The 20_000-token default is the
#: TS-canonical value; tuning happens via config not the constant.
DEFAULT_LEAF_CHUNK_TOKENS = 20_000

#: Sentinel "no fresh tail / boundary is at infinity" ordinal. Mirrors the
#: TS ``Infinity`` return from ``resolveFreshTailOrdinal`` (lines 922, 930).
#: Used as a comparator so the standard ``item.ordinal < boundary`` /
#: ``>= boundary`` checks behave correctly without special-casing.
#:
#: Same value as :data:`lossless_hermes.assembler.EMPTY_FRESH_TAIL_ORDINAL`
#: by intent; we don't import to keep the modules decoupled (the assembler
#: helper takes ``ResolvedItem`` while compaction's takes
#: ``ContextItemRecord`` — different walks over different inputs).
EMPTY_FRESH_TAIL_ORDINAL = sys.maxsize


# ---------------------------------------------------------------------------
# Public types — :class:`CompactionDecision` and :class:`LeafTriggerResult`
# ---------------------------------------------------------------------------

#: The set of reasons :class:`CompactionDecision` may carry. Mirrors TS
#: ``CompactionDecision.reason`` (compaction.ts line 13). ``"manual"``
#: is reserved for the operator-triggered path (the ``/lcm compact``
#: command in 08-04); ``evaluate()`` itself only ever returns
#: ``"threshold"`` or ``"none"``.
CompactionReason = Literal["threshold", "manual", "none"]


#: The set of reasons :class:`LeafTriggerResult` may carry. Not present in
#: TS (the TS leaf trigger result is reasonless — ``shouldCompact`` +
#: ``rawTokensOutsideTail`` + ``threshold`` only). The issue 04-01 spec
#: mandates a ``reason`` field for parity with :class:`CompactionDecision`,
#: making telemetry logging in 04-08 uniform across the two triggers.
LeafTriggerReason = Literal["leaf-trigger", "below-leaf-trigger"]


@dataclass(frozen=True)
class CompactionDecision:
    """The result of :meth:`CompactionEngine.evaluate`.

    Mirrors TS ``CompactionDecision`` (compaction.ts lines 11-16).

    Attributes:
        should_compact: ``True`` iff ``current_tokens > threshold``
            (strict greater-than — matches TS line 423).
        reason: ``"threshold"`` when ``should_compact=True``,
            ``"none"`` when ``False``. ``"manual"`` is reserved for
            operator-triggered compaction (08-04 ``/lcm compact``
            command) and never returned by :meth:`evaluate`.
        current_tokens: ``max(stored, live)`` — the larger of the
            persisted context-items running total and the caller's
            observed live count.
        threshold: ``floor(context_threshold * token_budget)``. A
            ``token_budget`` of 0 collapses to a ``threshold`` of 0;
            ``current_tokens > 0`` still trips the gate in that case
            (matches TS, by inspection of ``Math.floor(0.75 * 0) = 0``).
    """

    should_compact: bool
    reason: CompactionReason
    current_tokens: int
    threshold: int


@dataclass(frozen=True)
class LeafTriggerResult:
    """The result of :meth:`CompactionEngine.evaluate_leaf_trigger`.

    Mirrors the TS return shape (compaction.ts lines 447-451), with a
    ``reason`` field added for parity with :class:`CompactionDecision`
    so telemetry/decision logging in 04-08 can route both triggers
    through the same code path.

    Attributes:
        should_compact: ``True`` iff
            ``raw_tokens_outside_tail >= threshold`` (non-strict
            greater-or-equal — soft trigger fires AT the boundary,
            matches TS line 455).
        reason: ``"leaf-trigger"`` when ``should_compact=True``,
            ``"below-leaf-trigger"`` when ``False``. Added for parity
            with :class:`CompactionDecision`; not present in TS.
        raw_tokens_outside_tail: Sum of message token counts for
            context_items with ``ordinal < fresh_tail_ordinal``.
            Excludes the fresh tail (per the "outside" qualifier) and
            excludes summary-type items (only raw messages count).
        threshold: ``leaf_chunk_tokens_override or
            config.leaf_chunk_tokens`` (default 20_000). Returned in
            the result so callers/telemetry don't have to reach into
            the config.
    """

    should_compact: bool
    reason: LeafTriggerReason
    raw_tokens_outside_tail: int
    threshold: int


# ---------------------------------------------------------------------------
# Compaction-result types — :data:`CompactionLevel`,
# :class:`LeafPassResult`, :class:`CompactionResult`,
# :class:`CompactUntilUnderResult`
# ---------------------------------------------------------------------------


#: Escalation level recorded on each pass. Mirrors TS
#: ``CompactionLevel`` (compaction.ts line 63):
#:
#: * ``"normal"`` — first-pass summarize succeeded.
#: * ``"aggressive"`` — normal mode's output did not compress (Guard 3
#:   in :mod:`lossless_hermes.summarize` retried with aggressive).
#: * ``"fallback"`` — aggressive also did not compress (Guard 3 fell
#:   through to the deterministic non-LLM fallback).
#: * ``"capped"`` — summary was post-trimmed to honor
#:   ``summary_max_overage_factor``.
#:
#: 04-04 declares this type so :class:`CompactionResult` and
#: :class:`LeafPassResult` can reference it; the escalation logic that
#: produces ``"aggressive"`` / ``"fallback"`` lives in 04-06.
CompactionLevel = Literal["normal", "aggressive", "fallback", "capped"]


@dataclass(frozen=True)
class LeafPassResult:
    """The result of an internal leaf-pass or condensed-pass step.

    Mirrors TS ``PassResult`` (compaction.ts lines 75-94). 04-04 lands
    only the shape — the actual leaf-pass body that produces these
    records (and the persistence transaction that backs each
    ``summary_id``) lands in 04-02 / 04-03. Tests in 04-04 supply
    pre-built :class:`LeafPassResult` instances through the
    :meth:`CompactionEngine._run_leaf_pass` /
    :meth:`CompactionEngine._run_condensed_pass` subclass hooks so the
    anti-thrashing guards can be exercised end-to-end without a fully
    migrated SQLite DB.

    Attributes:
        summary_id: Identifier of the summary row created by the pass.
            Production code uses ``"sum_" + sha256(content+now)[:16]``;
            test stand-ins may use any non-empty string.
        level: Escalation level the summarizer settled on. See
            :data:`CompactionLevel`.
        content: The summary text the pass produced. Carried through to
            the next pass's ``previous_summary`` context (TS line 702
            ``previousSummaryContent = leafResult.content``).
        removed_tokens: Sum of source-message token counts that the
            pass replaced with the new summary. Feeds the running-delta
            arithmetic ``tokensAfter = tokensBefore - removed + added``
            (TS line 689).
        added_tokens: Token count of the newly created summary. Other
            half of the running-delta arithmetic.
    """

    summary_id: str
    level: CompactionLevel
    content: str
    removed_tokens: int
    added_tokens: int


@dataclass(frozen=True)
class CompactionResult:
    """The result of :meth:`CompactionEngine.compact_full_sweep` and
    :meth:`CompactionEngine.compact`.

    Mirrors TS ``CompactionResult`` (compaction.ts lines 18-32). 04-04
    lands the shape plus a small addition — ``passes_completed`` — so
    regression tests can assert "the Wave-12 guard broke us out of the
    loop after N passes, well below the budget". 04-08 finalizes the
    shape per ``docs/porting-guides/assembler-compaction.md``
    §"Public surface" by adding two more fields:

    * :attr:`reason` — human-readable why-not-compacted string for the
      no-op paths (``"under threshold"``, ``"no eligible chunk"``,
      ``"circuit breaker open"``, ``"auth failure"``). Producers may
      leave it ``None`` when ``action_taken=True``.
    * :attr:`phase_results` — per-phase nested results for
      :meth:`CompactionEngine.compact_full_sweep` aggregation. Empty
      for single-pass results; populated by the sweep so Epic 06's
      ``lcm_compact`` tool can report what each phase did.

    **Issue 04-07** adds the ``reason`` field so the circuit-breaker
    short-circuit at :meth:`CompactionEngine.compact` can return
    ``CompactionResult(action_taken=False, reason="circuit breaker open",
    ...)`` per the spec. Matches TS ``reason`` field on the
    ``CompactResult`` envelope returned by ``LcmContextEngine.compact``
    (engine.ts lines 3376-3380, 6895-6899).

    Attributes:
        action_taken: ``True`` iff at least one leaf or condensed pass
            ran to completion. Matches TS ``actionTaken``. ``False`` ↔
            no-op (under threshold, no eligible chunk, breaker open,
            auth failure).
        tokens_before: Token count read from the summary store at the
            start of the sweep.
        tokens_after: Token count at the end of the sweep, computed via
            running-delta arithmetic (NOT a fresh DB read — see TS
            line 668 for the delta-tracking comment).
        created_summary_id: ID of the most recent summary the sweep
            produced. ``None`` when ``action_taken=False``. Matches TS
            ``createdSummaryId`` (optional in TS; ``None`` here).
        condensed: ``True`` iff at least one phase-2 condensed pass ran.
            ``False`` when only leaf passes ran or nothing happened.
        level: Escalation level recorded by the most recent pass.
            ``None`` when no pass ran. Matches TS ``level``.
        passes_completed: Total number of leaf+condensed passes that
            ran successfully. **Not in TS** — added in 04-04 so
            regression tests for the Wave-12 guard can assert
            "broke early before exhausting the no-effective-bound
            phase loop". Telemetry in 04-08 may consume the field;
            production callers can ignore it.
        auth_failure: ``True`` iff a pass short-circuited because the
            LLM raised a provider-auth error. ``False`` by default.
            Matches TS ``authFailure`` (optional in TS; ``False`` here
            so the dataclass stays frozen + comparable).
        reason: Human-readable why-not-compacted string for no-op
            paths. ``None`` on the action-taken path. Added in 04-08
            (telemetry) and elaborated in 04-07 (circuit breaker
            integration): the :meth:`CompactionEngine.compact` wrapper
            uses this to distinguish "circuit breaker open" from
            "below threshold" / "compacted" without forcing callers
            to inspect ``auth_failure`` + ``action_taken`` individually.
            Consumed by Epic 06's ``lcm_compact`` tool to surface the
            "why nothing happened" verdict to the agent. Matches TS
            ``reason`` on the ``CompactResult`` envelope at
            ``engine.ts:3376-3380``.
        phase_results: Per-phase nested :class:`CompactionResult`
            instances. Empty for the single-result no-op paths and for
            04-04 skeletal sweeps that don't yet aggregate phase
            output. Reserved for the 04-02/04-03 production sweep to
            populate (phase-1 leaves, phase-2 condensed) so callers
            can introspect each phase's verdict separately.
    """

    action_taken: bool
    tokens_before: int
    tokens_after: int
    created_summary_id: str | None
    condensed: bool
    level: CompactionLevel | None
    passes_completed: int
    auth_failure: bool = False
    reason: str | None = None
    # Phase aggregation (per spec §"CompactionResult finalized shape").
    # ``field(default_factory=list)`` is required for mutable defaults
    # on frozen dataclasses — the list is freshly constructed per
    # instance so two CompactionResults don't share the same list.
    phase_results: list["CompactionResult"] = field(default_factory=list)


#: Type alias for the summarize callback ``compact_full_sweep`` /
#: ``compact_until_under`` accept. Mirrors TS ``CompactionSummarizeFn``
#: (compaction.ts lines 70-74). Sync per ADR-017 §"Option 1" — Hermes's
#: ``auxiliary_client.call_llm`` is synchronous, so the compaction
#: subsystem can stay sync end-to-end with no event-loop interaction.
#:
#: Args:
#:     text: The source text to summarize (concatenated raw messages or
#:         lower-tier summaries).
#:     aggressive: ``True`` when the caller wants the aggressive prompt
#:         template (Guard 3 escalation path in :mod:`summarize`).
#:     options: Optional dict carrying ``previous_summary`` /
#:         ``is_condensed`` / ``depth``. The 04-06 escalation cascade
#:         reads these.
#:
#: Returns:
#:     The summary text. Caller's anti-thrashing logic (Guard 3) checks
#:     the returned length against the input.
SummarizeFn = Callable[..., str]


@dataclass(frozen=True)
class CompactUntilUnderResult:
    """The result of :meth:`CompactionEngine.compact_until_under`.

    Mirrors the TS return shape (compaction.ts lines 786, 819-855).
    Carries the bail-out verdict + the round count + the final token
    count so callers + telemetry can distinguish "stopped because we
    succeeded" from "stopped because we were thrashing" from "exhausted
    max_rounds".

    Attributes:
        success: ``True`` iff ``final_tokens <= target_tokens``.
            ``False`` when Guard 2 (no-progress bail-out) tripped or
            ``max_rounds`` exhausted without reaching the target.
        rounds: Number of compaction rounds attempted. ``0`` when the
            initial token count was already at/below the target.
        final_tokens: The token count at the moment the loop exited
            (post-last-round, or the initial count when ``rounds=0``).
        auth_failure: ``True`` iff a round short-circuited on a
            provider-auth error.
    """

    success: bool
    rounds: int
    final_tokens: int
    auth_failure: bool = False


# ---------------------------------------------------------------------------
# Configuration — :class:`CompactionConfig`
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompactionConfig:
    """Configuration knobs for :class:`CompactionEngine`.

    Mirrors TS ``CompactionConfig`` (compaction.ts lines 34-61). Only the
    fields actually read by 04-01's trigger evaluators are documented as
    "load-bearing here" — the rest are placeholders that subsequent
    Epic 04 issues (leaf pass, condensed pass, full sweep) consume.

    The defaults match TS / Hermes / LCM v4.1 release values verified
    against ``lossless-claw/src/config.ts`` and the
    :class:`~lossless_hermes.db.config.LcmConfig` defaults at
    ``src/lossless_hermes/db/config.py`` lines 591-604.

    ### Why a new dataclass (not :class:`LcmConfig`)

    :class:`LcmConfig` is the full-fat user-facing config (pydantic
    model, validation aliases, nested objects). :class:`CompactionEngine`
    only needs a narrow slice; constructing the engine from a frozen
    dataclass keeps the unit-test surface trivial (``CompactionConfig(
    context_threshold=0.5)`` vs. building a full :class:`LcmConfig` for
    every test). The resolver in 04-08 (or whenever
    :class:`CompactionEngine` is wired into :class:`LCMEngine`) will
    project the :class:`LcmConfig` knobs into a
    :class:`CompactionConfig` instance.

    Attributes (load-bearing at 04-01):
        context_threshold: Fraction of ``token_budget`` above which
            :meth:`CompactionEngine.evaluate` returns
            ``should_compact=True``. Range ``[0.0, 1.0]``. Default
            ``0.75`` (TS line 36, Python default at
            ``db/config.py:591``).
        leaf_chunk_tokens: Per-call leaf-chunk size cap; also the
            default threshold for :meth:`evaluate_leaf_trigger` when
            no override is passed. Default ``20_000``
            (:data:`DEFAULT_LEAF_CHUNK_TOKENS`).
        fresh_tail_count: Maximum number of raw messages to protect at
            the tail of the context. Used by the private
            ``_resolve_fresh_tail_ordinal`` helper. Default ``8`` (TS
            line 38, "fresh tail turns" — not the same default as
            :class:`LcmConfig.fresh_tail_count` which is ``64`` for the
            assembler).
        fresh_tail_max_tokens: Optional token cap on the fresh tail.
            ``None`` means count-only gating. Default ``None``.

    Attributes (placeholder; consumed by 04-02..04-08):
        leaf_min_fanout: Minimum number of depth-0 summaries needed
            for condensation. Default ``8``.
        condensed_min_fanout: Minimum number of depth>=1 summaries
            needed for condensation. Default ``4``.
        condensed_min_fanout_hard: Relaxed minimum fanout for
            hard-trigger sweeps. Default ``2``.
        incremental_max_depth: Incremental depth passes after each
            leaf compaction. Default ``1``.
        leaf_target_tokens: Target tokens for leaf summaries.
            Default ``600``.
        condensed_target_tokens: Target tokens for condensed
            summaries. Default ``900``.
        max_rounds: Maximum compaction rounds for ``compact_until_under``.
            Default ``10``.
        timezone: IANA timezone string for summary timestamps. Default
            ``"UTC"``.
        summary_max_overage_factor: Maximum allowed overage factor for
            summaries relative to target tokens. Default ``3.0``.
    """

    # Load-bearing at 04-01 — evaluate / evaluate_leaf_trigger consume.
    context_threshold: float = 0.75
    leaf_chunk_tokens: int | None = DEFAULT_LEAF_CHUNK_TOKENS
    fresh_tail_count: int = 8
    fresh_tail_max_tokens: int | None = None

    # Placeholder — consumed by 04-02..04-08 leaf/condensed/sweep machinery.
    leaf_min_fanout: int = 8
    condensed_min_fanout: int = 4
    condensed_min_fanout_hard: int = 2
    incremental_max_depth: int = 1
    leaf_target_tokens: int = 600
    condensed_target_tokens: int = 900
    max_rounds: int = 10
    timezone: str = "UTC"
    summary_max_overage_factor: float = 3.0


# ---------------------------------------------------------------------------
# Store protocols — duck-typed contracts to keep this module decoupled
# ---------------------------------------------------------------------------
#
# We DON'T import :class:`~lossless_hermes.store.summary.SummaryStore` /
# :class:`~lossless_hermes.store.conversation.ConversationStore` directly,
# for two reasons:
#
# 1. Test isolation. Trigger-evaluation unit tests can supply a tiny
#    stand-in object satisfying these protocols without standing up a
#    full migrated SQLite DB. Integration tests use the real stores.
# 2. Decoupling. Future Epic 04 issues may add helper methods to the
#    stores; widening this Protocol intentionally signals "compaction
#    consumes this method" without dragging the entire store surface
#    into the type-check graph.
#
# At runtime these are just structural — Python's duck typing means any
# object with matching attribute names + signatures will satisfy them.


class _SummaryStoreLike(Protocol):
    """Narrow subset of :class:`SummaryStore` consumed by compaction."""

    def get_context_token_count(self, conversation_id: int) -> int: ...

    def get_context_items(self, conversation_id: int) -> "list[_ContextItemLike]": ...


class _ConversationStoreLike(Protocol):
    """Narrow subset of :class:`ConversationStore` consumed by compaction."""

    def get_message_by_id(
        self,
        message_id: int,
        *,
        include_suppressed: bool = ...,
    ) -> "_MessageRecordLike | None": ...


class _ContextItemLike(Protocol):
    """Structural shape compaction reads from each context_items row."""

    ordinal: int
    item_type: str  # "message" | "summary"
    message_id: int | None
    summary_id: str | None


class _MessageRecordLike(Protocol):
    """Structural shape compaction reads from each messages row."""

    content: str
    token_count: int


class _CompactionTelemetryStoreLike(Protocol):
    """Narrow contract compaction's persistence calls expect on the store.

    The full store contract lives in
    :class:`lossless_hermes.store.compaction_telemetry.CompactionTelemetryStore`
    (issue 01-10). This Protocol enumerates only the methods 04-08's call
    sites invoke. Stores are introspected with ``getattr`` rather than
    isinstance so a partially-implemented store (e.g., before 01-10's
    final write-paths land) degrades gracefully — see the
    :meth:`CompactionEngine._mark_*` helpers below.

    The methods are sync per ADR-017 §"sync stores everywhere". They
    return ``None`` and surface errors as exceptions; compaction's call
    sites swallow exceptions defensively (telemetry MUST NOT abort a
    successful compaction).
    """

    def mark_leaf_compaction_success(
        self,
        *,
        conversation_id: int,
        summary_id: str,
    ) -> None: ...

    def mark_condensed_compaction_success(
        self,
        *,
        conversation_id: int,
        summary_id: str,
    ) -> None: ...

    def mark_auth_failure(
        self,
        *,
        conversation_id: int,
    ) -> None: ...


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_leaf_chunk_tokens(
    config: CompactionConfig,
    leaf_chunk_tokens_override: int | None,
) -> int:
    """Normalize configured leaf-chunk size to a safe positive integer.

    Mirrors TS ``resolveLeafChunkTokens`` (compaction.ts lines 872-888).

    Precedence (matches TS verbatim):

    1. ``leaf_chunk_tokens_override`` when finite + positive → take it
       (after ``int()`` floor — TS ``Math.floor`` for positive values is
       equivalent to Python ``int()``).
    2. ``config.leaf_chunk_tokens`` when finite + positive → take it.
    3. :data:`DEFAULT_LEAF_CHUNK_TOKENS` (``20_000``).

    Args:
        config: The compaction config; ``config.leaf_chunk_tokens`` is
            consulted when no override is provided.
        leaf_chunk_tokens_override: Per-call override. ``None`` /
            non-positive falls through to the config / default.

    Returns:
        A positive integer leaf-chunk size.
    """
    # TS lines 873-879: override path. Python's int is unbounded and never
    # NaN/Infinity, so the TS ``Number.isFinite`` check collapses to "is a
    # plain int and > 0".
    if leaf_chunk_tokens_override is not None and leaf_chunk_tokens_override > 0:
        return int(leaf_chunk_tokens_override)
    # TS lines 880-886: config path. Same finite + positive guard.
    if config.leaf_chunk_tokens is not None and config.leaf_chunk_tokens > 0:
        return int(config.leaf_chunk_tokens)
    # TS line 887: default fallback.
    return DEFAULT_LEAF_CHUNK_TOKENS


def _resolve_fresh_tail_count(config: CompactionConfig) -> int:
    """Normalize configured fresh-tail count to a safe non-negative integer.

    Mirrors TS ``resolveFreshTailCount`` (compaction.ts lines 891-900).
    Returns ``0`` for non-positive / non-finite values (TS uses ``0`` as
    "no fresh-tail protection"); the caller's ordinal-resolver then
    short-circuits to :data:`EMPTY_FRESH_TAIL_ORDINAL`.
    """
    if config.fresh_tail_count > 0:
        return int(config.fresh_tail_count)
    return 0


def _resolve_fresh_tail_max_tokens(config: CompactionConfig) -> int | None:
    """Normalize configured fresh-tail token cap to a non-negative integer or ``None``.

    Mirrors TS ``resolveFreshTailMaxTokens`` (compaction.ts lines 903-912).
    Negative or non-finite values collapse to ``None`` (no cap), matching
    TS line 907 (``freshTailMaxTokens >= 0``).
    """
    if config.fresh_tail_max_tokens is not None and config.fresh_tail_max_tokens >= 0:
        return int(config.fresh_tail_max_tokens)
    return None


# ---------------------------------------------------------------------------
# :class:`CompactionEngine`
# ---------------------------------------------------------------------------


class CompactionEngine:
    """Compaction trigger evaluation + (in future issues) full machinery.

    Ports :class:`~lossless-claw/src/compaction.ts:CompactionEngine`
    (LCM commit ``1f07fbd`` on branch ``pr-613``) to Python.

    **Scope at issue 04-01:** trigger evaluation only.

    * :meth:`evaluate` — context-level threshold trigger.
    * :meth:`evaluate_leaf_trigger` — soft incremental leaf trigger.

    Issues 04-02..04-08 extend this class with the leaf pass, condensed
    pass, full sweep, ``compact_until_under``, anti-thrashing telemetry,
    and decision logging. The constructor signature is locked in at 04-01
    so subsequent issues only add methods.

    Args:
        conversation_store: A store satisfying the
            :class:`_ConversationStoreLike` protocol — must provide
            :meth:`get_message_by_id`. Production callers pass
            :class:`~lossless_hermes.store.conversation.ConversationStore`;
            tests may pass a minimal stand-in.
        summary_store: A store satisfying the :class:`_SummaryStoreLike`
            protocol — must provide :meth:`get_context_token_count` and
            :meth:`get_context_items`. Production callers pass
            :class:`~lossless_hermes.store.summary.SummaryStore`; tests
            may pass a minimal stand-in.
        config: The compaction configuration. The caller is responsible
            for projecting :class:`~lossless_hermes.db.config.LcmConfig`
            into a :class:`CompactionConfig` at wiring time.
        compaction_telemetry_store: Optional telemetry store satisfying
            :class:`_CompactionTelemetryStoreLike`. When provided, 04-08
            call sites mark leaf/condensed successes + auth failures
            after each pass. ``None`` (the default) makes the call
            sites no-op — Epic 02's cache-aware decision logic depends
            on these writes but the canonical telemetry path is the
            structured-log call in :meth:`_persist_compaction_event`,
            which fires regardless. The store's per-event methods are
            invoked with ``getattr`` introspection so a partial store
            (missing one of the three mark methods) degrades to
            no-op for the missing methods without raising.
        log: Optional :class:`logging.Logger` for compaction telemetry
            events. Defaults to a module-level logger named
            ``lossless_hermes.compaction`` — equivalent to the TS
            ``this.log`` field which defaults to ``NOOP_LCM_LOGGER``
            with structured extras when configured. The logger is
            invoked with ``extra={...}`` kwargs carrying the
            per-event fields (see :meth:`_persist_compaction_event`).
    """

    def __init__(
        self,
        conversation_store: _ConversationStoreLike,
        summary_store: _SummaryStoreLike,
        config: CompactionConfig,
        *,
        compaction_telemetry_store: _CompactionTelemetryStoreLike | None = None,
        log: logging.Logger | None = None,
    ) -> None:
        self._conversation_store = conversation_store
        self._summary_store = summary_store
        self._config = config
        self._compaction_telemetry_store = compaction_telemetry_store
        self._log = log if log is not None else logging.getLogger("lossless_hermes.compaction")

    # ------------------------------------------------------------------
    # evaluate() — context-level threshold trigger
    # ------------------------------------------------------------------

    def evaluate(
        self,
        conversation_id: int,
        token_budget: int,
        observed_token_count: int | None = None,
    ) -> CompactionDecision:
        """Evaluate whether context-level compaction should run this turn.

        Mirrors TS :meth:`CompactionEngine.evaluate` (compaction.ts lines
        408-438). Strict ``current_tokens > threshold`` decision — the
        gate fires only when the running token count *exceeds* the
        threshold, not when it equals it.

        Algorithm:

        1. ``stored_tokens =
           summary_store.get_context_token_count(conversation_id)``.
        2. ``live_tokens = floor(observed_token_count)`` when the
           caller supplies a positive, finite observation; else ``0``.
        3. ``current_tokens = max(stored_tokens, live_tokens)`` —
           defensive max so a stale stored count (telemetry hasn't
           refreshed after ingest) does not under-trigger.
        4. ``threshold = floor(context_threshold * token_budget)``.
        5. Return :class:`CompactionDecision` with
           ``should_compact = current_tokens > threshold`` and
           ``reason = "threshold" if exceeded else "none"``.

        Args:
            conversation_id: The conversation to evaluate.
            token_budget: The model's context window (or whatever
                budget the caller is sizing against). May be ``0`` —
                ``threshold`` then collapses to ``0`` and the gate
                trips on any positive ``current_tokens``.
            observed_token_count: Optional live observed token count
                from the caller (e.g., the host's pre-API-call
                pre-prompt assembly tally). When non-``None`` AND
                positive AND finite, it participates in the
                ``max(stored, live)`` defensive ordering. Non-positive
                values are ignored. Default ``None``.

        Returns:
            A :class:`CompactionDecision` carrying the verdict + the
            inputs the verdict was based on.
        """
        stored_tokens = self._summary_store.get_context_token_count(conversation_id)

        # TS lines 414-419: live_tokens path. Python ints are unbounded
        # + never NaN/Infinity, so the TS ``Number.isFinite`` check
        # collapses to "is a number + > 0". Use ``int()`` floor for
        # parity with ``Math.floor`` on positive values.
        if observed_token_count is not None and observed_token_count > 0:
            live_tokens = int(observed_token_count)
        else:
            live_tokens = 0

        # TS line 420: defensive max ordering.
        current_tokens = max(stored_tokens, live_tokens)

        # TS line 421: threshold = floor(context_threshold * budget).
        # Python ``int()`` truncates toward zero, equivalent to
        # ``Math.floor`` for non-negative inputs (the only inputs that
        # make sense here: a negative threshold would never fire).
        threshold = int(self._config.context_threshold * token_budget)

        # TS lines 423-437: strict > decision.
        if current_tokens > threshold:
            return CompactionDecision(
                should_compact=True,
                reason="threshold",
                current_tokens=current_tokens,
                threshold=threshold,
            )

        return CompactionDecision(
            should_compact=False,
            reason="none",
            current_tokens=current_tokens,
            threshold=threshold,
        )

    # ------------------------------------------------------------------
    # evaluate_leaf_trigger() — soft incremental trigger
    # ------------------------------------------------------------------

    def evaluate_leaf_trigger(
        self,
        conversation_id: int,
        leaf_chunk_tokens_override: int | None = None,
    ) -> LeafTriggerResult:
        """Evaluate whether the soft leaf trigger is active this turn.

        Mirrors TS :meth:`CompactionEngine.evaluateLeafTrigger`
        (compaction.ts lines 447-459). Sums raw-message token counts for
        items *outside* the fresh tail and compares against
        ``leaf_chunk_tokens`` (override if provided, else config, else
        :data:`DEFAULT_LEAF_CHUNK_TOKENS`).

        The leaf trigger uses ``>=`` (NOT strict ``>``) — the soft
        trigger fires AT the boundary (TS line 455). This lets a caller
        run an incremental maintenance pass exactly when the next leaf
        chunk's worth of raw messages has accumulated, without waiting
        for a "strict overflow" condition.

        Args:
            conversation_id: The conversation to evaluate.
            leaf_chunk_tokens_override: Per-call override of the
                trigger threshold. ``None`` falls through to
                ``config.leaf_chunk_tokens`` then
                :data:`DEFAULT_LEAF_CHUNK_TOKENS`.

        Returns:
            A :class:`LeafTriggerResult` carrying the verdict + the
            inputs the verdict was based on.
        """
        raw_tokens_outside_tail = self._count_raw_tokens_outside_fresh_tail(conversation_id)
        threshold = _resolve_leaf_chunk_tokens(self._config, leaf_chunk_tokens_override)

        # TS line 455: ``>=``, NOT strict ``>``. Soft trigger fires AT
        # the boundary; the strict-overflow gate is evaluate()'s job.
        if raw_tokens_outside_tail >= threshold:
            return LeafTriggerResult(
                should_compact=True,
                reason="leaf-trigger",
                raw_tokens_outside_tail=raw_tokens_outside_tail,
                threshold=threshold,
            )

        return LeafTriggerResult(
            should_compact=False,
            reason="below-leaf-trigger",
            raw_tokens_outside_tail=raw_tokens_outside_tail,
            threshold=threshold,
        )

    # ------------------------------------------------------------------
    # Internal helpers — fresh tail walk + raw token sum
    # ------------------------------------------------------------------

    def _resolve_fresh_tail_ordinal(
        self,
        context_items: "list[_ContextItemLike]",
    ) -> int:
        """Compute the ordinal boundary for protected fresh messages.

        Mirrors TS :meth:`CompactionEngine.resolveFreshTailOrdinal`
        (compaction.ts lines 919-962). **Distinct from the assembler's
        helper** at :func:`lossless_hermes.assembler.resolve_fresh_tail_ordinal`
        — the assembler walks already-resolved
        :class:`~lossless_hermes.assembler.ResolvedItem` objects while
        compaction walks :class:`_ContextItemLike` rows from the store
        and fetches per-message token counts on demand via
        ``conversation_store.get_message_by_id``.

        Algorithm (matches TS verbatim):

        * If ``fresh_tail_count <= 0`` → return
          :data:`EMPTY_FRESH_TAIL_ORDINAL` (no fresh-tail protection).
        * Filter ``context_items`` to raw-message rows
          (``item_type == "message" AND message_id is not None``).
        * If no raw messages → return
          :data:`EMPTY_FRESH_TAIL_ORDINAL` (nothing to protect).
        * Walk filtered list newest → oldest. Protect up to
          ``fresh_tail_count`` items, stopping early if adding the
          next item would push protected tokens past
          ``fresh_tail_max_tokens`` (the newest item is ALWAYS
          protected — TS lines 948-952 ``protectedCount > 0`` gate).
        * Return the ordinal of the oldest protected item.

        Args:
            context_items: Rows from
                :meth:`SummaryStore.get_context_items`. Must be in
                ordinal-ascending order (the standard return shape).

        Returns:
            The smallest ordinal in the protected fresh tail, or
            :data:`EMPTY_FRESH_TAIL_ORDINAL` when no item qualifies.
            Downstream code uses ``item.ordinal >= boundary`` for
            membership in the tail.
        """
        fresh_tail_count = _resolve_fresh_tail_count(self._config)
        # TS lines 921-923: zero / non-positive count short-circuits.
        if fresh_tail_count <= 0:
            return EMPTY_FRESH_TAIL_ORDINAL

        fresh_tail_max_tokens = _resolve_fresh_tail_max_tokens(self._config)

        # TS lines 926-928: filter to raw-message items.
        raw_message_items = [
            item
            for item in context_items
            if item.item_type == "message" and item.message_id is not None
        ]
        # TS lines 929-931: no raw messages → no fresh tail.
        if not raw_message_items:
            return EMPTY_FRESH_TAIL_ORDINAL

        protected_count = 0
        protected_tokens = 0
        tail_start_ordinal: int = EMPTY_FRESH_TAIL_ORDINAL

        # TS lines 937-959: walk newest → oldest.
        for item in reversed(raw_message_items):
            if protected_count >= fresh_tail_count:
                break

            # message_id non-None already enforced by filter above; the
            # narrow assert keeps ty happy when we pass it to
            # _get_message_token_count.
            assert item.message_id is not None
            message_tokens = self._get_message_token_count(item.message_id)

            # TS lines 948-952: newest is always kept (``protectedCount
            # > 0`` gate); subsequent items respect the cap.
            would_exceed_budget = (
                protected_count > 0
                and fresh_tail_max_tokens is not None
                and protected_tokens + message_tokens > fresh_tail_max_tokens
            )
            if would_exceed_budget:
                break

            tail_start_ordinal = item.ordinal
            protected_count += 1
            protected_tokens += message_tokens

        return tail_start_ordinal

    def _get_message_token_count(self, message_id: int) -> int:
        """Resolve a message's token count with content-length fallback.

        Mirrors TS :meth:`CompactionEngine.getMessageTokenCount`
        (compaction.ts lines 965-978).

        Path:

        1. Look up the message via
           ``conversation_store.get_message_by_id``. The compaction
           caller is internal (per the v4.1 Final.review.3 note in
           ``conversation.py:1048-1055``), so we MUST pass
           ``include_suppressed=True`` to count suppressed-but-not-yet-
           pruned messages in the leaf-trigger sum. Without that, a
           suppress-then-compact race would under-count the tail.
        2. If the row's ``token_count > 0`` and finite → return it.
        3. Else fall back to
           :func:`~lossless_hermes.estimate_tokens.estimate_tokens` on
           ``content``.
        4. Missing row (``None``) → return ``0``.

        Args:
            message_id: The messages.message_id primary key.

        Returns:
            A non-negative integer token count.
        """
        # Lazy import avoids a circular dependency if estimate_tokens
        # ever grows compaction-aware logic. Cheap on the hot path —
        # function objects are cached after first call.
        from lossless_hermes.estimate_tokens import estimate_tokens

        message = self._conversation_store.get_message_by_id(
            message_id,
            include_suppressed=True,
        )
        if message is None:
            return 0
        if message.token_count > 0:
            return int(message.token_count)
        return estimate_tokens(message.content)

    def _count_raw_tokens_outside_fresh_tail(self, conversation_id: int) -> int:
        """Sum raw-message tokens for context items outside the fresh tail.

        Mirrors TS :meth:`CompactionEngine.countRawTokensOutsideFreshTail`
        (compaction.ts lines 981-997).

        Walks ``context_items`` in ordinal order, summing the per-message
        token count for every item with ``ordinal < fresh_tail_ordinal``
        AND ``item_type == "message"`` AND ``message_id is not None``.
        Summary-type items are skipped (only raw messages count toward
        the leaf trigger — summaries are what the leaf pass *produces*).

        Args:
            conversation_id: The conversation to walk.

        Returns:
            Sum of message token counts strictly outside the protected
            fresh tail. ``0`` when no raw messages are outside the tail
            (or no context items exist at all).
        """
        context_items = self._summary_store.get_context_items(conversation_id)
        fresh_tail_ordinal = self._resolve_fresh_tail_ordinal(context_items)

        raw_tokens = 0
        for item in context_items:
            # TS lines 987-989: stop at the boundary. ``ordinal >=
            # boundary`` means we're in the fresh tail — don't count
            # those, and don't keep walking (the list is ordinal-
            # ascending so everything after is also in the tail).
            if item.ordinal >= fresh_tail_ordinal:
                break
            # TS lines 990-992: skip non-message rows. Only raw
            # messages contribute to the leaf trigger; summary rows
            # are what we'd PRODUCE during a leaf pass.
            if item.item_type != "message" or item.message_id is None:
                continue
            raw_tokens += self._get_message_token_count(item.message_id)

        return raw_tokens

    # ------------------------------------------------------------------
    # compact_full_sweep() — phase-1 + phase-2 loops with Wave-12 guards
    # ------------------------------------------------------------------

    def compact_full_sweep(
        self,
        conversation_id: int,
        token_budget: int,
        summarize: SummarizeFn,
        *,
        force: bool = False,
        hard_trigger: bool = False,
        summary_model: str | None = None,
    ) -> CompactionResult:
        """Run a full compaction sweep — phase-1 leaf passes + phase-2 condensed passes.

        Mirrors TS :meth:`CompactionEngine.compactFullSweep`
        (compaction.ts lines 626-774). **04-04 skeletal landing.** The
        guard logic (Wave-12 per-pass progress check at the phase-1 +
        phase-2 break sites) is load-bearing here; the leaf-chunk
        selection, persistence, telemetry, and full pass bodies land
        in issue 04-02 (and 04-03 for condensation). Tests inject
        :meth:`_run_leaf_pass` / :meth:`_run_condensed_pass` overrides
        to drive the guards.

        Algorithm (matches TS verbatim):

        1. Read ``tokens_before = summary_store.get_context_token_count``.
        2. Compute ``threshold = floor(context_threshold * token_budget)``.
        3. Evaluate the leaf trigger. If ``!force`` AND ``tokens_before <=
           threshold`` AND ``!leaf_trigger.should_compact`` → return a
           no-op result. (TS lines 640-647.)
        4. **Phase 1** — loop ``_run_leaf_pass`` until:
           * No leaf chunk left to process, OR
           * Pass returned ``None`` (provider-auth failure), OR
           * ``!force`` AND ``pass_tokens_after <= threshold`` (TS lines
             705-708 — under-threshold short-circuit), OR
           * **Guard 1 (Wave-12)** — pass made no progress against
             either the immediate floor (``pass_tokens_after >=
             pass_tokens_before``) or the running floor
             (``pass_tokens_after >= previous_tokens``). (TS lines
             709-712.)
        5. **Phase 2** — same loop pattern for condensed passes, only
           runs while ``force`` is set OR ``previous_tokens >
           threshold``. Same Wave-12 guard at the break point (TS lines
           757-759).
        6. Return :class:`CompactionResult` with running-delta
           ``tokens_after`` + the recorded escalation level + the
           passes-completed counter the 04-04 regression tests assert
           on.

        Args:
            conversation_id: The conversation to compact.
            token_budget: The model's context window. Threshold derives
                from this.
            summarize: The LLM summarize callback (sync per ADR-017).
                Passed through to :meth:`_run_leaf_pass` /
                :meth:`_run_condensed_pass`.
            force: When ``True``, skip the under-threshold short-circuit
                in step 3 + ignore the under-threshold break inside
                the phase loops. Used by
                :meth:`compact_until_under` to keep pressing past the
                target until either the budget is met or a guard
                trips.
            hard_trigger: Passed through to condensed-pass candidate
                selection (04-03). Loosens the fanout threshold.
            summary_model: Optional model override forwarded to the
                summarize callback.

        Returns:
            A :class:`CompactionResult`. ``action_taken=False`` when
            the trigger said "no work needed" or the context is
            empty; otherwise reflects the phase-1 + phase-2 deltas.
        """
        tokens_before = self._summary_store.get_context_token_count(conversation_id)
        threshold = int(self._config.context_threshold * token_budget)
        leaf_trigger = self.evaluate_leaf_trigger(conversation_id)

        # TS lines 640-647: short-circuit when neither trigger is active.
        if not force and tokens_before <= threshold and not leaf_trigger.should_compact:
            return CompactionResult(
                action_taken=False,
                tokens_before=tokens_before,
                tokens_after=tokens_before,
                created_summary_id=None,
                condensed=False,
                level=None,
                passes_completed=0,
                reason="under threshold",
            )

        # TS lines 649-657: empty-context short-circuit. 04-04 skeleton
        # uses ``get_context_items`` directly; 04-02 will switch to the
        # ``getContextItemsCached`` helper (TS line 649). For the
        # purposes of the guard tests this distinction is invisible —
        # the cached helper is a refcount wrapper, not a semantic
        # change.
        context_items = self._summary_store.get_context_items(conversation_id)
        if not context_items:
            return CompactionResult(
                action_taken=False,
                tokens_before=tokens_before,
                tokens_after=tokens_before,
                created_summary_id=None,
                condensed=False,
                level=None,
                passes_completed=0,
                reason="no eligible chunk",
            )

        action_taken = False
        condensed = False
        created_summary_id: str | None = None
        level: CompactionLevel | None = None
        previous_summary_content: str | None = None
        previous_tokens = tokens_before
        had_auth_failure = False
        passes_completed = 0
        # 04-08: per-phase CompactionResult records for the sweep's
        # ``phase_results`` aggregation. Each successful pass appends
        # one entry; the no-op-on-entry paths above return without
        # entries because nothing happened.
        phase_results: list[CompactionResult] = []

        # TS lines 670-713: phase-1 loop. Delta-tracked running token
        # count; on each pass we compute ``pass_tokens_after =
        # pass_tokens_before - removed + added`` (TS line 689).
        running_tokens = tokens_before
        while True:
            pass_tokens_before = running_tokens
            leaf_result = self._run_leaf_pass(
                conversation_id=conversation_id,
                summarize=summarize,
                previous_summary_content=previous_summary_content,
                summary_model=summary_model,
            )
            if leaf_result is None:
                # TS line 685-687: either nothing left to compact (TS
                # treats ``leafChunk.items.length === 0`` as a clean
                # break) OR a provider-auth failure surfaced from
                # ``_leafPass``. 04-04 skeleton funnels both through
                # the same ``None`` return; 04-02 will split them.
                break

            pass_tokens_after = (
                pass_tokens_before - leaf_result.removed_tokens + leaf_result.added_tokens
            )

            action_taken = True
            created_summary_id = leaf_result.summary_id
            level = leaf_result.level
            previous_summary_content = leaf_result.content
            running_tokens = pass_tokens_after
            passes_completed += 1

            # 04-08: persist the per-pass record + mark the leaf-success
            # in the telemetry store. The structured-log path runs
            # always; the store-write degrades to no-op when the
            # store doesn't implement the method.
            leaf_pass_record = CompactionResult(
                action_taken=True,
                tokens_before=pass_tokens_before,
                tokens_after=pass_tokens_after,
                created_summary_id=leaf_result.summary_id,
                condensed=False,
                level=leaf_result.level,
                passes_completed=1,
            )
            phase_results.append(leaf_pass_record)
            self._persist_compaction_event(conversation_id, leaf_pass_record)
            self._mark_leaf_compaction_success(
                conversation_id=conversation_id,
                summary_id=leaf_result.summary_id,
            )

            # TS lines 705-708: under-threshold short-circuit (only
            # honored when not forced). Sets the running floor so the
            # phase-2 ``force || previousTokens > threshold`` gate
            # behaves correctly when we transition.
            if not force and pass_tokens_after <= threshold:
                previous_tokens = pass_tokens_after
                break

            # LCM Wave-12 (2026-04-22): per-pass progress guard prevents
            # thrashing when the summarizer returns near-input-size
            # output. Break if pass made no progress against either the
            # immediate or running floor.
            # Original: lossless-claw/src/compaction.ts:709-712.
            if pass_tokens_after >= pass_tokens_before or pass_tokens_after >= previous_tokens:
                break
            previous_tokens = pass_tokens_after

        # TS lines 716-761: phase-2 loop. Only runs while we're either
        # forced or still over the threshold; otherwise phase-1 already
        # ended the sweep.
        while force or previous_tokens > threshold:
            pass_tokens_before = running_tokens
            condense_result = self._run_condensed_pass(
                conversation_id=conversation_id,
                hard_trigger=hard_trigger,
                summarize=summarize,
                summary_model=summary_model,
            )
            if condense_result is None:
                # TS lines 721-723 (no candidate) + 733-735 (auth
                # failure). 04-04 skeleton conflates these the same
                # way phase-1 does.
                break

            pass_tokens_after = (
                pass_tokens_before - condense_result.removed_tokens + condense_result.added_tokens
            )

            action_taken = True
            condensed = True
            created_summary_id = condense_result.summary_id
            level = condense_result.level
            running_tokens = pass_tokens_after
            passes_completed += 1

            # 04-08: persist the per-pass record + mark the condensed-
            # success in the telemetry store. Same degrade-safe path
            # as phase-1 above.
            condensed_pass_record = CompactionResult(
                action_taken=True,
                tokens_before=pass_tokens_before,
                tokens_after=pass_tokens_after,
                created_summary_id=condense_result.summary_id,
                condensed=True,
                level=condense_result.level,
                passes_completed=1,
            )
            phase_results.append(condensed_pass_record)
            self._persist_compaction_event(conversation_id, condensed_pass_record)
            self._mark_condensed_compaction_success(
                conversation_id=conversation_id,
                summary_id=condense_result.summary_id,
            )

            # TS lines 753-755: under-threshold short-circuit.
            if not force and pass_tokens_after <= threshold:
                previous_tokens = pass_tokens_after
                break

            # LCM Wave-12 (2026-04-22): per-pass progress guard, mirror
            # of the phase-1 break — prevents thrashing when the
            # condensed-pass summarizer also returns near-input-size
            # output. Break if pass made no progress against either
            # the immediate or running floor.
            # Original: lossless-claw/src/compaction.ts:757-759.
            if pass_tokens_after >= pass_tokens_before or pass_tokens_after >= previous_tokens:
                break
            previous_tokens = pass_tokens_after

        return CompactionResult(
            action_taken=action_taken,
            tokens_before=tokens_before,
            tokens_after=running_tokens,
            created_summary_id=created_summary_id,
            condensed=condensed,
            level=level,
            passes_completed=passes_completed,
            auth_failure=had_auth_failure,
            phase_results=phase_results,
        )

    # ------------------------------------------------------------------
    # compact_until_under() — bounded-rounds bail-out with Guard 2
    # ------------------------------------------------------------------

    def compact_until_under(
        self,
        conversation_id: int,
        token_budget: int,
        summarize: SummarizeFn,
        *,
        target_tokens: int | None = None,
        current_tokens: int | None = None,
        summary_model: str | None = None,
    ) -> CompactUntilUnderResult:
        """Repeatedly invoke :meth:`compact_full_sweep` until under the target.

        Mirrors TS :meth:`CompactionEngine.compactUntilUnder` (compaction.ts
        lines 779-867). **04-04 skeletal landing.** Guard 2 (the
        ``!result.actionTaken || result.tokensAfter >= lastTokens``
        bail-out) is the load-bearing piece this issue ports. The
        per-round ``compact_full_sweep`` call is the same one issue
        04-02 fully ports; 04-04 keeps the call live so the regression
        test can drive the round loop end-to-end.

        Algorithm (matches TS verbatim):

        1. Resolve ``target_tokens`` — caller's positive value, else
           ``token_budget``. (TS lines 799-804.)
        2. Read ``stored_tokens`` + optional ``current_tokens`` (live),
           seed ``last_tokens = max(stored, live)``. (TS lines 806-813.)
        3. If ``last_tokens < target_tokens`` already → return
           ``success=True, rounds=0``. Equality is intentionally
           treated as "still needs compaction" — see TS lines 815-820.
        4. Loop up to ``config.max_rounds``:
           * Call ``compact_full_sweep(force=True)`` (forced because
             :meth:`compact_until_under` is itself the "force"
             entrypoint — TS line 827).
           * Auth-failure path returns ``success=False, auth_failure=
             True``. (TS lines 831-838.)
           * Success path (``tokens_after <= target_tokens``) returns
             ``success=True``. (TS lines 840-846.)
           * **Guard 2** — bail out when the round made no progress
             (``!action_taken`` or ``tokens_after >= last_tokens``).
             (TS lines 848-855.)
           * Otherwise advance ``last_tokens`` to the new floor.
        5. ``max_rounds`` exhausted → return ``success = (final_tokens
           <= target_tokens)`` (the boundary case in TS lines 860-866;
           usually ``False`` unless the very last round just barely
           made it).

        Args:
            conversation_id: The conversation to compact.
            token_budget: The model's context window.
            summarize: The LLM summarize callback (sync, ADR-017).
            target_tokens: Optional target. When ``None`` / non-positive
                falls through to ``token_budget``. (TS treats the
                non-finite path same way.)
            current_tokens: Optional live token count override; max'd
                with the stored count.
            summary_model: Optional model override forwarded down.

        Returns:
            A :class:`CompactUntilUnderResult` capturing the round
            count + final tokens + success verdict + auth-failure
            flag.
        """
        # TS lines 799-804: resolve target. Negative / zero collapses to
        # the budget; Python ints are unbounded + never NaN so the TS
        # ``Number.isFinite`` check has no analogue.
        effective_target = (
            int(target_tokens) if target_tokens is not None and target_tokens > 0 else token_budget
        )

        stored_tokens = self._summary_store.get_context_token_count(conversation_id)
        live_tokens = (
            int(current_tokens) if current_tokens is not None and current_tokens > 0 else 0
        )
        last_tokens = max(stored_tokens, live_tokens)

        # TS lines 815-820: ``< target`` ⇒ already under. Equality is
        # NOT a success — TS comment says forced-overflow recovery may
        # pass an observed count equal to budget, and we still want to
        # try one more compaction pass to free up framing-overhead
        # headroom.
        if last_tokens < effective_target:
            return CompactUntilUnderResult(
                success=True,
                rounds=0,
                final_tokens=last_tokens,
            )

        for round_index in range(1, self._config.max_rounds + 1):
            result = self.compact_full_sweep(
                conversation_id=conversation_id,
                token_budget=token_budget,
                summarize=summarize,
                force=True,  # TS line 827: forced sweep inside the round loop.
                summary_model=summary_model,
            )

            # TS lines 831-838: short-circuit on provider-auth failure
            # so caller can surface a clean error instead of looping.
            if result.auth_failure:
                return CompactUntilUnderResult(
                    success=False,
                    rounds=round_index,
                    final_tokens=result.tokens_after,
                    auth_failure=True,
                )

            # TS lines 840-846: success path.
            if result.tokens_after <= effective_target:
                return CompactUntilUnderResult(
                    success=True,
                    rounds=round_index,
                    final_tokens=result.tokens_after,
                )

            # Anti-thrashing: bail out if a single round made no progress.
            # Either the sweep took no action at all (no eligible
            # chunks) or it did but the post-sweep token count is
            # still >= the pre-round floor. Without this, the loop
            # would burn through every ``max_rounds`` slot retrying a
            # configuration that cannot make progress.
            # Original: lossless-claw/src/compaction.ts:848-855.
            if not result.action_taken or result.tokens_after >= last_tokens:
                return CompactUntilUnderResult(
                    success=False,
                    rounds=round_index,
                    final_tokens=result.tokens_after,
                )

            last_tokens = result.tokens_after

        # TS lines 860-866: ``max_rounds`` exhausted. The final-tokens
        # boundary case (``finalTokens <= targetTokens``) is the only
        # way ``success`` ends up True via this exit — usually it's
        # False (otherwise the in-loop success short-circuit would
        # have returned).
        return CompactUntilUnderResult(
            success=last_tokens <= effective_target,
            rounds=self._config.max_rounds,
            final_tokens=last_tokens,
        )

    # ------------------------------------------------------------------
    # Telemetry write paths (issue 04-08)
    # ------------------------------------------------------------------
    #
    # Two surfaces:
    #
    # 1. Structured-log telemetry — :meth:`_persist_compaction_event` and
    #    :meth:`_persist_compaction_events`. Mirrors TS
    #    ``persistCompactionEvents`` (compaction.ts:1754-1812) +
    #    ``persistCompactionEvent`` (1815-1830). Per the porting guide
    #    §"Telemetry write paths" and the spec ``epics/04-compaction/
    #    04-08-telemetry-write.md``: this is intentionally a structured
    #    log call, NOT a chat-message-row write. Earlier LCM versions
    #    appended a synthetic assistant message describing each
    #    compaction; the LCM team removed that to avoid polluting the
    #    conversation history. The summary write itself (in
    #    ``_leafPass`` / ``_condensedPass`` transactions) is the
    #    canonical persistence point.
    #
    # 2. Telemetry-store integration — :meth:`_mark_leaf_compaction_success`,
    #    :meth:`_mark_condensed_compaction_success`, and
    #    :meth:`_mark_auth_failure`. These call the optional
    #    :class:`CompactionTelemetryStore` (issue 01-10) so Epic 02's
    #    cache-aware ``evaluate_incremental_compaction`` can read
    #    "when did we last compact this conversation?". The store is
    #    optional; missing methods on the store degrade to a no-op
    #    (per spec §"Telemetry-store calls are stubbed-safe").

    def _persist_compaction_event(
        self,
        conversation_id: int,
        result: CompactionResult,
    ) -> None:
        """Emit a structured log record for one compaction pass.

        Mirrors TS :meth:`CompactionEngine.persistCompactionEvent`
        (compaction.ts lines 1815-1830). **Despite the name, no DB row
        is written** — this is purely a log call. The TS source
        accumulates the same fields into a ``content`` template string
        + ``this.log.info``; the Python port emits them as ``extra=``
        kwargs so structured-logging consumers can index by field
        rather than parsing the message.

        Why no chat-message row: earlier LCM versions appended a
        synthetic assistant message describing each compaction
        ("LCM compaction leaf pass (normal): 100000 -> 60000"). That
        was removed to prevent compaction events from polluting the
        canonical conversation history. The summary row itself, written
        inside the leaf-pass / condensed-pass transactions, is the
        durable record of what happened.

        Args:
            conversation_id: The conversation the pass operated on.
            result: A per-pass :class:`CompactionResult` carrying the
                fields we want indexed. Producer (typically
                :meth:`compact_full_sweep`) constructs a one-pass
                ``CompactionResult`` for each successful pass.
        """
        # Compute the post-vs-pre delta once so consumers don't have
        # to re-derive it from the two raw counts.
        delta = result.tokens_before - result.tokens_after
        # TS line 1827: ``this.log.info`` with a templated message. The
        # template carries the human-readable summary; the extras
        # carry the structured fields. We mirror the TS message format
        # so log scrapers that parse it continue to work, AND emit
        # extras for structured-log consumers.
        self._log.info(
            "lcm compaction %s pass (%s): %d -> %d conversation=%d summary=%s",
            # The "pass" field is condensed=True/False — phase-2 vs
            # phase-1. We surface a human-readable label here.
            "condensed" if result.condensed else "leaf",
            result.level if result.level is not None else "none",
            result.tokens_before,
            result.tokens_after,
            conversation_id,
            result.created_summary_id if result.created_summary_id is not None else "<none>",
            extra={
                "compaction_event": {
                    "conversation_id": conversation_id,
                    "action_taken": result.action_taken,
                    "tokens_before": result.tokens_before,
                    "tokens_after": result.tokens_after,
                    "delta": delta,
                    "level": result.level,
                    "condensed": result.condensed,
                    "auth_failure": result.auth_failure,
                    "created_summary_id": result.created_summary_id,
                    "reason": result.reason,
                },
            },
        )

    def _persist_compaction_events(
        self,
        conversation_id: int,
        results: list[CompactionResult | None],
    ) -> None:
        """Persist a list of per-pass results, skipping ``None`` entries.

        Mirrors TS :meth:`CompactionEngine.persistCompactionEvents`
        (compaction.ts lines 1754-1812). The TS version takes a richer
        record bundling leaf+condensed results into a single call; the
        Python spec at ``epics/04-compaction/04-08-telemetry-write.md``
        finalizes the shape as a list of :class:`CompactionResult`
        (or ``None``) so producers that only have one phase's result
        don't have to synthesize a sentinel for the other.

        Args:
            conversation_id: The conversation the passes operated on.
            results: List of per-pass results. ``None`` entries are
                skipped (matches TS lines 1771-1773 + 1785/1799 None-
                guards).
        """
        for r in results:
            if r is not None:
                self._persist_compaction_event(conversation_id, r)

    def _mark_leaf_compaction_success(
        self,
        *,
        conversation_id: int,
        summary_id: str,
    ) -> None:
        """Record a successful leaf compaction in the telemetry store.

        Spec §"Compaction telemetry store updates": call after every
        successful ``_leaf_pass``. Updates the store's
        ``last_leaf_compaction_at`` and bumps the
        ``leaf_compaction_count``. The exact field set is defined by
        :class:`CompactionTelemetryStore` (Epic 01 issue 01-10).

        Defensively introspected with :func:`getattr` so a partial
        store (missing this method while the rest of the API is
        present) degrades to a no-op instead of raising. Telemetry
        failures MUST NOT abort a successful compaction.

        Args:
            conversation_id: The conversation whose telemetry to bump.
            summary_id: ID of the summary row the leaf pass wrote.
                Carried so the store can correlate the bump with the
                produced summary (the store may use it to ignore a
                duplicate call from a retry).
        """
        self._mark_telemetry(
            "mark_leaf_compaction_success",
            conversation_id=conversation_id,
            summary_id=summary_id,
        )

    def _mark_condensed_compaction_success(
        self,
        *,
        conversation_id: int,
        summary_id: str,
    ) -> None:
        """Record a successful condensed compaction in the telemetry store.

        Same contract as :meth:`_mark_leaf_compaction_success` but for
        depth>=1 condensed passes. Updates the store's
        ``last_condensed_compaction_at`` and the per-depth counters.
        """
        self._mark_telemetry(
            "mark_condensed_compaction_success",
            conversation_id=conversation_id,
            summary_id=summary_id,
        )

    def _mark_auth_failure(
        self,
        *,
        conversation_id: int,
    ) -> None:
        """Record a provider-auth failure in the telemetry store.

        Spec §"Compaction telemetry store updates": call when a
        ``_leaf_pass`` / ``_condensed_pass`` short-circuits because
        the summarizer raised :class:`LcmProviderAuthError` (the
        ``auth_failure=True`` path). Bumps the store's
        ``consecutive_auth_failures`` counter so the cache-aware
        decision logic can back off.

        Called from production code in issues 04-02 / 04-03 once the
        leaf-pass body raises ``LcmProviderAuthError``; 04-08 lands
        only the call-site helper.
        """
        self._mark_telemetry(
            "mark_auth_failure",
            conversation_id=conversation_id,
        )

    def _mark_telemetry(
        self,
        method_name: str,
        **kwargs: Any,
    ) -> None:
        """Defensively invoke a telemetry-store method by name.

        Centralizes the "store is optional + method may not exist +
        errors MUST NOT propagate" defensiveness. The three
        ``_mark_*`` helpers above delegate here. Failure modes
        handled:

        * ``self._compaction_telemetry_store is None`` — skip silently
          (the engine was constructed without a telemetry store; Epic
          01 store may not be wired yet at the call site).
        * Store doesn't implement ``method_name`` — skip silently (a
          partial store, e.g., before 01-10's write paths land).
        * Method raises any exception — swallow it and emit a debug
          log line. Telemetry MUST NOT abort a successful compaction.

        Args:
            method_name: One of ``mark_leaf_compaction_success``,
                ``mark_condensed_compaction_success``,
                ``mark_auth_failure``. Looked up on the store.
            **kwargs: Forwarded to the store method as keyword args.
        """
        store = self._compaction_telemetry_store
        if store is None:
            return
        method = getattr(store, method_name, None)
        if method is None:
            return
        try:
            method(**kwargs)
        except Exception:  # noqa: BLE001 — telemetry must never abort compaction.
            # Log at debug rather than warning: a missing telemetry
            # row is not actionable for the operator, and a failing
            # store call usually means a downstream test-fixture is
            # mid-port. The caller (Epic 02 cache-aware path) will
            # observe the missing bump on its next read.
            self._log.debug(
                "compaction telemetry call failed: %s",
                method_name,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Subclass hooks — pluggable leaf/condensed-pass bodies for 04-02/03
    # ------------------------------------------------------------------
    #
    # 04-04 needs to invoke a leaf or condensed pass to exercise the
    # Wave-12 guard and the ``compact_until_under`` bail-out. But the
    # actual pass bodies (chunk selection + summarizer call +
    # persistence transaction + cache invalidation) land in 04-02 and
    # 04-03. So 04-04 exposes ``_run_leaf_pass`` and
    # ``_run_condensed_pass`` as overridable hooks: production wiring
    # will replace these with the real implementations once 04-02 and
    # 04-03 land; regression tests for 04-04 supply stub overrides that
    # drive controlled progress (or lack thereof) into the guards.

    def _run_leaf_pass(
        self,
        *,
        conversation_id: int,
        summarize: SummarizeFn,
        previous_summary_content: str | None,
        summary_model: str | None,
    ) -> LeafPassResult | None:
        """Run one leaf pass and return the resulting summary, or ``None``.

        04-04 skeletal stub. Production body lands in issue 04-02
        (``_leafPass`` in TS, ``compaction.ts:1492-1607``). Return
        ``None`` to terminate the phase-1 loop — semantic overload that
        04-02 will split into "no chunk left" (clean break) vs
        "provider-auth failure" (sets ``auth_failure=True`` on the
        result).

        04-04 default returns ``None`` so a vanilla
        :class:`CompactionEngine` exits the phase-1 loop immediately
        without doing any work. Regression tests subclass and override
        to return controlled :class:`LeafPassResult` instances.

        Args:
            conversation_id: The conversation being compacted.
            summarize: The summarize callback passed down from
                :meth:`compact_full_sweep`.
            previous_summary_content: ``content`` field from the most
                recent ``LeafPassResult``, or ``None`` on the first
                call. Provides iterative-summarization continuity.
            summary_model: Optional model override for this pass.

        Returns:
            A :class:`LeafPassResult` if a pass produced a summary,
            ``None`` otherwise. The Wave-12 guard inspects
            ``removed_tokens`` + ``added_tokens`` (via the
            running-delta arithmetic) to decide whether the pass made
            progress.
        """
        # Silence the unused-arg warnings without ignoring the names —
        # subclasses in 04-02 + tests use every parameter.
        del conversation_id, summarize, previous_summary_content, summary_model
        return None

    def _run_condensed_pass(
        self,
        *,
        conversation_id: int,
        hard_trigger: bool,
        summarize: SummarizeFn,
        summary_model: str | None,
    ) -> LeafPassResult | None:
        """Run one condensed pass and return the resulting summary, or ``None``.

        04-04 skeletal stub. Production body lands in issue 04-03
        (the condensed-pass + chunk-selection helpers in TS). Same
        ``None``-terminates-the-loop contract as :meth:`_run_leaf_pass`.

        Args:
            conversation_id: The conversation being compacted.
            hard_trigger: Whether the caller is a hard-trigger sweep
                (loosens condensed-pass fanout thresholds).
            summarize: The summarize callback passed down from
                :meth:`compact_full_sweep`.
            summary_model: Optional model override for this pass.

        Returns:
            A :class:`LeafPassResult` if a pass produced a summary,
            ``None`` otherwise. Phase-2's Wave-12 guard inspects the
            same fields as phase-1.
        """
        del conversation_id, hard_trigger, summarize, summary_model
        return None
