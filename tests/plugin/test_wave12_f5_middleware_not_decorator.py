"""Regression test pinning Wave-12 F5: middleware-not-decorator.

This is the **load-bearing** test for issue 06-03. Per **Wave-12 F5**
([ADR-029] §"Known Wave-N fixes" row), ``run_with_token_gate`` is
applied as MIDDLEWARE in :meth:`LCMEngine.handle_tool_call` based on
:data:`TOKEN_GATE_TOOLS` membership AT INVOCATION TIME, not as a
decorator at registration time.

Why this matters
================

If the gate were applied as a decorator at plugin-init (the "naïve
TS-to-Python translation" of the decorator-like ``runWithTokenGate``
wrapping), the gate's runtime context (``current_token_count`` +
``token_budget``) would be FROZEN to whatever was true at plugin init.
At plugin init the cache is empty, so the gate would bypass forever —
and every tool call would dispatch regardless of context pressure,
defeating the purpose of the gate.

The middleware pattern is correct: the wrap runs on every dispatch,
and ``get_runtime_context`` reads the LIVE cache state every time.

Regression-test shape
=====================

This test exercises the contract directly:

1. Register / call the engine once with one cache state -> assert one
   behavior (e.g. proceed because cache is empty).
2. Mutate the cache to simulate a later state.
3. Call the engine AGAIN with the same params -> assert the gate now
   reads the MUTATED state and behaves accordingly (e.g. refuses).

If a future refactor accidentally re-introduces decorator-time
freezing, step 3 would still see step 1's state and produce step 1's
behavior — the assertion at step 3 fails loudly, with a comment that
explains why.

References
----------

* ADR-029 §"Known Wave-N fixes" Wave-12 F5 row.
* Issue spec: ``epics/06-tools/06-03-runwithtokengate-middleware.md``
  AC bullet "tests/plugin/test_wave12_f5_middleware_not_decorator.py".
* TS source: ``/Volumes/LEXAR/Claude/lossless-claw/src/plugin/needs-compact-gate.ts``.
"""

from __future__ import annotations

import json
import typing
from pathlib import Path

import pytest

from lossless_hermes.db.config import LcmConfig
from lossless_hermes.engine import LCMEngine
from lossless_hermes.plugin import token_state


@pytest.fixture(autouse=True)
def _reset_token_state() -> typing.Iterator[None]:
    token_state.__reset_token_state_for_testing()
    yield
    token_state.__reset_token_state_for_testing()


class _TestEngine(LCMEngine):
    """Test subclass that overrides :meth:`_dispatch_tool_call`.

    Returns a known echo string instead of raising :class:`NotImplementedError`,
    so we can observe the wrapper's behavior end-to-end without depending
    on real per-tool handler bodies (which land in 06-07..06-14).
    """

    def _dispatch_tool_call(self, name: str, args: dict, **kwargs: typing.Any) -> str:
        return json.dumps({"echo": {"name": name, "args": args}})


def _make_engine(tmp_path: Path) -> _TestEngine:
    """Construct the subclass; do NOT call on_session_start (we don't need DB)."""
    return _TestEngine(hermes_home=tmp_path / ".hermes", config=LcmConfig())


# ---------------------------------------------------------------------------
# The load-bearing regression: gate reads the LATEST context, not a snapshot
# ---------------------------------------------------------------------------


