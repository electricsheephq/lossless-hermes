"""Tests for the engine-side tool-dispatch table (issue 06-02).

Covers the four acceptance criteria called out in
``epics/06-tools/06-02-tool-dispatch-table.md``:

1. :data:`TOOL_DISPATCH` is a module-level :class:`dict` (writable so
   per-tool ports can register at import time).
2. :data:`TOKEN_GATE_TOOLS` is a module-level :class:`frozenset` whose
   membership matches the porting-guide table (every tool EXCEPT
   ``lcm_expand`` and ``lcm_compact``; ``lcm_expand_query`` is in the
   set even though it ships in v0.2.0 per ADR-012).
3. :meth:`LCMEngine.get_tool_schemas` delegates to
   :func:`lossless_hermes.tools.get_tool_schemas` (stable ordering,
   fresh-list contract preserved).
4. :meth:`LCMEngine.handle_tool_call`:

   * dispatches to the registered handler;
   * returns the structured JSON-error string for unknown names;
   * resolves ``session_key`` from kwargs / ``current_session_id`` /
     fallback chain;
   * resolves ``runtime_ctx`` via :meth:`get_runtime_context`;
   * routes through :meth:`_run_with_token_gate` for tools in
     :data:`TOKEN_GATE_TOOLS` and bypasses for tools not in the set;
   * is sync (per ADR-017) — never returns a coroutine.

The "register a stub handler that echoes its args" pattern in the spec
is implemented via the ``stub_handler`` fixture: each test registers
the stub in :data:`TOOL_DISPATCH` for the duration of the test (with a
finally-block cleanup so registry state doesn't leak between tests).

Source: lossless-claw at commit ``1f07fbd``, branch ``pr-613``.
"""

from __future__ import annotations

import inspect
import json
from typing import Any, Callable, Iterator, List

import pytest

from lossless_hermes.engine import (
    TOKEN_GATE_TOOLS,
    TOOL_DISPATCH,
    LCMEngine,
    RuntimeContext,
)
from lossless_hermes.tools import TOOL_SCHEMAS, get_tool_schemas

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _register_handler(name: str, handler: Callable[..., str]) -> Iterator[None]:
    """Yield a context where ``TOOL_DISPATCH[name] = handler``.

    Test-only seam: per-tool issues (06-07..06-14) register at import
    time, but the unit tests register at call-time so each case can
    install its own stub and clean up. Implemented as a generator so
    the finally-block deregistration runs even when the test asserts.
    """
    sentinel = object()
    previous: Any = TOOL_DISPATCH.get(name, sentinel)
    TOOL_DISPATCH[name] = handler
    try:
        yield
    finally:
        if previous is sentinel:
            TOOL_DISPATCH.pop(name, None)
        else:
            TOOL_DISPATCH[name] = previous


@pytest.fixture
def stub_handler() -> Iterator[None]:
    """Register a generic echo handler under the name ``"stub"``.

    The stub returns ``json.dumps(args)`` so tests can assert on the
    parsed payload directly. Auto-deregisters after the test runs.
    """

    def _echo(args: dict[str, Any], **_: Any) -> str:
        return json.dumps(args)

    yield from _register_handler("stub", _echo)


# ---------------------------------------------------------------------------
# TOOL_DISPATCH — module-level shape
# ---------------------------------------------------------------------------


def test_tool_dispatch_is_dict() -> None:
    """AC: ``TOOL_DISPATCH: dict[str, Callable]`` at module scope.

    The dict must be writable so per-tool ports can register at
    import time (per porting guide ``tools.md`` lines 622–634).
    """
    assert isinstance(TOOL_DISPATCH, dict)


def test_tool_dispatch_holds_only_adr035_diagnostic_tools() -> None:
    """The registry holds exactly the two ADR-035 diagnostic tools.

    The seven ported ``lcm_*`` tools' dispatch wiring still has not
    landed (per-tool dispatch issues 06-07..06-14 register
    ``handle_lcm_<tool>`` — none has). The only entries are the two
    read-only model-callable diagnostic tools added by ADR-035 (issue
    #135): ``lcm_status`` and ``lcm_doctor``. Any other entry would
    mean a per-tool issue landed before its prerequisites — fail fast.
    """
    assert set(TOOL_DISPATCH) == {"lcm_status", "lcm_doctor"}, (
        f"TOOL_DISPATCH should hold exactly the two ADR-035 diagnostic "
        f"tools (lcm_status, lcm_doctor); the seven ported tools' "
        f"dispatch wiring lands in 06-07..06-14. Got {sorted(TOOL_DISPATCH)}"
    )


