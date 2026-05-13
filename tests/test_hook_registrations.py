"""Tests for the four Hermes hook registrations wired by issue 02-07.

Covers the additive surface introduced by 02-07: that
:func:`lossless_hermes.register` calls ``ctx.register_hook`` exactly
four times — once per documented Hermes hook the engine needs — and
that each registered callback accepts the documented kwargs shape per
``docs/reference/hermes-hooks.md`` without raising.

The 02-07 issue spec scope is the WIRING (the `register_hook` calls),
not the hook bodies — those are no-op stubs at 02-07 (Epic 03 fills
ingest / assemble; Epic 06 fills subagent_stop). These tests assert:

* Exactly four ``ctx.register_hook`` calls, one per documented hook.
* Each hook name matches a constant from
  ``hermes_cli.plugins.VALID_HOOKS`` (matched via the documented strings
  in ``docs/reference/hermes-hooks.md`` lines 322–334).
* Each registered callable is a bound method on the engine, NOT a
  closure — so Epic 03 / Epic 06 patches can fill the method body
  without re-registering.
* Each registered callable accepts its documented kwargs shape and
  returns without raising (the no-op contract for 02-07).
* The existing ``register_context_engine`` (00-06) and
  ``register_command`` (02-10) registrations still happen — 02-07
  must not regress 00-06 or 02-10.

See:

* ``docs/reference/hermes-hooks.md`` — VALID_HOOKS + per-hook kwargs.
* ``docs/adr/009-per-message-ingest.md`` — ``post_llm_call`` as the
  per-turn ingest seam.
* ``docs/adr/010-always-on-assembly-emulation.md`` — ``pre_llm_call``
  as the always-on assembly substitution seam.
* ``docs/adr/012-subagent-context-sharing.md`` — ``subagent_stop`` as
  v1 no-op (Epic 06 wires v2).
* ``docs/adr/014-recall-policy-injection.md`` — user-message-position
  policy injection (Epic 03 fills the ``pre_llm_call`` body).
* ``epics/02-engine-skeleton/02-07-hook-registrations.md`` — this
  issue's acceptance criteria.
"""

from __future__ import annotations

import logging
from typing import Any, Callable
from unittest.mock import MagicMock

import pytest

from lossless_hermes import register
from lossless_hermes.engine import LCMEngine

# ---------------------------------------------------------------------------
# The four hook names per docs/reference/hermes-hooks.md lines 322–334
# ("Where LCM hooks land" table). Single source of truth so any change
# here surfaces as test failures in this file (and conversely so this
# file's assertions can be kept in sync with the spec at glance).
# ---------------------------------------------------------------------------

