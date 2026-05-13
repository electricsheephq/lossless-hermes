"""Tests for the :class:`LCMEngine` mixin shell (issue 02-01).

Covers the additive surface introduced by issue 02-01 on top of the
00-06 no-op engine. Specifically:

* The four mixins (:class:`_LifecycleMixin`, :class:`_CompactMixin`,
  :class:`_AssembleMixin`, :class:`_IngestMixin`) are composed in the
  MRO in the order specified by ADR-027 §Decision.
* :meth:`LCMEngine.__init__` initializes the state fields owned by the
  shell class (per ADR-027 §Consequences "All state lives on the shell
  class"): ``_db``, ``_conversation_store``, ``_summary_store``,
  ``_telemetry_store``, ``_maintenance_store``, ``_session_locks``,
  ``_circuit_breakers``, ``_last_seen_message_idx``.
* Store attributes default to ``None`` at 02-01 — they are populated by
  :meth:`on_session_start` (issue 02-03), preserving the
  ADR-001-mandated "no heavy init in ``__init__``" invariant.
* The mixin stubs (the Epic-03/04-bound bodies in ``ingest.py`` /
  ``assemble.py`` / ``lifecycle.py``) raise :class:`NotImplementedError`
  with the issue pointer.
* The shell preserves 00-06 behavior (``compress`` passthrough,
  ``should_compress`` False, ``update_from_response`` token updates,
  ``handle_tool_call`` Epic-06 stub).

The 00-06 regression suite (``tests/test_engine_noop.py``) still
applies — those tests continue to assert the v0 invariants that 02-01
must not break.

See:

* ``docs/adr/024-project-layout.md`` — engine/ package placement.
* ``docs/adr/027-engine-splitting.md`` — mixin pattern decisions.
* ``epics/02-engine-skeleton/02-01-engine-init.md`` — this issue's AC.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Dict

import pytest

from lossless_hermes.db.config import LcmConfig
from lossless_hermes.engine import LCMEngine
from lossless_hermes.engine.assemble import _AssembleMixin
from lossless_hermes.engine.compact import _CompactMixin
from lossless_hermes.engine.ingest import _IngestMixin
from lossless_hermes.engine.lifecycle import _LifecycleMixin
from lossless_hermes.engine.session_locks import SessionLockRegistry
from lossless_hermes.hermes_bridge import ContextEngine

# ---------------------------------------------------------------------------
# Skip marker: actions/setup-python macOS builds lack enable_load_extension
# ---------------------------------------------------------------------------
#
# Mirrors ``_skip_no_extension_loading`` in ``tests/test_db_connection.py``
# (ADR-004 §Open questions item 1, ADR-028 §Decision point 8). The
# ``on_session_start`` lifecycle body filled in by issue 02-03 opens an
# ``open_lcm_db()`` connection that loads sqlite-vec, which is impossible
# on the actions/setup-python macOS pre-built CPython. Tests that exercise
# only the construction-time invariants or monkey-patch the introspection
# hook remain runnable on those cells.
_skip_no_extension_loading = pytest.mark.skipif(
    not hasattr(sqlite3.Connection, "enable_load_extension"),
    reason=(
        "actions/setup-python on macOS ships a CPython build without "
        "--enable-loadable-sqlite-extensions; sqlite-vec cannot load. "
        "See ADR-004 §Open questions item 1 + ADR-028 §Decision point 8."
    ),
)


# ---------------------------------------------------------------------------
# Mixin composition (MRO order per ADR-027 §Decision)
# ---------------------------------------------------------------------------


def test_engine_inherits_from_all_four_mixins() -> None:
    """ADR-027 §Decision: LCMEngine composes _LifecycleMixin / _CompactMixin
    / _AssembleMixin / _IngestMixin.

    All four mixin classes must be on the MRO so subsequent Epic 02-04
    issues can fill in method bodies via method-resolution (no shell
    edits required).
    """
    engine = LCMEngine()
    assert isinstance(engine, _LifecycleMixin)
    assert isinstance(engine, _CompactMixin)
    assert isinstance(engine, _AssembleMixin)
    assert isinstance(engine, _IngestMixin)
    assert isinstance(engine, ContextEngine)


def test_mro_order_matches_adr_027() -> None:
    """ADR-027 §Decision MRO contract: LCMEngine -> _LifecycleMixin ->
    _CompactMixin -> _AssembleMixin -> _IngestMixin -> ContextEngine -> object.

    Python's C3 linearization on the bases tuple ``(_LifecycleMixin,
    _CompactMixin, _AssembleMixin, _IngestMixin, ContextEngine)``
    produces exactly that order. We assert the prefix (first 5 elements)
    because ``object`` and any ABC ``__abstractmethods__`` machinery
    classes at the tail are CPython-version-dependent. The prefix is
    what callers and Epic 02-04 mixin-fill issues depend on.
    """
    mro = LCMEngine.__mro__
    mro_prefix = mro[:6]
    assert mro_prefix == (
        LCMEngine,
        _LifecycleMixin,
        _CompactMixin,
        _AssembleMixin,
        _IngestMixin,
        ContextEngine,
    ), f"MRO prefix mismatch: {[c.__name__ for c in mro_prefix]}"


def test_mixin_classes_are_underscore_prefixed() -> None:
    """ADR-027 §Decision: "Each mixin is a `private` class
    (underscore-prefixed) — not instantiated directly."

    Defensive: catches a contributor renaming a mixin to a public name
    (which would imply external callers might rely on the mixin shape).
    """
    assert _LifecycleMixin.__name__.startswith("_")
    assert _CompactMixin.__name__.startswith("_")
    assert _AssembleMixin.__name__.startswith("_")
    assert _IngestMixin.__name__.startswith("_")


# ---------------------------------------------------------------------------
# State field initialization (ADR-027 §Consequences "All state lives on shell")
# ---------------------------------------------------------------------------


def test_state_fields_initialized_to_none() -> None:
    """ADR-001 §Consequences: heavy init belongs in ``on_session_start``,
    so DB + store attributes default to ``None`` at construction.

    02-03 fills them in. Mixin methods guard with ``if
    self._conversation_store is None: raise RuntimeError("...")``.
    """
    engine = LCMEngine()
    assert engine._db is None
    assert engine._conversation_store is None
    assert engine._summary_store is None
    assert engine._telemetry_store is None
    assert engine._maintenance_store is None


def test_session_locks_is_session_lock_registry() -> None:
    """ADR-018 §"Per-session queue": per-session lock dict for ingest
    + compact serialization.

    Issue 02-08 replaces the issue 02-01 ``defaultdict(asyncio.Lock)``
    placeholder with a :class:`SessionLockRegistry` that adds a
    refcount + lazy prune pass. Callers acquire via
    ``async with engine._session_locks.acquire(session_id): ...``.
    """
    engine = LCMEngine()
    assert isinstance(engine._session_locks, SessionLockRegistry)
    # Sanity: untouched registry has no entries.
    assert len(engine._session_locks) == 0
    assert engine._session_locks.pending_count() == 0


def test_circuit_breakers_is_empty_dict() -> None:
    """02-09 fills in the circuit-breaker state machine; at 02-01 the
    scaffold is just an empty dict."""
    engine = LCMEngine()
    assert engine._circuit_breakers == {}
    assert isinstance(engine._circuit_breakers, dict)


def test_last_seen_message_idx_is_empty_dict() -> None:
    """Epic 03 fills in the diff-based ingest; at 02-01 the per-session
    cursor is an empty dict."""
    engine = LCMEngine()
    assert engine._last_seen_message_idx == {}
    assert isinstance(engine._last_seen_message_idx, dict)


def test_token_state_initialized_to_zero() -> None:
    """ABC contract: ``last_*_tokens`` / ``threshold_tokens`` /
    ``context_length`` / ``compression_count`` start at 0 (read by
    run_agent.py)."""
    engine = LCMEngine()
    assert engine.last_prompt_tokens == 0
    assert engine.last_completion_tokens == 0
    assert engine.last_total_tokens == 0
    assert engine.threshold_tokens == 0
    assert engine.context_length == 0
    assert engine.compression_count == 0


# ---------------------------------------------------------------------------
# Construction signatures (preserved from 00-06)
# ---------------------------------------------------------------------------


def test_instantiates_with_no_args() -> None:
    """02-01 must not break the 00-06 no-args constructor."""
    engine = LCMEngine()
    assert engine.config is not None
    assert isinstance(engine.config, LcmConfig)
    assert isinstance(engine.hermes_home, Path)


def test_instantiates_with_explicit_args(tmp_home: Path) -> None:
    """02-01 must preserve 00-06's keyword-arg constructor surface."""
    cfg = LcmConfig()
    engine = LCMEngine(hermes_home=tmp_home, config=cfg)
    assert engine.hermes_home == tmp_home
    assert engine.config is cfg