# ---------------------------------------------------------------------------
# TOKEN_GATE_TOOLS — module-level membership
# ---------------------------------------------------------------------------


def test_token_gate_tools_is_frozenset() -> None:
    """AC: ``TOKEN_GATE_TOOLS: set[str]`` at module scope.

    Implemented as :class:`frozenset` (immutable) — membership is
    pinned by the porting guide / the TS source, not a runtime
    decision. A mutable :class:`set` would let tests accidentally
    re-bind it.
    """
    assert isinstance(TOKEN_GATE_TOOLS, frozenset)


def test_token_gate_tools_membership() -> None:
    """AC: every tool EXCEPT ``lcm_expand`` and ``lcm_compact``.

    Per ``docs/porting-guides/tools.md`` lines 636–639. The set
    contains six entries — ``lcm_expand_query`` is in the gate set
    even though it ships in v0.2.0 per ADR-012 (the gate decision is
    about the tool's behavior, not its release status).
    """
    expected = {
        "lcm_grep",
        "lcm_describe",
        "lcm_expand_query",
        "lcm_synthesize_around",
        "lcm_get_entity",
        "lcm_search_entities",
    }
    assert TOKEN_GATE_TOOLS == expected


def test_token_gate_tools_excludes_lcm_expand_and_lcm_compact() -> None:
    """Sub-agent dispatcher + the conscious-compaction trade are exempt.

    Defensive: catches a future contributor adding ``lcm_expand`` or
    ``lcm_compact`` to the gate set (which would break the TS-parity
    contract documented in the porting guide).
    """
    assert "lcm_expand" not in TOKEN_GATE_TOOLS
    assert "lcm_compact" not in TOKEN_GATE_TOOLS


# ---------------------------------------------------------------------------
# get_tool_schemas — delegates to lossless_hermes.tools.get_tool_schemas
# ---------------------------------------------------------------------------


def test_get_tool_schemas_returns_a_list() -> None:
    """:meth:`LCMEngine.get_tool_schemas` returns a :class:`list`.

    Per the ABC contract and ``test_schemas_wellformed.py`` —
    callers iterate over the result, so a list is the locked shape.
    """
    engine = LCMEngine()
    schemas = engine.get_tool_schemas()
    assert isinstance(schemas, list)


def test_get_tool_schemas_delegates_to_registry() -> None:
    """:meth:`get_tool_schemas` returns the same content as the registry.

    AC: "delegates to ``tools.get_tool_schemas()``". Verified by
    comparing the two return values content-wise — list identity is
    NOT asserted because the registry returns a fresh list per call
    (the documented mutability-safety contract).
    """
    engine = LCMEngine()
    assert engine.get_tool_schemas() == get_tool_schemas()


def test_get_tool_schemas_returns_fresh_list() -> None:
    """Callers may mutate the returned list without affecting future calls.

    Per the registry doc (``tools/__init__.py:155``): "A FRESH list
    (so callers can mutate it freely without affecting the registry)."
    The engine method preserves that — appending to one call's return
    is not visible to a subsequent call.
    """
    engine = LCMEngine()
    first = engine.get_tool_schemas()
    first.append({"name": "should-not-appear", "description": "x", "parameters": {}})
    second = engine.get_tool_schemas()
    assert not any(s.get("name") == "should-not-appear" for s in second)


def test_get_tool_schemas_order_is_stable() -> None:
    """AC: "ordering is stable (tests rely on it)".

    Two back-to-back calls return the same sequence. Per-tool ports
    landing later only ever append; the per-tool order is the import
    order of the per-tool modules.
    """
    engine = LCMEngine()
    first = engine.get_tool_schemas()
    second = engine.get_tool_schemas()
    assert [s.get("name") for s in first] == [s.get("name") for s in second]


