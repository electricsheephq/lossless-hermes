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

import hashlib
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Literal, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
    "LcmProviderAuthError",
    "LeafChunkSelection",
    "LeafPassOutcome",
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
class LeafPassOutcome:
    """Discriminated return value of :meth:`CompactionEngine._run_leaf_pass`.

    Distinguishes the three terminal states a leaf-pass (or condensed-pass)
    hook can reach:

    * **summary produced** — ``summary`` set, ``auth_failure=False``;
      caller increments the running token delta and continues the phase
      loop.
    * **empty chunk / voluntary skip** — ``summary=None``,
      ``auth_failure=False``; caller breaks the phase loop cleanly. TS
      equivalent: ``leafChunk.items.length === 0`` short-circuit at
      ``compaction.ts:673-675`` (Phase-1), or the condensed-pass
      ``!candidate`` short-circuit at ``compaction.ts:721-723``.
    * **provider-auth failure** — ``summary=None``, ``auth_failure=True``;
      caller breaks the phase loop AND sets ``CompactionResult.auth_failure
      = True`` so :meth:`compact_until_under` can short-circuit the round
      loop instead of retrying. TS equivalent: the ``hadAuthFailure =
      true; break`` pair at ``compaction.ts:685-687`` (Phase-1) and
      ``compaction.ts:733-735`` (Phase-2).

    Issue 04-02 introduced this split. Before 04-02 the protocol was just
    ``LeafPassResult | None`` and both empty-chunk and auth-failure
    funneled through the same ``None`` return — meaning
    :meth:`compact_full_sweep` could not set ``auth_failure=True`` on the
    final :class:`CompactionResult` and the downstream
    :meth:`compact_until_under` round loop would silently retry across
    a provider outage. PR #81 reviewer MAJOR finding.

    Attributes:
        summary: The :class:`LeafPassResult` produced by a successful
            pass. ``None`` for both empty-chunk and auth-failure
            terminations.
        auth_failure: ``True`` iff the pass aborted because the
            summarizer raised :class:`LcmProviderAuthError`. ``False``
            for empty-chunk / voluntary-skip terminations.
    """

    summary: LeafPassResult | None
    auth_failure: bool = False


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
    """Narrow subset of :class:`SummaryStore` consumed by compaction.

    Issue 04-02 extends the protocol with the persistence methods the
    leaf-pass body needs: :meth:`get_summary` (to resolve prior leaf-
    summary continuity), :meth:`insert_summary` (write the new leaf),
    :meth:`link_summary_to_messages` (DAG edges), and
    :meth:`replace_context_range_with_summary` (atomic context swap).
    :meth:`with_transaction` is required so the leaf-pass write +
    DAG link + context swap commit together (TS lines 1565-1603).
    """

    def get_context_token_count(self, conversation_id: int) -> int: ...

    def get_context_items(self, conversation_id: int) -> "list[_ContextItemLike]": ...

    # --- Methods added in issue 04-02 -----------------------------------------

    def get_summary(self, summary_id: str) -> "_SummaryRecordLike | None": ...

    def insert_summary(self, input_: Any) -> Any: ...

    def link_summary_to_messages(
        self,
        summary_id: str,
        message_ids: list[int],
    ) -> None: ...

    def replace_context_range_with_summary(self, input_: Any) -> None: ...

    def with_transaction(self) -> Any: ...


class _ConversationStoreLike(Protocol):
    """Narrow subset of :class:`ConversationStore` consumed by compaction.

    Issue 04-02 extends the protocol with :meth:`get_message_parts` so
    the leaf-pass body's media-annotation step (TS
    ``annotateMediaContent`` lines 1457-1485) can inspect the parts
    table to decide whether to swap message content with
    ``"[Media attachment]"`` or append ``" [with media attachment]"``.
    """

    def get_message_by_id(
        self,
        message_id: int,
        *,
        include_suppressed: bool = ...,
    ) -> "_MessageRecordLike | None": ...

    def get_message_parts(self, message_id: int) -> "list[_MessagePartLike]": ...


class _ContextItemLike(Protocol):
    """Structural shape compaction reads from each context_items row."""

    ordinal: int
    item_type: str  # "message" | "summary"
    message_id: int | None
    summary_id: str | None


class _MessageRecordLike(Protocol):
    """Structural shape compaction reads from each messages row.

    Issue 04-02 extends the protocol with ``message_id`` + ``created_at``
    so the leaf-pass body can emit ``[YYYY-MM-DD HH:mm TZ]`` timestamps
    and pass the integer ``message_id`` through to ``insert_summary``
    /``link_summary_to_messages``.
    """

    message_id: int
    content: str
    token_count: int
    created_at: datetime


class _MessagePartLike(Protocol):
    """Structural shape compaction reads from each message_parts row.

    Mirrors the narrow shape consumed by TS ``annotateMediaContent``
    + ``isMediaAttachmentPart`` (compaction.ts lines 1457-1485 and
    333-347). The 04-02 port reads ``part_type``, ``text_content``,
    and ``metadata`` only.
    """

    part_type: str
    text_content: str | None
    metadata: str | None


class _SummaryRecordLike(Protocol):
    """Structural shape compaction reads from each summaries row.

    Only the ``content`` field is consumed by the leaf-pass body's
    ``_resolve_prior_leaf_summary_context`` helper (TS lines
    1065-1104). The wider ``SummaryRecord`` shape is irrelevant here.
    """

    content: str


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
# Auth-failure sentinel (issue 04-02)
# ---------------------------------------------------------------------------


class LcmProviderAuthError(RuntimeError):
    """Raised when the summarizer reports a provider-auth failure.

    Ports the TS sentinel that ``_leaf_pass`` swallows to short-circuit
    persistence (TS line 1571 — ``if (!summary) return null``; the
    underlying error wrapping happens inside ``summarizeWithEscalation``
    at TS lines 1410-1448). 04-06 will introduce a richer
    ``LcmProviderAuthError`` hierarchy alongside the escalation cascade;
    04-02 lands the minimum sentinel so the leaf-pass auth short-
    circuit can catch + return ``None``.

    Wave-N tag: not a Wave fix per se, but the auth-short-circuit is
    LCM-canonical scar tissue — without it, transient provider outages
    persist truncation-fallback summaries that pollute the DAG. The
    ``# LCM auth-short-circuit`` comment inside :meth:`CompactionEngine.
    _leaf_pass` flags the load-bearing return per ADR-029.
    """