def test_constructor_does_not_open_db(tmp_home: Path) -> None:
    """ADR-001 §Consequences invariant preserved: ``__init__`` does NOT
    open the SQLite DB or run migrations.

    02-03's ``on_session_start`` body will perform that work.
    """
    LCMEngine(hermes_home=tmp_home, config=LcmConfig())
    db_files = list(tmp_home.rglob("*.db"))
    assert db_files == [], f"Constructor opened a DB: {db_files}"


# ---------------------------------------------------------------------------
# Mixin stubs raise the expected NotImplementedError
# ---------------------------------------------------------------------------


@_skip_no_extension_loading
def test_on_session_start_no_longer_stubbed(tmp_home: Path) -> None:
    """02-03 filled in the body; ``on_session_start`` now opens the DB.

    Was a ``NotImplementedError`` stub at 02-01. The detailed behavior
    is in ``tests/test_lifecycle.py``; this is a regression guard
    against re-stubbing.
    """
    engine = LCMEngine(hermes_home=tmp_home, config=LcmConfig())
    try:
        engine.on_session_start("sess-1")
        assert engine._db is not None
    finally:
        engine.on_session_end("sess-1", [])


@_skip_no_extension_loading
def test_on_session_end_no_longer_stubbed(tmp_home: Path) -> None:
    """02-03 filled in the body; ``on_session_end`` now closes the DB."""
    engine = LCMEngine(hermes_home=tmp_home, config=LcmConfig())
    engine.on_session_start("sess-1")
    engine.on_session_end("sess-1", [])
    assert engine._db is None


