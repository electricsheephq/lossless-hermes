"""Registry↔dispatch coverage regression test — the test that catches #156.

Issue [#156](https://github.com/electricsheephq/lossless-hermes/issues/156)
is the P0 where the seven ported ``lcm_*`` tools had ``TOOL_SCHEMAS``
entries (so the model *sees* them) but no ``TOOL_DISPATCH`` entries (so
they can never *run*). Every per-tool dispatch issue (Epic 06's 06-07..
06-14) was marked done, yet not one tool actually dispatched — because
the **dispatch-adapter layer** (per-tool typed-context construction +
``deps`` / collaborator wiring + the ``TOOL_DISPATCH`` registration) was
never built. Only ``lcm_status`` / ``lcm_doctor`` (PR #155, ADR-035)
carry a ``**_kwargs`` sink and dispatch today.

This file is the regression test that *would have caught* that gap, and
the ratchet that keeps it caught as the #156 four-PR sequence
(PR-0 → PR-3) wires the adapter layer incrementally.

What it asserts (parametrized over ``get_tool_schemas()``)
----------------------------------------------------------

For every advertised tool:

* **(a) coverage** — ``schema["name"]`` is a key in ``TOOL_DISPATCH``.
  An advertised tool with no dispatch entry is the #156 bug.
* **(b) no-unknown-tool-error** — ``LCMEngine().handle_tool_call(name,
  <minimal valid args>)`` on a real fixtured engine does NOT return
  ``{"error": "Unknown LCM tool: ..."}``. This is the *behavioural*
  twin of (a): (a) checks the registry, (b) checks the dispatch path
  end-to-end.
* **(c) no-exception-escape** — ``handle_tool_call`` returns a ``str``
  and never raises. This is PR-0's crash-hardening invariant (see
  below); it holds for **every** tool, so it is a hard pass, not a
  ratchet.

Plus a standalone ``test_lcm_expand_deferred`` pinning the ADR-037
deferral.

The xfail ratchet (assertions (a) and (b))
------------------------------------------

PR-0 (this file) wires NO adapters. The six not-yet-adapted ported
tools (``lcm_grep``, ``lcm_describe``, ``lcm_get_entity``,
``lcm_search_entities``, ``lcm_compact``, ``lcm_synthesize_around``)
therefore fail (a) and (b) — they are in ``TOOL_SCHEMAS`` but not in
``TOOL_DISPATCH``, and ``handle_tool_call`` returns the unknown-tool
error for them. Those two assertions are marked ``xfail(strict=True)``
for those six tools, so:

* **The suite is GREEN at every intermediate merge.** A known-failing
  assertion under ``xfail`` is a pass.
* **Each future adapter PR flips one tool.** When PR-1 wires
  ``lcm_grep``, its (a)/(b) assertions start *passing* — and a
  ``strict=True`` xfail that passes is an ``XPASS`` that FAILS the
  suite. That failure is the signal: the adapter landed, so remove
  ``lcm_grep`` from ``_NOT_YET_ADAPTED`` in the same PR. The ratchet
  cannot be left half-done.

``lcm_status`` and ``lcm_doctor`` already dispatch (PR #155) — they are
NOT in ``_NOT_YET_ADAPTED`` and must PASS (a)/(b)/(c) now.

Why assertion (c) is NOT in the ratchet
---------------------------------------

PR-0's deliverable (1) is crash-hardening in
``LCMEngine._dispatch_tool_call``: the ``handler(args, **kwargs)`` call
is wrapped so a ``TypeError`` (signature mismatch) or any handler
exception becomes a structured tool-error string instead of escaping to
Hermes's dispatch loop. After PR-0, ``handle_tool_call`` returns a
``str`` and never raises **for every tool** — including the six
un-adapted ones (which today take the ``handler is None`` unknown-tool
branch, itself exception-free). So (c) is a *universal invariant*
established by PR-0 itself, not a per-tool property that flips when an
adapter lands. Marking (c) as ``xfail(strict=True)`` for the six would
make it ``XPASS`` immediately and turn the suite RED — and would amount
to asserting that PR-0's crash-hardening does NOT work. (c) is therefore
a hard pass for all eight tools. This is a deliberate, documented
narrowing of the #156 scoping brief's "(a)/(b)/(c)" wording: the brief's
load-bearing constraints — "the suite is GREEN now" and "each adapter PR
flips its tool xfail→xpass" — are both honoured, and (c) genuinely is
not a per-tool ratchet.

References
----------

* Issue #156 — the P0 and its four-PR dispatch-adapter plan.
* ADR-037 (``docs/adr/037-lcm-expand-deferred.md``) — ``lcm_expand``
  deferral; ``LCM_EXPAND_SCHEMA`` removed from the registry.
* ``tests/test_tool_dispatch.py`` — the engine-fixture / dispatch
  unit-test pattern this file mirrors.
* ``src/lossless_hermes/engine/__init__.py::_dispatch_tool_call`` — the
  crash-hardening site (PR-0 deliverable 1).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterator

import pytest

from lossless_hermes.db.config import LcmConfig
from lossless_hermes.engine import TOOL_DISPATCH, LCMEngine
from lossless_hermes.tools import get_tool_schemas

# ---------------------------------------------------------------------------
# Skip marker — actions/setup-python macOS builds lack enable_load_extension
# ---------------------------------------------------------------------------
# Mirrors ``tests/test_handle_tool_call_ingest.py`` and
# ``tests/test_engine_ingest.py``: the (b)/(c) assertions run
# ``handle_tool_call`` on top of ``on_session_start``'s opened DB, which
# needs a full ``open_lcm_db`` connection (sqlite-vec loads via
# ``enable_load_extension``). Apple's system Python ships without
# ``--enable-loadable-sqlite-extensions``; the engine-fixture tests skip
# there. The pure-registry assertion (a) and ``test_lcm_expand_deferred``
# do NOT need the DB — they are split out so they always run.
_skip_no_extension_loading = pytest.mark.skipif(
    not hasattr(sqlite3.Connection, "enable_load_extension"),
    reason=(
        "actions/setup-python on macOS ships a CPython build without "
        "--enable-loadable-sqlite-extensions; sqlite-vec cannot load. The "
        "engine-fixtured dispatch assertions skip here (the registry-only "
        "assertion still runs)."
    ),
)


# ---------------------------------------------------------------------------
# The not-yet-adapted ratchet set
# ---------------------------------------------------------------------------
#
# The six ported ``lcm_*`` tools whose dispatch-adapter has NOT landed
# yet. PR-1..PR-3 of the #156 sequence remove tools from this set as
# their adapters ship:
#
#   * PR-1 → lcm_get_entity, lcm_search_entities, lcm_describe, lcm_grep
#   * PR-2 → lcm_compact
#   * PR-3 → lcm_synthesize_around
#
# When an adapter lands, its (a)/(b) ``xfail(strict=True)`` markers turn
# into ``XPASS`` and FAIL the suite — the signal to delete the tool from
# this set in the same PR. ``lcm_expand`` is NOT here: per ADR-037 it is
# deferred and absent from ``get_tool_schemas()`` entirely (see
# ``test_lcm_expand_deferred``).
_NOT_YET_ADAPTED: frozenset[str] = frozenset({
    "lcm_grep",
    "lcm_describe",
    "lcm_get_entity",
    "lcm_search_entities",
    "lcm_compact",
    "lcm_synthesize_around",
})


# Minimal schema-valid args per tool, for the (b)/(c) ``handle_tool_call``
# probes. The six un-adapted tools take the ``handler is None`` branch
# before args are ever inspected, so their args only need to be
# plausible; ``lcm_status`` / ``lcm_doctor`` have empty-parameter schemas
# (ADR-035) so ``{}`` is correct for them.
_MINIMAL_ARGS: dict[str, dict[str, Any]] = {
    "lcm_grep": {"pattern": "x"},
    "lcm_describe": {"id": "sum_1"},
    "lcm_get_entity": {"name": "x"},
    "lcm_search_entities": {},
    "lcm_compact": {},
    "lcm_synthesize_around": {"window_kind": "recent"},
    "lcm_status": {},
    "lcm_doctor": {},
}


def _all_tool_names() -> list[str]:
    """Sorted list of every advertised tool name (``get_tool_schemas()``).

    Sorted for a stable parametrization id order. Per ADR-037 this is
    the eight-tool surface — ``lcm_expand`` is deferred and absent.
    """
    return sorted(s["name"] for s in get_tool_schemas())


def _xfail_if_unadapted(name: str) -> Any:
    """Return a ``pytest.param`` for ``name``, xfail-marked if un-adapted.

    Assertions (a) and (b) are expected to FAIL for a tool still in
    :data:`_NOT_YET_ADAPTED` (no ``TOOL_DISPATCH`` entry → unknown-tool
    error). ``strict=True`` makes a landed adapter (assertion starts
    passing) an ``XPASS`` that fails the suite — the ratchet signal.
    """
    if name in _NOT_YET_ADAPTED:
        return pytest.param(
            name,
            marks=pytest.mark.xfail(
                strict=True,
                reason=(
                    f"{name}: dispatch-adapter not yet wired (#156 PR-1..PR-3). "
                    f"When the adapter lands this XPASSes — remove {name!r} "
                    f"from _NOT_YET_ADAPTED in the same PR."
                ),
            ),
        )
    return pytest.param(name)


_RATCHET_PARAMS: list[Any] = [_xfail_if_unadapted(n) for n in _all_tool_names()]


# ---------------------------------------------------------------------------
# Engine fixture — a real LCMEngine with on_session_start run
# ---------------------------------------------------------------------------


@pytest.fixture
def engine(tmp_home: Path) -> Iterator[LCMEngine]:
    """An :class:`LCMEngine` with ``on_session_start`` already run.

    Mirrors ``tests/test_handle_tool_call_ingest.py::engine`` — a real
    fixtured engine (opened DB, resolved session) so the (b)/(c)
    assertions exercise the genuine dispatch path, not a stub.
    """
    eng = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    eng.on_session_start("test-session")
    try:
        yield eng
    finally:
        eng.on_session_end("test-session", [])


# ---------------------------------------------------------------------------
# (a) coverage — every advertised tool has a TOOL_DISPATCH entry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _RATCHET_PARAMS)
def test_every_advertised_tool_has_a_dispatch_entry(name: str) -> None:
    """(a) Every ``get_tool_schemas()`` tool is a key in ``TOOL_DISPATCH``.

    This is the pure-registry check that pins the #156 invariant: a tool
    advertised to the model MUST have a dispatch handler. ``xfail`` for
    the six not-yet-adapted ported tools (the #156 sequence wires them
    in PR-1..PR-3); a hard pass for ``lcm_status`` / ``lcm_doctor``.
    """
    assert name in TOOL_DISPATCH, (
        f"Tool {name!r} is advertised in get_tool_schemas() but has no "
        f"TOOL_DISPATCH entry — this is the #156 bug. Wire its dispatch "
        f"adapter (see issue #156 PR-1..PR-3)."
    )


# ---------------------------------------------------------------------------
# (b) no-unknown-tool-error — handle_tool_call actually dispatches
# ---------------------------------------------------------------------------


@_skip_no_extension_loading
@pytest.mark.parametrize("name", _RATCHET_PARAMS)
def test_advertised_tool_does_not_return_unknown_tool_error(
    engine: LCMEngine,
    name: str,
) -> None:
    """(b) ``handle_tool_call`` does not return the unknown-tool error.

    The behavioural twin of (a): a real fixtured engine dispatches the
    tool by name and the result must NOT be
    ``{"error": "Unknown LCM tool: ..."}``. ``xfail`` for the six
    not-yet-adapted ported tools — they hit the ``handler is None``
    branch and return exactly that error.

    Note: a fresh fixtured engine has no LLM response yet, so the
    token-gate degrades to "skip the gate" (``current_token_count`` /
    ``token_budget`` are ``None``) — the result is the dispatch result,
    never a gate refusal. So the un-adapted tools reliably produce the
    unknown-tool error (a clean xfail), not an ambiguous refusal.
    """
    result = engine.handle_tool_call(name, _MINIMAL_ARGS[name])
    parsed = json.loads(result)
    error = parsed.get("error", "") if isinstance(parsed, dict) else ""
    assert not error.startswith("Unknown LCM tool:"), (
        f"handle_tool_call({name!r}) returned the unknown-tool error "
        f"{result!r} — the tool is advertised but does not dispatch (#156)."
    )


# ---------------------------------------------------------------------------
# (c) no-exception-escape — PR-0 crash-hardening invariant (hard pass)
# ---------------------------------------------------------------------------


@_skip_no_extension_loading
@pytest.mark.parametrize("name", _all_tool_names())
def test_handle_tool_call_returns_str_and_never_raises(
    engine: LCMEngine,
    name: str,
) -> None:
    """(c) ``handle_tool_call`` returns a ``str`` and never raises.

    PR-0's crash-hardening (``_dispatch_tool_call`` wraps the handler
    call in ``try/except``) makes this a UNIVERSAL invariant — true for
    every tool, including the six not-yet-adapted ones (which take the
    exception-free ``handler is None`` branch). So this is a HARD PASS,
    NOT part of the xfail ratchet — see the module docstring "Why
    assertion (c) is NOT in the ratchet".

    A regression here means either a handler escaped an exception
    (crash-hardening broke) or ``handle_tool_call`` returned a non-str —
    both surface to Hermes's dispatch loop as a 5xx-equivalent.
    """
    # No try/except — a raise fails the test directly, which is the point.
    result = engine.handle_tool_call(name, _MINIMAL_ARGS[name])
    assert isinstance(result, str), (
        f"handle_tool_call({name!r}) returned {type(result).__name__}, "
        f"not str — Hermes consumes the return value synchronously as a "
        f"JSON string."
    )
    # The string must be JSON (the tool-result contract). A non-JSON
    # return is as broken as a raise.
    json.loads(result)


def test_crash_hardening_converts_handler_exception_to_tool_error(
    engine: LCMEngine,
) -> None:
    """A handler that raises is converted to a structured tool-error.

    Directly exercises PR-0 deliverable (1): register a deliberately
    crashing handler, dispatch it, and assert the exception did NOT
    escape — it became a ``{"error": ...}`` JSON string. This is the
    invariant that makes the #156 incremental adapter rollout safe (an
    un-adapted / mis-wired tool degrades to "tool said no", not a turn
    crash).
    """

    def _boom(_args: dict[str, Any], **_kwargs: Any) -> str:
        raise RuntimeError("simulated handler crash")

    sentinel = object()
    previous: Any = TOOL_DISPATCH.get("lcm_status", sentinel)
    TOOL_DISPATCH["lcm_status"] = _boom
    try:
        result = engine.handle_tool_call("lcm_status", {})
    finally:
        if previous is sentinel:  # pragma: no cover — lcm_status is always present
            TOOL_DISPATCH.pop("lcm_status", None)
        else:
            TOOL_DISPATCH["lcm_status"] = previous

    assert isinstance(result, str)
    parsed = json.loads(result)
    assert isinstance(parsed, dict)
    assert "error" in parsed, (
        f"A crashing handler must yield a structured tool-error, got {result!r}"
    )
    assert "simulated handler crash" in parsed["error"], (
        "The structured error should carry the underlying exception text for debuggability."
    )


def test_crash_hardening_converts_typeerror_signature_mismatch(
    engine: LCMEngine,
) -> None:
    """A signature-mismatch ``TypeError`` is converted, not escaped.

    The #156 root cause: the ported handlers have strict keyword-only
    signatures with no ``**kwargs`` sink, so a naive
    ``TOOL_DISPATCH[name] = handle_lcm_x`` would ``TypeError`` on the
    first dispatch (``_dispatch_tool_call`` forwards ``runtime_ctx`` /
    ``ctx`` / ``session_key`` that the handler does not accept). This
    test registers a handler with exactly that too-strict shape and
    confirms PR-0's wrapper catches the ``TypeError`` — so the
    incremental adapter rollout cannot crash a turn.
    """

    def _too_strict(_args: dict[str, Any]) -> str:  # no **kwargs sink
        return "{}"  # pragma: no cover — never reached; the call TypeErrors

    sentinel = object()
    previous: Any = TOOL_DISPATCH.get("lcm_doctor", sentinel)
    TOOL_DISPATCH["lcm_doctor"] = _too_strict  # type: ignore[assignment]
    try:
        result = engine.handle_tool_call("lcm_doctor", {})
    finally:
        if previous is sentinel:  # pragma: no cover — lcm_doctor is always present
            TOOL_DISPATCH.pop("lcm_doctor", None)
        else:
            TOOL_DISPATCH["lcm_doctor"] = previous

    assert isinstance(result, str)
    parsed = json.loads(result)
    assert isinstance(parsed, dict) and "error" in parsed, (
        f"A signature-mismatch TypeError must become a structured tool-error, got {result!r}"
    )


# ---------------------------------------------------------------------------
# lcm_expand deferral — ADR-037
# ---------------------------------------------------------------------------


def test_lcm_expand_deferred() -> None:
    """``lcm_expand`` is deferred per ADR-037 — absent from both surfaces.

    ADR-037 (issue #156) defers ``lcm_expand`` to a post-v0.2.0 epic:
    its ``ExpansionOrchestrator`` / ``Retrieval`` collaborators are
    Protocol-only with no production implementation, and it is
    operationally dead without the ADR-012-deferred sub-agent delegation
    path. The deferral means it must appear in NEITHER the dispatch
    table NOR the advertised schema list — an unusable tool must not be
    advertised to the model (that is the #156 bug itself).

    Note: ``lcm_expand`` is intentionally NOT in :data:`_NOT_YET_ADAPTED`
    — that set is for tools awaiting an adapter (PR-1..PR-3). ``lcm_expand``
    is deferred *entirely*, so it is not parametrized into (a)/(b)/(c) at
    all; this standalone test pins its absence.
    """
    schema_names = {s["name"] for s in get_tool_schemas()}
    assert "lcm_expand" not in TOOL_DISPATCH, (
        "lcm_expand must NOT be in TOOL_DISPATCH — it is deferred per "
        "ADR-037 (docs/adr/037-lcm-expand-deferred.md)."
    )
    assert "lcm_expand" not in schema_names, (
        "lcm_expand must NOT be advertised in get_tool_schemas() — its "
        "schema registration was removed per ADR-037. Advertising an "
        "unusable tool is the #156 bug."
    )


def test_advertised_surface_is_the_expected_eight_tools() -> None:
    """``get_tool_schemas()`` advertises exactly the eight expected tools.

    Pins the post-ADR-037 surface: the six ported tools the #156
    sequence wires (``lcm_grep``, ``lcm_describe``, ``lcm_get_entity``,
    ``lcm_search_entities``, ``lcm_compact``, ``lcm_synthesize_around``)
    plus the two ADR-035 diagnostic tools (``lcm_status``,
    ``lcm_doctor``). ``lcm_expand`` (ADR-037) is excluded. A drift here
    means a tool was added or dropped without updating this test and
    ``_NOT_YET_ADAPTED``.
    """
    expected = {
        "lcm_grep",
        "lcm_describe",
        "lcm_get_entity",
        "lcm_search_entities",
        "lcm_compact",
        "lcm_synthesize_around",
        "lcm_status",
        "lcm_doctor",
    }
    actual = {s["name"] for s in get_tool_schemas()}
    assert actual == expected, (
        f"Advertised tool surface drifted. expected={sorted(expected)}, "
        f"actual={sorted(actual)}. If a tool was intentionally added or "
        f"removed, update this test AND _NOT_YET_ADAPTED."
    )