# ---------------------------------------------------------------------------
# Media / structured-content helpers — ported verbatim from TS (issue 04-02)
# ---------------------------------------------------------------------------
#
# These ports mirror the TS regex literals + constant sets in
# ``lossless-claw/src/compaction.ts`` lines 182-243 byte-for-byte. The
# leaf-pass body uses these to (a) strip embedded data URLs / MEDIA:/
# path references before sending text to the summarizer and (b) decide
# whether a message-parts row counts as a media attachment for the
# ``[Media attachment]`` / ``[with media attachment]`` annotation.
#
# Per ADR-029, the leaf-pass scar tissue is the auth short-circuit and
# the message-content sanitation. The sanitation is not Wave-tagged in
# the LCM source (it precedes Wave-1) but is regression-tested via
# ``test/compaction-maintenance-store.test.ts`` and must port exactly
# — silent divergence here would change the summarizer's view of every
# leaf chunk.

#: TS ``MEDIA_PATH_RE`` (line 187). Matches ``MEDIA:/<path>`` references
#: that appear in message content when the original message was a pure
#: media attachment.
_MEDIA_PATH_RE: re.Pattern[str] = re.compile(r"^MEDIA:/.+$")

#: TS ``EMBEDDED_DATA_URL_RE`` (line 188). The ``i`` (case-insensitive)
#: + ``g`` (global) flags map to ``re.IGNORECASE`` plus the ``findall``/
#: ``sub`` call semantics in Python (re.sub already does global by
#: default).
_EMBEDDED_DATA_URL_RE: re.Pattern[str] = re.compile(
    r"data:[^;\s\"'`]+;base64,[A-Za-z0-9+/=\s]+",
    re.IGNORECASE,
)

#: TS ``MEDIA_ATTACHMENT_PART_TYPES`` (line 189). ``Set`` of part_type
#: values that ALWAYS indicate a media attachment regardless of
#: metadata content.
_MEDIA_ATTACHMENT_PART_TYPES: frozenset[str] = frozenset({"file", "snapshot"})

#: TS ``MEDIA_ATTACHMENT_RAW_TYPES`` (line 190). ``Set`` of values
#: ``metadata.rawType`` / ``metadata.raw.type`` may carry that indicate
#: a media attachment when the part_type itself is ambiguous.
_MEDIA_ATTACHMENT_RAW_TYPES: frozenset[str] = frozenset({"file", "image", "snapshot"})

#: TS ``PROVIDER_REASONING_RAW_TYPES`` (line 191). Structured-content
#: rawTypes the summarizer must NEVER see (encrypted reasoning blocks
#: + the agent's chain-of-thought).
_PROVIDER_REASONING_RAW_TYPES: frozenset[str] = frozenset({"reasoning", "thinking"})

#: TS ``STRUCTURED_MEDIA_TEXT_KEYS`` (line 192). Keys in a structured-
#: content record whose string values are extracted as fragments.
_STRUCTURED_MEDIA_TEXT_KEYS: tuple[str, ...] = ("text", "caption", "alt", "title", "summary")

#: TS ``STRUCTURED_MEDIA_NESTED_KEYS`` (line 193). Keys whose values
#: are recursively walked for more text fragments.
_STRUCTURED_MEDIA_NESTED_KEYS: tuple[str, ...] = (
    "content",
    "parts",
    "items",
    "message",
    "messages",
)

#: Recursion depth cap matching TS ``extractSanitizedStructuredText``'s
#: ``depth >= 4`` guard (line 269). Hard-coded both sides; 4 is enough
#: to walk a parts-of-parts structure without runaway recursion.
_STRUCTURED_TEXT_MAX_DEPTH: int = 4


def _looks_like_binary_payload(value: str) -> bool:
    """Detect whether a string is mostly base64/binary, not prose.

    Ports TS ``looksLikeBinaryPayload`` (compaction.ts lines 225-242).
    The heuristics matter: a base64 payload that slips through into the
    summarizer can blow the prompt budget AND pollute the summary.

    Path:

    1. Empty / whitespace-only → ``False``.
    2. Starts with ``data:<type>;base64,`` → ``True`` (definite).
    3. After stripping whitespace, length must be ≥256 AND a multiple
       of 4 (base64 alignment). Otherwise → ``False``.
    4. Compact form must be alphanumeric / ``+/=`` only → else
       ``False``.
    5. If the original (non-compacted) string contains any punctuation
       from ``" .,:;!?()[]{}"``, treat as prose → ``False``. Else
       → ``True``.
    """
    if not isinstance(value, str):
        return False
    trimmed = value.strip()
    if not trimmed:
        return False
    if re.match(r"^data:[^;\s\"'`]+;base64,", trimmed, re.IGNORECASE):
        return True
    compact = re.sub(r"\s+", "", trimmed)
    if len(compact) < 256 or len(compact) % 4 != 0:
        return False
    if not re.match(r"^[A-Za-z0-9+/=]+$", compact):
        return False
    # If the trimmed (un-compacted) string contains any common-prose
    # punctuation, treat as prose despite the base64 character set.
    return not re.search(r"[ .,:;!?()\[\]{}]", trimmed)