def test_get_tool_schemas_sees_registry_updates() -> None:
    """Appending to :data:`TOOL_SCHEMAS` is visible via the engine method.

    The registry pattern (06-01) lets per-tool modules
    ``TOOL_SCHEMAS.append(...)`` at import time — this test exercises
    the same seam to confirm the engine reflects the current state.
    """
    fake_schema = {
        "name": "lcm_test_inject",
        "description": "fixture-only fake",
        "parameters": {"type": "object", "properties": {}},
    }
    engine = LCMEngine()
    try:
        TOOL_SCHEMAS.append(fake_schema)
        names = [s.get("name") for s in engine.get_tool_schemas()]
        assert "lcm_test_inject" in names
    finally:
        TOOL_SCHEMAS.remove(fake_schema)


# ---------------------------------------------------------------------------
# handle_tool_call — dispatch table happy path
# ---------------------------------------------------------------------------


def test_handle_tool_call_dispatches_to_registered_handler(
    stub_handler: None,
) -> None:
    """AC: "register a stub handler that echoes its args; call
    ``handle_tool_call("stub", {"foo": 1})``; assert the returned
    string parses to ``{"foo": 1}``."

    Direct quote from the spec acceptance criteria.
    """
    engine = LCMEngine()
    result = engine.handle_tool_call("stub", {"foo": 1})
    assert json.loads(result) == {"foo": 1}


def test_handle_tool_call_returns_string(stub_handler: None) -> None:
    """The return type is :class:`str` — per ``handle_tool_call``'s
    ABC contract ("Hermes wraps caller-side failures in its own JSON
    envelope; the return value is the inner JSON-string").
    """
    engine = LCMEngine()
    result = engine.handle_tool_call("stub", {"x": 1})
    assert isinstance(result, str)


def test_handle_tool_call_is_sync(stub_handler: None) -> None:
    """Per ADR-017: the method is ``def``, not ``async def``.

    Inner-async paths bridge via the engine's background event loop.
    A coroutine return here would break Hermes's tool-dispatch loop
    (``run_agent.py:11249`` consumes the value synchronously).
    """
    engine = LCMEngine()
    result = engine.handle_tool_call("stub", {})
    assert not inspect.iscoroutine(result)


# ---------------------------------------------------------------------------
# handle_tool_call — unknown-name error path
# ---------------------------------------------------------------------------


def test_handle_tool_call_returns_json_error_for_unknown_name() -> None:
    """AC: "call ``handle_tool_call("does_not_exist", {})``; assert
    returned string is ``{"error": "Unknown LCM tool: does_not_exist"}``."

    Direct quote from the spec acceptance criteria.
    """
    engine = LCMEngine()
    result = engine.handle_tool_call("does_not_exist", {})
    assert result == json.dumps({"error": "Unknown LCM tool: does_not_exist"})
    # Sanity: the string parses to the documented error shape.
    assert json.loads(result) == {"error": "Unknown LCM tool: does_not_exist"}


def test_handle_tool_call_unknown_does_not_raise() -> None:
    """Per spec: "Returns a JSON string in every code path (no
    exceptions surfaced to the caller)."

    A raise would surface to Hermes's tool-dispatch loop as a 5xx
    rather than as a tool refusal. The JSON-encoded error is the
    canonical refusal shape.
    """
    engine = LCMEngine()
    # No try/except — a raise would fail the test directly.
    result = engine.handle_tool_call("does_not_exist", {})
    assert isinstance(result, str)


def test_handle_tool_call_unknown_name_includes_name_in_error() -> None:
    """The error message names the attempted tool for debuggability.

    Pinned by the spec quote: ``"Unknown LCM tool: {name}"``.
    """
    engine = LCMEngine()
    parsed = json.loads(engine.handle_tool_call("typo_in_name", {}))
    assert parsed == {"error": "Unknown LCM tool: typo_in_name"}


# ---------------------------------------------------------------------------
# handle_tool_call — session_key resolution
# ---------------------------------------------------------------------------