def test_on_session_reset_no_longer_stubbed() -> None:
    """02-03 filled in the body; ``on_session_reset`` resets token state."""
    engine = LCMEngine()
    engine.last_prompt_tokens = 100
    engine.on_session_reset()
    assert engine.last_prompt_tokens == 0


def test_on_post_llm_call_stub_returns_none() -> None:
    """Issue 02-07 demotes the stub from ``NotImplementedError`` to no-op.

    The hook must be callable without raising so :func:`register` can
    wire it via ``ctx.register_hook("post_llm_call", engine._on_post_llm_call)``
    without the agent loop crashing every turn. Epic 03 fills in the
    real diff-and-ingest body.

    **Sync invariant:** Hermes's ``invoke_hook`` calls callbacks
    synchronously (``ret = cb(**kwargs)`` — ``hermes_cli/plugins.py:1218-1232``).
    ``async def`` would return a coroutine that Hermes treats as a
    non-``None`` return. This test asserts the direct (sync) call
    returns ``None``, guarding against accidental re-introduction of
    ``async def``.
    """
    engine = LCMEngine()

    result = engine._on_post_llm_call(
        session_id="sess-1",
        user_message="hi",
        assistant_response="hello",
        conversation_history=[],
        model="claude-haiku",
        platform="anthropic",
    )

    assert result is None
    # Sync guard: a coroutine return would mean someone re-introduced
    # ``async def``. Hermes would then store the coroutine in its
    # results list instead of treating the hook as observer-only.
    import inspect

    assert not inspect.iscoroutine(result)


