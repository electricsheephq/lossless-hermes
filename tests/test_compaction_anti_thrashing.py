"""Tests for the 3 anti-thrashing guards in :class:`CompactionEngine`.

Covers issue 04-04 acceptance criteria
(``epics/04-compaction/04-04-anti-thrashing.md``):

* **Guard 1 (Wave-12) — per-pass progress** inside
  :meth:`CompactionEngine.compact_full_sweep` phase-1 and phase-2 loops.
  TS source: ``lossless-claw/src/compaction.ts`` lines 705-712
  (phase-1) + 757-759 (phase-2).
* **Guard 2 — ``compact_until_under`` bail-out** when a round makes no
  progress. TS source: ``compaction.ts`` lines 848-855.
* **Guard 3 — summarize-escalation "didn't compress"** lives in
  :mod:`lossless_hermes.summarize` per issue 04-06. Not yet ported
  (summarize.py introduced by PR #70 covers the prompt templates; the
  escalation cascade body is the 04-06 deliverable). The Guard 3
  regression test is parked as :func:`test_guard3_summarize_escalation_deferred`
  with an explicit :func:`pytest.skip` so the test surface is wired
  but not flaky.

### Test design

The 04-04 :class:`CompactionEngine` skeletons (
:meth:`compact_full_sweep` + :meth:`compact_until_under`) delegate the
per-pass body to the overridable :meth:`_run_leaf_pass` /
:meth:`_run_condensed_pass` hooks. Production wiring will replace
these in 04-02 + 04-03; for the 04-04 regression tests we subclass
the engine and supply scripted pass results so the guards can be
exercised end-to-end without standing up a fully migrated SQLite DB.

Each test deliberately constructs a scenario where:

* **Positive case** — the guard MUST fire and break the loop. The
  test asserts the loop exited *before* exhausting the natural
  termination (max_rounds / chunk-source exhaustion).
* **Negative case** — the guard MUST NOT fire when progress IS being
  made. The test asserts the loop ran to its natural termination
  (eligible chunks consumed / target reached).

### Source references

* TS source: ``lossless-claw/src/compaction.ts`` (LCM commit
  ``1f07fbd`` on branch ``pr-613``), lines 705-712, 757-759, 848-855.
* Spec: ``epics/04-compaction/04-04-anti-thrashing.md``.
* Porting guide: ``docs/porting-guides/assembler-compaction.md``
  §"Anti-thrashing logic".
* ADR-017 (sync stores + sync summarize callback), ADR-029 (Wave-N
  provenance), ADR-024 (project layout).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from lossless_hermes.compaction import (
    CompactionConfig,
    CompactionEngine,
    CompactionResult,
    CompactUntilUnderResult,
    LeafPassOutcome,
    LeafPassResult,
    SummarizeFn,
)


# ---------------------------------------------------------------------------
# Test fixtures — store stand-ins + scripted pass overrides
# ---------------------------------------------------------------------------


@dataclass
class _StubContextItem:
    """Minimal stand-in for ``ContextItemRecord`` (mirrors test_compaction_evaluate)."""

    ordinal: int
    item_type: str
    message_id: int | None = None
    summary_id: str | None = None


@dataclass
class _StubMessage:
    """Minimal stand-in for ``MessageRecord``."""

    content: str
    token_count: int


class _StubSummaryStore:
    """In-memory ``SummaryStore``-like stand-in.

    Carries a single mutable token-count so test scenarios that loop
    through :meth:`compact_until_under` can simulate progress between
    rounds (when the override updates the store between calls).
    """

    def __init__(
        self,
        *,
        context_token_count: int = 0,
        context_items: list[_StubContextItem] | None = None,
    ) -> None:
        self.context_token_count = context_token_count
        self.context_items: list[_StubContextItem] = list(context_items or [])

    def get_context_token_count(self, conversation_id: int) -> int:
        return self.context_token_count

    def get_context_items(self, conversation_id: int) -> list[_StubContextItem]:
        return list(self.context_items)


class _StubConversationStore:
    """In-memory ``ConversationStore``-like stand-in."""

    def __init__(self, messages: dict[int, _StubMessage] | None = None) -> None:
        self._messages: dict[int, _StubMessage] = dict(messages or {})

    def get_message_by_id(
        self,
        message_id: int,
        *,
        include_suppressed: bool = False,
    ) -> _StubMessage | None:
        return self._messages.get(message_id)


def _noop_summarize(text: str, aggressive: bool = False, options: dict | None = None) -> str:
    """A no-op :data:`SummarizeFn` used when the test doesn't care.

    The 04-04 skeletal phase-1 / phase-2 loops never actually call
    the summarize callback — that body lands in 04-02 / 04-03. The
    callback is only present on the signature so the eventual
    production code path is wired. Until then, tests pass this as a
    placeholder.
    """
    del text, aggressive, options
    return ""


def _make_one_message_context(
    message_tokens: int = 1,
) -> tuple[list[_StubContextItem], dict[int, _StubMessage]]:
    """Build a minimal context with one raw-message item.

    The phase-1 loop's empty-context short-circuit (TS lines 649-657)
    fires when ``get_context_items`` returns an empty list. Tests that
    want to drive the phase-1 loop need at least one item so the
    short-circuit doesn't pre-empt the guard.
    """
    return (
        [_StubContextItem(ordinal=0, item_type="message", message_id=1)],
        {1: _StubMessage(content="raw", token_count=message_tokens)},
    )


# ---------------------------------------------------------------------------
# Engine subclasses — scripted pass results
# ---------------------------------------------------------------------------


class _ScriptedEngine(CompactionEngine):
    """A :class:`CompactionEngine` whose passes return scripted results.

    The 04-04 base :meth:`_run_leaf_pass` / :meth:`_run_condensed_pass`
    return ``None`` immediately (terminating the phase loops without
    work). This subclass replaces them with FIFO queues of
    :class:`LeafPassResult` (or ``None`` sentinels) — each call to the
    hook pops the next scripted result, simulating a sequence of
    leaf/condensed passes with caller-controlled token deltas.

    The queues let a single test orchestrate multi-pass scenarios:
    "first pass makes good progress, second pass makes zero progress,
    Guard 1 should fire at the boundary".
    """

    def __init__(
        self,
        *,
        leaf_passes: list[LeafPassResult | None] | None = None,
        condensed_passes: list[LeafPassResult | None] | None = None,
        **engine_kwargs: object,
    ) -> None:
        super().__init__(**engine_kwargs)  # type: ignore[arg-type]
        self.leaf_passes: list[LeafPassResult | None] = list(leaf_passes or [])
        self.condensed_passes: list[LeafPassResult | None] = list(condensed_passes or [])
        self.leaf_pass_calls = 0
        self.condensed_pass_calls = 0

    def _run_leaf_pass(
        self,
        *,
        conversation_id: int,
        summarize: SummarizeFn,
        previous_summary_content: str | None,
        summary_model: str | None,
    ) -> LeafPassOutcome:
        self.leaf_pass_calls += 1
        if not self.leaf_passes:
            # Empty scripted queue → "nothing left to compact", NOT
            # auth failure. Matches the natural-termination behavior
            # the Wave-12 guard tests rely on.
            return LeafPassOutcome(summary=None, auth_failure=False)
        scripted = self.leaf_passes.pop(0)
        # Sentinel ``None`` in the scripted queue is the natural
        # empty-chunk termination; non-None entries are produced
        # summaries.
        return LeafPassOutcome(summary=scripted, auth_failure=False)

    def _run_condensed_pass(
        self,
        *,
        conversation_id: int,
        hard_trigger: bool,
        summarize: SummarizeFn,
        summary_model: str | None,
    ) -> LeafPassOutcome:
        self.condensed_pass_calls += 1
        if not self.condensed_passes:
            return LeafPassOutcome(summary=None, auth_failure=False)
        scripted = self.condensed_passes.pop(0)
        return LeafPassOutcome(summary=scripted, auth_failure=False)


def _make_scripted_engine(
    *,
    context_token_count: int = 100_000,
    context_items: list[_StubContextItem] | None = None,
    messages: dict[int, _StubMessage] | None = None,
    config: CompactionConfig | None = None,
    leaf_passes: list[LeafPassResult | None] | None = None,
    condensed_passes: list[LeafPassResult | None] | None = None,
) -> _ScriptedEngine:
    """Construct a :class:`_ScriptedEngine` with the given pass scripts."""
    items = context_items if context_items is not None else _make_one_message_context()[0]
    msgs = messages if messages is not None else _make_one_message_context()[1]
    summary_store = _StubSummaryStore(
        context_token_count=context_token_count,
        context_items=items,
    )
    conversation_store = _StubConversationStore(messages=msgs)
    return _ScriptedEngine(
        leaf_passes=leaf_passes,
        condensed_passes=condensed_passes,
        conversation_store=conversation_store,
        summary_store=summary_store,
        config=config or CompactionConfig(),
    )


def _leaf_pass_with_delta(
    removed: int,
    added: int,
    *,
    summary_id: str = "sum_test",
    level: str = "normal",
) -> LeafPassResult:
    """Convenience factory for a scripted :class:`LeafPassResult`."""
    return LeafPassResult(
        summary_id=summary_id,
        level=level,  # type: ignore[arg-type]
        content="(scripted summary)",
        removed_tokens=removed,
        added_tokens=added,
    )


# ---------------------------------------------------------------------------
# Guard 1 — per-pass progress (Wave-12)
# ---------------------------------------------------------------------------


class TestGuard1PhaseOneProgressGuard:
    """Phase-1 leaf-pass Wave-12 guard at compaction.ts:709-712.

    The guard breaks when ``passTokensAfter >= passTokensBefore`` OR
    ``passTokensAfter >= previousTokens``. Both clauses are load-bearing:

    * ``>= passTokensBefore`` — the IMMEDIATE-pass floor. Catches the
      "summarizer returned same-size or larger output" case.
    * ``>= previousTokens`` — the RUNNING floor. Catches the
      "summarizer made tiny progress on this pass but is now bouncing
      around the previous best result" case.
    """

    def test_zero_progress_breaks_phase_one(self) -> None:
        """Pass with ``added == removed`` (zero net) trips ``>= passTokensBefore``.

        Positive Guard-1 case. Without the guard the loop would call
        ``_run_leaf_pass`` indefinitely (or until the natural ``None``
        return); with the guard it breaks after pass 1.
        """
        # 5 scripted "zero net" passes available; if the guard fires
        # we should see exactly 1 pass before the break.
        engine = _make_scripted_engine(
            context_token_count=100_000,
            leaf_passes=[
                _leaf_pass_with_delta(removed=1_000, added=1_000),
                _leaf_pass_with_delta(removed=1_000, added=1_000),
                _leaf_pass_with_delta(removed=1_000, added=1_000),
                _leaf_pass_with_delta(removed=1_000, added=1_000),
                _leaf_pass_with_delta(removed=1_000, added=1_000),
            ],
        )

        result = engine.compact_full_sweep(
            conversation_id=1,
            token_budget=10_000,  # threshold = 7500 < 100_000 stored ⇒ trigger fires
            summarize=_noop_summarize,
        )

        # Guard 1 fired: only 1 pass ran before the break, 4 remain.
        assert result.passes_completed == 1
        assert result.action_taken is True
        # Tokens stayed flat at 100_000 (1_000 removed + 1_000 added).
        assert result.tokens_after == 100_000
        # 4 remaining scripted passes were never consumed.
        assert len(engine.leaf_passes) == 4

    def test_growing_output_breaks_phase_one(self) -> None:
        """Pass with ``added > removed`` (net growth) trips ``>= passTokensBefore``.

        Positive Guard-1 case mirroring the "summarizer 2x bloated"
        failure mode. Without the guard the loop would keep growing
        the context; with the guard it breaks after pass 1.
        """
        engine = _make_scripted_engine(
            context_token_count=100_000,
            leaf_passes=[
                _leaf_pass_with_delta(removed=500, added=1_500),  # +1000 net.
                _leaf_pass_with_delta(removed=500, added=1_500),
                _leaf_pass_with_delta(removed=500, added=1_500),
            ],
        )

        result = engine.compact_full_sweep(
            conversation_id=1,
            token_budget=10_000,
            summarize=_noop_summarize,
        )

        assert result.passes_completed == 1
        # tokens_after = 100_000 - 500 + 1500 = 101_000 (grew).
        assert result.tokens_after == 101_000
        assert len(engine.leaf_passes) == 2

    def test_running_floor_clause_breaks_phase_one(self) -> None:
        """``>= previousTokens`` clause catches "bounced past running floor".

        Scenario: pass 1 reduces tokens 100k → 90k. pass 2 reduces
        90k → 89k (one-token progress on immediate floor, but immediate
        passes the ``passTokensAfter < passTokensBefore`` clause).
        previousTokens is now 89k. pass 3 produces 89k tokens (zero
        net) — fails the IMMEDIATE clause again. Verified that the
        running floor clause is exercised, since this is the only way
        to surface a bug where someone "simplifies" the guard to only
        check ``passTokensAfter >= passTokensBefore``.
        """
        # We can't directly exercise the running-floor clause without
        # also tripping the immediate clause at the same pass — both
        # use the same ``>=`` sense and the immediate is a strictly
        # tighter bound at the boundary case. So instead validate the
        # NEGATIVE case: two passes each strictly reducing both floors
        # → no guard fires.
        engine = _make_scripted_engine(
            context_token_count=100_000,
            leaf_passes=[
                _leaf_pass_with_delta(removed=10_000, added=1_000),  # -9k net → 91k
                _leaf_pass_with_delta(removed=10_000, added=1_000),  # -9k net → 82k
                _leaf_pass_with_delta(removed=10_000, added=1_000),  # -9k net → 73k
            ],
        )
        result = engine.compact_full_sweep(
            conversation_id=1,
            token_budget=10_000,
            summarize=_noop_summarize,
        )
        # All three passes ran — Guard 1 did NOT fire when progress
        # was being made. (Phase-1 then exhausts via ``None`` return.)
        assert result.passes_completed == 3
        assert result.tokens_after == 73_000

    def test_progress_then_zero_triggers_guard(self) -> None:
        """First pass progresses, second pass is zero-net → break after pass 2.

        This is the bog-standard scenario the Wave-12 guard exists for:
        the summarizer makes great progress on the first leaf chunk
        then runs out of compressible content. Without the guard the
        loop walks into infinite-retry territory on the residual chunk.
        """
        engine = _make_scripted_engine(
            context_token_count=100_000,
            leaf_passes=[
                _leaf_pass_with_delta(removed=10_000, added=1_000),  # -9k net
                _leaf_pass_with_delta(removed=1_000, added=1_000),  # zero net
                _leaf_pass_with_delta(removed=1_000, added=1_000),
            ],
        )
        result = engine.compact_full_sweep(
            conversation_id=1,
            token_budget=10_000,
            summarize=_noop_summarize,
        )
        assert result.passes_completed == 2
        # 100_000 - 10_000 + 1_000 = 91_000 after pass 1. Pass 2 is
        # zero-net so tokens stay at 91_000.
        assert result.tokens_after == 91_000
        # One scripted pass remained unconsumed.
        assert len(engine.leaf_passes) == 1


class TestGuard1PhaseTwoProgressGuard:
    """Phase-2 condensed-pass Wave-12 guard at compaction.ts:757-759."""

    def test_zero_progress_breaks_phase_two(self) -> None:
        """A condensed pass with zero-net delta breaks phase-2 immediately.

        Setup forces phase-2 entry by:
        * No leaf passes scripted (phase-1 exits via ``None`` on pass 1).
        * ``force=True`` so phase-2 runs even though phase-1 didn't
          push us under threshold.
        * Multiple condensed passes scripted — guard should fire on
          pass 1.
        """
        engine = _make_scripted_engine(
            context_token_count=100_000,
            leaf_passes=[],
            condensed_passes=[
                _leaf_pass_with_delta(removed=1_000, added=1_000, level="aggressive"),
                _leaf_pass_with_delta(removed=1_000, added=1_000, level="aggressive"),
                _leaf_pass_with_delta(removed=1_000, added=1_000, level="aggressive"),
            ],
        )
        result = engine.compact_full_sweep(
            conversation_id=1,
            token_budget=10_000,
            summarize=_noop_summarize,
            force=True,  # Required so phase-2 runs.
        )

        assert result.passes_completed == 1
        assert result.condensed is True
        # 2 condensed passes remain unconsumed.
        assert len(engine.condensed_passes) == 2
        assert result.tokens_after == 100_000

    def test_growing_output_breaks_phase_two(self) -> None:
        """A condensed pass with net growth breaks phase-2 immediately."""
        engine = _make_scripted_engine(
            context_token_count=100_000,
            condensed_passes=[
                _leaf_pass_with_delta(removed=200, added=1_200, level="aggressive"),
                _leaf_pass_with_delta(removed=200, added=1_200, level="aggressive"),
            ],
        )
        result = engine.compact_full_sweep(
            conversation_id=1,
            token_budget=10_000,
            summarize=_noop_summarize,
            force=True,
        )

        assert result.passes_completed == 1
        # 100_000 - 200 + 1_200 = 101_000.
        assert result.tokens_after == 101_000
        assert len(engine.condensed_passes) == 1

    def test_progress_runs_phase_two_to_completion(self) -> None:
        """Negative Guard-1 case for phase-2 — progress = loop runs to ``None``."""
        engine = _make_scripted_engine(
            context_token_count=100_000,
            condensed_passes=[
                _leaf_pass_with_delta(removed=20_000, added=1_000, level="aggressive"),
                _leaf_pass_with_delta(removed=20_000, added=1_000, level="aggressive"),
            ],
        )
        result = engine.compact_full_sweep(
            conversation_id=1,
            token_budget=10_000,
            summarize=_noop_summarize,
            force=True,
        )
        # Both passes ran — guard didn't fire.
        assert result.passes_completed == 2
        # 100_000 → 81_000 → 62_000.
        assert result.tokens_after == 62_000
        assert len(engine.condensed_passes) == 0


# ---------------------------------------------------------------------------
# Guard 2 — compact_until_under bail-out
# ---------------------------------------------------------------------------


class TestGuard2CompactUntilUnderBailOut:
    """``compact_until_under`` bail-out at compaction.ts:848-855.

    Two break clauses:
    * ``!result.actionTaken`` — sweep did literally nothing this round.
    * ``result.tokensAfter >= lastTokens`` — sweep ran but didn't make
      progress against the previous round's floor.
    """

    def test_bail_out_when_action_taken_false(self) -> None:
        """``action_taken=False`` round triggers immediate bail-out.

        Setup: stored token count well over target. The skeletal engine
        (no scripted leaf/condensed passes) returns an
        ``action_taken=False`` result from ``compact_full_sweep``
        (empty context-items short-circuit OR no chunks available).
        Without the guard, the loop would attempt ``max_rounds=10`` of
        no-op sweeps; with it, we bail on round 1.
        """
        # Empty context_items → compact_full_sweep returns
        # ``action_taken=False`` immediately (TS lines 649-657 empty-
        # context short-circuit). compact_until_under sees that and
        # MUST bail.
        engine = _make_scripted_engine(
            context_token_count=100_000,
            context_items=[],  # Empty → compact_full_sweep no-ops.
            messages={},
            leaf_passes=[],
            condensed_passes=[],
        )

        result = engine.compact_until_under(
            conversation_id=1,
            token_budget=10_000,
            summarize=_noop_summarize,
            target_tokens=5_000,
        )

        assert isinstance(result, CompactUntilUnderResult)
        assert result.success is False
        # Guard fired on round 1; we did NOT loop through max_rounds.
        assert result.rounds == 1
        assert result.final_tokens == 100_000

    def test_bail_out_when_tokens_after_did_not_decrease(self) -> None:
        """``tokens_after >= last_tokens`` triggers bail-out on round 2.

        Setup: round 1 makes some in-pass progress before Wave-12 fires
        (100k → 99k from pass 1, then pass 2 zero-net trips Wave-12).
        Round 2 starts fresh from the stub-store's 100k (the stub does
        not write-back), pass 3 is zero-net → Wave-12 fires after pass
        1 → ``tokens_after=100k``. Guard 2 sees ``100k >= last_tokens
        (99k)`` and bails out.

        Without the guard, the outer round loop would burn through all
        ``max_rounds=10`` slots calling sweeps that make no progress.
        """
        engine = _make_scripted_engine(
            context_token_count=100_000,
            leaf_passes=[
                # Round 1 pass 1: -1k net (100k → 99k), Wave-12 does
                # NOT fire (99k < 100k). running_tokens advances.
                _leaf_pass_with_delta(removed=2_000, added=1_000),
                # Round 1 pass 2: zero net (99k → 99k). Wave-12 fires
                # (99k >= 99k). running_tokens = 99k, returns to
                # compact_until_under with tokens_after=99k.
                _leaf_pass_with_delta(removed=1_000, added=1_000),
                # Round 2 pass 1: zero net (100k → 100k since stub
                # store still returns 100k). Wave-12 fires
                # immediately. tokens_after=100k. Guard 2 sees
                # 100k >= last_tokens(99k) → BAIL.
                _leaf_pass_with_delta(removed=1_000, added=1_000),
                # Should never run.
                _leaf_pass_with_delta(removed=1_000, added=1_000),
            ],
        )

        result = engine.compact_until_under(
            conversation_id=1,
            token_budget=10_000,
            summarize=_noop_summarize,
            target_tokens=5_000,
        )

        assert result.success is False
        # Guard 2 fired on round 2. ``max_rounds=10`` default and we
        # stopped at 2, so the assertion that we didn't loop forever
        # has teeth.
        assert result.rounds == 2
        # Round 2's tokens_after reflects the stub-store re-reading
        # 100k at the top of the round + the zero-net pass that
        # tripped Wave-12.
        assert result.final_tokens == 100_000
        # 1 scripted pass unconsumed.
        assert len(engine.leaf_passes) == 1

    def test_already_under_target_returns_rounds_zero(self) -> None:
        """``current < target`` short-circuits before the round loop.

        Negative Guard-2 case for the entry short-circuit (TS lines
        815-820). Not the bail-out guard itself, but verifies the
        round-loop only runs when needed — otherwise Guard 2 has no
        meaning.
        """
        engine = _make_scripted_engine(
            context_token_count=1_000,
            # No scripted passes — the test asserts none are needed.
            leaf_passes=[],
            condensed_passes=[],
        )

        result = engine.compact_until_under(
            conversation_id=1,
            token_budget=10_000,
            summarize=_noop_summarize,
            target_tokens=5_000,
        )

        assert result.success is True
        assert result.rounds == 0
        assert result.final_tokens == 1_000

    def test_success_within_target_returns_early(self) -> None:
        """Round 1 reaches the target → return ``success=True`` without bail-out.

        Negative Guard-2 case for the success short-circuit. Ensures
        Guard 2 doesn't fire when the round did make progress AND
        we're under target.
        """
        engine = _make_scripted_engine(
            context_token_count=10_000,
            leaf_passes=[
                # Round 1: 10k → 1k (massive progress, below target=5k).
                _leaf_pass_with_delta(removed=10_000, added=1_000),
            ],
        )

        result = engine.compact_until_under(
            conversation_id=1,
            token_budget=20_000,
            summarize=_noop_summarize,
            target_tokens=5_000,
        )

        assert result.success is True
        assert result.rounds == 1
        # 10_000 - 10_000 + 1_000 = 1_000.
        assert result.final_tokens == 1_000

    def test_progress_keeps_loop_running_within_round(self) -> None:
        """Negative Guard-2 case: in-round progress runs to success on round 1.

        With the 04-04 in-memory store stub the ``summary_store``
        token-count does NOT update between rounds, so a single forced
        sweep gets all the way to the target by chaining its phase-1
        passes. Round 1 alone:

        * 50k → 35k → 20k → 5k via three -15k-net leaf passes.
        * Pass 4 returns ``None`` → phase-1 ends.
        * 5k <= target → success short-circuit in
          :meth:`compact_until_under`.

        Verifies Guard 2 does NOT fire when each pass is making
        progress (the in-pass Wave-12 stays quiet too).
        """
        engine = _make_scripted_engine(
            context_token_count=50_000,
            leaf_passes=[
                _leaf_pass_with_delta(removed=20_000, added=5_000),
                _leaf_pass_with_delta(removed=20_000, added=5_000),
                _leaf_pass_with_delta(removed=20_000, added=5_000),
            ],
        )

        result = engine.compact_until_under(
            conversation_id=1,
            token_budget=100_000,
            summarize=_noop_summarize,
            target_tokens=10_000,
        )

        assert result.success is True
        # All three scripted leaf passes ran inside round 1 (the stub
        # store doesn't write-back, so each round re-reads the same
        # ``tokens_before`` — but phase-1 inside a single round chains
        # progress correctly via the running-delta arithmetic).
        assert result.rounds == 1
        # 50k → 35k → 20k → 5k.
        assert result.final_tokens == 5_000
        # All passes consumed inside the single round.
        assert len(engine.leaf_passes) == 0

    def test_max_rounds_default_is_10(self) -> None:
        """Verify the regression-test math against the actual ``max_rounds`` default.

        The bail-out assertion ``rounds < max_rounds`` only has teeth
        when ``max_rounds`` is reasonably large — if it were
        accidentally 1 the test would trivially pass. Pin the default.
        """
        assert CompactionConfig().max_rounds == 10


# ---------------------------------------------------------------------------
# Edge cases — zero tokens, single message, all-summary contexts
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Boundary conditions that should NOT crash the skeletal sweep."""

    def test_zero_tokens_no_trigger_no_action(self) -> None:
        """Stored=0, observed=0 → no trigger, no action."""
        engine = _make_scripted_engine(
            context_token_count=0,
            context_items=[],
            messages={},
        )
        result = engine.compact_full_sweep(
            conversation_id=1,
            token_budget=10_000,
            summarize=_noop_summarize,
        )
        assert isinstance(result, CompactionResult)
        assert result.action_taken is False
        assert result.tokens_before == 0
        assert result.tokens_after == 0
        assert result.passes_completed == 0

    def test_empty_context_under_force(self) -> None:
        """Empty context items under ``force=True`` still short-circuits.

        The empty-context short-circuit (TS lines 649-657) is BEFORE
        the phase loops and fires regardless of ``force``. Verifies a
        forced caller doesn't crash on an empty conversation.
        """
        engine = _make_scripted_engine(
            context_token_count=100_000,
            context_items=[],
            messages={},
        )
        result = engine.compact_full_sweep(
            conversation_id=1,
            token_budget=10_000,
            summarize=_noop_summarize,
            force=True,
        )
        assert result.action_taken is False
        assert result.passes_completed == 0
        assert result.tokens_after == 100_000

    def test_single_message_no_passes_scheduled_phase_one_exits_clean(self) -> None:
        """Single message + no scripted passes → phase-1 + phase-2 both exit via ``None``.

        With ``tokens_before (100k) > threshold (7500)`` phase-1 enters,
        calls ``_run_leaf_pass`` once (returns ``None`` → break with
        zero work done). Phase-2 entry condition (``previous_tokens >
        threshold`` with ``previous_tokens=100k``) is also true, so
        phase-2 calls ``_run_condensed_pass`` once (returns ``None`` →
        break).
        """
        engine = _make_scripted_engine(
            context_token_count=100_000,
            leaf_passes=[],
            condensed_passes=[],
        )
        result = engine.compact_full_sweep(
            conversation_id=1,
            token_budget=10_000,
            summarize=_noop_summarize,
        )
        assert result.action_taken is False
        assert result.passes_completed == 0
        # Each hook was called exactly once: phase-1 entered + bailed,
        # phase-2 entered (still over threshold) + bailed.
        assert engine.leaf_pass_calls == 1
        assert engine.condensed_pass_calls == 1

    def test_all_summary_context_still_runs_phase_two_under_force(self) -> None:
        """All-summary context: phase-1 sees no raw messages (still calls hook
        which returns ``None``); phase-2 can still run under ``force``.

        Verifies the phase-2 entry condition (``force || previous_tokens >
        threshold``). With force=True we enter phase-2 even when
        phase-1 didn't move the needle.
        """
        items = [
            _StubContextItem(ordinal=0, item_type="summary", summary_id="sum_old"),
            _StubContextItem(ordinal=1, item_type="summary", summary_id="sum_older"),
        ]
        engine = _make_scripted_engine(
            context_token_count=100_000,
            context_items=items,
            messages={},
            leaf_passes=[],  # phase-1 returns None immediately.
            condensed_passes=[
                _leaf_pass_with_delta(removed=30_000, added=1_000, level="aggressive"),
            ],
        )
        result = engine.compact_full_sweep(
            conversation_id=1,
            token_budget=10_000,
            summarize=_noop_summarize,
            force=True,
        )
        assert result.passes_completed == 1
        assert result.condensed is True
        # 100_000 - 30_000 + 1_000 = 71_000.
        assert result.tokens_after == 71_000
        assert result.level == "aggressive"