def _strip_embedded_media_payloads(content: str) -> str:
    """Strip attachment payloads from plain strings before the summarizer.

    Ports TS ``stripEmbeddedMediaPayloads`` (compaction.ts lines
    245-265).

    Path:

    1. Replace embedded ``data:<mime>;base64,...`` runs with the
       literal ``[embedded media omitted]`` placeholder.
    2. Split into lines, strip trailing whitespace from each.
    3. Drop empty lines, ``MEDIA:/<path>`` lines, and lines flagged by
       :func:`_looks_like_binary_payload`.
    4. Join surviving lines with ``"\\n"`` + final ``str.strip()``.

    Returns ``""`` for non-string inputs (matches TS ``typeof !==
    'string'`` guard).
    """
    if not isinstance(content, str):
        return ""
    without_data_urls = _EMBEDDED_DATA_URL_RE.sub("[embedded media omitted]", content)
    sanitized_lines: list[str] = []
    for line in re.split(r"\r?\n", without_data_urls):
        line = line.rstrip()
        trimmed = line.strip()
        if not trimmed:
            continue
        if _MEDIA_PATH_RE.match(trimmed):
            continue
        if _looks_like_binary_payload(trimmed):
            continue
        sanitized_lines.append(line)
    return "\n".join(sanitized_lines).strip()


def _extract_sanitized_structured_text(value: Any, depth: int = 0) -> list[str]:
    """Walk a structured-content value, returning its prose fragments.

    Ports TS ``extractSanitizedStructuredText`` (compaction.ts lines
    268-310). Recursive walk over dict / list / string with three
    invariants:

    1. ``PROVIDER_REASONING_RAW_TYPES`` records (rawType == "reasoning"
       / "thinking") return ``[]`` — encrypted reasoning never reaches
       the summarizer.
    2. ``MEDIA_ATTACHMENT_RAW_TYPES`` records (rawType == "image" /
       "file" / "snapshot") return ONLY the direct text-key fragments;
       nested keys are NOT walked (the structure inside an image part
       is media content, not prose).
    3. Depth ≥ :data:`_STRUCTURED_TEXT_MAX_DEPTH` cuts the walk —
       matches TS line 269.
    """
    if depth >= _STRUCTURED_TEXT_MAX_DEPTH or value is None:
        return []
    if isinstance(value, str):
        sanitized = _strip_embedded_media_payloads(value)
        return [sanitized] if sanitized else []
    if isinstance(value, list):
        out: list[str] = []
        for entry in value:
            out.extend(_extract_sanitized_structured_text(entry, depth + 1))
        return out
    if not isinstance(value, dict):
        return []

    record = value
    raw_type_val = record.get("type")
    raw_type = raw_type_val.strip().lower() if isinstance(raw_type_val, str) else ""
    if raw_type in _PROVIDER_REASONING_RAW_TYPES:
        return []
    text_fragments: list[str] = []
    for key in _STRUCTURED_MEDIA_TEXT_KEYS:
        candidate = record.get(key)
        if not isinstance(candidate, str):
            continue
        sanitized = _strip_embedded_media_payloads(candidate)
        if sanitized:
            text_fragments.append(sanitized)

    if raw_type in _MEDIA_ATTACHMENT_RAW_TYPES:
        return text_fragments

    for key in _STRUCTURED_MEDIA_NESTED_KEYS:
        text_fragments.extend(_extract_sanitized_structured_text(record.get(key), depth + 1))

    return text_fragments


def _extract_meaningful_message_text(content: str) -> str:
    """Normalize a message content string to summary-safe prose.

    Ports TS ``extractMeaningfulMessageText`` (compaction.ts lines
    313-331).

    Path:

    1. ``None`` / non-str → ``""``.
    2. If trimmed starts/ends with ``[]`` or ``{}``, try to parse as
       JSON. Success → walk via
       :func:`_extract_sanitized_structured_text` and join fragments
       with ``"\\n"``.
    3. Failure or non-JSON → fall through to
       :func:`_strip_embedded_media_payloads` on the raw string.
    """
    if not isinstance(content, str):
        return ""
    trimmed = content.strip()
    if not trimmed:
        return ""
    is_json_shaped = (trimmed.startswith("[") and trimmed.endswith("]")) or (
        trimmed.startswith("{") and trimmed.endswith("}")
    )
    if is_json_shaped:
        try:
            parsed = json.loads(trimmed)
        except (json.JSONDecodeError, ValueError):
            # Fall through to plain-text sanitation below.
            pass
        else:
            extracted = [
                fragment.strip()
                for fragment in _extract_sanitized_structured_text(parsed)
                if fragment.strip()
            ]
            return "\n".join(extracted).strip()
    return _strip_embedded_media_payloads(content)


def _parse_message_part_metadata(metadata: str | None) -> dict[str, Any]:
    """Parse a message-part's ``metadata`` JSON column without raising.

    Ports TS ``parseMessagePartMetadata`` (compaction.ts lines 210-222).
    Returns an empty dict on missing / unparseable / non-object input.
    """
    if not isinstance(metadata, str) or not metadata.strip():
        return {}
    try:
        parsed = json.loads(metadata)
    except (json.JSONDecodeError, ValueError):
        return {}
    if isinstance(parsed, dict):
        return parsed
    return {}


def _is_media_attachment_part(part: _MessagePartLike) -> bool:
    """Decide whether a stored part represents a media attachment.

    Ports TS ``isMediaAttachmentPart`` (compaction.ts lines 333-347).
    Two paths to ``True``:

    * ``part_type`` is in :data:`_MEDIA_ATTACHMENT_PART_TYPES`
      (``"file"`` / ``"snapshot"``).
    * ``part_type`` is something else (e.g. ``"text"``) but the
      metadata's ``rawType`` (or nested ``raw.type``) is in
      :data:`_MEDIA_ATTACHMENT_RAW_TYPES`.
    """
    if part.part_type in _MEDIA_ATTACHMENT_PART_TYPES:
        return True
    metadata = _parse_message_part_metadata(part.metadata)
    raw_type_val = metadata.get("rawType")
    if isinstance(raw_type_val, str):
        raw_type = raw_type_val.strip().lower()
    else:
        raw_obj = metadata.get("raw")
        if isinstance(raw_obj, dict):
            inner = raw_obj.get("type")
            raw_type = inner.strip().lower() if isinstance(inner, str) else ""
        else:
            raw_type = ""
    return raw_type in _MEDIA_ATTACHMENT_RAW_TYPES


