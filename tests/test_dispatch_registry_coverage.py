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
the ratchet that kept it caught as the #156 four-PR sequence
(PR-0 → PR-3) plus #164 PR-2 (which finished the 8th adapter) wired the
adapter layer incrementally. As of #164 PR-2 the ratchet is fully
discharged — :data:`_NOT_YET_ADAPTED` is empty and dispatch coverage is
8/8.

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

The xfail ratchet (assertions (a) and (b)) — now fully discharged
-----------------------------------------------------------------

The ratchet worked as designed across the rollout. PR-0 wired no
adapters, so the six not-yet-adapted ported tools (``lcm_grep``,
``lcm_describe``, ``lcm_get_entity``, ``lcm_search_entities``,
``lcm_compact``, ``lcm_synthesize_around``) failed (a) and (b) — in
``TOOL_SCHEMAS`` but not ``TOOL_DISPATCH`` — and were marked
``xfail(strict=True)``. As each adapter landed, its (a)/(b) assertions
started passing, the ``strict=True`` xfail became an ``XPASS`` that
failed the suite, and the same PR removed that tool from
:data:`_NOT_YET_ADAPTED`:

* **The suite stayed GREEN at every intermediate merge.** A known-
  failing assertion under ``xfail`` is a pass.
* **#156 PR-1** flipped ``lcm_get_entity`` / ``lcm_search_entities`` /
  ``lcm_describe`` / ``lcm_grep``; **#156 PR-2** flipped ``lcm_compact``;
  **#164 PR-2** flipped the 8th tool ``lcm_synthesize_around`` (deferred
  from #156 PR-3 — its ``build_llm_call`` factory needed a summarizer
  surface ``LCMEngine`` did not expose).

As of #164 PR-2, :data:`_NOT_YET_ADAPTED` is **empty**: all eight
advertised tools dispatch, so (a)/(b) are hard PASSes for every tool and
#156 is closed. A tool re-appearing in the set would mean the #156 bug
regressed.

``lcm_status`` and ``lcm_doctor`` dispatch via PR #155 — they were never
in ``_NOT_YET_ADAPTED`` and PASS (a)/(b)/(c).

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
# ``tests/test_engine_ingest.py``: assertions (b) and (c) run
# ``handle_tool_call`` on the ``engine`` fixture, which calls
# ``on_session_start`` and so needs a full ``open_lcm_db`` connection
# (sqlite-vec loads via ``enable_load_extension``). Apple's system Python
# ships without ``--enable-loadable-sqlite-extensions``, and the engine
# hard-raises a ``RuntimeError`` at construction on such a build — so the
# ``engine``-fixtured tests skip there.
#
# Tests that do NOT use the ``engine`` fixture run on every platform:
#   * assertion (a) — a pure ``TOOL_DISPATCH`` registry check.
#   * ``test_lcm_expand_deferred`` / ``test_advertised_surface_*`` —
#     pure ``get_tool_schemas()`` / registry checks.
#   * ``test_crash_hardening_*`` — use a **bare** ``LCMEngine()`` (no
#     ``on_session_start``); the crash-hardening seam in
#     ``_dispatch_tool_call`` is DB-independent.
_skip_no_extension_loading = pytest.mark.skipif(
    not hasattr(sqlite3.Connection, "enable_load_extension"),
    reason=(
        "actions/setup-python on macOS ships a CPython build without "
        "--enable-loadable-sqlite-extensions; sqlite-vec cannot load and "
        "the engine hard-raises at construction. The engine-fixtured "
        "dispatch assertions skip here (the registry-only and bare-engine "
        "tests still run)."
    ),
)