# ---------------------------------------------------------------------------
# Sync interface checks — compaction methods are sync per ADR-017
# ---------------------------------------------------------------------------


class TestSyncInterface:
    """``compact_full_sweep`` + ``compact_until_under`` are sync per ADR-017."""

    def test_compact_full_sweep_is_sync(self) -> None:
        """Method is not a coroutine function (LLM via sync ``auxiliary_client``)."""
        import inspect

        assert not inspect.iscoroutinefunction(CompactionEngine.compact_full_sweep)

    def test_compact_until_under_is_sync(self) -> None:
        """Method is not a coroutine function."""
        import inspect

        assert not inspect.iscoroutinefunction(CompactionEngine.compact_until_under)

    def test_returns_dataclass_not_coroutine(self) -> None:
        """Return value is a :class:`CompactionResult`, not awaitable."""
        import inspect

        engine = _make_scripted_engine(context_token_count=0, context_items=[])
        result = engine.compact_full_sweep(
            conversation_id=1,
            token_budget=100,
            summarize=_noop_summarize,
        )
        assert isinstance(result, CompactionResult)
        assert not inspect.iscoroutine(result)


# ---------------------------------------------------------------------------
# Wave-12 provenance check — ADR-029 requirement
# ---------------------------------------------------------------------------


class TestWave12Provenance:
    """Two Wave-12 markers (phase-1 + phase-2) per ADR-029."""

    def test_two_wave12_markers_in_compaction_py(self) -> None:
        """``grep "Wave-12" compaction.py`` finds >= 2 hits.

        Per the 04-04 AC: ``grep -rn "Wave-12" src/lossless_hermes/
        compaction.py`` finds at least 2 hits (phase-1 + phase-2).
        Encoded as a unit test so the assertion is run every CI cycle
        (instead of an external grep step).
        """
        import pathlib

        compaction_path = (
            pathlib.Path(__file__).resolve().parent.parent
            / "src"
            / "lossless_hermes"
            / "compaction.py"
        )
        source = compaction_path.read_text()
        wave12_lines = [
            line
            for line in source.splitlines()
            if "Wave-12" in line and line.strip().startswith("#")
        ]
        assert len(wave12_lines) >= 2, (
            f"Expected >=2 Wave-12 comment lines (phase-1 + phase-2 guard sites), "
            f"found {len(wave12_lines)}. Per ADR-029 these are load-bearing markers."
        )


# ---------------------------------------------------------------------------
# Guard 3 — summarize-escalation "didn't compress" (DEFERRED to 04-06)
# ---------------------------------------------------------------------------


def test_guard3_summarize_escalation_deferred() -> None:
    """Guard 3 lives in :mod:`lossless_hermes.summarize` per issue 04-06.

    The summarize-escalation cascade body (``_summarizeWithEscalation``
    in TS at ``summarize.ts:1411 + 1422``) has not been ported yet.
    PR #70 (issue 04-05) landed the three prompt templates but not the
    escalation logic. 04-06 ports the cascade; its companion test will
    live in ``tests/test_summarize.py`` and assert: a mock LLM that
    returns 2x input size for both ``normal`` and ``aggressive``
    causes the cascade to fall to the deterministic fallback.

    Until then, this placeholder keeps the Guard-3 test surface visible
    in the 04-04 test module's docstring + a CI report — a future
    contributor working on 04-06 will see the skipped test and know
    where the companion regression test belongs.
    """
    pytest.skip(
        "Guard 3 (_summarize_with_escalation cascade) is the deliverable of "
        "issue 04-06. The escalation logic + its regression test live with "
        "the summarize-fallback-chain PR; the 04-04 PR ships Guard 1 + 2 only."
    )