def test_session_key_resolved_from_explicit_kwarg() -> None:
    """``session_key`` kwarg wins over all fallbacks."""
    captured: dict[str, Any] = {}

    def capture(args: dict[str, Any], **kwargs: Any) -> str:
        captured.update(kwargs)
        return "{}"

    engine = LCMEngine()
    engine.current_session_id = "from-on-session-start"
    sentinel = object()
    previous = TOOL_DISPATCH.get("stub", sentinel)
    TOOL_DISPATCH["stub"] = capture
    try:
        engine.handle_tool_call(
            "stub",
            {},
            session_key="explicit",
            session_id="ignored",
            sender_id="also-ignored",
        )
    finally:
        if previous is sentinel:
            TOOL_DISPATCH.pop("stub", None)
        else:
            TOOL_DISPATCH["stub"] = previous

    assert captured["session_key"] == "explicit"


def test_session_key_resolved_from_session_id_kwarg() -> None:
    """``session_id`` is the Hermes-today key — second in the chain."""
    captured: dict[str, Any] = {}

    def capture(args: dict[str, Any], **kwargs: Any) -> str:
        captured.update(kwargs)
        return "{}"

    engine = LCMEngine()
    sentinel = object()
    previous = TOOL_DISPATCH.get("stub", sentinel)
    TOOL_DISPATCH["stub"] = capture
    try:
        engine.handle_tool_call("stub", {}, session_id="from-session-id")
    finally:
        if previous is sentinel:
            TOOL_DISPATCH.pop("stub", None)
        else:
            TOOL_DISPATCH["stub"] = previous

    assert captured["session_key"] == "from-session-id"


def test_session_key_resolved_from_sender_id_kwarg() -> None:
    """``sender_id`` is the forward-compat fallback."""
    captured: dict[str, Any] = {}

    def capture(args: dict[str, Any], **kwargs: Any) -> str:
        captured.update(kwargs)
        return "{}"

    engine = LCMEngine()
    sentinel = object()
    previous = TOOL_DISPATCH.get("stub", sentinel)
    TOOL_DISPATCH["stub"] = capture
    try:
        engine.handle_tool_call("stub", {}, sender_id="from-sender-id")
    finally:
        if previous is sentinel:
            TOOL_DISPATCH.pop("stub", None)
        else:
            TOOL_DISPATCH["stub"] = previous

    assert captured["session_key"] == "from-sender-id"


def test_session_key_falls_back_to_current_session_key() -> None:
    """:attr:`_current_session_key` is the last fallback (on_session_start)."""
    captured: dict[str, Any] = {}

    def capture(args: dict[str, Any], **kwargs: Any) -> str:
        captured.update(kwargs)
        return "{}"

    engine = LCMEngine()
    engine.current_session_id = "from-on-session-start"
    sentinel = object()
    previous = TOOL_DISPATCH.get("stub", sentinel)
    TOOL_DISPATCH["stub"] = capture
    try:
        engine.handle_tool_call("stub", {})
    finally:
        if previous is sentinel:
            TOOL_DISPATCH.pop("stub", None)
        else:
            TOOL_DISPATCH["stub"] = previous

    assert captured["session_key"] == "from-on-session-start"


def test_session_key_is_none_when_no_source() -> None:
    """All four sources absent → handler receives ``session_key=None``."""
    captured: dict[str, Any] = {}

    def capture(args: dict[str, Any], **kwargs: Any) -> str:
        captured.update(kwargs)
        return "{}"

    engine = LCMEngine()
    assert engine.current_session_id is None  # sanity
    sentinel = object()
    previous = TOOL_DISPATCH.get("stub", sentinel)
    TOOL_DISPATCH["stub"] = capture
    try:
        engine.handle_tool_call("stub", {})
    finally:
        if previous is sentinel:
            TOOL_DISPATCH.pop("stub", None)
        else:
            TOOL_DISPATCH["stub"] = previous

    assert captured["session_key"] is None


# ---------------------------------------------------------------------------
# handle_tool_call — runtime_ctx plumbing
# ---------------------------------------------------------------------------