# ---------------------------------------------------------------------------
# The not-yet-adapted ratchet set — now EMPTY (dispatch coverage is 8/8)
# ---------------------------------------------------------------------------
#
# The ported ``lcm_*`` tools whose dispatch-adapter has not landed yet.
# The #156 sequence (and #164 PR-2, which finished it) removed tools
# from this set as their adapters shipped:
#
#   * #156 PR-1 → lcm_get_entity, lcm_search_entities, lcm_describe,
#     lcm_grep
#   * #156 PR-2 → lcm_compact
#   * #164 PR-2 → lcm_synthesize_around — the 8th tool. It was deferred
#     from #156 PR-3 because its ``build_llm_call`` factory needs a
#     summarizer surface ``LCMEngine`` did not expose; #164 PR-2 built
#     that surface and wired the adapter.
#
# This set is now **empty**: every advertised tool has a ``TOOL_DISPATCH``
# entry, so the (a)/(b) assertions are hard PASSes for all eight tools
# and #156 is closed. ``lcm_expand`` is NOT — and never was — in this
# set: per ADR-037 it is deferred and absent from ``get_tool_schemas()``
# entirely (see ``test_lcm_expand_deferred``), so it is not part of the
# advertised surface this ratchet covers.
#
# A non-empty set here again would mean a new ported tool was advertised
# without its adapter — the #156 bug regressing.
_NOT_YET_ADAPTED: frozenset[str] = frozenset()