def test_on_pre_llm_call_returns_recall_policy_dict() -> None:
    """Issue 03-10 lifts the hook from the 02-07 no-op stub to the
    always-on recall-policy injection per ADR-014.

    The hook must be callable without raising so :func:`register` can
    wire it via ``ctx.register_hook("pre_llm_call", engine._on_pre_llm_call)``
    without the agent loop crashing every turn. Per Hermes's hook
    contract (``hermes_cli/plugins.py:1218-1232``) the dict return is
    routed to user-message content; ``{"context": <text>}`` is the
    documented injection shape.

    **Sync invariant:** Hermes invokes hooks synchronously; ``async``
    would inject a coroutine instead of policy text. Guarded below.
    The in-depth ``pre_llm_call`` coverage lives in
    ``tests/test_pre_llm_call.py``; here we keep the skeleton-level
    contract that 03-10 did not regress the sync invariant from
    02-07's PR #34.
    """
    from lossless_hermes.recall_policy import LOSSLESS_RECALL_POLICY_PROMPT

    engine = LCMEngine()

    result = engine._on_pre_llm_call(session_id="sess-1", conversation_history=[])

    assert result == {"context": LOSSLESS_RECALL_POLICY_PROMPT}
    import inspect

    assert not inspect.iscoroutine(result)


# ---------------------------------------------------------------------------
# 00-06 behavior preserved through the mixins (regression guard)
# ---------------------------------------------------------------------------


def test_compress_is_passthrough_via_compact_mixin() -> None:
    """:class:`_CompactMixin.compress` returns ``messages`` unchanged at 02-01.

    The mixin owns the body; the shell class no longer declares
    ``compress`` directly. MRO routes ``engine.compress(...)`` to the
    mixin.
    """
    engine = LCMEngine()
    msgs: list[Dict] = [{"role": "user", "content": "hi"}]
    result = engine.compress(msgs)
    # Identity preserved — the v0 invariant from 00-06 was "pure
    # passthrough, not copy".
    assert result is msgs


def test_should_compress_returns_false_via_compact_mixin() -> None:
    """:class:`_CompactMixin.should_compress` returns ``False`` at 02-01."""
    engine = LCMEngine()
    assert engine.should_compress() is False
    assert engine.should_compress(prompt_tokens=999_999_999) is False


def test_update_from_response_still_works_on_shell() -> None:
    """``update_from_response`` lives on the shell class (not a mixin)
    because it touches multiple state fields owned by the shell."""
    engine = LCMEngine()
    engine.update_from_response({
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
    })
    assert engine.last_prompt_tokens == 100
    assert engine.last_completion_tokens == 50
    assert engine.last_total_tokens == 150


def test_get_tool_schemas_returns_empty_at_02_01() -> None:
    """Tools land in Epic 06; the registry returns ``[]`` until then.

    Issue 06-02 wired :meth:`get_tool_schemas` to delegate to
    ``lossless_hermes.tools.get_tool_schemas`` — empty until per-tool
    issues 06-07..06-14 register schemas at import time.
    """
    engine = LCMEngine()
    assert engine.get_tool_schemas() == []


def test_handle_tool_call_unknown_returns_json_error() -> None:
    """Issue 06-02 lifted dispatch from the Epic-06-pointer stub to the
    real :data:`TOOL_DISPATCH` table.

    Unknown tool names now return the structured JSON error string per
    spec — handlers land in 06-07..06-14 and register into the table
    at import time. The 02-01 ``NotImplementedError`` semantics moved
    to "registered but unimplemented" instead of "any name raises".
    """
    import json as _json

    engine = LCMEngine()
    result = engine.handle_tool_call("lcm_grep", {})
    assert _json.loads(result) == {"error": "Unknown LCM tool: lcm_grep"}


# ---------------------------------------------------------------------------
# Public surface invariant — name + class attributes
# ---------------------------------------------------------------------------


def test_name_is_lcm_class_attribute() -> None:
    """ADR-001 §Consequences "config.yaml must set context.engine: lcm" —
    the string "lcm" is the selector.
    """
    assert LCMEngine.name == "lcm"
    engine = LCMEngine()
    assert engine.name == "lcm"


def test_threshold_percent_default_is_075() -> None:
    """Standard LCM default per the porting guide."""
    assert LCMEngine.threshold_percent == 0.75


def test_protect_last_n_default_is_8() -> None:
    """LCM-specific override of ABC default 6, per the porting guide."""
    assert LCMEngine.protect_last_n == 8