def test_runtime_ctx_passed_to_handler(stub_handler: None) -> None:
    """The handler receives ``runtime_ctx`` as a kwarg."""
    captured: dict[str, Any] = {}

    def capture(args: dict[str, Any], **kwargs: Any) -> str:
        captured.update(kwargs)
        return "{}"

    engine = LCMEngine()
    sentinel = object()
    previous = TOOL_DISPATCH.get("captured_rt", sentinel)
    TOOL_DISPATCH["captured_rt"] = capture
    try:
        engine.handle_tool_call("captured_rt", {})
    finally:
        if previous is sentinel:
            TOOL_DISPATCH.pop("captured_rt", None)
        else:
            TOOL_DISPATCH["captured_rt"] = previous

    assert "runtime_ctx" in captured
    assert isinstance(captured["runtime_ctx"], RuntimeContext)


def test_runtime_ctx_none_when_no_token_state() -> None:
    """Pre-first-response: ``current_token_count`` and ``token_budget``
    both ``None`` (graceful gate degradation per the porting guide)."""
    engine = LCMEngine()
    ctx = engine.get_runtime_context("any-session")
    assert ctx.current_token_count is None
    assert ctx.token_budget is None


def test_runtime_ctx_reflects_engine_token_state() -> None:
    """After :meth:`update_from_response` + threshold set, the context
    snapshots the live state."""
    engine = LCMEngine()
    engine.update_from_response({"prompt_tokens": 1234})
    engine.threshold_tokens = 8000
    ctx = engine.get_runtime_context("any-session")
    assert ctx.current_token_count == 1234
    assert ctx.token_budget == 8000


# ---------------------------------------------------------------------------
# handle_tool_call — token-gate routing
# ---------------------------------------------------------------------------


def test_token_gate_routes_through_middleware_for_gated_tool() -> None:
    """A gated tool (``lcm_grep``) goes through :meth:`_run_with_token_gate`."""
    captured: dict[str, Any] = {}

    def capture(args: dict[str, Any], **kwargs: Any) -> str:
        captured.update(kwargs)
        return "{}"

    gate_calls: List[str] = []
    engine = LCMEngine()
    original_gate = engine._run_with_token_gate

    def spy_gate(**kw: Any) -> str:
        gate_calls.append(kw["name"])
        return original_gate(**kw)

    engine._run_with_token_gate = spy_gate  # type: ignore[assignment]

    sentinel = object()
    previous = TOOL_DISPATCH.get("lcm_grep", sentinel)
    TOOL_DISPATCH["lcm_grep"] = capture
    try:
        engine.handle_tool_call("lcm_grep", {"pattern": "x"})
    finally:
        if previous is sentinel:
            TOOL_DISPATCH.pop("lcm_grep", None)
        else:
            TOOL_DISPATCH["lcm_grep"] = previous

    assert gate_calls == ["lcm_grep"]
    # The handler still ran. Per ADR-035, _dispatch_tool_call also
    # passes the engine to the handler as ``ctx`` (the diagnostic tools
    # read the DB + session state off it); handlers that don't need it
    # absorb it via **kwargs.
    assert captured == {
        "runtime_ctx": captured.get("runtime_ctx"),
        "session_key": None,
        "ctx": engine,
    }


def test_token_gate_bypassed_for_non_gated_tool() -> None:
    """A non-gated tool (``lcm_expand``) bypasses the gate seam."""
    captured: dict[str, Any] = {}

    def capture(args: dict[str, Any], **kwargs: Any) -> str:
        captured.update(kwargs)
        return "{}"

    gate_calls: List[str] = []
    engine = LCMEngine()

    def spy_gate(**kw: Any) -> str:
        gate_calls.append(kw["name"])
        return ""

    engine._run_with_token_gate = spy_gate  # type: ignore[assignment]

    sentinel = object()
    previous = TOOL_DISPATCH.get("lcm_expand", sentinel)
    TOOL_DISPATCH["lcm_expand"] = capture
    try:
        engine.handle_tool_call("lcm_expand", {})
    finally:
        if previous is sentinel:
            TOOL_DISPATCH.pop("lcm_expand", None)
        else:
            TOOL_DISPATCH["lcm_expand"] = previous

    assert gate_calls == []  # gate NOT called
    # The handler was still invoked directly.
    assert "runtime_ctx" in captured