def test_gate_reads_latest_runtime_context_not_a_snapshot(tmp_path: Path) -> None:
    """**Wave-12 F5 regression test.**

    Issue spec verbatim: "register a tool, mutate runtime context, call
    again; assert the gate reads the LATEST runtime context, not a
    snapshot taken at registration time."

    The test below:

    1. Constructs an engine and sets up an empty token-state cache.
    2. Calls ``handle_tool_call("lcm_describe", ...)`` — gate bypasses
       (no telemetry) so the inner echoes the args.
    3. Mutates the cache to put context at 95% of budget.
    4. Calls ``handle_tool_call`` AGAIN with the SAME params.
    5. Asserts the gate NOW refuses because the latest cache state
       crosses the threshold.

    If the gate were frozen at registration time (decorator-pattern
    regression), step 5 would still echo because it would see the
    cache as empty.
    """
    engine = _make_engine(tmp_path)
    engine.current_session_id = "sess"

    # --- Step 1+2: empty cache, gate bypasses, inner echoes -------------
    result1 = engine.handle_tool_call(
        "lcm_describe",
        {"expandChildren": True, "expandMessages": True},
    )
    payload1 = json.loads(result1)
    assert payload1 == {
        "echo": {
            "name": "lcm_describe",
            "args": {"expandChildren": True, "expandMessages": True},
        }
    }

    # --- Step 3: mutate cache to put context at 95% of budget ---------
    # 200K budget, 190K used = 95%. lcm_describe with both flags estimates
    # to 10K (capped). (190K + 10K) / 200K = 1.0 > 0.92 -> refuse.
    token_state.record_llm_output(
        session_key="sess",
        usage={"input_tokens": 190_000},
        token_budget=200_000,
    )

    # --- Step 4+5: same params, gate now refuses ---------------------
    result2 = engine.handle_tool_call(
        "lcm_describe",
        {"expandChildren": True, "expandMessages": True},
    )
    payload2 = json.loads(result2)
    assert payload2["ok"] is False, (
        "REGRESSION: gate did NOT see the post-mutation cache. "
        "If you got here, run_with_token_gate is reading frozen state "
        "from registration time (decorator-pattern). Per Wave-12 F5 / "
        "ADR-029 the wrap MUST run at invocation time with a fresh "
        "get_runtime_context call. See `LCMEngine.handle_tool_call`."
    )
    assert payload2["needsCompact"] is True
    assert payload2["reason"] == "context-overflow-prevention"


def test_gate_recovery_after_post_compact_reset(tmp_path: Path) -> None:
    """After a successful compact, the gate stops refusing.

    Sequence:

    1. Cache anchored at 190K -> gate refuses ``lcm_describe`` (95% context).
    2. ``note_successful_compact`` clears the cache.
    3. Same call now bypasses (cache empty = bypass) and echoes.

    Pins the W2A1 post-compact-cache-reset hook works end-to-end via the
    engine's :meth:`handle_tool_call`.
    """
    engine = _make_engine(tmp_path)
    engine.current_session_id = "sess"

    token_state.record_llm_output(
        session_key="sess",
        usage={"input_tokens": 190_000},
        token_budget=200_000,
    )

    # Step 1: refused.
    result1 = engine.handle_tool_call(
        "lcm_describe",
        {"expandChildren": True, "expandMessages": True},
    )
    assert json.loads(result1)["ok"] is False

    # Step 2: post-compact reset.
    token_state.note_successful_compact("sess")

    # Step 3: cache empty -> bypass -> echo.
    result2 = engine.handle_tool_call(
        "lcm_describe",
        {"expandChildren": True, "expandMessages": True},
    )
    payload2 = json.loads(result2)
    assert "echo" in payload2


def test_gate_is_applied_per_dispatch_not_per_construction(tmp_path: Path) -> None:
    """Calls to ``handle_tool_call`` between two state mutations see fresh state.

    A weaker form of the F5 regression: even WITHIN a single engine
    instance's lifetime, two back-to-back calls with different cache
    states see different gate decisions. If the gate were memoized
    across calls (a related antipattern), this test fails.
    """
    engine = _make_engine(tmp_path)
    engine.current_session_id = "sess"

    # Call 1: empty cache -> bypass -> echo.
    r1 = engine.handle_tool_call("lcm_describe", {"expandChildren": True, "expandMessages": True})
    assert "echo" in json.loads(r1)

    # Mutate to high context.
    token_state.record_llm_output(
        session_key="sess",
        usage={"input_tokens": 190_000},
        token_budget=200_000,
    )

    # Call 2: gate refuses.
    r2 = engine.handle_tool_call("lcm_describe", {"expandChildren": True, "expandMessages": True})
    assert json.loads(r2)["ok"] is False

    # Reset cache.
    token_state.note_successful_compact("sess")

    # Call 3: bypass again.
    r3 = engine.handle_tool_call("lcm_describe", {"expandChildren": True, "expandMessages": True})
    assert "echo" in json.loads(r3)


