"""Tests for :mod:`lossless_hermes.tools.compact` — ``lcm_compact`` tool.

Mirrors ``lossless-claw/test/v41-lcm-compact-tool.test.ts`` (333 LOC TS
-> ~280 LOC Python). Covers:

* Operator opt-in gate — disabled by default + always-register.
* Engine availability — ``engine-unavailable`` when ctx is None.
* Session resolution — ``no-session`` when neither key nor id resolves.
* Engine-side gate refusals — below-floor with default + custom
  ``reserveFraction``; clamping to ``[0.5, 1.0]``.
* Per-window cap — 2 calls succeed, 3rd refused; ``retryAfterIso``
  surfaced.
* **Wave-12 reviewer P2 invariant** — gate-refused calls do NOT burn
  the cap.
* **Wave-12 W2A1 P0 regression** — successful compact calls
  :func:`note_successful_compact` to clear the token-state cache.
* Per-session-key isolation of the counter.
* Engine reason mapping — all eight tool-facing enums via scripted
  ``CompactionResult`` from a stand-in :class:`CompactContext`.
* Engine throws -> ``{ok: False, reason: "exception"}`` without
  propagating to the caller.
* Schema + description contract — prose pinned, ``reserveFraction``
  bounds enforced.

Source pin: ``lossless-claw`` at commit ``1f07fbd`` on branch ``pr-613``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional
from unittest.mock import patch

import pytest

from lossless_hermes.compaction import CompactionResult
from lossless_hermes.db.config import LcmConfig
from lossless_hermes.plugin import token_state as _token_state
from lossless_hermes.tools.compact import (
    LCM_COMPACT_DESCRIPTION,
    LCM_COMPACT_SCHEMA,
    CompactContext,
    GateState,
    RuntimeContext,
    __reset_lcm_compact_counters_for_testing,
    handle_lcm_compact,
)


# ===========================================================================
# Fixtures + helpers
# ===========================================================================


def _success_result(action_taken: bool = True, reason: str = "compacted") -> CompactionResult:
    """Build a 'successful compaction' :class:`CompactionResult`."""
    return CompactionResult(
        action_taken=action_taken,
        tokens_before=80_000,
        tokens_after=40_000,
        created_summary_id="sum_x",
        condensed=True,
        level=None,
        passes_completed=1,
        auth_failure=False,
        reason=reason,
    )


def _noop_result(reason: str = "below threshold") -> CompactionResult:
    """Build a 'no-op' :class:`CompactionResult` (action_taken=False)."""
    return CompactionResult(
        action_taken=False,
        tokens_before=40_000,
        tokens_after=40_000,
        created_summary_id=None,
        condensed=False,
        level=None,
        passes_completed=0,
        auth_failure=False,
        reason=reason,
    )


def _default_gate_state(
    *,
    session_id: str,
    session_key: str,
    current_token_count: Optional[int],
    token_budget: Optional[int],
    reserve_fraction: float,
) -> GateState:
    """Default gate-state impl that mirrors TS test fixture ``defaultGateState``.

    Floor check only: refuses with ``below-floor`` when ratio <
    ``reserve_fraction``. Production wiring (Epic 04 wrap-up) adds the
    migration + cache-hot checks.
    """
    del session_id, session_key  # unused in default impl
    have_budget = isinstance(token_budget, int) and token_budget > 0
    have_current = isinstance(current_token_count, int) and current_token_count >= 0
    if have_budget and have_current:
        ratio = current_token_count / token_budget  # type: ignore[operator]
    else:
        ratio = None

    if ratio is not None and ratio < reserve_fraction:
        return GateState(
            owns_compaction=True,
            below_floor=True,
            should_refuse=True,
            refusal_reason="below-floor",
            refusal_note=(
                f"Context is at {ratio * 100:.1f}% of budget — below the "
                f"{reserve_fraction * 100:.0f}% floor. No need to compact yet; "
                "chained tool calls have headroom."
            ),
            context_ratio=ratio,
        )
    return GateState(
        owns_compaction=True,
        below_floor=False,
        should_refuse=False,
        context_ratio=ratio,
    )


@dataclass
class _StubCtx:
    """A concrete :class:`CompactContext` for tests.

    Attributes:
        config: The :class:`LcmConfig` (controls ``agent_compaction_tool_enabled``).
        gate_state_impl: Override for :meth:`get_agent_compaction_gate_state`.
        compact_impl: Override for :meth:`compact`. When ``None``,
            returns the default no-op (matching TS fixture).
        compact_calls: Inspector — every call to :meth:`compact` is
            recorded as a kwargs dict so tests can assert what was
            passed through.
    """

    config: LcmConfig
    gate_state_impl: Optional[Callable[..., GateState]] = None
    compact_impl: Optional[Callable[..., CompactionResult]] = None
    compact_calls: list[dict[str, Any]] = field(default_factory=list)

    def get_agent_compaction_gate_state(
        self,
        *,
        session_id: str,
        session_key: str,
        current_token_count: Optional[int],
        token_budget: Optional[int],
        reserve_fraction: float,
    ) -> GateState:
        impl = self.gate_state_impl or _default_gate_state
        return impl(
            session_id=session_id,
            session_key=session_key,
            current_token_count=current_token_count,
            token_budget=token_budget,
            reserve_fraction=reserve_fraction,
        )

    def compact(
        self,
        *,
        session_id: str,
        session_key: str,
        session_file: str,
        token_budget: Optional[int],
        current_token_count: Optional[int],
        force: bool,
    ) -> CompactionResult:
        self.compact_calls.append(
            {
                "session_id": session_id,
                "session_key": session_key,
                "session_file": session_file,
                "token_budget": token_budget,
                "current_token_count": current_token_count,
                "force": force,
            },
        )
        impl = self.compact_impl
        if impl is None:
            # Default fixture impl: noop with "no conversation found".
            return _noop_result(reason="no conversation found")
        return impl(
            session_id=session_id,
            session_key=session_key,
            session_file=session_file,
            token_budget=token_budget,
            current_token_count=current_token_count,
            force=force,
        )


def _ctx(enabled: bool = True, **kwargs: Any) -> _StubCtx:
    """Helper — build a stub context with the compaction flag toggled."""
    cfg = LcmConfig()
    cfg.agent_compaction_tool_enabled = enabled
    return _StubCtx(config=cfg, **kwargs)


def _parse(result_text: str) -> dict[str, Any]:
    """Parse a :func:`handle_lcm_compact` return value."""
    return json.loads(result_text)


@pytest.fixture(autouse=True)
def reset_counter() -> Iterator[None]:
    """Reset the per-window cap counter between tests."""
    __reset_lcm_compact_counters_for_testing()
    _token_state.__reset_token_state_for_testing()
    try:
        yield
    finally:
        __reset_lcm_compact_counters_for_testing()
        _token_state.__reset_token_state_for_testing()


# ===========================================================================
# Schema + description contract
# ===========================================================================


def test_schema_name_is_lcm_compact() -> None:
    assert LCM_COMPACT_SCHEMA["name"] == "lcm_compact"


def test_schema_description_warns_blocking_and_refusal_conditions() -> None:
    """Mirror TS lines 316-320 — description prose contract."""
    desc = LCM_COMPACT_DESCRIPTION
    assert "PROACTIVELY" in desc
    assert "blocking" in desc
    assert "REFUSES" in desc
    assert "70%" in desc  # when-to-call hint
    assert "compacted view" in desc  # what the agent gets


def test_schema_constrains_reserve_fraction_bounds() -> None:
    """Mirror TS lines 324-331 — reserveFraction min/max enforced."""
    schema = LCM_COMPACT_SCHEMA["parameters"]
    rf = schema["properties"]["reserveFraction"]
    assert rf["minimum"] == 0.5
    assert rf["maximum"] == 1.0


def test_schema_reserve_fraction_is_optional() -> None:
    """reserveFraction is the single optional param; required is empty."""
    schema = LCM_COMPACT_SCHEMA["parameters"]
    assert schema["required"] == []
    assert "reserveFraction" in schema["properties"]


# ===========================================================================
# Stage 1 — Operator opt-in gate
# ===========================================================================


def test_operator_disabled_when_flag_is_false() -> None:
    """Mirror TS lines 52-64 — default (disabled) returns operator-disabled."""
    ctx = _ctx(enabled=False)
    out = _parse(handle_lcm_compact({}, ctx=ctx, session_key="agent:main:main"))
    assert out["ok"] is False
    assert out["reason"] == "operator-disabled"
    assert "agentCompactionToolEnabled" in out["note"]


def test_schema_registers_regardless_of_flag() -> None:
    """Mirror TS lines 66-82 — tool name available always (always-register)."""
    # The schema is statically registered at import time — independent of
    # any runtime config. Just verify the name is canonical.
    assert LCM_COMPACT_SCHEMA["name"] == "lcm_compact"


# ===========================================================================
# Stage 2 — Engine availability
# ===========================================================================


def test_engine_unavailable_when_ctx_is_none() -> None:
    """Mirror TS lines 283-294 — no engine -> engine-unavailable."""
    out = _parse(handle_lcm_compact({}, ctx=None, session_key="agent:main:main"))
    assert out["ok"] is False
    assert out["reason"] == "engine-unavailable"


# ===========================================================================
# Stage 3 — Session-key resolution
# ===========================================================================


def test_no_session_when_neither_key_nor_id_resolves() -> None:
    """Mirror TS lines 296-306 — no sessionKey/sessionId -> no-session."""
    ctx = _ctx(enabled=True)
    out = _parse(handle_lcm_compact({}, ctx=ctx, session_key=None, session_id=None))
    assert out["ok"] is False
    assert out["reason"] == "no-session"


def test_no_session_when_session_key_is_whitespace() -> None:
    """Whitespace-only session_key + session_id is treated as absent."""
    ctx = _ctx(enabled=True)
    out = _parse(handle_lcm_compact({}, ctx=ctx, session_key="   ", session_id="  "))
    assert out["reason"] == "no-session"


def test_session_id_used_when_session_key_absent() -> None:
    """Fall through to session_id when session_key is missing."""
    ctx = _ctx(enabled=True, compact_impl=lambda **_kwargs: _success_result())
    rt = RuntimeContext(current_token_count=70_000, token_budget=100_000)
    out = _parse(
        handle_lcm_compact(
            {},
            ctx=ctx,
            session_key=None,
            session_id="agent:main:main",
            runtime_context=rt,
        ),
    )
    # Should not be no-session — session_id resolved.
    assert out["reason"] != "no-session"


# ===========================================================================
# Stage 4 — Engine-side gate refusals (below-floor)
# ===========================================================================


def test_below_floor_refusal_at_default_reserve_fraction() -> None:
    """Mirror TS lines 86-109 — context at 30% < 50% floor -> below-floor."""
    ctx = _ctx(enabled=True)
    rt = RuntimeContext(current_token_count=30_000, token_budget=100_000)
    out = _parse(
        handle_lcm_compact(
            {},
            ctx=ctx,
            session_id="agent:main:main",
            session_key="agent:main:main",
            runtime_context=rt,
        ),
    )
    # Gate-refusal is "ran successfully and refused" — ok=True.
    assert out["ok"] is True
    assert out["compacted"] is False
    assert out["reason"] == "below-floor"
    assert "30.0%" in out["note"]
    assert "50%" in out["note"]
    assert out["contextRatio"] == pytest.approx(0.3, abs=0.01)


def test_no_below_floor_refusal_when_above_floor() -> None:
    """Mirror TS lines 111-128 — 75% above 50% floor -> proceeds."""
    ctx = _ctx(enabled=True)
    rt = RuntimeContext(current_token_count=75_000, token_budget=100_000)
    out = _parse(
        handle_lcm_compact(
            {},
            ctx=ctx,
            session_id="agent:main:main",
            session_key="agent:main:main",
            runtime_context=rt,
        ),
    )
    assert out["reason"] != "below-floor"


def test_custom_reserve_fraction_70_refuses_at_60_percent() -> None:
    """Mirror TS lines 130-146 — reserveFraction=0.7 refuses at 60%."""
    ctx = _ctx(enabled=True)
    rt = RuntimeContext(current_token_count=60_000, token_budget=100_000)
    out = _parse(
        handle_lcm_compact(
            {"reserveFraction": 0.7},
            ctx=ctx,
            session_id="agent:main:main",
            session_key="agent:main:main",
            runtime_context=rt,
        ),
    )
    assert out["reason"] == "below-floor"
    assert "70%" in out["note"]


def test_reserve_fraction_clamped_to_0_5_floor() -> None:
    """Mirror TS lines 148-166 — values < 0.5 clamp up to 0.5."""
    ctx = _ctx(enabled=True)
    rt = RuntimeContext(current_token_count=40_000, token_budget=100_000)
    out = _parse(
        handle_lcm_compact(
            {"reserveFraction": 0.1},
            ctx=ctx,
            session_id="agent:main:main",
            session_key="agent:main:main",
            runtime_context=rt,
        ),
    )
    # 40% < 50% clamped floor -> refuses with the floor (50%) in the note.
    assert out["reason"] == "below-floor"
    assert "50%" in out["note"]


def test_reserve_fraction_clamped_to_1_0_ceiling() -> None:
    """Values > 1.0 clamp down to 1.0."""
    ctx = _ctx(enabled=True)
    # context at 95% — even with reserveFraction=2.0 (clamped to 1.0),
    # 95% < 100% so below-floor refusal fires.
    rt = RuntimeContext(current_token_count=95_000, token_budget=100_000)
    out = _parse(
        handle_lcm_compact(
            {"reserveFraction": 2.0},
            ctx=ctx,
            session_id="agent:main:main",
            session_key="agent:main:main",
            runtime_context=rt,
        ),
    )
    assert out["reason"] == "below-floor"
    # Note formatting: clamped ceiling shows as 100%.
    assert "100%" in out["note"]


def test_reserve_fraction_rejects_non_numeric() -> None:
    """Non-numeric reserveFraction falls back to 0.5 default."""
    ctx = _ctx(enabled=True)
    rt = RuntimeContext(current_token_count=40_000, token_budget=100_000)
    out = _parse(
        handle_lcm_compact(
            {"reserveFraction": "not a number"},
            ctx=ctx,
            session_id="agent:main:main",
            session_key="agent:main:main",
            runtime_context=rt,
        ),
    )
    # Default 0.5 applied -> 40% < 50% -> below-floor.
    assert out["reason"] == "below-floor"


# ===========================================================================
# Stage 5 — Per-window cap
# ===========================================================================


def test_cap_allows_up_to_2_calls_per_window() -> None:
    """Mirror TS lines 170-199 — 2 calls allowed, 3rd refused."""
    ctx = _ctx(enabled=True, compact_impl=lambda **_kwargs: _success_result())
    rt = RuntimeContext(current_token_count=70_000, token_budget=100_000)

    out1 = _parse(
        handle_lcm_compact(
            {},
            ctx=ctx,
            session_id="agent:main:main",
            session_key="agent:main:main",
            runtime_context=rt,
        ),
    )
    assert out1["reason"] != "capped-this-turn"

    out2 = _parse(
        handle_lcm_compact(
            {},
            ctx=ctx,
            session_id="agent:main:main",
            session_key="agent:main:main",
            runtime_context=rt,
        ),
    )
    assert out2["reason"] != "capped-this-turn"

    out3 = _parse(
        handle_lcm_compact(
            {},
            ctx=ctx,
            session_id="agent:main:main",
            session_key="agent:main:main",
            runtime_context=rt,
        ),
    )
    assert out3["ok"] is False
    assert out3["reason"] == "capped-this-turn"
    assert "2/2" in out3["note"]
    assert "retryAfterIso" in out3


def test_cap_resets_after_window_expires() -> None:
    """Mirror TS implicit — 6 minutes later, cap resets."""
    ctx = _ctx(enabled=True, compact_impl=lambda **_kwargs: _success_result())
    rt = RuntimeContext(current_token_count=70_000, token_budget=100_000)

    # Burn the cap at t=0.
    base_ms = 1_700_000_000_000

    with patch("lossless_hermes.tools.compact._now_ms", return_value=base_ms):
        for _ in range(2):
            handle_lcm_compact(
                {},
                ctx=ctx,
                session_id="agent:main:main",
                session_key="agent:main:main",
                runtime_context=rt,
            )
        # 3rd call: capped.
        cap_out = _parse(
            handle_lcm_compact(
                {},
                ctx=ctx,
                session_id="agent:main:main",
                session_key="agent:main:main",
                runtime_context=rt,
            ),
        )
        assert cap_out["reason"] == "capped-this-turn"

    # Jump 6 minutes ahead — window expires.
    with patch(
        "lossless_hermes.tools.compact._now_ms",
        return_value=base_ms + 6 * 60 * 1000,
    ):
        fresh_out = _parse(
            handle_lcm_compact(
                {},
                ctx=ctx,
                session_id="agent:main:main",
                session_key="agent:main:main",
                runtime_context=rt,
            ),
        )
        assert fresh_out["reason"] != "capped-this-turn"


def test_wave_12_invariant_gate_refusals_do_not_burn_cap() -> None:
    """**Wave-12 reviewer P2:** gate-refused calls do NOT consume the cap.

    Mirror TS lines 201-245. Pre-fix bug: counter incremented BEFORE the
    engine gate, so an agent probing at 30% (below-floor) burned its
    2-call budget and was locked out at 80% when it actually needed
    compaction. Post-fix: refusals are free; only gate-accepted calls
    count.
    """
    ctx = _ctx(enabled=True, compact_impl=lambda **_kwargs: _success_result())

    # 5 probes at 30% (below-floor) — all should refuse with below-floor
    # and NEVER hit the 2-call cap.
    low_rt = RuntimeContext(current_token_count=30_000, token_budget=100_000)
    for _ in range(5):
        out = _parse(
            handle_lcm_compact(
                {},
                ctx=ctx,
                session_id="agent:main:main",
                session_key="agent:main:main",
                runtime_context=low_rt,
            ),
        )
        assert out["reason"] == "below-floor"
        assert out["reason"] != "capped-this-turn"

    # Now switch to high-context — cap must be fresh (0 used).
    high_rt = RuntimeContext(current_token_count=80_000, token_budget=100_000)
    out1 = _parse(
        handle_lcm_compact(
            {},
            ctx=ctx,
            session_id="agent:main:main",
            session_key="agent:main:main",
            runtime_context=high_rt,
        ),
    )
    assert out1["reason"] != "capped-this-turn"
    out2 = _parse(
        handle_lcm_compact(
            {},
            ctx=ctx,
            session_id="agent:main:main",
            session_key="agent:main:main",
            runtime_context=high_rt,
        ),
    )
    assert out2["reason"] != "capped-this-turn"


def test_cap_is_per_session_key() -> None:
    """Mirror TS lines 247-280 — different sessions have isolated cap."""
    ctx = _ctx(enabled=True, compact_impl=lambda **_kwargs: _success_result())
    rt = RuntimeContext(current_token_count=70_000, token_budget=100_000)

    # Burn session A's cap.
    for _ in range(2):
        handle_lcm_compact(
            {},
            ctx=ctx,
            session_id="agent:main:main",
            session_key="agent:main:main",
            runtime_context=rt,
        )
    a_blocked = _parse(
        handle_lcm_compact(
            {},
            ctx=ctx,
            session_id="agent:main:main",
            session_key="agent:main:main",
            runtime_context=rt,
        ),
    )
    assert a_blocked["reason"] == "capped-this-turn"

    # Session B unaffected.
    b_fresh = _parse(
        handle_lcm_compact(
            {},
            ctx=ctx,
            session_id="agent:main:cron:job-1",
            session_key="agent:main:cron:job-1",
            runtime_context=rt,
        ),
    )
    assert b_fresh["reason"] != "capped-this-turn"


# ===========================================================================
# Stage 6/7/8 — Successful compaction + Wave-12 W2A1 P0 cache reset
# ===========================================================================


def test_successful_compact_returns_compacted_reason() -> None:
    """Successful compaction surfaces the ``compacted`` enum + note."""
    ctx = _ctx(
        enabled=True,
        compact_impl=lambda **_kwargs: _success_result(reason="compacted"),
    )
    rt = RuntimeContext(current_token_count=80_000, token_budget=100_000)
    out = _parse(
        handle_lcm_compact(
            {},
            ctx=ctx,
            session_id="agent:main:main",
            session_key="agent:main:main",
            runtime_context=rt,
        ),
    )
    assert out["ok"] is True
    assert out["compacted"] is True
    assert out["reason"] == "compacted"
    assert "compacted view" in out["note"]
    assert out["rawEngineReason"] == "compacted"


def test_wave_12_w2a1_p0_clears_token_state_cache_on_success() -> None:
    """**Wave-12 W2A1 P0:** successful compact calls :func:`note_successful_compact`.

    Without this, the cached pre-compact ratio survives the compact, the
    next gated tool refuses (stale high ratio), and the agent wedges in
    compact->refuse loops.
    """
    ctx = _ctx(
        enabled=True,
        compact_impl=lambda **_kwargs: _success_result(reason="compacted"),
    )
    session_key = "agent:main:main"

    # Seed the token-state cache with a high pre-compact ratio.
    _token_state.record_llm_output(
        session_key=session_key,
        usage={"input_tokens": 184_000},
        token_budget=200_000,
    )
    assert _token_state.get_runtime_context(session_key)["current_token_count"] == 184_000

    # Run a successful compact.
    rt = RuntimeContext(current_token_count=184_000, token_budget=200_000)
    out = _parse(
        handle_lcm_compact(
            {},
            ctx=ctx,
            session_id=session_key,
            session_key=session_key,
            runtime_context=rt,
        ),
    )
    assert out["reason"] == "compacted"

    # Cache should be cleared — fresh runtime_context lookup returns empty.
    assert _token_state.get_runtime_context(session_key) == {}


def test_wave_12_w2a1_p0_does_not_clear_cache_on_noop() -> None:
    """No-op compact (action_taken=False) MUST NOT clear the cache.

    TS line 345 guard: ``result.ok && result.compacted`` — only fires
    on a real compaction, not on noop / below-threshold paths.
    """
    ctx = _ctx(
        enabled=True,
        compact_impl=lambda **_kwargs: _noop_result(reason="below threshold"),
    )
    session_key = "agent:main:main"

    _token_state.record_llm_output(
        session_key=session_key,
        usage={"input_tokens": 70_000},
        token_budget=100_000,
    )

    rt = RuntimeContext(current_token_count=70_000, token_budget=100_000)
    handle_lcm_compact(
        {},
        ctx=ctx,
        session_id=session_key,
        session_key=session_key,
        runtime_context=rt,
    )
    # Cache survives the no-op.
    snap = _token_state.get_runtime_context(session_key)
    assert snap["current_token_count"] == 70_000


# ===========================================================================
# Stage 8 — Engine reason mapping (12 raw -> 8 enum)
# ===========================================================================


@pytest.mark.parametrize(
    "raw_reason, expected_tool_reason",
    [
        # compacted
        ("compacted", "compacted"),
        ("compaction successful in 2 passes", "compacted"),
        # noop
        ("below threshold", "noop"),
        ("already under target", "noop"),
        ("nothing to compact", "noop"),
        ("already compacted", "noop"),
        # auth-failure
        ("circuit breaker open", "auth-failure"),
        ("provider auth failure", "auth-failure"),
        # session-excluded
        ("session excluded by config", "session-excluded"),
        ("stateless session", "session-excluded"),
        # no-conversation
        ("no conversation found for sessionId=foo", "no-conversation"),
        # missing-budget
        ("missing token budget", "missing-budget"),
        # partial-compact
        ("live context still exceeds target", "partial-compact"),
        ("deferred compaction no longer needed", "partial-compact"),
        # unknown
        ("an entirely unmapped reason", "unknown"),
        ("", "unknown"),
    ],
)
def test_engine_reason_mapping(raw_reason: str, expected_tool_reason: str) -> None:
    """Each engine reason string maps to the canonical tool-facing enum."""
    # auth-failure paths require auth_failure=True for ok=False; the
    # mapping itself depends only on the raw_reason string.
    auth = expected_tool_reason == "auth-failure"
    result = CompactionResult(
        action_taken=False,
        tokens_before=80_000,
        tokens_after=80_000,
        created_summary_id=None,
        condensed=False,
        level=None,
        passes_completed=0,
        auth_failure=auth,
        reason=raw_reason,
    )
    ctx = _ctx(enabled=True, compact_impl=lambda **_kwargs: result)
    rt = RuntimeContext(current_token_count=80_000, token_budget=100_000)
    out = _parse(
        handle_lcm_compact(
            {},
            ctx=ctx,
            session_id="agent:main:main",
            session_key="agent:main:main",
            runtime_context=rt,
        ),
    )
    assert out["reason"] == expected_tool_reason
    assert out["rawEngineReason"] == raw_reason


def test_engine_reason_none_maps_to_unknown() -> None:
    """``CompactionResult.reason=None`` maps to ``unknown``."""
    result = CompactionResult(
        action_taken=False,
        tokens_before=80_000,
        tokens_after=80_000,
        created_summary_id=None,
        condensed=False,
        level=None,
        passes_completed=0,
        auth_failure=False,
        reason=None,
    )
    ctx = _ctx(enabled=True, compact_impl=lambda **_kwargs: result)
    rt = RuntimeContext(current_token_count=80_000, token_budget=100_000)
    out = _parse(
        handle_lcm_compact(
            {},
            ctx=ctx,
            session_id="agent:main:main",
            session_key="agent:main:main",
            runtime_context=rt,
        ),
    )
    assert out["reason"] == "unknown"


# ===========================================================================
# Exception path — engine throws
# ===========================================================================


def test_engine_throws_surfaces_exception_reason() -> None:
    """Engine raises -> {ok: False, reason: 'exception'} without propagating."""

    def _boom(**_kwargs: Any) -> CompactionResult:
        raise RuntimeError("the engine fell over")

    ctx = _ctx(enabled=True, compact_impl=_boom)
    rt = RuntimeContext(current_token_count=80_000, token_budget=100_000)
    out = _parse(
        handle_lcm_compact(
            {},
            ctx=ctx,
            session_id="agent:main:main",
            session_key="agent:main:main",
            runtime_context=rt,
        ),
    )
    assert out["ok"] is False
    assert out["reason"] == "exception"
    assert "fell over" in out["note"]


# ===========================================================================
# Auth-failure path — ok=False propagated
# ===========================================================================


def test_auth_failure_returns_ok_false() -> None:
    """``CompactionResult.auth_failure=True`` -> tool ``ok=False``."""
    result = CompactionResult(
        action_taken=False,
        tokens_before=80_000,
        tokens_after=80_000,
        created_summary_id=None,
        condensed=False,
        level=None,
        passes_completed=0,
        auth_failure=True,
        reason="provider auth failure",
    )
    ctx = _ctx(enabled=True, compact_impl=lambda **_kwargs: result)
    rt = RuntimeContext(current_token_count=80_000, token_budget=100_000)
    out = _parse(
        handle_lcm_compact(
            {},
            ctx=ctx,
            session_id="agent:main:main",
            session_key="agent:main:main",
            runtime_context=rt,
        ),
    )
    assert out["ok"] is False
    assert out["reason"] == "auth-failure"
    assert out["compacted"] is False


# ===========================================================================
# Compact() call surface — verify the kwargs forwarded to engine
# ===========================================================================


def test_compact_forwards_session_keys_and_runtime_context() -> None:
    """The engine's compact() receives the resolved session + runtime metrics."""
    ctx = _ctx(enabled=True, compact_impl=lambda **_kwargs: _success_result())
    rt = RuntimeContext(
        current_token_count=75_000,
        token_budget=100_000,
        session_file="/tmp/test-session.jsonl",
    )
    handle_lcm_compact(
        {},
        ctx=ctx,
        session_id="agent:main:main",
        session_key="agent:main:main",
        runtime_context=rt,
    )
    assert len(ctx.compact_calls) == 1
    call = ctx.compact_calls[0]
    assert call["session_id"] == "agent:main:main"
    assert call["session_key"] == "agent:main:main"
    assert call["session_file"] == "/tmp/test-session.jsonl"
    assert call["token_budget"] == 100_000
    assert call["current_token_count"] == 75_000
    assert call["force"] is False  # honors engine-side gates


def test_compact_skips_call_on_gate_refusal() -> None:
    """When gate refuses, the engine's compact() is NEVER invoked."""
    ctx = _ctx(enabled=True, compact_impl=lambda **_kwargs: _success_result())
    # 30% < 50% -> below-floor refusal.
    rt = RuntimeContext(current_token_count=30_000, token_budget=100_000)
    handle_lcm_compact(
        {},
        ctx=ctx,
        session_id="agent:main:main",
        session_key="agent:main:main",
        runtime_context=rt,
    )
    assert ctx.compact_calls == []