def test_token_gate_bypassed_for_lcm_compact() -> None:
    """``lcm_compact`` also bypasses (the deliberate "spend to free" trade)."""
    captured: dict[str, Any] = {}

    def capture(args: dict[str, Any], **kwargs: Any) -> str:
        captured.update(kwargs)
        return "{}"

    gate_calls: List[str] = []
    engine = LCMEngine()

    def spy_gate(**kw: Any) -> str:
        gate_calls.append(kw["name"])
        return ""

    engine._run_with_token_gate = spy_gate  # type: ignore[assignment]

    sentinel = object()
    previous = TOOL_DISPATCH.get("lcm_compact", sentinel)
    TOOL_DISPATCH["lcm_compact"] = capture
    try:
        engine.handle_tool_call("lcm_compact", {})
    finally:
        if previous is sentinel:
            TOOL_DISPATCH.pop("lcm_compact", None)
        else:
            TOOL_DISPATCH["lcm_compact"] = previous

    assert gate_calls == []


# ---------------------------------------------------------------------------
# handle_tool_call — kwargs forwarding
# ---------------------------------------------------------------------------


def test_orchestrator_kwargs_not_forwarded_to_handler() -> None:
    """``session_id`` / ``sender_id`` / ``session_key`` are consumed at
    the dispatch seam — handlers see ``session_key`` (the resolved
    value) but NOT the raw kwargs."""
    captured: dict[str, Any] = {}

    def capture(args: dict[str, Any], **kwargs: Any) -> str:
        captured.update(kwargs)
        return "{}"

    engine = LCMEngine()
    sentinel = object()
    previous = TOOL_DISPATCH.get("stub", sentinel)
    TOOL_DISPATCH["stub"] = capture
    try:
        engine.handle_tool_call(
            "stub",
            {},
            session_id="x",
            sender_id="y",
            session_key="z",
        )
    finally:
        if previous is sentinel:
            TOOL_DISPATCH.pop("stub", None)
        else:
            TOOL_DISPATCH["stub"] = previous

    # Resolved session_key passed through.
    assert captured["session_key"] == "z"
    # Raw kwargs NOT forwarded.
    assert "session_id" not in captured
    assert "sender_id" not in captured


def test_messages_kwarg_forwarded_to_handler() -> None:
    """``messages`` is consumed by the ingest prelude AND forwarded to
    the handler — the prelude is an additional read, not a strip."""
    captured: dict[str, Any] = {}

    def capture(args: dict[str, Any], **kwargs: Any) -> str:
        captured.update(kwargs)
        return "{}"

    engine = LCMEngine()
    sentinel = object()
    previous = TOOL_DISPATCH.get("stub", sentinel)
    TOOL_DISPATCH["stub"] = capture
    try:
        # No session_id → ingest prelude is a no-op, but messages still
        # forwards to the handler.
        engine.handle_tool_call(
            "stub",
            {},
            messages=[{"role": "user", "content": "hi"}],
        )
    finally:
        if previous is sentinel:
            TOOL_DISPATCH.pop("stub", None)
        else:
            TOOL_DISPATCH["stub"] = previous

    assert captured["messages"] == [{"role": "user", "content": "hi"}]


# ---------------------------------------------------------------------------
# RuntimeContext shape
# ---------------------------------------------------------------------------


def test_runtime_context_is_frozen() -> None:
    """:class:`RuntimeContext` is immutable so handlers can't mutate
    the snapshot. Frozen + slots per ADR convention for value records."""
    ctx = RuntimeContext(current_token_count=100, token_budget=8000)
    with pytest.raises((AttributeError, Exception)):
        # ``frozen=True`` raises FrozenInstanceError on assignment;
        # we widen the except to be defensive against future dataclass
        # changes that swap the error class.
        ctx.current_token_count = 200  # type: ignore[misc]


def test_runtime_context_default_values_are_none() -> None:
    """Default-constructed :class:`RuntimeContext` has both fields ``None``."""
    ctx = RuntimeContext()
    assert ctx.current_token_count is None
    assert ctx.token_budget is None