def test_exempt_tools_bypass_the_middleware(tmp_path: Path) -> None:
    """``lcm_expand`` and ``lcm_compact`` are NOT wrapped.

    Even when the cache is at refusal threshold, these tools call
    straight through to :meth:`_dispatch_tool_call`. The middleware
    wrap is keyed off :data:`TOKEN_GATE_TOOLS` membership.
    """
    engine = _make_engine(tmp_path)
    engine.current_session_id = "sess"
    token_state.record_llm_output(
        session_key="sess",
        usage={"input_tokens": 190_000},
        token_budget=200_000,
    )

    # ``lcm_compact`` is NOT in TOKEN_GATE_TOOLS.
    result_compact = engine.handle_tool_call("lcm_compact", {})
    payload_compact = json.loads(result_compact)
    assert "echo" in payload_compact, (
        "lcm_compact must bypass the middleware (it's not in TOKEN_GATE_TOOLS)"
    )

    # ``lcm_expand`` is NOT in TOKEN_GATE_TOOLS.
    result_expand = engine.handle_tool_call("lcm_expand", {})
    payload_expand = json.loads(result_expand)
    assert "echo" in payload_expand, (
        "lcm_expand must bypass the middleware (sub-agent grant ledger handles its budget)"
    )


def test_session_key_kwarg_overrides_current_session_id(tmp_path: Path) -> None:
    """Forward-compat: ``kwargs["session_key"]`` wins over the engine's session_id.

    When Hermes eventually surfaces ``session_key`` to ``handle_tool_call``
    (it doesn't today), the wrapper picks it up automatically. The
    forward-compat chain is
    ``kwargs.get("session_key") or session_id or self.current_session_id``.
    """
    engine = _make_engine(tmp_path)
    engine.current_session_id = "engine-default"
    # Stage cache under the kwargs-provided session-key (not the engine's).
    token_state.record_llm_output(
        session_key="explicit-kwarg",
        usage={"input_tokens": 190_000},
        token_budget=200_000,
    )

    # Same args, but pass session_key kwarg -> gate sees the high cache.
    result = engine.handle_tool_call(
        "lcm_describe",
        {"expandChildren": True, "expandMessages": True},
        session_key="explicit-kwarg",
    )
    assert json.loads(result)["ok"] is False

    # Without the kwarg, gate consults engine-default which has no anchor.
    result2 = engine.handle_tool_call(
        "lcm_describe",
        {"expandChildren": True, "expandMessages": True},
    )
    assert "echo" in json.loads(result2)


def test_gate_wrap_site_documents_wave12_f5() -> None:
    """The inline comment at the wrap site cites Wave-12 F5 + ADR-029.

    Per ADR-029 §"Known Wave-N fixes" Wave-12 row, the wrap site MUST
    carry an inline ``# LCM Wave-N (date): description`` comment that
    cites the original TS source path. This pins the convention so a
    future refactor that touches the wrap site is forced to confront
    the rationale.
    """
    import inspect

    source = inspect.getsource(LCMEngine.handle_tool_call)
    # Pin the inline comment substring.
    assert "Wave-12 F5" in source, (
        "Missing Wave-12 F5 inline comment at the wrap site. "
        "Per ADR-029, every Wave-N-load-bearing line carries an inline "
        "comment with the format `# LCM Wave-N (date): description`."
    )
    assert "middleware-not-decorator" in source, (
        "Missing 'middleware-not-decorator' rationale in the wrap-site "
        "comment. The comment must explain WHY the wrap is middleware "
        "(decorator would freeze state at registration)."
    )
    assert "needs-compact-gate.ts" in source, (
        "Missing original TS path citation in the wrap-site comment."
    )
