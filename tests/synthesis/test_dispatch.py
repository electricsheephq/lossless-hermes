"""Tests for :mod:`lossless_hermes.synthesis.dispatch` (issue 07-05).

Ports ``lossless-claw/test/synthesis-dispatch.test.ts`` (commit
``1f07fbd`` on branch ``pr-613``, 570 LOC) plus the 13 Wave-N
behavioral parity checklist items called out in the issue spec.

### Case mapping (TS → Python)

| TS describe block | Python class | Notes |
|---|---|---|
| constants | :class:`TestConstants` | tier coverage + pass strategy table |
| single-pass tiers (daily, weekly) | :class:`TestSinglePass` | one LLM call, one audit row |
| monthly (single + verify_fidelity) | :class:`TestMonthlyVerifyFidelity` | hallucination flag matrix |
| yearly (best-of-N + judge) | :class:`TestYearlyBestOfN` | judge picks winner |
| error handling | :class:`TestErrorHandling` | missing_prompt, llm_failure |
| model resolution | :class:`TestModelResolution` | force_model precedence matrix |
| template rendering | :class:`TestTemplateRendering` | placeholder substitution |
| yearly P0 regression coverage | :class:`TestYearlyBestOfNP0Regression` | Wave-7 + Wave-8 |

### Additional parity-checklist tests (per issue spec)

| Parity item | Python test |
|---|---|
| 1: missing_target validates BEFORE LLM | :meth:`TestParityChecklist.test_1_missing_target_validates_before_llm` |
| 2: force_model w/o override → tier default | :meth:`TestParityChecklist.test_2_force_model_no_override_uses_tier_default` |
| 3: best_of_n hard cap = 5 | :meth:`TestParityChecklist.test_3_best_of_n_hard_cap_5` |
| 4: shared pass_session_id | :meth:`TestParityChecklist.test_4_all_yearly_passes_share_pass_session_id` |
| 5: verify regex (UNSUPPORTED + OK = fail) | :meth:`TestParityChecklist.test_5_verify_negative_marker_with_ok_still_flags` |
| 6: judge parser Winner: precedence | :meth:`TestParityChecklist.test_6_judge_winner_precedence_over_first_digit` |
| 7: gather return_exceptions + survivor-of-one skips judge | :meth:`TestParityChecklist.test_7_yearly_survivor_of_one_skips_judge` |
| 8: audit insert FK violation → typed error | :meth:`TestParityChecklist.test_8_audit_insert_fk_violation_typed_error` |
| 9: verify placeholder aliases | :meth:`TestParityChecklist.test_9_verify_prompt_placeholder_aliases` |
| 10: empty tier_label → NULL | :meth:`TestParityChecklist.test_10_empty_tier_label_normalizes_to_null` |
| 11: session_key fallback (in 07-06 cache, not dispatch) | n/a — see note below |
| 12: pass_input + pass_output truncated to 8000 chars | :meth:`TestParityChecklist.test_12_pass_io_truncated_to_8000_chars` |
| 13: started audit row BEFORE LLM, completed AFTER | :meth:`TestParityChecklist.test_13_audit_lifecycle_started_then_completed` |

Item 11 (the 4-step ``session_key`` fallback) is a cache-write-path
concern (issue 07-06 ``lcm_synthesize_around.ts`` caller); dispatch
itself only accepts the resolved value via :attr:`SynthesizeRequest`
fields. The dispatch port respects the AC ("no module-level
singletons") and the TS source pin which does NOT include
session_key fallback inside dispatch.ts.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable, Iterator

import pytest

from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.synthesis.dispatch import (
    DEFAULT_MODEL_BY_TIER,
    HARD_CAP_BEST_OF_N,
    PASS_STRATEGY_BY_TIER,
    LlmCallArgs,
    LlmCallResult,
    SynthesisDispatcher,
    SynthesisDispatchError,
    SynthesizeRequest,
    dispatch_synthesis,
)
from lossless_hermes.synthesis.prompt_registry import (
    RegisterPromptOptions,
    register_prompt,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _setup_db() -> sqlite3.Connection:
    """Build an in-memory DB with FK enforcement + the v4.1 schema applied.

    Also inserts a conversation + summary so ``target_summary_id`` FK is
    valid for audit rows. Mirrors ``setupDb()`` in
    ``synthesis-dispatch.test.ts:13-23``.

    ``isolation_level=None`` (autocommit) is required because
    :func:`register_prompt` (issue 07-08) issues its own
    ``BEGIN IMMEDIATE`` — Python's default ``isolation_level=""``
    injects an implicit BEGIN on DML that would conflict.
    """

    db = sqlite3.connect(":memory:", isolation_level=None)
    db.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(db, fts5_available=False, seed_default_prompts=False)
    db.execute("INSERT INTO conversations (session_id, session_key) VALUES ('s1', 'sk1')")
    db.execute(
        "INSERT INTO summaries (summary_id, conversation_id, kind, content,"
        " token_count) VALUES ('sum_target', 1, 'condensed', 'placeholder', 1)"
    )
    return db


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    """Migrated in-memory DB with FK enforcement + summary target ready."""

    conn = _setup_db()
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Mock LLM helpers
# ---------------------------------------------------------------------------


def _make_static_llm(
    output: str = "mock output", *, latency_ms: float = 42.0, cost_cents: int | None = 10
) -> Callable[[LlmCallArgs], Awaitable[LlmCallResult]]:
    """Build a deterministic LLM mock that returns a fixed ``output``."""

    async def _call(_args: LlmCallArgs) -> LlmCallResult:
        return LlmCallResult(output=output, latency_ms=latency_ms, cost_cents=cost_cents)

    return _call


# ---------------------------------------------------------------------------
# TestConstants — TS describe("synthesis-dispatch — constants")
# ---------------------------------------------------------------------------


class TestConstants:
    """TS: ``synthesis-dispatch.test.ts:44-60``."""

    def test_default_model_by_tier_covers_all_tiers(self) -> None:
        """TS line 45: every tier has a default model entry."""
        assert DEFAULT_MODEL_BY_TIER["daily"]
        assert DEFAULT_MODEL_BY_TIER["weekly"]
        assert DEFAULT_MODEL_BY_TIER["monthly"]
        assert DEFAULT_MODEL_BY_TIER["yearly"]
        assert DEFAULT_MODEL_BY_TIER["custom"]
        assert DEFAULT_MODEL_BY_TIER["filtered"]

    def test_pass_strategy_by_tier_differs_by_tier(self) -> None:
        """TS line 54: tier → pass-strategy table."""
        assert PASS_STRATEGY_BY_TIER["daily"] == ["single"]
        assert PASS_STRATEGY_BY_TIER["weekly"] == ["single"]
        assert PASS_STRATEGY_BY_TIER["monthly"] == ["single", "verify_fidelity"]
        assert PASS_STRATEGY_BY_TIER["yearly"] == ["best_of_n_judge"]
        assert PASS_STRATEGY_BY_TIER["custom"] == ["single"]
        assert PASS_STRATEGY_BY_TIER["filtered"] == ["single"]

    def test_hard_cap_best_of_n_is_5(self) -> None:
        """Parity item 3: hard cap = 5."""
        assert HARD_CAP_BEST_OF_N == 5


# ---------------------------------------------------------------------------
# TestSinglePass — TS describe("synthesis-dispatch — single-pass tiers")
# ---------------------------------------------------------------------------


class TestSinglePass:
    """TS: ``synthesis-dispatch.test.ts:62-119``."""

    @pytest.mark.asyncio
    async def test_daily_one_llm_call_one_audit_row(self, db: sqlite3.Connection) -> None:
        """TS line 63: daily tier runs one LLM call + one audit row."""
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-condensed",
                tier_label="daily",
                pass_kind="single",
                template="Daily summary of: {{source_text}}",
            ),
        )
        llm = _make_static_llm(output="the daily summary")
        result = await dispatch_synthesis(
            db,
            llm,
            SynthesizeRequest(
                tier="daily",
                memory_type="episodic-condensed",
                source_text="hello world",
                pass_session_id="ps1",
                target_summary_id="sum_target",
            ),
        )
        assert result.output == "the daily summary"
        assert len(result.audit_ids) == 1
        assert result.total_latency_ms == 42
        assert result.total_cost_cents == 10
        assert result.hallucination_flagged is None
        assert result.best_of_n is None

        audit = db.execute(
            "SELECT status, pass_kind, pass_input_truncated, target_summary_id"
            " FROM lcm_synthesis_audit"
        ).fetchone()
        assert audit[0] == "completed"
        assert audit[1] == "single"
        assert audit[2] == "hello world"
        assert audit[3] == "sum_target"

    @pytest.mark.asyncio
    async def test_weekly_uses_tier_default_model(self, db: sqlite3.Connection) -> None:
        """TS line 95: weekly tier uses :data:`DEFAULT_MODEL_BY_TIER`."""
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-condensed",
                tier_label="weekly",
                pass_kind="single",
                template="Weekly: {{source_text}}",
            ),
        )
        model_used = ""

        async def _llm(args: LlmCallArgs) -> LlmCallResult:
            nonlocal model_used
            model_used = args.model
            return LlmCallResult(output="weekly summary", latency_ms=50)

        result = await dispatch_synthesis(
            db,
            _llm,
            SynthesizeRequest(
                tier="weekly",
                memory_type="episodic-condensed",
                source_text="x",
                pass_session_id="ps2",
                target_summary_id="sum_target",
            ),
        )
        assert result.output == "weekly summary"
        assert model_used == DEFAULT_MODEL_BY_TIER["weekly"]


# ---------------------------------------------------------------------------
# TestMonthlyVerifyFidelity — TS describe("synthesis-dispatch — monthly ...")
# ---------------------------------------------------------------------------


class TestMonthlyVerifyFidelity:
    """TS: ``synthesis-dispatch.test.ts:121-219``."""

    @pytest.mark.asyncio
    async def test_monthly_flags_hallucination_on_negative_marker(
        self, db: sqlite3.Connection
    ) -> None:
        """TS line 122: monthly with HALLUCINATION marker → flagged True."""
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-condensed",
                tier_label="monthly",
                pass_kind="single",
                template="Monthly: {{source_text}}",
            ),
        )
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-condensed",
                tier_label="monthly",
                pass_kind="verify_fidelity",
                template="Check {{candidate_summary}} vs {{source_text}}",
            ),
        )

        async def _llm(args: LlmCallArgs) -> LlmCallResult:
            if args.pass_kind == "single":
                return LlmCallResult(
                    output="this might be made up",
                    latency_ms=50,
                    cost_cents=5,
                )
            return LlmCallResult(
                output="HALLUCINATION: 'this might be made up' isn't in source",
                latency_ms=30,
                cost_cents=3,
            )

        result = await dispatch_synthesis(
            db,
            _llm,
            SynthesizeRequest(
                tier="monthly",
                memory_type="episodic-condensed",
                source_text="actual source",
                pass_session_id="ps3",
                target_summary_id="sum_target",
            ),
        )
        assert result.output == "this might be made up"
        assert len(result.audit_ids) == 2
        assert result.hallucination_flagged is True
        assert result.total_cost_cents == 8

        audits = db.execute(
            "SELECT pass_kind, status FROM lcm_synthesis_audit ORDER BY ran_at, audit_id"
        ).fetchall()
        # Sort by pass_kind alphabetically since both rows have same ran_at.
        kinds = sorted([a[0] for a in audits])
        assert kinds == ["single", "verify_fidelity"]
        for _, status in audits:
            assert status == "completed"

    @pytest.mark.asyncio
    async def test_monthly_not_flagged_when_verify_returns_ok(self, db: sqlite3.Connection) -> None:
        """TS line 168: clean OK output → ``hallucination_flagged=False``."""
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-condensed",
                tier_label="monthly",
                pass_kind="single",
                template="x",
            ),
        )
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-condensed",
                tier_label="monthly",
                pass_kind="verify_fidelity",
                template="y",
            ),
        )

        async def _llm(args: LlmCallArgs) -> LlmCallResult:
            if args.pass_kind == "single":
                return LlmCallResult(output="good summary", latency_ms=10)
            return LlmCallResult(output="OK", latency_ms=5)

        result = await dispatch_synthesis(
            db,
            _llm,
            SynthesizeRequest(
                tier="monthly",
                memory_type="episodic-condensed",
                source_text="x",
                pass_session_id="ps4",
                target_summary_id="sum_target",
            ),
        )
        assert result.hallucination_flagged is False

    @pytest.mark.asyncio
    async def test_monthly_clean_ok_with_grounded_suffix(self, db: sqlite3.Connection) -> None:
        """Final.review.3 Loop 4 Bug 4.1: ``OK: all N claims grounded`` clears."""
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-condensed",
                tier_label="monthly",
                pass_kind="single",
                template="x",
            ),
        )
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-condensed",
                tier_label="monthly",
                pass_kind="verify_fidelity",
                template="y",
            ),
        )

        async def _llm(args: LlmCallArgs) -> LlmCallResult:
            if args.pass_kind == "single":
                return LlmCallResult(output="summary", latency_ms=1)
            return LlmCallResult(output="OK: all 3 claims grounded", latency_ms=1)

        result = await dispatch_synthesis(
            db,
            _llm,
            SynthesizeRequest(
                tier="monthly",
                memory_type="episodic-condensed",
                source_text="x",
                pass_session_id="ps-grounded",
                target_summary_id="sum_target",
            ),
        )
        assert result.hallucination_flagged is False

    @pytest.mark.asyncio
    async def test_monthly_without_verify_prompt_skips_silently(
        self, db: sqlite3.Connection
    ) -> None:
        """TS line 198: monthly without verify prompt → no flag set."""
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-condensed",
                tier_label="monthly",
                pass_kind="single",
                template="x",
            ),
        )
        llm = _make_static_llm(output="x")
        result = await dispatch_synthesis(
            db,
            llm,
            SynthesizeRequest(
                tier="monthly",
                memory_type="episodic-condensed",
                source_text="x",
                pass_session_id="ps5",
                target_summary_id="sum_target",
            ),
        )
        assert len(result.audit_ids) == 1
        assert result.hallucination_flagged is None


# ---------------------------------------------------------------------------
# TestYearlyBestOfN — TS describe("synthesis-dispatch — yearly ...")
# ---------------------------------------------------------------------------


class TestYearlyBestOfN:
    """TS: ``synthesis-dispatch.test.ts:221-350``."""

    @pytest.mark.asyncio
    async def test_yearly_runs_3_candidates_plus_judge(self, db: sqlite3.Connection) -> None:
        """TS line 222: yearly runs 3 candidates + judge picks one."""
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-yearly",
                tier_label="yearly",
                pass_kind="single",
                template="Yearly: {{source_text}}",
            ),
        )
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-yearly",
                tier_label="yearly",
                pass_kind="best_of_n_judge",
                template="Pick best:\n{{candidates}}",
            ),
        )

        candidate_counter = 0

        async def _llm(args: LlmCallArgs) -> LlmCallResult:
            nonlocal candidate_counter
            if args.pass_kind == "single":
                n = candidate_counter
                candidate_counter += 1
                return LlmCallResult(output=f"candidate {n}", latency_ms=100, cost_cents=50)
            return LlmCallResult(output="1", latency_ms=30, cost_cents=5)

        result = await dispatch_synthesis(
            db,
            _llm,
            SynthesizeRequest(
                tier="yearly",
                memory_type="episodic-yearly",
                source_text="the source",
                pass_session_id="ps6",
                target_summary_id="sum_target",
            ),
        )
        assert result.best_of_n is not None
        assert result.best_of_n.n == 3
        assert result.best_of_n.selected_index == 1
        # Order is preserved from asyncio.gather; mock is sequential so we
        # know candidates went 0, 1, 2.
        assert result.best_of_n.candidates == [
            "candidate 0",
            "candidate 1",
            "candidate 2",
        ]
        assert result.output == "candidate 1"
        assert len(result.audit_ids) == 4
        assert result.total_cost_cents == 155

    @pytest.mark.asyncio
    async def test_yearly_best_of_n_5_runs_5_candidates(self, db: sqlite3.Connection) -> None:
        """TS line 265: explicit best_of_n=5 runs 5 candidates + judge."""
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-yearly",
                tier_label="yearly",
                pass_kind="single",
                template="x",
            ),
        )
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-yearly",
                tier_label="yearly",
                pass_kind="best_of_n_judge",
                template="y",
            ),
        )

        counter = 0

        async def _llm(args: LlmCallArgs) -> LlmCallResult:
            nonlocal counter
            if args.pass_kind == "single":
                n = counter
                counter += 1
                return LlmCallResult(output=f"c{n}", latency_ms=1)
            return LlmCallResult(output="0", latency_ms=1)

        result = await dispatch_synthesis(
            db,
            _llm,
            SynthesizeRequest(
                tier="yearly",
                memory_type="episodic-yearly",
                source_text="x",
                pass_session_id="ps7",
                target_summary_id="sum_target",
                best_of_n=5,
            ),
        )
        assert result.best_of_n is not None
        assert result.best_of_n.n == 5
        assert len(result.best_of_n.candidates) == 5
        assert len(result.audit_ids) == 6  # 5 + judge

    @pytest.mark.asyncio
    async def test_yearly_judge_failure_when_no_digit(self, db: sqlite3.Connection) -> None:
        """TS line 299: judge output without a digit raises judge_failure."""
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-yearly",
                tier_label="yearly",
                pass_kind="single",
                template="x",
            ),
        )
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-yearly",
                tier_label="yearly",
                pass_kind="best_of_n_judge",
                template="y",
            ),
        )

        async def _llm(args: LlmCallArgs) -> LlmCallResult:
            if args.pass_kind == "single":
                return LlmCallResult(output="candidate", latency_ms=1)
            return LlmCallResult(output="I cannot decide", latency_ms=1)

        with pytest.raises(SynthesisDispatchError) as exc_info:
            await dispatch_synthesis(
                db,
                _llm,
                SynthesizeRequest(
                    tier="yearly",
                    memory_type="episodic-yearly",
                    source_text="x",
                    pass_session_id="ps8",
                    target_summary_id="sum_target",
                ),
            )
        assert exc_info.value.kind == "judge_failure"

    @pytest.mark.asyncio
    async def test_yearly_without_judge_prompt_raises_missing_prompt(
        self, db: sqlite3.Connection
    ) -> None:
        """TS line 329: yearly without best_of_n_judge prompt → missing_prompt."""
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-yearly",
                tier_label="yearly",
                pass_kind="single",
                template="x",
            ),
        )
        llm = _make_static_llm(output="x")
        with pytest.raises(SynthesisDispatchError) as exc_info:
            await dispatch_synthesis(
                db,
                llm,
                SynthesizeRequest(
                    tier="yearly",
                    memory_type="episodic-yearly",
                    source_text="x",
                    pass_session_id="ps9",
                    target_summary_id="sum_target",
                ),
            )
        assert exc_info.value.kind == "missing_prompt"


# ---------------------------------------------------------------------------
# TestErrorHandling — TS describe("synthesis-dispatch — error handling")
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """TS: ``synthesis-dispatch.test.ts:352-397``."""

    @pytest.mark.asyncio
    async def test_missing_primary_prompt_raises(self, db: sqlite3.Connection) -> None:
        """TS line 353: no active prompt → missing_prompt."""
        llm = _make_static_llm(output="x")
        with pytest.raises(SynthesisDispatchError) as exc_info:
            await dispatch_synthesis(
                db,
                llm,
                SynthesizeRequest(
                    tier="daily",
                    memory_type="episodic-condensed",
                    source_text="x",
                    pass_session_id="ps10",
                    target_summary_id="sum_target",
                ),
            )
        assert exc_info.value.kind == "missing_prompt"

    @pytest.mark.asyncio
    async def test_llm_failure_records_failed_audit_row(self, db: sqlite3.Connection) -> None:
        """TS line 368: LLM raises → llm_failure + failed audit row."""
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-condensed",
                tier_label="daily",
                pass_kind="single",
                template="x",
            ),
        )

        async def _llm(args: LlmCallArgs) -> LlmCallResult:
            raise RuntimeError("API timeout")

        with pytest.raises(SynthesisDispatchError) as exc_info:
            await dispatch_synthesis(
                db,
                _llm,
                SynthesizeRequest(
                    tier="daily",
                    memory_type="episodic-condensed",
                    source_text="x",
                    pass_session_id="ps11",
                    target_summary_id="sum_target",
                ),
            )
        assert exc_info.value.kind == "llm_failure"

        audit = db.execute("SELECT status, last_error FROM lcm_synthesis_audit").fetchone()
        assert audit[0] == "failed"
        assert "API timeout" in audit[1]


# ---------------------------------------------------------------------------
# TestModelResolution — TS describe("synthesis-dispatch — model resolution")
# ---------------------------------------------------------------------------


class TestModelResolution:
    """TS: ``synthesis-dispatch.test.ts:399-451``."""

    @pytest.mark.asyncio
    async def test_prompt_model_recommendation_overrides_tier_default(
        self, db: sqlite3.Connection
    ) -> None:
        """TS line 400: prompt's :attr:`PromptRecord.model_recommendation`
        wins over the tier default."""
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-condensed",
                tier_label="daily",
                pass_kind="single",
                template="x",
                model_recommendation="specific-model-for-this-prompt",
            ),
        )
        model_used = ""

        async def _llm(args: LlmCallArgs) -> LlmCallResult:
            nonlocal model_used
            model_used = args.model
            return LlmCallResult(output="x", latency_ms=1)

        await dispatch_synthesis(
            db,
            _llm,
            SynthesizeRequest(
                tier="daily",
                memory_type="episodic-condensed",
                source_text="x",
                pass_session_id="ps12",
                target_summary_id="sum_target",
            ),
        )
        assert model_used == "specific-model-for-this-prompt"

    @pytest.mark.asyncio
    async def test_force_model_with_override_wins(self, db: sqlite3.Connection) -> None:
        """TS line 425: ``force_model`` + ``model_override`` wins over prompt."""
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-condensed",
                tier_label="daily",
                pass_kind="single",
                template="x",
                model_recommendation="should-not-be-used",
            ),
        )
        model_used = ""

        async def _llm(args: LlmCallArgs) -> LlmCallResult:
            nonlocal model_used
            model_used = args.model
            return LlmCallResult(output="x", latency_ms=1)

        await dispatch_synthesis(
            db,
            _llm,
            SynthesizeRequest(
                tier="daily",
                memory_type="episodic-condensed",
                source_text="x",
                pass_session_id="ps13",
                target_summary_id="sum_target",
                model_override="force-this",
                force_model=True,
            ),
        )
        assert model_used == "force-this"


# ---------------------------------------------------------------------------
# TestTemplateRendering — TS describe("synthesis-dispatch — template rendering")
# ---------------------------------------------------------------------------


class TestTemplateRendering:
    """TS: ``synthesis-dispatch.test.ts:453-477``."""

    @pytest.mark.asyncio
    async def test_substitutes_source_text_tier_memory_type(self, db: sqlite3.Connection) -> None:
        """TS line 454: substitutes ``{{source_text}}``, ``{{tier}}``,
        ``{{memory_type}}``."""
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-leaf",
                tier_label="daily",
                pass_kind="single",
                template="Type={{memory_type}} Tier={{tier}} Src={{source_text}}",
            ),
        )
        rendered_prompt = ""

        async def _llm(args: LlmCallArgs) -> LlmCallResult:
            nonlocal rendered_prompt
            rendered_prompt = args.prompt
            return LlmCallResult(output="x", latency_ms=1)

        await dispatch_synthesis(
            db,
            _llm,
            SynthesizeRequest(
                tier="daily",
                memory_type="episodic-leaf",
                source_text="the source",
                pass_session_id="ps14",
                target_summary_id="sum_target",
            ),
        )
        assert rendered_prompt == "Type=episodic-leaf Tier=daily Src=the source"


# ---------------------------------------------------------------------------
# TestYearlyBestOfNP0Regression — Wave-7 + Wave-8 P0 regression coverage
# ---------------------------------------------------------------------------


class TestYearlyBestOfNP0Regression:
    """TS: ``synthesis-dispatch.test.ts:479-570``.

    Wave-7 + Wave-8 P0 fix regression coverage — survivor-of-one path
    AND judge survivor-count handling.
    """

    @pytest.mark.asyncio
    async def test_wave_8_p0_one_survivor_returns_complete_result(
        self, db: sqlite3.Connection
    ) -> None:
        """TS line 481: 1-survivor short-circuit returns COMPLETE
        SynthesizeResult (all required fields populated)."""
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-yearly",
                tier_label="yearly",
                pass_kind="single",
                template="Yearly: {{source_text}}",
            ),
        )
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-yearly",
                tier_label="yearly",
                pass_kind="best_of_n_judge",
                template="Pick best:\n{{candidates}}",
            ),
        )

        call_count = 0

        async def _llm(args: LlmCallArgs) -> LlmCallResult:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return LlmCallResult(output="survivor", latency_ms=100, cost_cents=50)
            raise RuntimeError("simulated candidate failure")

        result = await dispatch_synthesis(
            db,
            _llm,
            SynthesizeRequest(
                tier="yearly",
                memory_type="episodic-yearly",
                source_text="src",
                pass_session_id="ps-1survivor",
                target_summary_id="sum_target",
                best_of_n=3,
            ),
        )

        # Critical Wave-8 P0 fix: 1-survivor path was missing 4 required
        # fields. Verify all are present + populated.
        assert result.output == "survivor"
        assert result.primary_prompt_id is not None
        assert isinstance(result.primary_prompt_id, str)
        assert result.audit_ids is not None
        assert isinstance(result.audit_ids, list)
        assert len(result.audit_ids) > 0
        assert result.total_latency_ms == 100
        assert result.total_cost_cents == 50
        assert result.best_of_n is not None
        assert result.best_of_n.n == 1
        assert result.best_of_n.selected_index == 0


# ---------------------------------------------------------------------------
# TestParityChecklist — 13 Wave-N behavioral parity items (issue spec)
# ---------------------------------------------------------------------------


class TestParityChecklist:
    """One test per item in the spec's behavioral parity checklist."""

    @pytest.mark.asyncio
    async def test_1_missing_target_validates_before_llm(self, db: sqlite3.Connection) -> None:
        """Parity 1: ``missing_target`` validates BEFORE the LLM call.

        Group D adversarial Gap 1.
        """
        llm_called = False

        async def _llm(args: LlmCallArgs) -> LlmCallResult:
            nonlocal llm_called
            llm_called = True
            return LlmCallResult(output="x", latency_ms=1)

        with pytest.raises(SynthesisDispatchError) as exc_info:
            await dispatch_synthesis(
                db,
                _llm,
                SynthesizeRequest(
                    tier="daily",
                    memory_type="episodic-condensed",
                    source_text="x",
                    pass_session_id="ps-missing-target",
                    target_summary_id=None,
                    target_cache_id=None,
                ),
            )
        assert exc_info.value.kind == "missing_target"
        assert not llm_called, "LLM must not be called when target is missing"

        # Also verify no audit row was inserted.
        rows = db.execute("SELECT COUNT(*) FROM lcm_synthesis_audit").fetchone()
        assert rows[0] == 0

    @pytest.mark.asyncio
    async def test_2_force_model_no_override_uses_tier_default(
        self, db: sqlite3.Connection
    ) -> None:
        """Parity 2: ``force_model`` without ``model_override`` →
        :data:`DEFAULT_MODEL_BY_TIER`, NOT prompt's
        ``model_recommendation``.

        Wave-4 Auditor #5 P1.
        """
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-condensed",
                tier_label="daily",
                pass_kind="single",
                template="x",
                model_recommendation="should-NOT-be-used-because-force-model",
            ),
        )
        model_used = ""

        async def _llm(args: LlmCallArgs) -> LlmCallResult:
            nonlocal model_used
            model_used = args.model
            return LlmCallResult(output="x", latency_ms=1)

        await dispatch_synthesis(
            db,
            _llm,
            SynthesizeRequest(
                tier="daily",
                memory_type="episodic-condensed",
                source_text="x",
                pass_session_id="ps-force-model-only",
                target_summary_id="sum_target",
                force_model=True,
                # model_override intentionally None
            ),
        )
        # Wave-4 P1: falls through to tier default, NOT prompt's recommendation.
        assert model_used == DEFAULT_MODEL_BY_TIER["daily"]

    @pytest.mark.asyncio
    async def test_3_best_of_n_hard_cap_5(self, db: sqlite3.Connection) -> None:
        """Parity 3: ``best_of_n=10`` clamps to 5; surfaces
        :attr:`BestOfNDetail.requested` + :attr:`BestOfNDetail.capped`."""
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-yearly",
                tier_label="yearly",
                pass_kind="single",
                template="x",
            ),
        )
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-yearly",
                tier_label="yearly",
                pass_kind="best_of_n_judge",
                template="y",
            ),
        )

        counter = 0

        async def _llm(args: LlmCallArgs) -> LlmCallResult:
            nonlocal counter
            if args.pass_kind == "single":
                n = counter
                counter += 1
                return LlmCallResult(output=f"c{n}", latency_ms=1)
            return LlmCallResult(output="0", latency_ms=1)

        result = await dispatch_synthesis(
            db,
            _llm,
            SynthesizeRequest(
                tier="yearly",
                memory_type="episodic-yearly",
                source_text="x",
                pass_session_id="ps-hardcap",
                target_summary_id="sum_target",
                best_of_n=10,
            ),
        )
        assert result.best_of_n is not None
        # Actual N executed clamps at HARD_CAP_BEST_OF_N.
        assert result.best_of_n.n == HARD_CAP_BEST_OF_N
        # Requested / capped surface the operator's original ask.
        assert result.best_of_n.requested == 10
        assert result.best_of_n.capped is True

    @pytest.mark.asyncio
    async def test_4_all_yearly_passes_share_pass_session_id(self, db: sqlite3.Connection) -> None:
        """Parity 4: all candidates + judge share ONE ``pass_session_id``.

        Group D adversarial Gap 2 — no ``_cand{i}`` suffix.
        """
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-yearly",
                tier_label="yearly",
                pass_kind="single",
                template="x",
            ),
        )
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-yearly",
                tier_label="yearly",
                pass_kind="best_of_n_judge",
                template="y",
            ),
        )

        async def _llm(args: LlmCallArgs) -> LlmCallResult:
            if args.pass_kind == "single":
                return LlmCallResult(output="c", latency_ms=1)
            return LlmCallResult(output="0", latency_ms=1)

        await dispatch_synthesis(
            db,
            _llm,
            SynthesizeRequest(
                tier="yearly",
                memory_type="episodic-yearly",
                source_text="x",
                pass_session_id="ps-shared",
                target_summary_id="sum_target",
            ),
        )

        # All 4 audit rows (3 candidates + judge) must share pass_session_id.
        rows = db.execute("SELECT DISTINCT pass_session_id FROM lcm_synthesis_audit").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "ps-shared"

    @pytest.mark.asyncio
    async def test_5_verify_negative_marker_with_ok_still_flags(
        self, db: sqlite3.Connection
    ) -> None:
        """Parity 5: BOTH OK and UNSUPPORTED → flagged.

        Wave-4 Auditor #5 P0 — a previous relaxation matched
        ``"UNSUPPORTED: X\\nOK on rest"`` and CLEARED the flag.
        """
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-condensed",
                tier_label="monthly",
                pass_kind="single",
                template="x",
            ),
        )
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-condensed",
                tier_label="monthly",
                pass_kind="verify_fidelity",
                template="y",
            ),
        )

        async def _llm(args: LlmCallArgs) -> LlmCallResult:
            if args.pass_kind == "single":
                return LlmCallResult(output="summary", latency_ms=1)
            # Both UNSUPPORTED marker AND a stray OK on next line.
            return LlmCallResult(output="UNSUPPORTED: claim X\nOK on the rest", latency_ms=1)

        result = await dispatch_synthesis(
            db,
            _llm,
            SynthesizeRequest(
                tier="monthly",
                memory_type="episodic-condensed",
                source_text="x",
                pass_session_id="ps-verify-tight",
                target_summary_id="sum_target",
            ),
        )
        # Must FLAG because UNSUPPORTED marker present, regardless of OK.
        assert result.hallucination_flagged is True

    @pytest.mark.asyncio
    async def test_6_judge_winner_precedence_over_first_digit(self, db: sqlite3.Connection) -> None:
        """Parity 6: judge parser prefers ``Winner: N``; falls back to last
        digit.

        Final.review.3 Loop 4 Bug 4.3 — a year "2026" in reasoning, or
        "0" from the "0-indexed" instruction echo, must NOT win.
        """
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-yearly",
                tier_label="yearly",
                pass_kind="single",
                template="x",
            ),
        )
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-yearly",
                tier_label="yearly",
                pass_kind="best_of_n_judge",
                template="y",
            ),
        )

        counter = 0

        async def _llm(args: LlmCallArgs) -> LlmCallResult:
            nonlocal counter
            if args.pass_kind == "single":
                n = counter
                counter += 1
                return LlmCallResult(output=f"c{n}", latency_ms=1)
            # Reasoning has "0" from "0-indexed" echo + a year + 12 monthlies;
            # explicit Winner: 2 at end. The Winner: anchor MUST win.
            return LlmCallResult(
                output=(
                    "I'm using 0-indexed scoring (0..N-1).\n"
                    "Candidate 0 covered 2026 events well.\n"
                    "Candidate 1 missed 12 monthlies.\n"
                    "Winner: 2"
                ),
                latency_ms=1,
            )

        result = await dispatch_synthesis(
            db,
            _llm,
            SynthesizeRequest(
                tier="yearly",
                memory_type="episodic-yearly",
                source_text="x",
                pass_session_id="ps-winner-precedence",
                target_summary_id="sum_target",
            ),
        )
        assert result.best_of_n is not None
        # MUST be 2 (the Winner: anchor), not 0 (first digit in
        # "0-indexed") or 2026 / 12 (intermediate digits).
        assert result.best_of_n.selected_index == 2
        assert result.output == "c2"

    @pytest.mark.asyncio
    async def test_7_yearly_survivor_of_one_skips_judge(self, db: sqlite3.Connection) -> None:
        """Parity 7: ``asyncio.gather(*, return_exceptions=True)`` for
        yearly candidates; single-candidate survivor → SKIP the judge.

        Wave-7 P1.1/P1.2 + Wave-8 P1 CRITICAL.
        """
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-yearly",
                tier_label="yearly",
                pass_kind="single",
                template="x",
            ),
        )
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-yearly",
                tier_label="yearly",
                pass_kind="best_of_n_judge",
                template="y",
            ),
        )

        call_count = 0
        judge_called = False

        async def _llm(args: LlmCallArgs) -> LlmCallResult:
            nonlocal call_count, judge_called
            if args.pass_kind == "best_of_n_judge":
                judge_called = True
                return LlmCallResult(output="0", latency_ms=1)
            call_count += 1
            if call_count == 1:
                return LlmCallResult(output="survivor", latency_ms=100, cost_cents=50)
            raise RuntimeError("simulated failure")

        result = await dispatch_synthesis(
            db,
            _llm,
            SynthesizeRequest(
                tier="yearly",
                memory_type="episodic-yearly",
                source_text="x",
                pass_session_id="ps-survivor",
                target_summary_id="sum_target",
                best_of_n=3,
            ),
        )
        # Survivor-of-one short-circuits — judge MUST NOT be called.
        assert not judge_called, "Judge must skip when only 1 survivor"
        assert result.output == "survivor"
        assert result.best_of_n is not None
        assert result.best_of_n.n == 1
        assert result.best_of_n.selected_index == 0
        # Required fields are populated (Wave-8 P0 fix).
        assert result.primary_prompt_id is not None
        assert len(result.audit_ids) > 0

    @pytest.mark.asyncio
    async def test_7b_all_candidates_fail_raises_llm_failure(self, db: sqlite3.Connection) -> None:
        """Parity 7 part 2: if ALL candidates fail, raise ``llm_failure``."""
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-yearly",
                tier_label="yearly",
                pass_kind="single",
                template="x",
            ),
        )
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-yearly",
                tier_label="yearly",
                pass_kind="best_of_n_judge",
                template="y",
            ),
        )

        async def _llm(args: LlmCallArgs) -> LlmCallResult:
            raise RuntimeError("API outage")

        with pytest.raises(SynthesisDispatchError) as exc_info:
            await dispatch_synthesis(
                db,
                _llm,
                SynthesizeRequest(
                    tier="yearly",
                    memory_type="episodic-yearly",
                    source_text="x",
                    pass_session_id="ps-all-fail",
                    target_summary_id="sum_target",
                    best_of_n=3,
                ),
            )
        assert exc_info.value.kind == "llm_failure"

    @pytest.mark.asyncio
    async def test_8_audit_insert_fk_violation_typed_error(self, db: sqlite3.Connection) -> None:
        """Parity 8: audit insert FK violation → typed
        ``audit_insert_failure`` BEFORE LLM is called.

        Group D adversarial Gap 4.
        """
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-condensed",
                tier_label="daily",
                pass_kind="single",
                template="x",
            ),
        )

        llm_called = False

        async def _llm(args: LlmCallArgs) -> LlmCallResult:
            nonlocal llm_called
            llm_called = True
            return LlmCallResult(output="x", latency_ms=1)

        # Pass a target_summary_id that does NOT exist → FK violation on
        # the audit insert.
        with pytest.raises(SynthesisDispatchError) as exc_info:
            await dispatch_synthesis(
                db,
                _llm,
                SynthesizeRequest(
                    tier="daily",
                    memory_type="episodic-condensed",
                    source_text="x",
                    pass_session_id="ps-fk",
                    target_summary_id="nonexistent_summary_id",
                ),
            )
        assert exc_info.value.kind == "audit_insert_failure"
        # The LLM must not have been called — the typed error means the
        # forensic record failed BEFORE LLM spend.
        assert not llm_called

    @pytest.mark.asyncio
    async def test_9_verify_prompt_placeholder_aliases(self, db: sqlite3.Connection) -> None:
        """Parity 9: BOTH ``{{source_text}}`` AND ``{{source_leaves}}``
        substitute; BOTH ``{{candidate_summary}}`` AND ``{{draft}}``
        substitute.

        Final.review.3 Loop 4 Bug 4.2.
        """
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-condensed",
                tier_label="monthly",
                pass_kind="single",
                template="x",
            ),
        )
        # Use the spec-named placeholders (architecture-v4.1.md §12).
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-condensed",
                tier_label="monthly",
                pass_kind="verify_fidelity",
                template="DRAFT={{draft}} SRC_LEAVES={{source_leaves}}",
            ),
        )

        verify_prompt_seen = ""

        async def _llm(args: LlmCallArgs) -> LlmCallResult:
            nonlocal verify_prompt_seen
            if args.pass_kind == "verify_fidelity":
                verify_prompt_seen = args.prompt
                return LlmCallResult(output="OK", latency_ms=1)
            return LlmCallResult(output="the candidate", latency_ms=1)

        await dispatch_synthesis(
            db,
            _llm,
            SynthesizeRequest(
                tier="monthly",
                memory_type="episodic-condensed",
                source_text="the source",
                pass_session_id="ps-aliases",
                target_summary_id="sum_target",
            ),
        )

        # Both alias placeholders MUST have been substituted.
        assert "DRAFT=the candidate" in verify_prompt_seen
        assert "SRC_LEAVES=the source" in verify_prompt_seen
        # No leftover placeholders.
        assert "{{draft}}" not in verify_prompt_seen
        assert "{{source_leaves}}" not in verify_prompt_seen

    def test_10_empty_tier_label_normalizes_to_null(self, db: sqlite3.Connection) -> None:
        """Parity 10: empty-string tier_label normalizes to NULL in BOTH
        ``get_active_prompt`` AND ``register_prompt``.

        Group D adversarial Gap 3. The prompt registry (issue 07-08)
        owns the normalization; dispatch passes ``req.tier`` straight
        through. This test exists to anchor the invariant.
        """
        from lossless_hermes.synthesis.prompt_registry import get_active_prompt

        # Register with empty-string tier_label → normalizes to NULL.
        prompt_id = register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-leaf",
                tier_label="",
                pass_kind="single",
                template="leaf",
            ),
        )
        # Lookup with None and lookup with "" must both find it.
        active_none = get_active_prompt(
            db, memory_type="episodic-leaf", tier_label=None, pass_kind="single"
        )
        active_empty = get_active_prompt(
            db, memory_type="episodic-leaf", tier_label="", pass_kind="single"
        )
        assert active_none is not None
        assert active_empty is not None
        assert active_none.prompt_id == prompt_id
        assert active_empty.prompt_id == prompt_id

    @pytest.mark.asyncio
    async def test_12_pass_io_truncated_to_8000_chars(self, db: sqlite3.Connection) -> None:
        """Parity 12: ``pass_input_truncated`` AND ``pass_output``
        truncated to 8000 chars with ``"…(truncated)"`` marker.
        """
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-condensed",
                tier_label="daily",
                pass_kind="single",
                template="x",
            ),
        )

        long_input = "A" * 9000
        long_output = "B" * 9000

        async def _llm(args: LlmCallArgs) -> LlmCallResult:
            return LlmCallResult(output=long_output, latency_ms=1)

        await dispatch_synthesis(
            db,
            _llm,
            SynthesizeRequest(
                tier="daily",
                memory_type="episodic-condensed",
                source_text=long_input,
                pass_session_id="ps-truncate",
                target_summary_id="sum_target",
            ),
        )

        row = db.execute(
            "SELECT pass_input_truncated, pass_output FROM lcm_synthesis_audit"
        ).fetchone()
        pass_input_stored, pass_output_stored = row[0], row[1]
        # Both truncated to 8000 + "…(truncated)" marker (13 chars).
        assert len(pass_input_stored) == 8000 + len("…(truncated)")
        assert pass_input_stored.endswith("…(truncated)")
        assert pass_input_stored.startswith("A" * 8000)
        assert len(pass_output_stored) == 8000 + len("…(truncated)")
        assert pass_output_stored.endswith("…(truncated)")
        assert pass_output_stored.startswith("B" * 8000)

    @pytest.mark.asyncio
    async def test_12b_short_input_not_truncated(self, db: sqlite3.Connection) -> None:
        """Parity 12 (boundary): short input/output is NOT marked truncated."""
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-condensed",
                tier_label="daily",
                pass_kind="single",
                template="x",
            ),
        )

        async def _llm(args: LlmCallArgs) -> LlmCallResult:
            return LlmCallResult(output="short out", latency_ms=1)

        await dispatch_synthesis(
            db,
            _llm,
            SynthesizeRequest(
                tier="daily",
                memory_type="episodic-condensed",
                source_text="short in",
                pass_session_id="ps-short",
                target_summary_id="sum_target",
            ),
        )

        row = db.execute(
            "SELECT pass_input_truncated, pass_output FROM lcm_synthesis_audit"
        ).fetchone()
        assert row[0] == "short in"
        assert row[1] == "short out"

    @pytest.mark.asyncio
    async def test_13_audit_lifecycle_started_then_completed(self, db: sqlite3.Connection) -> None:
        """Parity 13: ``status='started'`` insert BEFORE LLM, UPDATE to
        ``'completed'`` after.

        The forensic record survives a crash between call and ack.
        """
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-condensed",
                tier_label="daily",
                pass_kind="single",
                template="x",
            ),
        )

        # Capture audit-row state at the moment the LLM is called.
        # The audit row must be 'started' before LLM is awaited.
        audit_status_at_llm_time: list[str] = []

        async def _llm(args: LlmCallArgs) -> LlmCallResult:
            row = db.execute("SELECT status FROM lcm_synthesis_audit").fetchone()
            if row is not None:
                audit_status_at_llm_time.append(row[0])
            return LlmCallResult(output="done", latency_ms=1)

        await dispatch_synthesis(
            db,
            _llm,
            SynthesizeRequest(
                tier="daily",
                memory_type="episodic-condensed",
                source_text="x",
                pass_session_id="ps-lifecycle",
                target_summary_id="sum_target",
            ),
        )
        # When LLM was called, audit row was in 'started' state.
        assert audit_status_at_llm_time == ["started"]

        # After completion, the row is in 'completed' state with telemetry.
        row = db.execute(
            "SELECT status, pass_output, latency_ms FROM lcm_synthesis_audit"
        ).fetchone()
        assert row[0] == "completed"
        assert row[1] == "done"
        assert row[2] == 1


