"""Compaction engine — trigger evaluation foundation (issue 04-01).

Ports :class:`CompactionEngine` from
``lossless-claw/src/compaction.ts`` (LCM commit ``1f07fbd`` on branch
``pr-613``) to Python. **Issue 04-01 lands only the trigger-evaluation
foundation** — the two evaluators that decide whether compaction should
run this turn:

* :meth:`CompactionEngine.evaluate` — context-level threshold trigger
  (TS lines 408-438). Returns a :class:`CompactionDecision` carrying
  ``should_compact``, ``reason``, ``current_tokens``, ``threshold``.
* :meth:`CompactionEngine.evaluate_leaf_trigger` — soft incremental
  leaf trigger (TS lines 447-459). Returns a :class:`LeafTriggerResult`
  carrying ``should_compact``, ``reason``, ``raw_tokens_outside_tail``,
  ``threshold``.

The heavy machinery (leaf pass, condensed pass, full sweep,
``compact_until_under``, telemetry-decision logging, anti-thrashing
state) ships in subsequent issues 04-02..04-08. This file is the
foundation: subsequent issues extend :class:`CompactionEngine` with
additional methods + private helpers, sharing this module's imports +
``config`` / ``store`` plumbing.

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

This file does NOT touch any of the eight known Wave-N fix sites
enumerated in ADR-029 §"Known Wave-N fixes to preserve". The evaluate
trigger logic is pre-Wave-N (it's been stable since LCM v3); the
load-bearing fixes are in the leaf-pass / condensed-pass / telemetry
machinery that lands in 04-02..04-08. If a Wave-N tagged TS line is
ported into this file by a follow-up issue, that issue MUST carry the
``# LCM Wave-N`` comment per ADR-029.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Literal, Protocol

__all__ = [
    "CompactionConfig",
    "CompactionDecision",
    "CompactionEngine",
    "CompactionReason",
    "DEFAULT_LEAF_CHUNK_TOKENS",
    "EMPTY_FRESH_TAIL_ORDINAL",
    "LeafTriggerReason",
    "LeafTriggerResult",
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
    """

    def __init__(
        self,
        conversation_store: _ConversationStoreLike,
        summary_store: _SummaryStoreLike,
        config: CompactionConfig,
    ) -> None:
        self._conversation_store = conversation_store
        self._summary_store = summary_store
        self._config = config

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
