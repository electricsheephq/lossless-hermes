"""Direct unit tests for the ``lcm_compact`` dispatch-adapter shim.

Issue [#156](https://github.com/electricsheephq/lossless-hermes/issues/156)
PR-2 added :class:`~lossless_hermes.tools._adapters._CompactCtx` — the
2-method shim implementing
:class:`~lossless_hermes.tools.compact.CompactContext` so the ported
``lcm_compact`` handler can dispatch. Unlike the four PR-1 adapter
contexts (plain attribute bags), ``_CompactCtx`` is a *behavioural*
object: it reimplements ``get_agent_compaction_gate_state`` (the engine
has no such method) and bridges ``compact()`` to
:meth:`LCMEngine.compact`.

The #156 regression test (``tests/test_dispatch_registry_coverage.py``)
verifies ``lcm_compact`` *dispatches* — but its assertion (b) uses a
fixtured engine whose ``agent_compaction_tool_enabled`` defaults to
``False``, so the handler returns ``operator-disabled`` at Stage 1 and
**never reaches the shim**. This file is the dedicated, direct coverage
the shim itself needs: it instantiates ``_CompactCtx`` and drives both
methods.

What this file covers
---------------------

* ``get_agent_compaction_gate_state`` — every branch:
  - ``owns_compaction`` not ``True`` → ``engine-unhealthy`` refusal.
  - ratio below the floor → ``below-floor`` refusal.
  - ratio at / above the floor → accept.
  - absent telemetry (no budget / no current) → floor check skipped,
    accept.
  - ``reserve_fraction`` clamp at both ``[0.5, 1.0]`` bounds + the
    non-finite → default fallback.
* ``compact()``:
  - no-conversation no-op → ``ok=true`` (TS parity).
  - **missing-budget no-op → ``ok=false``** — pins the #156-PR-2
    MAJOR-1 fix (TS ``executeCompactionCore`` returns ``ok: false``).
  - delegate path → the shim resolves session→conversation and calls
    the real ``LCMEngine.compact``, returning its verbatim result.
  - ``force=True`` emits a defensive ``logger.warning``.
* The shim builds from a fixtured :class:`LCMEngine` and structurally
  satisfies :class:`CompactContext`.

Gate-state expectations are cross-checked against TS
``LcmContextEngine.getAgentCompactionGateState``
(``lossless-claw/src/engine.ts:7118-7183`` @ ``1f07fbd``); the
missing-budget ``ok`` expectation against TS ``executeCompactionCore``
(``engine.ts:3363-3369``).

Platform note
-------------

The ``get_agent_compaction_gate_state`` tests use a **bare**
:class:`LCMEngine` (no ``on_session_start``) — the gate logic touches
only ``engine.info`` + ``engine.config``, never the DB — so they run on
every platform. The ``compact()`` tests need a real conversation store,
so they use the ``on_session_start``-fixtured engine and carry the same
``enable_load_extension`` skip marker as
``tests/test_dispatch_registry_coverage.py``.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path
from typing import Any, Iterator

import pytest

from lossless_hermes.compaction import CompactionResult
from lossless_hermes.db.config import LcmConfig
from lossless_hermes.engine import LCMEngine
from lossless_hermes.tools._adapters import _CompactCtx
from lossless_hermes.tools.compact import CompactContext, GateState, _result_ok

# ---------------------------------------------------------------------------
# Skip marker — Apple system Python lacks enable_load_extension
# ---------------------------------------------------------------------------
# Mirrors ``tests/test_dispatch_registry_coverage.py``: tests that call
# ``on_session_start`` need a full ``open_lcm_db`` connection (sqlite-vec
# loads via ``enable_load_extension``); Apple's system CPython ships
# without ``--enable-loadable-sqlite-extensions`` and the engine
# hard-raises at construction. The gate-state tests (bare engine, no DB)
# run everywhere; only the ``compact()`` tests carry this marker.
_skip_no_extension_loading = pytest.mark.skipif(
    not hasattr(sqlite3.Connection, "enable_load_extension"),
    reason=(
        "actions/setup-python on macOS ships a CPython build without "
        "--enable-loadable-sqlite-extensions; sqlite-vec cannot load and "
        "the engine hard-raises at construction. The on_session_start-"
        "fixtured compact() tests skip here (the bare-engine gate-state "
        "tests still run)."
    ),
)


# ===========================================================================
# Fixtures + helpers
# ===========================================================================


def _bare_engine(*, owns_compaction: bool = True) -> LCMEngine:
    """Build a bare :class:`LCMEngine` — no ``on_session_start``, no DB.

    ``_CompactCtx.get_agent_compaction_gate_state`` reads only
    ``engine.info.owns_compaction`` + (indirectly) ``engine.config``, so
    the gate-state tests need no DB. ``ContextEngineInfo`` is frozen, so
    a non-default ``owns_compaction`` is set via :func:`dataclasses.replace`.
    """
    eng = LCMEngine(config=LcmConfig())
    if not owns_compaction:
        eng.info = dataclasses.replace(eng.info, owns_compaction=False)
    return eng


def _compact_ctx(*, owns_compaction: bool = True) -> _CompactCtx:
    """Build a :class:`_CompactCtx` over a bare engine for gate-state tests."""
    eng = _bare_engine(owns_compaction=owns_compaction)
    return _CompactCtx(config=eng.config, _engine=eng)


@pytest.fixture
def session_engine(tmp_home: Path) -> Iterator[LCMEngine]:
    """An :class:`LCMEngine` with ``on_session_start`` run — DB opened.

    Mirrors ``tests/test_dispatch_registry_coverage.py::engine``. The
    ``compact()`` tests need a live ``_conversation_store``.
    """
    eng = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    eng.on_session_start("test-session")
    try:
        yield eng
    finally:
        eng.on_session_end("test-session", [])


# ===========================================================================
# Structural conformance — _CompactCtx satisfies CompactContext
# ===========================================================================


def test_compact_ctx_structurally_satisfies_compact_context() -> None:
    """``_CompactCtx`` is usable everywhere a :class:`CompactContext` is.

    ``CompactContext`` is a runtime-uncheckable Protocol (it declares
    methods), so this asserts the structural surface directly: the shim
    exposes ``config`` plus both required methods, and a typed binding
    to ``CompactContext`` holds. ``ty`` enforces this statically at the
    ``handle_lcm_compact(ctx=...)`` call site in ``_adapters.py``; this
    test pins it at runtime so a drift in either Protocol or shim is
    caught here too.
    """
    ctx = _compact_ctx()
    bound: CompactContext = ctx  # static + runtime: the shim is a CompactContext
    assert isinstance(bound.config, LcmConfig)
    assert callable(bound.get_agent_compaction_gate_state)
    assert callable(bound.compact)


# ===========================================================================
# get_agent_compaction_gate_state — Gate 1: owns_compaction
# ===========================================================================


def test_gate_state_owns_compaction_false_refuses_engine_unhealthy() -> None:
    """``owns_compaction`` not ``True`` → ``engine-unhealthy`` refusal.

    TS ``engine.ts:7134-7144``: ``ownsCompaction === true`` is the first
    gate; when false the engine refuses with ``refusalReason:
    "engine-unhealthy"``. The Python port checks ``is not True``.
    """
    ctx = _compact_ctx(owns_compaction=False)
    state = ctx.get_agent_compaction_gate_state(
        session_id="s",
        session_key="s",
        current_token_count=90_000,
        token_budget=100_000,
        reserve_fraction=0.5,
    )
    assert isinstance(state, GateState)
    assert state.owns_compaction is False
    assert state.below_floor is False
    assert state.should_refuse is True
    assert state.refusal_reason == "engine-unhealthy"
    assert "migration did not complete" in (state.refusal_note or "")


def test_gate_state_owns_compaction_false_wins_over_below_floor() -> None:
    """Gate 1 (``owns_compaction``) is checked before Gate 2 (below-floor).

    TS order — first refusal wins (``engine.ts:7101`` comment). Even
    when the ratio is also below the floor, the ``engine-unhealthy``
    refusal is returned, not ``below-floor``.
    """
    ctx = _compact_ctx(owns_compaction=False)
    # ratio 0.1 — far below floor; engine-unhealthy must still win.
    state = ctx.get_agent_compaction_gate_state(
        session_id="s",
        session_key="s",
        current_token_count=10_000,
        token_budget=100_000,
        reserve_fraction=0.5,
    )
    assert state.refusal_reason == "engine-unhealthy"


# ===========================================================================
# get_agent_compaction_gate_state — Gate 2: below-floor
# ===========================================================================


def test_gate_state_ratio_below_floor_refuses() -> None:
    """ratio < ``reserve_fraction`` → ``below-floor`` refusal.

    TS ``engine.ts:7152-7175``: ``contextRatio < reserveFraction``
    refuses with ``refusalReason: "below-floor"`` and echoes
    ``contextRatio``. ratio here is 0.30, floor 0.50.
    """
    ctx = _compact_ctx()
    state = ctx.get_agent_compaction_gate_state(
        session_id="s",
        session_key="s",
        current_token_count=30_000,
        token_budget=100_000,
        reserve_fraction=0.5,
    )
    assert state.owns_compaction is True
    assert state.below_floor is True
    assert state.should_refuse is True
    assert state.refusal_reason == "below-floor"
    assert state.context_ratio == pytest.approx(0.30)
    # Note prose mirrors the TS .1f / .0f format strings.
    assert "30.0%" in (state.refusal_note or "")
    assert "50%" in (state.refusal_note or "")


def test_gate_state_ratio_above_floor_accepts() -> None:
    """ratio >= ``reserve_fraction`` → accept (no refusal).

    TS ``engine.ts:7177-7183``: the fall-through accept path —
    ``shouldRefuse: false``, ``contextRatio`` echoed. ratio 0.90, floor
    0.50.
    """
    ctx = _compact_ctx()
    state = ctx.get_agent_compaction_gate_state(
        session_id="s",
        session_key="s",
        current_token_count=90_000,
        token_budget=100_000,
        reserve_fraction=0.5,
    )
    assert state.owns_compaction is True
    assert state.below_floor is False
    assert state.should_refuse is False
    assert state.refusal_reason is None
    assert state.context_ratio == pytest.approx(0.90)


def test_gate_state_ratio_exactly_at_floor_accepts() -> None:
    """ratio == ``reserve_fraction`` is NOT below the floor → accept.

    TS uses a strict ``<`` (``engine.ts:7172`` ``contextRatio <
    reserveFraction``), so a ratio exactly equal to the floor accepts.
    ratio 0.50 == floor 0.50.
    """
    ctx = _compact_ctx()
    state = ctx.get_agent_compaction_gate_state(
        session_id="s",
        session_key="s",
        current_token_count=50_000,
        token_budget=100_000,
        reserve_fraction=0.5,
    )
    assert state.below_floor is False
    assert state.should_refuse is False
    assert state.context_ratio == pytest.approx(0.50)


# ===========================================================================
# get_agent_compaction_gate_state — absent telemetry skips the floor check
# ===========================================================================


@pytest.mark.parametrize(
    "current, budget",
    [
        (None, 100_000),  # no current — pre-first-LLM-response
        (90_000, None),  # no budget — empty runtime telemetry
        (None, None),  # neither
    ],
    ids=["no-current", "no-budget", "neither"],
)
def test_gate_state_absent_telemetry_skips_floor_and_accepts(
    current: Any,
    budget: Any,
) -> None:
    """Absent token telemetry → the floor check is skipped → accept.

    TS ``engine.ts:7163-7172``: ``contextRatio`` is ``undefined`` unless
    *both* ``haveBudget`` and ``haveCurrent`` hold, and ``belowFloor``
    is only true when ``contextRatio !== undefined``. So a missing
    figure makes the gate accept with ``contextRatio`` ``None``. This is
    the live path that reaches the shim's missing-budget guard inside
    ``compact()`` (see ``test_compact_missing_budget_*``).
    """
    ctx = _compact_ctx()
    state = ctx.get_agent_compaction_gate_state(
        session_id="s",
        session_key="s",
        current_token_count=current,
        token_budget=budget,
        reserve_fraction=0.5,
    )
    assert state.owns_compaction is True
    assert state.below_floor is False
    assert state.should_refuse is False
    assert state.context_ratio is None


def test_gate_state_nonpositive_budget_treated_as_absent() -> None:
    """A zero / negative ``token_budget`` is treated as absent telemetry.

    TS ``engine.ts:7165`` requires ``params.tokenBudget > 0`` for
    ``haveBudget``. A zero budget would otherwise be a divide-by-zero;
    the predicate guards it, and the gate accepts (floor check skipped).
    """
    ctx = _compact_ctx()
    state = ctx.get_agent_compaction_gate_state(
        session_id="s",
        session_key="s",
        current_token_count=90_000,
        token_budget=0,
        reserve_fraction=0.5,
    )
    assert state.should_refuse is False
    assert state.context_ratio is None


# ===========================================================================
# get_agent_compaction_gate_state — reserve_fraction clamp [0.5, 1.0]
# ===========================================================================


def test_gate_state_reserve_fraction_clamped_at_lower_bound() -> None:
    """``reserve_fraction`` below 0.5 is clamped UP to 0.5.

    TS ``engine.ts:7149`` ``Math.max(0.5, Math.min(1.0, r))``. A passed
    fraction of 0.10 clamps to 0.50, so a ratio of 0.30 (which would
    accept against an un-clamped 0.10 floor) instead refuses
    ``below-floor`` against the clamped 0.50 floor.
    """
    ctx = _compact_ctx()
    state = ctx.get_agent_compaction_gate_state(
        session_id="s",
        session_key="s",
        current_token_count=30_000,
        token_budget=100_000,
        reserve_fraction=0.10,  # clamps up to 0.50
    )
    assert state.should_refuse is True
    assert state.refusal_reason == "below-floor"
    # The clamped 0.50 floor is what the note reports.
    assert "50%" in (state.refusal_note or "")


def test_gate_state_reserve_fraction_clamped_at_upper_bound() -> None:
    """``reserve_fraction`` above 1.0 is clamped DOWN to 1.0.

    TS ``engine.ts:7149``. A passed fraction of 5.0 clamps to 1.0; a
    ratio of 0.95 is then below the clamped 1.0 floor → ``below-floor``.
    """
    ctx = _compact_ctx()
    state = ctx.get_agent_compaction_gate_state(
        session_id="s",
        session_key="s",
        current_token_count=95_000,
        token_budget=100_000,
        reserve_fraction=5.0,  # clamps down to 1.0
    )
    assert state.should_refuse is True
    assert state.refusal_reason == "below-floor"
    # The clamped 1.0 floor → "100%" in the note.
    assert "100%" in (state.refusal_note or "")


def test_gate_state_reserve_fraction_exact_bounds_pass_through() -> None:
    """The exact bounds 0.5 and 1.0 pass through the clamp unchanged."""
    ctx = _compact_ctx()
    # 0.5 floor, ratio 0.50 → exactly at floor → accept (strict <).
    at_low = ctx.get_agent_compaction_gate_state(
        session_id="s",
        session_key="s",
        current_token_count=50_000,
        token_budget=100_000,
        reserve_fraction=0.5,
    )
    assert at_low.should_refuse is False
    # 1.0 floor, ratio 0.99 → below → refuse.
    at_high = ctx.get_agent_compaction_gate_state(
        session_id="s",
        session_key="s",
        current_token_count=99_000,
        token_budget=100_000,
        reserve_fraction=1.0,
    )
    assert at_high.should_refuse is True
    assert at_high.refusal_reason == "below-floor"


@pytest.mark.parametrize(
    "bad_reserve",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        True,  # bool is an int subclass — TS has no bool/number conflation
    ],
    ids=["nan", "inf", "-inf", "bool"],
)
def test_gate_state_non_finite_reserve_fraction_falls_back_to_default(
    bad_reserve: Any,
) -> None:
    """Non-finite / bool ``reserve_fraction`` → the 0.5 default floor.

    TS ``engine.ts:7148`` ``if (typeof r !== "number" ||
    !Number.isFinite(r)) return 0.5``. The Python port additionally
    rejects ``bool`` (an ``int`` subclass — a real value an agent
    provider can emit). Either way the floor becomes 0.5: a ratio of
    0.30 then refuses ``below-floor`` against that 0.5 default.
    """
    ctx = _compact_ctx()
    state = ctx.get_agent_compaction_gate_state(
        session_id="s",
        session_key="s",
        current_token_count=30_000,
        token_budget=100_000,
        reserve_fraction=bad_reserve,
    )
    assert state.should_refuse is True
    assert state.refusal_reason == "below-floor"
    assert "50%" in (state.refusal_note or "")


# ===========================================================================
# compact() — no-conversation no-op (ok=true, TS parity)
# ===========================================================================


@_skip_no_extension_loading
def test_compact_no_conversation_returns_ok_true_noop(
    session_engine: LCMEngine,
) -> None:
    """No conversation for the session → ``ok=true`` no-op.

    TS ``compact()`` (``engine.ts:7223-7227``) returns ``{ok: true,
    compacted: false, reason: "no conversation found for session"}``
    when no conversation exists — a fresh session with no recorded
    history is not an error. The shim reproduces that: ``auth_failure``
    is ``False`` and the reason does NOT match a non-auth-failure
    substring, so :func:`_result_ok` reports ``ok=true``.
    """
    ctx = _CompactCtx(config=session_engine.config, _engine=session_engine)
    # The fixtured engine's session has no conversation row recorded.
    result = ctx.compact(
        session_id="unknown-session-xyz",
        session_key="unknown-session-xyz",
        session_file="",
        token_budget=100_000,
        current_token_count=90_000,
        force=False,
    )
    assert isinstance(result, CompactionResult)
    assert result.action_taken is False
    assert result.auth_failure is False
    assert result.reason == "no conversation found for session"
    # TS parity: the no-conversation no-op is ok=true.
    assert _result_ok(result) is True


# ===========================================================================
# compact() — missing-budget no-op (ok=FALSE) — #156-PR-2 MAJOR-1 fix
# ===========================================================================


@_skip_no_extension_loading
def test_compact_missing_budget_returns_ok_false_noop(
    session_engine: LCMEngine,
) -> None:
    """Missing ``token_budget`` → ``ok=FALSE`` no-op. Pins MAJOR-1.

    TS ``executeCompactionCore`` (``engine.ts:3363-3369``) returns
    ``{ok: false, compacted: false, reason: "missing token budget in
    compact params"}``. The shim's missing-budget guard reproduces that
    reason; the #156-PR-2 fix made :func:`_result_ok` recognise it as a
    non-auth failure, so ``ok`` is ``False`` — WITHOUT setting
    ``auth_failure`` (a budget problem must not be mislabelled an auth
    failure).

    A conversation must exist for the guard to be reached: the shim
    resolves session→conversation FIRST, then checks the budget. So this
    test creates a conversation, then calls ``compact`` with
    ``token_budget=None``.
    """
    store = session_engine._conversation_store
    assert store is not None
    conv = store.get_or_create_conversation("budget-test-session")

    ctx = _CompactCtx(config=session_engine.config, _engine=session_engine)
    result = ctx.compact(
        session_id="budget-test-session",
        session_key="budget-test-session",
        session_file="",
        token_budget=None,  # the missing-budget path
        current_token_count=90_000,
        force=False,
    )
    assert isinstance(result, CompactionResult)
    assert result.action_taken is False
    assert result.reason == "missing token budget in compact params"
    # MAJOR-1: auth_failure stays False — this is a budget problem, an
    # honest non-auth failure, not an auth failure.
    assert result.auth_failure is False
    # MAJOR-1: _result_ok now reports ok=FALSE for the missing-budget
    # reason (TS executeCompactionCore parity). Before the fix this
    # derived ok=true and misinformed the agent.
    assert _result_ok(result) is False
    # The conversation lookup did succeed (the guard fired AFTER it).
    assert conv.conversation_id is not None


@_skip_no_extension_loading
@pytest.mark.parametrize(
    "bad_budget",
    [None, 0, -1, True],
    ids=["none", "zero", "negative", "bool-true"],
)
def test_compact_invalid_budget_variants_all_ok_false(
    session_engine: LCMEngine,
    bad_budget: Any,
) -> None:
    """Every non-positive-int ``token_budget`` hits the missing-budget guard.

    The shim's ``have_budget`` predicate requires a real positive
    ``int`` (``isinstance int``, not ``bool``, ``> 0``). ``None`` / ``0``
    / negative / ``True`` all fail it → the missing-budget no-op → the
    MAJOR-1 ``ok=false``.
    """
    store = session_engine._conversation_store
    assert store is not None
    store.get_or_create_conversation("budget-variant-session")

    ctx = _CompactCtx(config=session_engine.config, _engine=session_engine)
    result = ctx.compact(
        session_id="budget-variant-session",
        session_key="budget-variant-session",
        session_file="",
        token_budget=bad_budget,
        current_token_count=90_000,
        force=False,
    )
    assert result.reason == "missing token budget in compact params"
    assert result.auth_failure is False
    assert _result_ok(result) is False


# ===========================================================================
# compact() — delegate path: session→conversation + real LCMEngine.compact
# ===========================================================================


class _ScriptedCompactEngine(LCMEngine):
    """An :class:`LCMEngine` with a scripted ``_execute_compaction_core``.

    ``LCMEngine._execute_compaction_core`` is an unwired hook that raises
    ``NotImplementedError`` until the Epic-04 wrap-up composes a real
    :class:`~lossless_hermes.compaction.CompactionEngine` — so a plain
    fixtured engine cannot exercise the shim's *delegate* path
    end-to-end. Overriding the hook with a scripted result (the
    documented test pattern — see ``_execute_compaction_core``'s own
    docstring) lets the test drive the full bridge: session→conversation
    resolution, the budget guard passing, and the real
    :meth:`LCMEngine.compact` body (breaker gate + result pass-through).
    """

    scripted_result: CompactionResult

    def _execute_compaction_core(
        self,
        *,
        conversation_id: int,
        token_budget: int,
        current_tokens: int,
        provider: str | None,
        model: str | None,
    ) -> CompactionResult:
        # Record the conversation_id the bridge resolved so the test can
        # assert the session→conversation translation happened.
        self.seen_conversation_id = conversation_id
        self.seen_token_budget = token_budget
        self.seen_current_tokens = current_tokens
        return self.scripted_result


@_skip_no_extension_loading
def test_compact_delegate_path_resolves_conversation_and_returns_result(
    tmp_home: Path,
) -> None:
    """The delegate path: shim → ``LCMEngine.compact`` → verbatim result.

    With a present conversation and a valid budget the shim resolves
    ``session_id`` → ``conversation_id`` (TS ``engine.ts:7218-7228``),
    passes the budget guard, and delegates to the real
    :meth:`LCMEngine.compact`. The engine's breaker is closed (fresh
    engine), so the call reaches ``_execute_compaction_core`` — here
    scripted to a success result, which the shim returns verbatim.
    """
    scripted = CompactionResult(
        action_taken=True,
        tokens_before=90_000,
        tokens_after=40_000,
        created_summary_id="sum_delegate",
        condensed=True,
        level=None,
        passes_completed=1,
        auth_failure=False,
        reason="compacted",
    )
    eng = _ScriptedCompactEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    eng.scripted_result = scripted
    eng.on_session_start("delegate-session")
    try:
        store = eng._conversation_store
        assert store is not None
        conv = store.get_or_create_conversation("delegate-session")

        ctx = _CompactCtx(config=eng.config, _engine=eng)
        result = ctx.compact(
            session_id="delegate-session",
            session_key="delegate-session",
            session_file="",
            token_budget=100_000,
            current_token_count=90_000,
            force=False,
        )
        # The shim returned the engine's result verbatim.
        assert result is scripted
        assert result.action_taken is True
        assert _result_ok(result) is True
        # The session→conversation translation happened — the engine saw
        # the resolved integer conversation_id, not the session string.
        assert eng.seen_conversation_id == conv.conversation_id
        assert eng.seen_token_budget == 100_000
        # current_token_count forwarded as observed-tokens.
        assert eng.seen_current_tokens == 90_000
    finally:
        eng.on_session_end("delegate-session", [])


@_skip_no_extension_loading
def test_compact_delegate_defaults_current_tokens_to_zero_when_none(
    tmp_home: Path,
) -> None:
    """A ``None`` ``current_token_count`` is forwarded to the engine as 0.

    The shim docstring (Step 3) defaults ``current_token_count`` to 0
    when ``None`` — the engine consumes it only for breaker-open
    telemetry. ``token_budget`` must still be a real int (else the
    missing-budget guard fires first), so only ``current_token_count``
    is ``None`` here.
    """
    scripted = CompactionResult(
        action_taken=False,
        tokens_before=0,
        tokens_after=0,
        created_summary_id=None,
        condensed=False,
        level=None,
        passes_completed=0,
        auth_failure=False,
        reason="below threshold",
    )
    eng = _ScriptedCompactEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    eng.scripted_result = scripted
    eng.on_session_start("delegate-none-session")
    try:
        store = eng._conversation_store
        assert store is not None
        store.get_or_create_conversation("delegate-none-session")

        ctx = _CompactCtx(config=eng.config, _engine=eng)
        ctx.compact(
            session_id="delegate-none-session",
            session_key="delegate-none-session",
            session_file="",
            token_budget=100_000,
            current_token_count=None,  # → engine sees 0
            force=False,
        )
        assert eng.seen_current_tokens == 0
    finally:
        eng.on_session_end("delegate-none-session", [])


# ===========================================================================
# compact() — store-unset self-consistency guard
# ===========================================================================


def test_compact_store_unset_returns_no_conversation_noop() -> None:
    """A bare engine (no ``on_session_start``) → no-conversation no-op.

    The adapter's engine-readiness guard catches an unset
    ``_conversation_store`` before the shim is built — but the shim's
    ``compact()`` is independently self-consistent: with no store it
    returns the ``ok=true`` no-conversation no-op rather than raising
    an ``AttributeError``. Uses a bare engine, so this runs on every
    platform.
    """
    eng = _bare_engine()
    assert eng._conversation_store is None
    ctx = _CompactCtx(config=eng.config, _engine=eng)
    result = ctx.compact(
        session_id="s",
        session_key="s",
        session_file="",
        token_budget=100_000,
        current_token_count=90_000,
        force=False,
    )
    assert result.action_taken is False
    assert result.auth_failure is False
    assert result.reason == "no conversation found for session"
    assert _result_ok(result) is True


# ===========================================================================
# compact() — NIT: force=True emits a defensive warning
# ===========================================================================


def test_compact_force_true_emits_defensive_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``force=True`` → a ``logger.warning`` (the dropped-flag defence).

    ``LCMEngine.compact()`` has no ``force`` parameter and
    ``handle_lcm_compact`` hardcodes ``force=False`` — so ``force=True``
    is currently unreachable and silently dropped. The shim emits a
    ``logger.warning`` if it is ever received, so a future handler
    change is visible in the gateway log. A bare engine is fine — the
    warning fires before the (here no-op) compaction body.
    """
    eng = _bare_engine()
    ctx = _CompactCtx(config=eng.config, _engine=eng)
    with caplog.at_level("WARNING", logger="lossless_hermes.tools._adapters"):
        ctx.compact(
            session_id="s",
            session_key="s",
            session_file="",
            token_budget=100_000,
            current_token_count=90_000,
            force=True,
        )
    assert any(
        "force=True" in rec.message and rec.levelname == "WARNING" for rec in caplog.records
    ), f"expected a force=True warning, got {[r.message for r in caplog.records]}"


def test_compact_force_false_emits_no_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``force=False`` (the only value the handler passes) → no warning.

    The defensive warning must not be noise on the live path — it fires
    only for the currently-unreachable ``force=True``.
    """
    eng = _bare_engine()
    ctx = _CompactCtx(config=eng.config, _engine=eng)
    with caplog.at_level("WARNING", logger="lossless_hermes.tools._adapters"):
        ctx.compact(
            session_id="s",
            session_key="s",
            session_file="",
            token_budget=100_000,
            current_token_count=90_000,
            force=False,
        )
    assert not any("force" in rec.message for rec in caplog.records)