# ---------------------------------------------------------------------------
# TestDispatcherClass — class-based use (no module-level singletons)
# ---------------------------------------------------------------------------


class TestDispatcherClass:
    """Verify :class:`SynthesisDispatcher` matches the AC: constructor
    stores ``(db, llm_call)`` as instance state with no module-level
    singletons."""

    @pytest.mark.asyncio
    async def test_synthesizer_uses_constructor_bound_state(self, db: sqlite3.Connection) -> None:
        """Dispatcher instance holds DB + LLM — multiple .synthesize() calls
        use the same bound state."""
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-condensed",
                tier_label="daily",
                pass_kind="single",
                template="x",
            ),
        )

        call_count = 0

        async def _llm(args: LlmCallArgs) -> LlmCallResult:
            nonlocal call_count
            call_count += 1
            return LlmCallResult(output=f"out{call_count}", latency_ms=1)

        # One dispatcher instance, two .synthesize() calls.
        dispatcher = SynthesisDispatcher(db, _llm)

        # Insert a second summary target so the FK is satisfied for both runs.
        db.execute(
            "INSERT INTO summaries (summary_id, conversation_id, kind, content,"
            " token_count) VALUES ('sum_target_2', 1, 'condensed', 'p', 1)"
        )

        r1 = await dispatcher.synthesize(
            SynthesizeRequest(
                tier="daily",
                memory_type="episodic-condensed",
                source_text="x",
                pass_session_id="ps-c1",
                target_summary_id="sum_target",
            )
        )
        r2 = await dispatcher.synthesize(
            SynthesizeRequest(
                tier="daily",
                memory_type="episodic-condensed",
                source_text="x",
                pass_session_id="ps-c2",
                target_summary_id="sum_target_2",
            )
        )
        assert r1.output == "out1"
        assert r2.output == "out2"
        # Both audit rows present.
        rows = db.execute("SELECT COUNT(*) FROM lcm_synthesis_audit").fetchone()
        assert rows[0] == 2