def _dedupe_ordered_ids(ids: Iterable[str]) -> list[str]:
    """Return ``ids`` in first-seen order with duplicates removed.

    Ports TS ``dedupeOrderedIds`` (compaction.ts lines 197-207).
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for value in ids:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def _format_timestamp(value: datetime, tz_name: str = "UTC") -> str:
    """Format a timestamp as ``YYYY-MM-DD HH:mm TZ`` for prompt source text.

    Ports TS ``formatTimestamp`` (compaction.ts lines 125-150). The
    output is the per-message timestamp header in the concatenated
    leaf-pass input — agents see it in their compacted summaries when
    asked "when was this said". Drift here would change the summarizer's
    view of timestamp framing.

    Path:

    1. Try to load ``tz_name`` as an IANA zone. On
       :class:`ZoneInfoNotFoundError` (or non-string input), fall back
       to UTC.
    2. Convert ``value`` into the target zone.
    3. Format as ``YYYY-MM-DD HH:mm <abbrev>``. The abbreviation is the
       resolved ``tzname()`` of the localized timestamp; UTC always
       reports ``"UTC"``.

    Args:
        value: The timestamp to format. Naive datetimes are assumed
            UTC (matches TS ``Date`` behavior — the source has been
            UTC-tagged at ingest by ConversationStore).
        tz_name: An IANA timezone name (e.g. ``"America/Los_Angeles"``).
            Default ``"UTC"``.

    Returns:
        A string of the form ``"2026-04-22 14:35 UTC"`` (or
        ``"2026-04-22 07:35 PDT"`` if a non-UTC zone resolves).
    """
    # Treat naive timestamps as UTC. The conversation store always
    # stores ISO-with-zone, so naive only happens in tests.
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)

    target: timezone | ZoneInfo
    if tz_name == "UTC":
        target = timezone.utc
        abbrev = "UTC"
    else:
        try:
            target = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError):
            # TS falls back to UTC on invalid timezone (lines 142-149).
            target = timezone.utc
            abbrev = "UTC"
        else:
            localized = value.astimezone(target)
            abbrev = localized.tzname() or tz_name
    localized = value.astimezone(target)
    return (
        f"{localized.year:04d}-{localized.month:02d}-{localized.day:02d} "
        f"{localized.hour:02d}:{localized.minute:02d} {abbrev}"
    )


def _generate_summary_id(content: str) -> str:
    """Build a deterministic-ish summary id from content + current time.

    Ports TS ``generateSummaryId`` (compaction.ts lines 168-176): the
    ID is ``"sum_" + sha256(content + str(now_ms))[:16]``. The
    current-time suffix lets back-to-back identical summaries get
    distinct IDs.
    """
    now_ms = int(time.time() * 1000)
    digest = hashlib.sha256((content + str(now_ms)).encode("utf-8")).hexdigest()
    return "sum_" + digest[:16]


# ---------------------------------------------------------------------------
# :class:`LeafChunkSelection` — return type for :meth:`_select_oldest_leaf_chunk`
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LeafChunkSelection:
    """Result of selecting the next leaf-eligible chunk.

    Mirrors TS ``LeafChunkSelection`` (compaction.ts inline return type
    at line 1008). Returned from
    :meth:`CompactionEngine._select_oldest_leaf_chunk`.

    Attributes:
        items: The selected context-item rows (in ordinal-ascending
            order). Empty when no chunk is eligible — caller treats as
            "no leaf pass to run". MAY contain a single oversize item
            (the always-include-≥1-message invariant guarantees we
            never return an empty chunk while compactable raw messages
            exist).
        raw_tokens_outside_tail: Sum of message token counts for every
            raw-message item with ``ordinal < fresh_tail_ordinal``.
            Telemetry signal — not used to terminate selection (the
            threshold cap does that). Returned so the caller / decision
            log can record "we had X raw tokens to draw from this pass".
        threshold: The leaf-chunk size cap that bounded the selection.
            ``leaf_chunk_tokens_override or config.leaf_chunk_tokens or
            DEFAULT_LEAF_CHUNK_TOKENS``.
    """

    items: list[_ContextItemLike]
    raw_tokens_outside_tail: int
    threshold: int


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
    # Leaf-pass helpers — issue 04-02
    # ------------------------------------------------------------------

    def _select_oldest_leaf_chunk(
        self,
        conversation_id: int,
        leaf_chunk_tokens_override: int | None = None,
    ) -> LeafChunkSelection:
        """Pick the oldest contiguous raw-message chunk outside the fresh tail.

        Ports TS :meth:`CompactionEngine.selectOldestLeafChunk`
        (compaction.ts lines 1005-1057). The chunk is bounded by
        ``leaf_chunk_tokens`` but ALWAYS includes ≥1 message when any
        compactable raw message exists (TS lines 1024-1052) — the
        always-include-one invariant.

        Termination conditions (in order, matching TS verbatim):

        1. We're at ``ordinal >= fresh_tail_ordinal`` → stop the walk
           (TS lines 1015-1017, 1028-1030).
        2. We haven't started a chunk yet and the current item is a
           non-message → skip and keep walking (TS lines 1032-1035).
        3. We've started a chunk and the next item is a non-message →
           STOP at it (TS lines 1037-1039). The TS source treats ANY
           non-message item as a chunk-boundary, not just summaries —
           future item types are guarded by the same rule.
        4. We have ≥1 message in the chunk and adding the next message
           would push tokens above the cap → break BEFORE adding it
           (TS lines 1045-1047). The always-include-one invariant
           guarantees a single oversize message is still included.
        5. After adding a message, if the running token total is
           already ≥ cap → break (TS lines 1051-1053).

        Args:
            conversation_id: The conversation to scan.
            leaf_chunk_tokens_override: Optional per-call override of
                ``config.leaf_chunk_tokens``. Negative / zero falls
                through to the config / :data:`DEFAULT_LEAF_CHUNK_TOKENS`.

        Returns:
            A :class:`LeafChunkSelection` with the selected items +
            telemetry signals. ``items`` is empty when no compactable
            chunk exists (no raw messages outside the fresh tail OR the
            very first eligible item is at the fresh-tail boundary).
        """
        context_items = self._summary_store.get_context_items(conversation_id)
        fresh_tail_ordinal = self._resolve_fresh_tail_ordinal(context_items)
        threshold = _resolve_leaf_chunk_tokens(self._config, leaf_chunk_tokens_override)

        # TS lines 1013-1022: pre-walk computes the raw-tokens-outside-tail
        # telemetry signal. We could re-use _count_raw_tokens_outside_fresh_tail
        # here, but inlining matches the TS source (the helper calls a
        # different cached path — we're outside ``withContextCache`` here)
        # and avoids double-walking the items list.
        raw_tokens_outside_tail = 0
        for item in context_items:
            if item.ordinal >= fresh_tail_ordinal:
                break
            if item.item_type != "message" or item.message_id is None:
                continue
            raw_tokens_outside_tail += self._get_message_token_count(item.message_id)

        chunk: list[_ContextItemLike] = []
        chunk_tokens = 0
        started = False
        # TS lines 1024-1054: chunk-selection walk.
        for item in context_items:
            if item.ordinal >= fresh_tail_ordinal:
                break

            if not started:
                if item.item_type != "message" or item.message_id is None:
                    # TS lines 1033-1034: skip non-messages until first
                    # raw message — context may have leading summaries.
                    continue
                started = True
            elif item.item_type != "message" or item.message_id is None:
                # TS lines 1037-1038: ANY non-message item terminates
                # the chunk once started — guards future item types,
                # not just "summary".
                break

            # TS line 1041-1042: defensive — the started/elif branches
            # above already enforce message_id != None for chunk items.
            if item.message_id is None:  # pragma: no cover
                continue
            message_tokens = self._get_message_token_count(item.message_id)
            # TS lines 1045-1046: always-include-one — only respect the
            # cap when we already have at least one message in the chunk.
            if chunk and chunk_tokens + message_tokens > threshold:
                break

            chunk.append(item)
            chunk_tokens += message_tokens
            # TS lines 1051-1052: stop greedily once we're at/past cap.
            if chunk_tokens >= threshold:
                break

        return LeafChunkSelection(
            items=chunk,
            raw_tokens_outside_tail=raw_tokens_outside_tail,
            threshold=threshold,
        )

    def _resolve_prior_leaf_summary_context(
        self,
        conversation_id: int,
        message_items: list[_ContextItemLike],
    ) -> str | None:
        """Resolve up-to-2 prior summary contexts for iterative continuity.

        Ports TS :meth:`CompactionEngine.resolvePriorLeafSummaryContext`
        (compaction.ts lines 1065-1104). The last 2 summary items with
        ``ordinal < min(chunk_ordinals)`` are loaded; their content
        fields are trimmed, filtered for empties, then joined with
        ``"\\n\\n"``.

        Args:
            conversation_id: The conversation being compacted.
            message_items: The chunk picked by
                :meth:`_select_oldest_leaf_chunk`. Empty → return
                ``None``.

        Returns:
            Joined prior-summary content for the summarizer's
            ``previous_summary`` option, or ``None`` when no eligible
            summaries exist (or none have non-empty content).
        """
        if not message_items:
            return None
        start_ordinal = min(item.ordinal for item in message_items)

        # TS lines 1074-1081: ordinal < start_ordinal AND item_type ==
        # "summary" AND summary_id is a string. Take the LAST 2 such
        # items (most-recent-by-ordinal).
        context_items = self._summary_store.get_context_items(conversation_id)
        prior_summary_items = [
            item
            for item in context_items
            if item.ordinal < start_ordinal
            and item.item_type == "summary"
            and isinstance(item.summary_id, str)
        ][-2:]

        if not prior_summary_items:
            return None

        summary_contents: list[str] = []
        for item in prior_summary_items:
            if not isinstance(item.summary_id, str):  # pragma: no cover
                continue
            summary = self._summary_store.get_summary(item.summary_id)
            if summary is None:
                continue
            content = summary.content.strip() if isinstance(summary.content, str) else ""
            if content:
                summary_contents.append(content)

        if not summary_contents:
            return None
        return "\n\n".join(summary_contents)

    def _annotate_media_content(self, message_id: int, content: str) -> str:
        """Return ``content`` annotated with media-attachment markers.

        Ports TS :meth:`CompactionEngine.annotateMediaContent`
        (compaction.ts lines 1457-1485).

        Path:

        1. Fetch ``conversation_store.get_message_parts(message_id)``.
        2. If no part is a media attachment (per
           :func:`_is_media_attachment_part`) → return ``content``
           unchanged.
        3. Collect non-media-part ``text_content`` values, strip
           embedded media payloads, join with ``"\\n"``, trim. Fall
           back to :func:`_extract_meaningful_message_text` on the
           original ``content`` if the parts list is empty / pure-
           media.
        4. Pure-media result → ``"[Media attachment]"``.
        5. Mixed result with text → append ``" [with media attachment]"``
           UNLESS it's already present.

        Args:
            message_id: The owning message's primary key.
            content: The raw ``messages.content`` string for the row.

        Returns:
            The annotated content string suitable for the summarizer
            prompt.
        """
        parts = self._conversation_store.get_message_parts(message_id)
        has_media_parts = any(_is_media_attachment_part(p) for p in parts)
        if not has_media_parts:
            return content

        # Build the "prose only" view from non-media parts.
        part_text_fragments: list[str] = []
        for part in parts:
            if _is_media_attachment_part(part):
                continue
            text = part.text_content if isinstance(part.text_content, str) else ""
            stripped = _strip_embedded_media_payloads(text).strip()
            if stripped:
                part_text_fragments.append(stripped)
        part_text = "\n".join(part_text_fragments).strip()
        fallback_text = _extract_meaningful_message_text(content)
        meaningful_text = (part_text or fallback_text).strip()

        if not meaningful_text:
            return "[Media attachment]"
        # TS line 1482: don't double-annotate.
        if "[with media attachment]" in meaningful_text:
            return meaningful_text
        return f"{meaningful_text} [with media attachment]"

    def _invalidate_context_cache(self, conversation_id: int) -> None:
        """Invalidate the per-conversation context cache after a mutation.

        Ports TS :meth:`CompactionEngine.invalidateContextCache`
        (compaction.ts lines 385-388). The 04-02 :class:`CompactionEngine`
        doesn't yet expose the context-cache (that lands as part of the
        ``with_context_cache`` refactor in 04-03/04-04 followup); the
        method exists as a no-op so the leaf-pass body's cache-
        invalidation step is properly wired and a future cache addition
        slots in without touching the leaf-pass code.

        Args:
            conversation_id: The conversation whose cache entry is
                invalidated. Currently unused — kept on the signature so
                the caller doesn't need updating when the cache lands.
        """
        # 04-02 placeholder. The cache itself lives in TS lines 362-403
        # (``_contextItemsCache`` + ``getContextItemsCached`` + ref-
        # counted ``withContextCache``). Once that lands in the Python
        # port, this method becomes a one-liner ``self._context_items_cache
        # .pop(conversation_id, None)``.
        del conversation_id

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
            leaf_outcome = self._run_leaf_pass(
                conversation_id=conversation_id,
                summarize=summarize,
                previous_summary_content=previous_summary_content,
                summary_model=summary_model,
            )
            if leaf_outcome.auth_failure:
                # TS lines 685-687: a provider-auth failure surfaced
                # from ``_leafPass``. Mirror the TS ``hadAuthFailure =
                # true; break`` pair so the final ``CompactionResult``
                # propagates ``auth_failure=True`` (which
                # ``compact_until_under`` reads at TS lines 831-838 to
                # short-circuit the round loop instead of retrying
                # across a provider outage).
                had_auth_failure = True
                break
            if leaf_outcome.summary is None:
                # TS lines 673-675: empty chunk / voluntary skip —
                # clean termination, NOT an auth failure. Caller
                # leaves ``had_auth_failure = False``.
                break
            leaf_result = leaf_outcome.summary

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
            condense_outcome = self._run_condensed_pass(
                conversation_id=conversation_id,
                hard_trigger=hard_trigger,
                summarize=summarize,
                summary_model=summary_model,
            )
            if condense_outcome.auth_failure:
                # TS lines 733-735: provider-auth failure surfaces the
                # same way phase-1 does — set the
                # ``CompactionResult.auth_failure`` flag and break so
                # ``compact_until_under`` short-circuits.
                had_auth_failure = True
                break
            if condense_outcome.summary is None:
                # TS lines 721-723: no candidate / voluntary skip — a
                # clean break that leaves ``auth_failure`` False.
                break
            condense_result = condense_outcome.summary

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
    ) -> LeafPassOutcome:
        """Run one leaf pass and return a :class:`LeafPassOutcome`.

        Issue 04-02 production body. Ports TS ``leafPass``
        (``compaction.ts:1492-1607``) end-to-end:

        1. Select the next chunk via :meth:`_select_oldest_leaf_chunk`.
           Empty selection → return ``LeafPassOutcome(summary=None,
           auth_failure=False)`` (clean "nothing to do" termination —
           caller breaks the phase-1 loop with ``auth_failure`` clear).
        2. Resolve prior leaf-summary context via
           :meth:`_resolve_prior_leaf_summary_context` for iterative
           continuity.
        3. Fetch full message rows, annotate media via
           :meth:`_annotate_media_content`, strip reasoning blocks via
           :func:`_extract_meaningful_message_text`, and concatenate
           ``[YYYY-MM-DD HH:mm TZ]\\n<text>`` entries with ``"\\n\\n"``.
        4. Extract file IDs from the annotated content via
           :func:`~lossless_hermes.large_files.extract_file_ids_from_content`.
        5. Call :meth:`_summarize_with_escalation` (a 04-02 stub; the
           full escalation cascade lands in issue 04-06). On
           :class:`LcmProviderAuthError` raised by the summarizer →
           return ``LeafPassOutcome(summary=None, auth_failure=True)``.
           The caller distinguishes this from the empty-chunk case and
           propagates ``CompactionResult.auth_failure=True`` so
           :meth:`compact_until_under` can short-circuit the round
           loop. TS line 685-687 (``hadAuthFailure = true; break``).
        6. Persist atomically inside ``summary_store.with_transaction()``:
           ``insert_summary`` → ``link_summary_to_messages`` →
           ``replace_context_range_with_summary``.
        7. Invalidate the context cache via
           :meth:`_invalidate_context_cache` (04-02 no-op until the
           cache lands).
        8. Return :class:`LeafPassOutcome` carrying a
           :class:`LeafPassResult` with the running-delta
           ``removed_tokens`` / ``added_tokens`` Wave-12 guard consumes.

        Subclasses (regression tests, 04-04 scripted engines) may
        override to inject scripted results without standing up real
        stores. Overrides MUST return a :class:`LeafPassOutcome` and
        MUST set ``auth_failure=True`` on the provider-auth path so the
        sweep flag propagates correctly.

        Args:
            conversation_id: The conversation being compacted.
            summarize: The summarize callback passed down from
                :meth:`compact_full_sweep`.
            previous_summary_content: ``content`` field from the most
                recent successful :class:`LeafPassResult`, or ``None``
                on the first call. Provides iterative-summarization
                continuity.
            summary_model: Optional model override for this pass.

        Returns:
            A :class:`LeafPassOutcome` whose ``summary`` field carries
            the produced :class:`LeafPassResult` (when a pass succeeded)
            and whose ``auth_failure`` flag distinguishes the
            provider-auth path from the empty-chunk path.
        """
        return self._leaf_pass(
            conversation_id=conversation_id,
            summarize=summarize,
            previous_summary_content=previous_summary_content,
            summary_model=summary_model,
        )

    def _leaf_pass(
        self,
        *,
        conversation_id: int,
        summarize: SummarizeFn,
        previous_summary_content: str | None,
        summary_model: str | None,
    ) -> LeafPassOutcome:
        """Body of one leaf pass — chunk → summarize → persist.

        Ports TS ``leafPass`` (compaction.ts lines 1492-1607). See
        :meth:`_run_leaf_pass` for the algorithm summary.

        Separated from :meth:`_run_leaf_pass` so subclasses (04-04
        scripted engines, 04-03 condensed-pass-only overrides) can
        replace the dispatching hook without losing access to the
        full body when they want to test the persistence path
        directly.
        """
        from lossless_hermes.large_files import extract_file_ids_from_content

        # 1. Select chunk
        selection = self._select_oldest_leaf_chunk(conversation_id)
        message_items = selection.items
        if not message_items:
            # TS lines 673-675 — empty chunk is a clean termination,
            # NOT an auth failure. Caller breaks the phase-1 loop with
            # ``CompactionResult.auth_failure`` left False.
            return LeafPassOutcome(summary=None, auth_failure=False)

        # 2. Resolve prior leaf-summary continuity. ``previous_summary_content``
        #    from the caller wins (when set — TS reuses the most recent
        #    pass's content as the continuity signal inside one sweep);
        #    fall back to walking the context items when the caller has
        #    no continuity yet (first pass in the sweep).
        if previous_summary_content is not None and previous_summary_content.strip():
            previous_summary: str | None = previous_summary_content
        else:
            previous_summary = self._resolve_prior_leaf_summary_context(
                conversation_id,
                message_items,
            )

        # 3. Fetch full message rows, annotate media, build per-message
        #    descriptors.
        message_contents: list[
            dict[str, Any]
        ] = []  # {message_id, content, created_at, token_count}
        for item in message_items:
            if item.message_id is None:  # pragma: no cover
                continue
            msg = self._conversation_store.get_message_by_id(
                item.message_id,
                include_suppressed=True,
            )
            if msg is None:
                continue
            annotated = self._annotate_media_content(msg.message_id, msg.content)
            message_contents.append({
                "message_id": msg.message_id,
                "content": annotated,
                "created_at": msg.created_at,
                "token_count": self._resolve_message_token_count(msg),
            })

        # 4. Concatenate. Reasoning/thinking blocks stripped via
        #    extract_meaningful_message_text; empty results filtered.
        concatenated_parts: list[str] = []
        for entry in message_contents:
            text = _extract_meaningful_message_text(entry["content"])
            if not text:
                continue
            timestamp = _format_timestamp(entry["created_at"], self._config.timezone)
            concatenated_parts.append(f"[{timestamp}]\n{text}")
        concatenated = "\n\n".join(concatenated_parts)

        # 5. Extract file ids for the new summary's file_ids index.
        flat_ids: list[str] = []
        for entry in message_contents:
            flat_ids.extend(extract_file_ids_from_content(entry["content"]))
        file_ids = _dedupe_ordered_ids(flat_ids)

        # 6. Summarize (04-02 stub → 04-06 escalation cascade).
        try:
            summary = self._summarize_with_escalation(
                source_text=concatenated,
                summarize=summarize,
                target_tokens=self._config.leaf_target_tokens,
                previous_summary=previous_summary,
                is_condensed=False,
                summary_model=summary_model,
            )
        except LcmProviderAuthError:
            # LCM auth-short-circuit: avoid persisting fallback-truncation summaries
            # during transient provider outages — preserves DAG integrity.
            # Original: lossless-claw/src/compaction.ts:1571 (early-return on null).
            # Signal auth_failure=True so ``compact_full_sweep`` sets
            # ``CompactionResult.auth_failure=True`` (mirror of TS
            # ``hadAuthFailure = true`` at compaction.ts:686).
            return LeafPassOutcome(summary=None, auth_failure=True)
        if summary is None:
            # Per TS lines 1544-1549 — summarizer voluntarily skipped
            # (e.g. empty source after sanitation). This is NOT an
            # auth failure; caller treats as non-compacting skip with
            # ``auth_failure`` left False.
            return LeafPassOutcome(summary=None, auth_failure=False)

        # 7. Persist atomically.
        summary_content: str = summary["content"]
        summary_level: CompactionLevel = summary["level"]

        summary_id = _generate_summary_id(summary_content)
        # estimate_tokens import is local to avoid circular dependency
        # if estimate_tokens ever grows compaction-aware logic.
        from lossless_hermes.estimate_tokens import estimate_tokens

        token_count = estimate_tokens(summary_content)
        # Note: removed_tokens uses _resolve_message_token_count values
        # (which fall back to estimate_tokens for messages with
        # token_count <= 0). This can diverge from get_context_token_count
        # which would sum the stored 0. The delta feeds into stopping
        # decisions (threshold checks, progress guards), but the
        # divergence is bounded to empty/corrupt messages
        # (token_count=0) which are rare. TS source comments at lines
        # 1554-1559.
        removed_tokens = sum(max(0, int(entry["token_count"])) for entry in message_contents)

        # Earliest / latest timestamps for the summary's metadata.
        if message_contents:
            timestamps = [entry["created_at"] for entry in message_contents]
            earliest_at: datetime | None = min(timestamps)
            latest_at: datetime | None = max(timestamps)
        else:
            earliest_at = None
            latest_at = None

        ordinals = [item.ordinal for item in message_items]
        start_ordinal = min(ordinals)
        end_ordinal = max(ordinals)
        message_ids = [entry["message_id"] for entry in message_contents]

        from lossless_hermes.store.summary import (
            CreateSummaryInput,
            ReplaceContextRangeInput,
        )

        create_input = CreateSummaryInput(
            summary_id=summary_id,
            conversation_id=conversation_id,
            kind="leaf",
            depth=0,
            content=summary_content,
            token_count=token_count,
            file_ids=file_ids,
            earliest_at=earliest_at,
            latest_at=latest_at,
            descendant_count=0,
            descendant_token_count=0,
            source_message_token_count=removed_tokens,
            model=summary_model,
        )
        replace_input = ReplaceContextRangeInput(
            conversation_id=conversation_id,
            start_ordinal=start_ordinal,
            end_ordinal=end_ordinal,
            summary_id=summary_id,
        )

        with self._summary_store.with_transaction():
            self._summary_store.insert_summary(create_input)
            # Link to source messages BEFORE the context swap becomes
            # visible (TS lines 1588-1590).
            if message_ids:
                self._summary_store.link_summary_to_messages(summary_id, message_ids)
            self._summary_store.replace_context_range_with_summary(replace_input)

        # 8. Invalidate context cache after the swap.
        self._invalidate_context_cache(conversation_id)

        return LeafPassOutcome(
            summary=LeafPassResult(
                summary_id=summary_id,
                level=summary_level,
                content=summary_content,
                removed_tokens=removed_tokens,
                added_tokens=token_count,
            ),
            auth_failure=False,
        )

    def _resolve_message_token_count(self, message: _MessageRecordLike) -> int:
        """Resolve a message's token count with content-length fallback.

        Ports TS ``resolveMessageTokenCount`` (compaction.ts lines
        1118-1128). Unlike :meth:`_get_message_token_count` (which
        takes a primary key + fetches the row), this overload reads
        the count off an already-loaded :class:`_MessageRecordLike`.

        Path:

        1. If ``message.token_count > 0`` → return it (rounded toward
           zero).
        2. Else fall back to
           :func:`~lossless_hermes.estimate_tokens.estimate_tokens` on
           ``message.content``.
        """
        from lossless_hermes.estimate_tokens import estimate_tokens

        if message.token_count > 0:
            return int(message.token_count)
        return estimate_tokens(message.content)

    def _summarize_with_escalation(
        self,
        *,
        source_text: str,
        summarize: SummarizeFn,
        target_tokens: int,
        previous_summary: str | None,
        is_condensed: bool,
        summary_model: str | None,
    ) -> dict[str, Any] | None:
        """Run the summarize cascade and return ``{content, level}``.

        04-02 STUB. The full escalation cascade (normal → aggressive
        → deterministic fallback → cap) lands in issue 04-06
        (``summarizeWithEscalation`` in TS at lines 1410-1448 plus the
        per-pass length check). For 04-02 we use a single-shot
        ``summarize(...)`` call so the leaf-pass body's atomic persist /
        DAG-link / context-swap path can be tested without 04-06 being
        complete.

        Subclasses (regression tests for 04-02 acceptance criteria,
        the eventual 04-06 production wiring) MAY override this method
        to inject controlled outputs. Tests use the override to verify
        ``LcmProviderAuthError`` short-circuits, fallback-level
        propagation, etc.

        Args:
            source_text: The concatenated message text the leaf-pass
                produced. May be empty when all messages stripped to
                empty after sanitation.
            summarize: The :data:`SummarizeFn` provided by the caller.
            target_tokens: Target output size in tokens
                (``config.leaf_target_tokens`` for leaf passes,
                ``config.condensed_target_tokens`` for condensed).
            previous_summary: Prior leaf-summary content for iterative
                continuity. ``None`` when no prior context exists.
            is_condensed: ``True`` for condensed passes (04-03+);
                ``False`` for leaf passes. Forwarded into the
                ``options`` dict of the summarize callback so 04-06's
                template dispatcher can pick the right prompt builder.
            summary_model: Optional model override.

        Returns:
            ``{"content": <str>, "level": "normal" | "aggressive" |
            "fallback" | "capped"}`` on success. ``None`` when the
            summarizer voluntarily skipped (e.g. empty / unsalvageable
            source). Raises :class:`LcmProviderAuthError` on provider
            auth failure so the caller can short-circuit.
        """
        if not source_text.strip():
            return None

        options: dict[str, Any] = {
            "previous_summary": previous_summary,
            "is_condensed": is_condensed,
            "target_tokens": target_tokens,
            "summary_model": summary_model,
        }
        # 04-02 stub: single-shot summarize call. The escalation
        # cascade (Guard 3) lands in 04-06 alongside the prompt-
        # template dispatcher. Until then any output is "normal" level.
        content = summarize(source_text, False, options)
        if not isinstance(content, str) or not content.strip():
            return None
        return {"content": content, "level": "normal"}

    def _run_condensed_pass(
        self,
        *,
        conversation_id: int,
        hard_trigger: bool,
        summarize: SummarizeFn,
        summary_model: str | None,
    ) -> LeafPassOutcome:
        """Run one condensed pass and return a :class:`LeafPassOutcome`.

        04-04 skeletal stub. Production body lands in issue 04-03
        (the condensed-pass + chunk-selection helpers in TS). Same
        outcome-discrimination contract as :meth:`_run_leaf_pass`:

        * empty candidate / voluntary skip → ``LeafPassOutcome(summary=None,
          auth_failure=False)`` (TS lines 721-723).
        * provider-auth failure → ``LeafPassOutcome(summary=None,
          auth_failure=True)`` (TS lines 733-735, ``hadAuthFailure =
          true`` parity with phase-1).
        * pass produced a summary →
          ``LeafPassOutcome(summary=LeafPassResult(...), auth_failure=False)``.

        Args:
            conversation_id: The conversation being compacted.
            hard_trigger: Whether the caller is a hard-trigger sweep
                (loosens condensed-pass fanout thresholds).
            summarize: The summarize callback passed down from
                :meth:`compact_full_sweep`.
            summary_model: Optional model override for this pass.

        Returns:
            A :class:`LeafPassOutcome` whose ``summary`` field carries
            the produced :class:`LeafPassResult` (when a pass
            succeeded) and whose ``auth_failure`` flag mirrors the
            phase-1 contract so ``compact_full_sweep`` propagates the
            sweep-level auth flag identically across both phases.
        """
        del conversation_id, hard_trigger, summarize, summary_model
        return LeafPassOutcome(summary=None, auth_failure=False)
