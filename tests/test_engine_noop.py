"""Tests for the v0 no-op :class:`LCMEngine` and its passthrough behavior.

Covers every acceptance-criterion in
``epics/00-scaffolding/issues/00-06-noop-engine.md`` that targets the
engine class itself (the ``register()`` wiring is covered in
``tests/test_register.py``).

Specifically:

* ``LCMEngine.name == "lcm"`` (string equality, ADR-001 contract).
* ``LCMEngine(hermes_home=tmp_path, config=LcmConfig())`` constructs
  without DB open / migrations.
* ``compress()`` is a round-trip identity over a variety of message
  shapes (AC item: "round-trip property test for empty list, single
  message, multi-turn, multi-modal content blocks").
* ``should_compress()`` returns ``False`` unconditionally.
* ``update_from_response()`` updates the standard ``last_*_tokens``
  fields without raising.
* Lifecycle stubs (``on_session_start``, ``on_session_end``,
  ``on_session_reset``) raise :class:`NotImplementedError` with a
  message naming Epic 02.
* ``get_tool_schemas()`` returns ``[]``.
* ``handle_tool_call()`` raises :class:`NotImplementedError` naming
  Epic 06.
* Apple system Python guard fires when ``enable_load_extension`` is
  monkey-patched off ``sqlite3.Connection``.

The Hermes-less environment is the default test env (per ADR-007 §
Decision — Hermes is host-installed, not pinned). The engine must
instantiate via the bridge's stub ``ContextEngine`` class. See
``tests/test_hermes_bridge.py`` for the bridge's import-time fallback.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from lossless_hermes.db.config import LcmConfig
from lossless_hermes.engine import APPLE_SYSTEM_PYTHON_MSG, LCMEngine

# ---------------------------------------------------------------------------
# Skip marker: actions/setup-python macOS builds lack enable_load_extension
# ---------------------------------------------------------------------------
#
# Mirrors ``_skip_no_extension_loading`` in ``tests/test_db_connection.py``
# (ADR-004 §Open questions item 1, ADR-028 §Decision point 8). The
# ``on_session_start`` lifecycle body filled in by issue 02-03 opens an
# ``open_lcm_db()`` connection that loads sqlite-vec, which is impossible
# on the actions/setup-python macOS pre-built CPython. The
# Apple-Python-guard tests below remain runnable on those cells because
# they monkey-patch ``_has_sqlite_extension_loading`` rather than
# depending on the OS-level capability.
_skip_no_extension_loading = pytest.mark.skipif(
    not hasattr(sqlite3.Connection, "enable_load_extension"),
    reason=(
        "actions/setup-python on macOS ships a CPython build without "
        "--enable-loadable-sqlite-extensions; sqlite-vec cannot load. "
        "Apple-Python-guard tests still run (they monkey-patch the "
        "introspection hook). See ADR-004 §Open questions item 1 + "
        "ADR-028 §Decision point 8."
    ),
)


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def test_name_is_lcm() -> None:
    """AC: ``LCMEngine.name == "lcm"`` — matches ``context.engine: lcm``."""
    engine = LCMEngine()
    assert engine.name == "lcm"


def test_name_is_class_attribute() -> None:
    """AC: ``name`` is a string class attribute (not the class name)."""
    assert LCMEngine.name == "lcm"
    assert isinstance(LCMEngine.name, str)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_instantiates_with_no_args() -> None:
    """v0 engine must construct without args (defaults sensible)."""
    engine = LCMEngine()
    assert engine.config is not None
    assert isinstance(engine.config, LcmConfig)
    # hermes_home defaults to ~/.hermes when not passed
    assert isinstance(engine.hermes_home, Path)


def test_instantiates_with_explicit_args(tmp_home: Path) -> None:
    """AC: ``LCMEngine(hermes_home=tmp_path, config=LcmConfig())`` round-trip."""
    cfg = LcmConfig()
    engine = LCMEngine(hermes_home=tmp_home, config=cfg)
    assert engine.hermes_home == tmp_home
    assert engine.config is cfg


def test_constructor_does_not_open_db(tmp_home: Path) -> None:
    """ADR-001 §Consequences: heavy init belongs in on_session_start.

    The constructor must NOT open the SQLite DB or run migrations. We
    can't easily assert "no DB file created" without a DB-open
    instrumentation, but we can assert no ``.db`` files appear in
    ``hermes_home`` after construction.
    """
    LCMEngine(hermes_home=tmp_home, config=LcmConfig())
    # No DB files should have been created under the home dir.
    db_files = list(tmp_home.rglob("*.db"))
    assert db_files == [], f"Constructor opened a DB: {db_files}"


def test_token_state_initialized_to_zero() -> None:
    """ABC contract: ``last_*_tokens`` start at 0 (read by run_agent.py)."""
    engine = LCMEngine()
    assert engine.last_prompt_tokens == 0
    assert engine.last_completion_tokens == 0
    assert engine.last_total_tokens == 0
    assert engine.threshold_tokens == 0
    assert engine.context_length == 0
    assert engine.compression_count == 0


# ---------------------------------------------------------------------------
# compress() — round-trip identity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "messages",
    [
        pytest.param([], id="empty-list"),
        pytest.param([{"role": "user", "content": "hi"}], id="single-message"),
        pytest.param(
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "world"},
                {"role": "user", "content": "follow-up"},
            ],
            id="multi-turn",
        ),
        pytest.param(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "look at this"},
                        {"type": "image", "source": {"data": "fake-b64"}},
                    ],
                }
            ],
            id="multi-modal-content-blocks",
        ),
        pytest.param(
            [
                {"role": "system", "content": "system prompt"},
                {"role": "user", "content": "user1"},
                {
                    "role": "assistant",
                    "content": "tool call coming",
                    "tool_calls": [{"id": "t1", "type": "function"}],
                },
                {"role": "tool", "tool_call_id": "t1", "content": "result"},
            ],
            id="with-tool-calls",
        ),
    ],
)
def test_compress_is_identity(messages: list[dict]) -> None:
    """AC: ``engine.compress(msgs) == msgs`` for a variety of shapes."""
    engine = LCMEngine()
    result = engine.compress(messages)
    assert result == messages


def test_compress_returns_same_object() -> None:
    """v0 is a pure passthrough — the returned list is the input list."""
    engine = LCMEngine()
    msgs = [{"role": "user", "content": "hi"}]
    result = engine.compress(msgs)
    # Identity, not just equality — confirms it's a true passthrough,
    # not a copy. This matters for downstream code that may rely on
    # reference identity (and surfaces accidental copying as a regression).
    assert result is msgs


def test_compress_ignores_current_tokens() -> None:
    """AC: ``current_tokens`` argument is accepted and ignored at v0."""
    engine = LCMEngine()
    msgs = [{"role": "user", "content": "hi"}]
    assert engine.compress(msgs, current_tokens=999_999) is msgs


def test_compress_ignores_focus_topic() -> None:
    """AC: ``focus_topic`` argument is accepted and ignored at v0."""
    engine = LCMEngine()
    msgs = [{"role": "user", "content": "hi"}]
    assert engine.compress(msgs, focus_topic="anything") is msgs


# ---------------------------------------------------------------------------
# should_compress() — always False
# ---------------------------------------------------------------------------


def test_should_compress_returns_false_with_no_args() -> None:
    """AC: ``should_compress()`` returns ``False`` unconditionally."""
    engine = LCMEngine()
    assert engine.should_compress() is False


def test_should_compress_returns_false_for_huge_token_count() -> None:
    """AC: even at ridiculous token counts, v0 returns False."""
    engine = LCMEngine()
    assert engine.should_compress(prompt_tokens=999_999_999) is False


def test_should_compress_returns_false_for_zero() -> None:
    """Boundary: zero tokens also returns False."""
    engine = LCMEngine()
    assert engine.should_compress(prompt_tokens=0) is False


# ---------------------------------------------------------------------------
# update_from_response() — state update
# ---------------------------------------------------------------------------


def test_update_from_response_records_tokens() -> None:
    """AC: ``update_from_response()`` updates state without error."""
    engine = LCMEngine()
    engine.update_from_response({
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
    })
    assert engine.last_prompt_tokens == 100
    assert engine.last_completion_tokens == 50
    assert engine.last_total_tokens == 150


def test_update_from_response_tolerates_anthropic_keys() -> None:
    """Anthropic uses ``input_tokens`` / ``output_tokens`` — both work."""
    engine = LCMEngine()
    engine.update_from_response({"input_tokens": 200, "output_tokens": 75})
    assert engine.last_prompt_tokens == 200
    assert engine.last_completion_tokens == 75
    assert engine.last_total_tokens == 275


def test_update_from_response_with_missing_keys() -> None:
    """Missing keys default to 0; total computes from parts."""
    engine = LCMEngine()
    engine.update_from_response({})
    assert engine.last_prompt_tokens == 0
    assert engine.last_completion_tokens == 0
    assert engine.last_total_tokens == 0


def test_update_from_response_computes_total_when_absent() -> None:
    """``total_tokens`` is computed from parts when not provided."""
    engine = LCMEngine()
    engine.update_from_response({"prompt_tokens": 30, "completion_tokens": 20})
    assert engine.last_total_tokens == 50


# ---------------------------------------------------------------------------
# Lifecycle methods — bodies landed in Epic 02 issue 02-03
# ---------------------------------------------------------------------------
#
# These were ``NotImplementedError`` stubs at 02-01. Issue 02-03 filled
# in the heavy-init bodies on ``_LifecycleMixin``. The detailed lifecycle
# behavior is exercised in ``tests/test_lifecycle.py`` — here we just
# confirm the surface no longer raises (regression guard that the bodies
# don't get reverted to stubs).


@_skip_no_extension_loading
def test_on_session_start_no_longer_raises(tmp_home: Path) -> None:
    """02-03: ``on_session_start`` opens the DB rather than raising."""
    engine = LCMEngine(hermes_home=tmp_home, config=LcmConfig())
    try:
        engine.on_session_start("session-id")
        # DB connection is now open.
        assert engine._db is not None
    finally:
        engine.on_session_end("session-id", [])


@_skip_no_extension_loading
def test_on_session_end_no_longer_raises(tmp_home: Path) -> None:
    """02-03: ``on_session_end`` closes the DB rather than raising."""
    engine = LCMEngine(hermes_home=tmp_home, config=LcmConfig())
    engine.on_session_start("session-id")
    engine.on_session_end("session-id", [])
    # DB closed.
    assert engine._db is None


def test_on_session_reset_no_longer_raises() -> None:
    """02-03: ``on_session_reset`` zeroes token state rather than raising."""
    engine = LCMEngine()
    engine.last_prompt_tokens = 100
    engine.on_session_reset()
    assert engine.last_prompt_tokens == 0


# ---------------------------------------------------------------------------
# Tools — empty schemas, JSON-error on unknown dispatch
# ---------------------------------------------------------------------------


def test_get_tool_schemas_returns_empty_list() -> None:
    """AC: tools land in Epic 06 — v0 returns ``[]``.

    Issue 06-02 (PR #87 follow-on) wired :meth:`get_tool_schemas` to
    delegate to ``lossless_hermes.tools.get_tool_schemas`` — the v0
    schemas list is still empty until per-tool issues 06-07..06-14
    land, so the assertion holds.
    """
    engine = LCMEngine()
    assert engine.get_tool_schemas() == []


def test_handle_tool_call_returns_json_error_for_unknown_name() -> None:
    """AC (06-02 update): unknown tool name returns the structured JSON
    error string — does NOT raise.

    Was a ``NotImplementedError`` raise at 02-01 / 03-03 (Epic 06
    pointer). Issue 06-02 lifted the body to the real dispatch table;
    unknown names now return ``{"error": "Unknown LCM tool: ..."}``
    per spec — Hermes wraps caller-side failures in its own JSON
    envelope, so the refusal shape is canonical.
    """
    import json as _json

    engine = LCMEngine()
    result = engine.handle_tool_call("lcm_grep", {"pattern": "foo"})
    assert _json.loads(result) == {"error": "Unknown LCM tool: lcm_grep"}


def test_handle_tool_call_includes_name_in_error_payload() -> None:
    """The structured error names the attempted tool for debuggability."""
    import json as _json

    engine = LCMEngine()
    result = engine.handle_tool_call("lcm_grep", {})
    assert _json.loads(result)["error"] == "Unknown LCM tool: lcm_grep"


# ---------------------------------------------------------------------------
# Apple system Python guard (ADR-004 §Consequences)
# ---------------------------------------------------------------------------


def test_apple_python_guard_helper_raises_when_extension_loading_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC: guard raises before any DB open attempt.

    The guard is :func:`_check_sqlite_extension_loading`, exposed for
    :meth:`on_session_start` (Epic 02) to call before its first
    ``open_lcm_db()`` invocation. At v0 there is no DB open attempt, so
    the guard is **not** wired into ``LCMEngine.__init__`` — firing it
    there would reject working Python installations (e.g.
    ``actions/setup-python``'s macOS pre-built CPython, which lacks
    ``enable_load_extension`` despite being usable for everything except
    sqlite-vec) without justification.

    We monkey-patch :func:`_has_sqlite_extension_loading` (the
    introspection hook the guard consults — ``sqlite3.Connection`` is
    C-immutable so we can't ``delattr`` it directly) and call the
    guard. It must raise :class:`RuntimeError` with the documented
    message.
    """
    import lossless_hermes.engine as engine_mod

    monkeypatch.setattr(engine_mod, "_has_sqlite_extension_loading", lambda: False)
    with pytest.raises(RuntimeError, match=r"sqlite3.Connection.enable_load_extension"):
        engine_mod._check_sqlite_extension_loading()


def test_engine_constructor_does_not_invoke_apple_python_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0 invariant: ``__init__`` does not call the sqlite guard.

    Documented in the class docstring and module-level "Apple system
    Python guard" section. Verifies the deferral so a regression
    (re-adding ``_check_sqlite_extension_loading()`` to ``__init__``)
    surfaces in CI rather than only on the macOS runners.
    """
    import lossless_hermes.engine as engine_mod

    # If __init__ called the guard, this stub would block construction
    # because we force the introspection hook to report False.
    monkeypatch.setattr(engine_mod, "_has_sqlite_extension_loading", lambda: False)
    engine = LCMEngine()
    assert engine.name == "lcm"


def test_apple_python_guard_message_is_actionable() -> None:
    """Confirms the message names concrete install paths (brew/pyenv/uv)."""
    assert "Homebrew" in APPLE_SYSTEM_PYTHON_MSG
    assert "pyenv" in APPLE_SYSTEM_PYTHON_MSG
    assert "uv" in APPLE_SYSTEM_PYTHON_MSG


# ---------------------------------------------------------------------------
# Inheritance
# ---------------------------------------------------------------------------


def test_engine_inherits_from_context_engine() -> None:
    """AC: ``LCMEngine`` is a subclass of the (bridged) ``ContextEngine``.

    In the default test env (Hermes-less) the bridge's stub class is
    the parent. In a Hermes-available env it's the real ABC. Either
    way, ``isinstance`` must hold.
    """
    from lossless_hermes.hermes_bridge import ContextEngine

    engine = LCMEngine()
    assert isinstance(engine, ContextEngine)