# Minimal schema-valid args per tool, for the (b)/(c) ``handle_tool_call``
# probes. All eight tools now dispatch, so each tool's args reach its
# handler — they must therefore be plausible enough that the handler
# takes a *structured-error* path (e.g. "no conversation found", "missing
# prompt") rather than the unknown-tool path. ``lcm_synthesize_around``
# gets a valid ``window_kind="period"``: on the fresh fixtured engine
# (no conversation, no summary-model config) the handler returns a
# structured "No LCM conversation found" error — a clean (b) PASS (not
# the unknown-tool error) and a clean (c) PASS (a JSON string). ``"recent"``
# would be an invalid window_kind — still a clean structured error, but
# a valid mode keeps the probe honest. ``lcm_status`` / ``lcm_doctor``
# have empty-parameter schemas (ADR-035) so ``{}`` is correct for them.
_MINIMAL_ARGS: dict[str, dict[str, Any]] = {
    "lcm_grep": {"pattern": "x"},
    "lcm_describe": {"id": "sum_1"},
    "lcm_get_entity": {"name": "x"},
    "lcm_search_entities": {},
    "lcm_compact": {},
    "lcm_synthesize_around": {"window_kind": "period", "period": "yesterday"},
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

    :data:`_NOT_YET_ADAPTED` is now empty (#156 closed by #164 PR-2), so
    in practice every tool gets a plain :func:`pytest.param`. The
    machinery is retained as a regression guard: if a future ported tool
    is advertised in ``get_tool_schemas()`` before its adapter lands,
    adding it to :data:`_NOT_YET_ADAPTED` re-arms the ``strict=True``
    xfail ratchet — a landed adapter then ``XPASS``-fails the suite,
    forcing the set back to empty.
    """
    if name in _NOT_YET_ADAPTED:
        return pytest.param(
            name,
            marks=pytest.mark.xfail(
                strict=True,
                reason=(
                    f"{name}: dispatch-adapter not yet wired. When the "
                    f"adapter lands this XPASSes — remove {name!r} from "
                    f"_NOT_YET_ADAPTED in the same PR."
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
    advertised to the model MUST have a dispatch handler. With #156
    closed (#164 PR-2 wired the 8th adapter), this is a hard PASS for
    all eight advertised tools — :data:`_NOT_YET_ADAPTED` is empty so no
    parameter is xfail-marked.
    """
    assert name in TOOL_DISPATCH, (
        f"Tool {name!r} is advertised in get_tool_schemas() but has no "
        f"TOOL_DISPATCH entry — this is the #156 bug. Wire its dispatch "
        f"adapter in tools/_adapters.py."
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
    ``{"error": "Unknown LCM tool: ..."}``. With #156 closed this is a
    hard PASS for all eight tools — each dispatches to its adapter,
    which (on the fresh fixtured engine) returns a structured
    handler-level error, not the unknown-tool error.

    Note: a fresh fixtured engine has no LLM response yet, so the
    token-gate degrades to "skip the gate" (``current_token_count`` /
    ``token_budget`` are ``None``) — the result is the dispatch result,
    never a gate refusal.
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


def test_crash_hardening_converts_handler_exception_to_tool_error() -> None:
    """A handler that raises is converted to a structured tool-error.

    Directly exercises PR-0 deliverable (1): register a deliberately
    crashing handler, dispatch it, and assert the exception did NOT
    escape — it became a ``{"error": ...}`` JSON string. This is the
    invariant that makes the #156 incremental adapter rollout safe (an
    un-adapted / mis-wired tool degrades to "tool said no", not a turn
    crash).

    Uses a **bare** ``LCMEngine()`` (no ``on_session_start``) — the
    crash-hardening lives in ``_dispatch_tool_call`` and never touches
    the DB, so this test needs no opened-DB fixture and therefore runs
    on every platform (it does NOT carry ``_skip_no_extension_loading``).
    Mirrors ``tests/test_tool_dispatch.py``'s bare-engine pattern.
    """

    def _boom(_args: dict[str, Any], **_kwargs: Any) -> str:
        raise RuntimeError("simulated handler crash")

    engine = LCMEngine()
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


def test_crash_hardening_converts_typeerror_signature_mismatch() -> None:
    """A signature-mismatch ``TypeError`` is converted, not escaped.

    The #156 root cause: the ported handlers have strict keyword-only
    signatures with no ``**kwargs`` sink, so a naive
    ``TOOL_DISPATCH[name] = handle_lcm_x`` would ``TypeError`` on the
    first dispatch (``_dispatch_tool_call`` forwards ``runtime_ctx`` /
    ``ctx`` / ``session_key`` that the handler does not accept). This
    test registers a handler with exactly that too-strict shape and
    confirms PR-0's wrapper catches the ``TypeError`` — so the
    incremental adapter rollout cannot crash a turn.

    Uses a **bare** ``LCMEngine()`` (no ``on_session_start``) for the
    same reason as the sibling test above — the crash-hardening seam is
    DB-independent, so this runs on every platform.
    """

    def _too_strict(_args: dict[str, Any]) -> str:  # no **kwargs sink
        return "{}"  # pragma: no cover — never reached; the call TypeErrors

    engine = LCMEngine()
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
    """The advertised surface AND ``TOOL_DISPATCH`` are exactly the eight tools.

    Pins the post-ADR-037, post-#156 surface: the six ported tools
    (``lcm_grep``, ``lcm_describe``, ``lcm_get_entity``,
    ``lcm_search_entities``, ``lcm_compact``, ``lcm_synthesize_around``)
    plus the two ADR-035 diagnostic tools (``lcm_status``,
    ``lcm_doctor``). ``lcm_expand`` (ADR-037) is excluded.

    With #156 closed by #164 PR-2, this asserts the **dispatch surface
    equals the advertised surface** — ``set(TOOL_DISPATCH)`` is exactly
    ``{name for s in get_tool_schemas()}``, i.e. 8/8 coverage with no
    advertised-but-undispatchable tool and no dispatchable-but-unadvertised
    tool. That set-equality is the strongest form of the #156 invariant.
    A drift in either set means a tool was added or dropped without
    updating this test (and, for a new ported tool, ``_NOT_YET_ADAPTED``).
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
    # #156 closed: dispatch coverage is total. Every advertised tool
    # dispatches, and nothing dispatches that is not advertised.
    assert set(TOOL_DISPATCH) == expected, (
        f"TOOL_DISPATCH drifted from the advertised surface. "
        f"expected={sorted(expected)}, actual={sorted(TOOL_DISPATCH)}. "
        f"#156 requires dispatch coverage to equal the advertised "
        f"surface exactly (8/8)."
    )