_EXPECTED_HOOKS = frozenset({
    "post_llm_call",
    "pre_llm_call",
    "on_session_end",
    "subagent_stop",
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stub_ctx() -> MagicMock:
    """Build a Mock ``PluginContext`` with all the methods 02-07 calls.

    Per ``hermes_cli/plugins.py:287-665``, the real ``PluginContext``
    surface includes ``register_context_engine``, ``register_hook``,
    and ``register_command`` — every method 02-07's wiring exercises.
    The Mock surface lets us assert exactly what
    :func:`register` does even in a Hermes-less env where the real
    ``PluginContext`` isn't importable.
    """
    ctx = MagicMock()
    ctx.register_context_engine = MagicMock()
    ctx.register_hook = MagicMock()
    ctx.register_command = MagicMock()
    return ctx


@pytest.fixture
def hermes_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``HERMES_AVAILABLE = True`` so :func:`register` bypasses the
    health-check guard.

    Mirrors the fixture in ``tests/test_register.py``. Patches both the
    bridge module's flag AND the rebound name on ``lossless_hermes``
    itself (the function reads its module-level import of the flag),
    AND patches ``get_hermes_home`` since the real function raises in
    the Hermes-less env.
    """
    import lossless_hermes
    import lossless_hermes.hermes_bridge as bridge

    monkeypatch.setattr(bridge, "HERMES_AVAILABLE", True)
    monkeypatch.setattr(bridge, "get_hermes_home", lambda: "/tmp/.hermes-test")
    monkeypatch.setattr(lossless_hermes, "HERMES_AVAILABLE", True)


def _hooks_registered(ctx: MagicMock) -> dict[str, Callable[..., Any]]:
    """Return ``{hook_name: callback}`` map from a Mock context's calls."""
    return {call.args[0]: call.args[1] for call in ctx.register_hook.call_args_list}


# ---------------------------------------------------------------------------
# register() wires exactly the four documented hooks
# ---------------------------------------------------------------------------


def test_register_calls_register_hook_exactly_four_times(
    hermes_available: None,
) -> None:
    """02-07 AC: ``register_hook`` is called once for each of the four
    hooks listed in ``docs/reference/hermes-hooks.md`` "Where LCM hooks
    land" table (lines 322–334)."""
    ctx = _make_stub_ctx()
    register(ctx)
    assert ctx.register_hook.call_count == 4, (
        f"expected 4 register_hook calls, got {ctx.register_hook.call_count}: "
        f"{ctx.register_hook.call_args_list}"
    )


def test_register_hook_names_match_documented_set(hermes_available: None) -> None:
    """02-07 AC: the four hook names match ``docs/reference/hermes-hooks.md``
    "Where LCM hooks land" exactly — ``post_llm_call``, ``pre_llm_call``,
    ``on_session_end``, ``subagent_stop``."""
    ctx = _make_stub_ctx()
    register(ctx)
    hook_names = {call.args[0] for call in ctx.register_hook.call_args_list}
    assert hook_names == _EXPECTED_HOOKS, (
        f"hook name set mismatch: registered={hook_names}, expected={_EXPECTED_HOOKS}"
    )


def test_register_hook_callbacks_are_engine_bound_methods(
    hermes_available: None,
) -> None:
    """02-07 design invariant: each registered callback is a bound method
    on the engine instance, NOT a closure created inside :func:`register`.

    Per ADR-001 §Invariant "the package's top-level
    ``lossless_hermes:register`` callable must remain stable across
    versions" — wiring the hook handlers as bound methods means Epic 03
    / Epic 06 patches only have to fill the method bodies, not edit
    :func:`register` and re-register. The handle stays stable across
    versions of the engine.
    """
    ctx = _make_stub_ctx()
    register(ctx)
    # The engine instance registered at register_context_engine — the
    # same instance should own the four hook methods.
    (engine,) = ctx.register_context_engine.call_args.args
    assert isinstance(engine, LCMEngine)

    hooks = _hooks_registered(ctx)
    assert hooks["post_llm_call"].__self__ is engine
    assert hooks["pre_llm_call"].__self__ is engine
    assert hooks["on_session_end"].__self__ is engine
    assert hooks["subagent_stop"].__self__ is engine

    # And the underlying functions are the engine's documented hook
    # handler methods (NOT slash-command handlers or other coincident
    # methods).
    assert hooks["post_llm_call"].__func__ is type(engine)._on_post_llm_call
    assert hooks["pre_llm_call"].__func__ is type(engine)._on_pre_llm_call
    assert hooks["on_session_end"].__func__ is type(engine)._on_session_end_hook
    assert hooks["subagent_stop"].__func__ is type(engine)._on_subagent_stop


# ---------------------------------------------------------------------------
# Each registered callback accepts its documented kwargs shape (no raise)
# ---------------------------------------------------------------------------


def test_post_llm_call_accepts_documented_kwargs(hermes_available: None) -> None:
    """02-07 AC: the registered ``post_llm_call`` callback accepts the
    kwargs documented in ``docs/reference/hermes-hooks.md`` line 92
    (``session_id``, ``user_message``, ``assistant_response``,
    ``conversation_history``, ``model``, ``platform``) without raising.

    The body is a no-op at 02-07 — Epic 03 fills the real diff-and-
    ingest. This test is the "Hermes can wire it" contract.
    """
    ctx = _make_stub_ctx()
    register(ctx)
    hook = _hooks_registered(ctx)["post_llm_call"]

    # Sync call: Hermes's ``invoke_hook`` does ``ret = cb(**kwargs)``
    # without ``await`` (``hermes_cli/plugins.py:1218-1232``). Calling
    # the hook directly mirrors how Hermes will invoke it.
    result = hook(
        session_id="sess-1",
        user_message="hello",
        assistant_response="hi there",
        conversation_history=[{"role": "user", "content": "hello"}],
        model="claude-haiku",
        platform="anthropic",
    )

    # Observer-only hook — return is ignored by Hermes, but we still
    # confirm no exception fired and the return is ``None`` (no-op stub).
    assert result is None


def test_post_llm_call_tolerates_extra_kwargs(hermes_available: None) -> None:
    """Forward-compat: the ``post_llm_call`` callback accepts ``**kwargs``
    so future Hermes additions don't break the plugin (mirrors the
    pattern in ``hermes_cli/plugins.py:1218-1232`` of dispatching with
    arbitrary kwargs)."""
    ctx = _make_stub_ctx()
    register(ctx)
    hook = _hooks_registered(ctx)["post_llm_call"]

    result = hook(
        session_id="sess-1",
        user_message="hello",
        assistant_response="hi",
        conversation_history=[],
        model="claude-haiku",
        platform="anthropic",
        # Future-only kwargs that 02-07 must tolerate
        future_field_added_in_hermes_v999="ignored",
        another_thing=42,
    )
    assert result is None


def test_pre_llm_call_accepts_documented_kwargs(hermes_available: None) -> None:
    """02-07 AC: the registered ``pre_llm_call`` callback accepts the
    kwargs documented in ``docs/reference/hermes-hooks.md`` line 91
    (``session_id``, ``user_message``, ``conversation_history``,
    ``is_first_turn``, ``model``, ``platform``, ``sender_id``) without
    raising. Issue 03-10 fills the body — the hook returns
    ``{"context": LOSSLESS_RECALL_POLICY_PROMPT}`` per ADR-014. The
    ``test_pre_llm_call*.py`` files exercise the injected text in
    depth; here we only assert the wired shape (Hermes can call it
    with the documented kwargs and receives a ``context``-bearing
    dict).
    """
    from lossless_hermes.recall_policy import LOSSLESS_RECALL_POLICY_PROMPT

    ctx = _make_stub_ctx()
    register(ctx)
    hook = _hooks_registered(ctx)["pre_llm_call"]

    result = hook(
        session_id="sess-1",
        user_message="hello",
        conversation_history=[],
        is_first_turn=True,
        model="claude-haiku",
        platform="anthropic",
        sender_id="",
    )
    # Issue 03-10: the hook returns the recall-policy text per ADR-014.
    # Hermes appends ``result["context"]`` to the current turn's user
    # message at API-call time.
    assert isinstance(result, dict)
    assert result["context"] == LOSSLESS_RECALL_POLICY_PROMPT


def test_on_session_end_accepts_interrupted_kwarg(
    hermes_available: None, caplog: pytest.LogCaptureFixture
) -> None:
    """02-07 AC: the registered ``on_session_end`` callback accepts the
    PLUGIN-HOOK kwargs (``session_id``, ``completed``, ``interrupted``,
    ``model``, ``platform``) — distinct from the ABC
    :meth:`on_session_end` signature (which takes ``messages``). When
    ``interrupted=True``, a debug-level breadcrumb fires so Epic 03's
    tail-ingest path has a clear log signal to wire against.
    """
    ctx = _make_stub_ctx()
    register(ctx)
    hook = _hooks_registered(ctx)["on_session_end"]

    with caplog.at_level(logging.DEBUG, logger="lossless_hermes.engine.lifecycle"):
        result = hook(
            session_id="sess-1",
            completed=False,
            interrupted=True,
            model="claude-haiku",
            platform="anthropic",
        )

    # Observer-only — Hermes ignores returns. Confirm no raise + no
    # truthy return that could confuse a future caller.
    assert result is None
    # The interrupted-path log line is the contract Epic 03's tail-
    # ingest will read; confirm it fires.
    assert any("interrupted=True" in rec.getMessage() for rec in caplog.records), (
        f"expected interrupted breadcrumb, got: {[r.getMessage() for r in caplog.records]}"
    )


def test_on_session_end_completed_path_does_not_log_interrupted(
    hermes_available: None, caplog: pytest.LogCaptureFixture
) -> None:
    """Negative pairing: when ``interrupted=False`` (the happy path), the
    interrupted-only breadcrumb MUST NOT fire — otherwise Epic 03's
    tail-ingest would mis-trigger on completed turns and re-ingest
    messages ``post_llm_call`` already handled."""
    ctx = _make_stub_ctx()
    register(ctx)
    hook = _hooks_registered(ctx)["on_session_end"]

    with caplog.at_level(logging.DEBUG, logger="lossless_hermes.engine.lifecycle"):
        hook(
            session_id="sess-1",
            completed=True,
            interrupted=False,
            model="claude-haiku",
            platform="anthropic",
        )

    assert not any("interrupted=True" in rec.getMessage() for rec in caplog.records), (
        "completed-path must not emit the interrupted breadcrumb"
    )


def test_subagent_stop_accepts_documented_kwargs(
    hermes_available: None,
) -> None:
    """02-07 AC: the registered ``subagent_stop`` callback accepts the
    kwargs documented in ``docs/reference/hermes-hooks.md`` line 99
    (``parent_session_id``, ``child_role``, ``child_summary``,
    ``child_status``, ``duration_ms``) without raising. v1 no-op per
    ADR-012; Epic 06 wires subagent context-sharing.
    """
    ctx = _make_stub_ctx()
    register(ctx)
    hook = _hooks_registered(ctx)["subagent_stop"]

    result = hook(
        parent_session_id="parent-1",
        child_role={"name": "research"},  # Any per hermes-hooks.md
        child_summary="ran 3 web searches, found 2 relevant docs",
        child_status="completed",
        duration_ms=4523,
    )

    # v1 no-op — return is ``None`` and no exception fired.
    assert result is None


# ---------------------------------------------------------------------------
# 02-07 must not regress 00-06 / 02-10 — the engine + slash-command
# registrations still happen.
# ---------------------------------------------------------------------------


def test_register_context_engine_still_called_at_02_07(
    hermes_available: None,
) -> None:
    """02-07 must not regress 00-06: ``register_context_engine`` is still
    called exactly once with an :class:`LCMEngine` instance."""
    ctx = _make_stub_ctx()
    register(ctx)
    ctx.register_context_engine.assert_called_once()
    (engine,) = ctx.register_context_engine.call_args.args
    assert isinstance(engine, LCMEngine)
    assert engine.name == "lcm"


def test_register_command_for_lcm_still_called_at_02_07(
    hermes_available: None,
) -> None:
    """02-07 must not regress 02-10: ``register_command`` for ``/lcm`` is
    still called exactly once with the dispatcher handler."""
    ctx = _make_stub_ctx()
    register(ctx)
    ctx.register_command.assert_called_once()
    call = ctx.register_command.call_args
    assert call.args[0] == "lcm"
    assert callable(call.args[1])
    assert call.kwargs.get("args_hint") == "<subcommand>"


# ---------------------------------------------------------------------------
# Failure-mode coverage: Hermes-missing health check still fires
# ---------------------------------------------------------------------------


def test_hook_registration_blocked_when_hermes_missing() -> None:
    """When ``HERMES_AVAILABLE`` is False (the default test env), the
    Hermes-missing guard fires BEFORE any hook registration — so the
    Mock context's ``register_hook`` must not have been touched.

    Mirrors ``test_register.py::test_register_raises_when_hermes_missing``
    for the hook surface. Registration is all-or-nothing — partially-
    registered hooks would leave the agent in an unrecoverable state.
    """
    from lossless_hermes.hermes_bridge import LosslessHermesEnvironmentError

    ctx = _make_stub_ctx()
    with pytest.raises(LosslessHermesEnvironmentError, match=r"hermes-agent"):
        register(ctx)
    ctx.register_hook.assert_not_called()
    ctx.register_context_engine.assert_not_called()
    ctx.register_command.assert_not_called()
