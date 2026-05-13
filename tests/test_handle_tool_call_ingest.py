"""Tests for the ``handle_tool_call`` belt-and-suspenders ingest path (issue 03-03).

Covers ADR-009 §Decision "Option C": ``ContextEngine.handle_tool_call``
runs the same diff-against-cursor / ``_ingest_batch`` body as
``post_llm_call`` BEFORE dispatching the tool, so tool-only turns that
exit before a final response (Ctrl-C, max-iterations, no-final-response)
still get their pre-tool user-turn into the LCM DAG.

The body shared with ``_on_post_llm_call`` lives in
:meth:`_IngestMixin._do_ingest_history_diff`; this file exercises the
*entry seam* (:meth:`LCMEngine.handle_tool_call` override +
:meth:`_IngestMixin._ingest_from_handle_tool_call`) and the
**idempotency invariant** that makes Option B + Option C safe to
double-fire: the cursor + per-session sync lock dedup the second
fire.

References:

* ``epics/03-ingest-assembly/03-03-ingest-from-handle-tool-call.md`` — spec.
* ``docs/adr/009-per-message-ingest.md`` §"Option C" — design rationale.
* ``docs/reference/hermes-hooks.md`` line 59 — ``handle_tool_call``
  kwargs shape (only ``messages`` today; ``session_id`` / ``sender_id``
  are forward-compat).
* ``hermes-agent/run_agent.py`` line 11249 — the call site.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest

from lossless_hermes.db.config import LcmConfig
from lossless_hermes.engine import LCMEngine

# ---------------------------------------------------------------------------
# Skip marker: actions/setup-python macOS builds lack enable_load_extension
# ---------------------------------------------------------------------------
# Mirrors the marker in ``tests/test_engine_ingest.py``. The prelude path
# runs on top of ``on_session_start``'s opened DB, so we need a full
# ``open_lcm_db`` connection (which loads sqlite-vec). Apple's system
# Python ships without ``enable_load_extension``.
_skip_no_extension_loading = pytest.mark.skipif(
    not hasattr(sqlite3.Connection, "enable_load_extension"),
    reason=(
        "actions/setup-python on macOS ships a CPython build without "
        "--enable-loadable-sqlite-extensions; sqlite-vec cannot load. "
        "handle_tool_call ingest tests require the full lifecycle DB so "
        "they skip here."
    ),
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine(tmp_home: Path) -> Iterator[LCMEngine]:
    """An :class:`LCMEngine` with ``on_session_start`` already run.

    Identical fixture to ``tests/test_engine_ingest.py::engine`` — kept
    independent so this file can be run / xfailed in isolation.
    """
    eng = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    eng.on_session_start("test-session")
    try:
        yield eng
    finally:
        eng.on_session_end("test-session", [])


# ---------------------------------------------------------------------------
# Helper — call handle_tool_call. The 03-03 prelude runs FIRST; the
# dispatch then either routes to a registered handler or returns the
# structured JSON error string for unknown names. For the tests below
# we register no handler, so dispatch returns the JSON error — the
# tests assert on the ingest side-effects regardless.
# ---------------------------------------------------------------------------


def _invoke(engine: LCMEngine, name: str, args: Dict[str, Any], **kwargs: Any) -> None:
    """Call ``engine.handle_tool_call(name, args, **kwargs)`` and discard the result.

    Issue 06-02 lifted dispatch from the 03-03 ``NotImplementedError``
    stub to the real :data:`TOOL_DISPATCH` table. For unknown tool
    names (the case below — no per-tool ports registered) the call
    returns the structured JSON error string. For tests below the
    return value is incidental — we care about the ingest side-effects
    the prelude produces BEFORE the dispatch step runs.
    """
    engine.handle_tool_call(name, args, **kwargs)


# ---------------------------------------------------------------------------
# Happy path — kwargs["messages"] + session_id → ingest fires
# ---------------------------------------------------------------------------


@_skip_no_extension_loading
def test_messages_plus_session_id_ingests(engine: LCMEngine) -> None:
    """``messages`` + ``session_id`` kwargs → prelude ingests then raises.

    Spec AC: "A test calls an ``lcm_*`` tool on a session with N new
    un-ingested messages — assert all N land in the DB."
    """
    messages = [
        {"role": "user", "content": "search for foo"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call-1",
                    "name": "lcm_grep",
                    "input": {"pattern": "foo"},
                }
            ],
        },
    ]
    _invoke(engine, "lcm_grep", {"pattern": "foo"}, messages=messages, session_id="sess-A")

    # Cursor advanced.
    assert engine._last_seen_message_idx["sess-A"] == 2
    # Both messages landed.
    conv = engine._conversation_store.get_conversation_by_session_id("sess-A")
    assert conv is not None
    msgs = engine._conversation_store.get_messages(conv.conversation_id)
    assert len(msgs) == 2
    assert msgs[0].role == "user"
    assert msgs[0].content == "search for foo"


@_skip_no_extension_loading
def test_messages_plus_sender_id_ingests(engine: LCMEngine) -> None:
    """``sender_id`` (forward-compat) is used when ``session_id`` is absent.

    Per the spec override: ``session_id = kwargs.get("session_id") or
    kwargs.get("sender_id")``. The CLI today doesn't surface
    ``sender_id`` to ``handle_tool_call``, but the gateway-platform
    user id might land here in the future.
    """
    messages = [{"role": "user", "content": "hello via sender_id"}]
    _invoke(engine, "lcm_grep", {}, messages=messages, sender_id="user-X")

    assert engine._last_seen_message_idx["user-X"] == 1
    conv = engine._conversation_store.get_conversation_by_session_id("user-X")
    assert conv is not None
    assert len(engine._conversation_store.get_messages(conv.conversation_id)) == 1


@_skip_no_extension_loading
def test_session_id_preferred_over_sender_id(engine: LCMEngine) -> None:
    """When both keys are present, ``session_id`` wins (left of the ``or``)."""
    messages = [{"role": "user", "content": "both keys provided"}]
    _invoke(
        engine,
        "lcm_grep",
        {},
        messages=messages,
        session_id="primary",
        sender_id="fallback",
    )
    # Cursor landed under ``primary``, not ``fallback``.
    assert engine._last_seen_message_idx["primary"] == 1
    assert "fallback" not in engine._last_seen_message_idx


# ---------------------------------------------------------------------------
# Tool-only turn coverage (the whole point of Option C)
# ---------------------------------------------------------------------------


@_skip_no_extension_loading
def test_tool_only_turn_ingested_when_post_llm_call_doesnt_fire(
    engine: LCMEngine,
) -> None:
    """A tool-only turn (Ctrl-C, no final response) is captured here.

    Simulates the production failure mode: ``post_llm_call`` never
    fires (no ``final_response``), so the ONLY observation point is
    ``handle_tool_call`` mid-loop. The user's pre-tool turn + any
    assistant tool-call should land via this seam alone.
    """
    # NOTE: we deliberately do NOT call ``_on_post_llm_call``. This
    # mirrors the Ctrl-C / max-iterations scenario.
    messages = [
        {"role": "user", "content": "delete everything (canceled)"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call-1",
                    "name": "lcm_grep",
                    "input": {"pattern": ".*"},
                }
            ],
        },
    ]
    _invoke(engine, "lcm_grep", {}, messages=messages, session_id="sess-A")

    # Both messages landed despite post_llm_call never firing.
    conv = engine._conversation_store.get_conversation_by_session_id("sess-A")
    assert conv is not None
    msgs = engine._conversation_store.get_messages(conv.conversation_id)
    assert len(msgs) == 2
    assert engine._last_seen_message_idx["sess-A"] == 2


# ---------------------------------------------------------------------------
# Idempotency invariant — Option B + Option C double-fire is safe
# ---------------------------------------------------------------------------


@_skip_no_extension_loading
def test_no_double_ingest_when_both_hooks_fire(engine: LCMEngine) -> None:
    """post_llm_call after handle_tool_call on the same turn → no duplicates.

    Spec AC: "A test fires ``post_llm_call`` for the same conversation
    AFTER ``handle_tool_call`` already ingested — assert no duplicate
    rows (cursor dedup works)."

    The cursor (``_last_seen_message_idx``) is the dedup mechanism at
    v0.1 per ADR-009 §Decision "Option B primary path".
    """
    messages = [
        {"role": "user", "content": "u1"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call-1",
                    "name": "lcm_grep",
                    "input": {},
                }
            ],
        },
    ]

    # Option C fires first (tool-call dispatch).
    _invoke(engine, "lcm_grep", {}, messages=messages, session_id="sess-A")
    assert engine._last_seen_message_idx["sess-A"] == 2

    conv = engine._conversation_store.get_conversation_by_session_id("sess-A")
    msgs_after_c = engine._conversation_store.get_messages(conv.conversation_id)
    assert len(msgs_after_c) == 2

    # Option B fires next (post_llm_call with the same history).
    engine._on_post_llm_call(session_id="sess-A", conversation_history=messages)

    # No new rows, cursor stays.
    msgs_after_b = engine._conversation_store.get_messages(conv.conversation_id)
    assert len(msgs_after_b) == 2
    assert engine._last_seen_message_idx["sess-A"] == 2


@_skip_no_extension_loading
def test_no_double_ingest_when_handle_tool_call_fires_after_post_llm_call(
    engine: LCMEngine,
) -> None:
    """Reverse order — Option B then Option C — also doesn't double-ingest.

    The cursor advance after Option B's ingest means Option C sees
    ``current_idx >= len(history)`` under the lock and short-circuits.
    """
    messages = [{"role": "user", "content": "u1"}]

    # Option B fires first.
    engine._on_post_llm_call(session_id="sess-A", conversation_history=messages)
    assert engine._last_seen_message_idx["sess-A"] == 1

    conv = engine._conversation_store.get_conversation_by_session_id("sess-A")
    assert len(engine._conversation_store.get_messages(conv.conversation_id)) == 1

    # Option C fires next.
    _invoke(engine, "lcm_grep", {}, messages=messages, session_id="sess-A")

    # No new rows; cursor stable.
    assert len(engine._conversation_store.get_messages(conv.conversation_id)) == 1
    assert engine._last_seen_message_idx["sess-A"] == 1


@_skip_no_extension_loading
def test_tool_only_followed_by_successful_turn_no_double_ingest(
    engine: LCMEngine,
) -> None:
    """Turn 1 ingests via handle_tool_call; turn 2's post_llm_call only adds the delta.

    Spec AC (from issue orchestration): "tool-only turn followed by
    successful turn: no double-ingest (idempotency via cursor)."

    The next successful turn's post_llm_call fires with the FULL
    history (turn-1 messages + turn-2 messages); the diff against
    the cursor advanced by handle_tool_call means only the turn-2
    tail goes in.
    """
    # Turn 1: tool-only, captured via handle_tool_call.
    turn_1_messages = [
        {"role": "user", "content": "u1"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "call-1", "name": "lcm_grep", "input": {}}],
        },
    ]
    _invoke(engine, "lcm_grep", {}, messages=turn_1_messages, session_id="sess-A")
    assert engine._last_seen_message_idx["sess-A"] == 2

    # Turn 2: successful, post_llm_call fires with full history.
    turn_2_messages = turn_1_messages + [
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "answer"},
    ]
    engine._on_post_llm_call(session_id="sess-A", conversation_history=turn_2_messages)
    assert engine._last_seen_message_idx["sess-A"] == 4

    conv = engine._conversation_store.get_conversation_by_session_id("sess-A")
    msgs = engine._conversation_store.get_messages(conv.conversation_id)
    # All 4 messages land exactly once (no duplicates). We sample roles
    # (rather than content, since the tool_use list serializes to an
    # implementation-defined JSON string) — the load-bearing assertion
    # is that the cursor dedup works.
    assert len(msgs) == 4
    assert [m.role for m in msgs] == ["user", "assistant", "user", "assistant"]
    # User messages' content survives the round-trip.
    user_msgs = [m for m in msgs if m.role == "user"]
    assert [m.content for m in user_msgs] == ["u1", "u2"]


# ---------------------------------------------------------------------------
# No-op paths — missing kwargs, missing session id, no messages
# ---------------------------------------------------------------------------


def test_missing_messages_kwarg_is_no_op(tmp_home: Path) -> None:
    """No ``messages`` kwarg → prelude is a no-op; dispatch returns JSON error.

    Spec AC (from issue orchestration): "Missing ``messages`` kwarg:
    handle_tool_call still works (no-op ingest)."

    Issue 06-02 lifted dispatch from the 03-03 ``NotImplementedError``
    stub to the real :data:`TOOL_DISPATCH` table — ``lcm_grep`` is
    still unknown (06-07 not yet landed), so dispatch returns the
    structured JSON error string. The prelude short-circuits BEFORE
    touching stores when ``messages`` is missing, so this test does
    NOT call ``on_session_start`` — it runs on any Python build
    regardless of sqlite-vec availability.
    """
    eng = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    eng.handle_tool_call("lcm_grep", {}, session_id="sess-A")
    # No ingest happened.
    assert "sess-A" not in eng._last_seen_message_idx


def test_none_messages_kwarg_is_no_op(tmp_home: Path) -> None:
    """``messages=None`` → prelude is a no-op.

    No DB open — the prelude short-circuits before any store access.
    Runs on every Python build (including the macOS CI lane without
    ``enable_load_extension``).
    """
    eng = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    eng.handle_tool_call("lcm_grep", {}, messages=None, session_id="sess-A")
    assert "sess-A" not in eng._last_seen_message_idx


def test_empty_messages_kwarg_is_no_op(tmp_home: Path) -> None:
    """``messages=[]`` is also falsy → prelude is a no-op.

    The prelude gate is ``if messages and session_id:``; an empty list
    is falsy in Python so the prelude short-circuits without acquiring
    the lock or touching stores. No DB open required.
    """
    eng = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    eng.handle_tool_call("lcm_grep", {}, messages=[], session_id="sess-A")
    assert "sess-A" not in eng._last_seen_message_idx


def test_missing_session_id_and_sender_id_is_no_op(tmp_home: Path) -> None:
    """No session_id AND no sender_id → prelude is a no-op.

    Spec AC (from issue orchestration): "Missing ``session_id`` AND
    ``sender_id``: no-op ingest."

    This is the Hermes-today shape per ``run_agent.py:11249``: only
    ``messages=messages`` is passed. The forward-compat
    ``session_id``/``sender_id`` chain returns ``None``, and the
    prelude short-circuits. The pre-prelude branch happens BEFORE any
    DB access, so the test does not need ``on_session_start``.
    """
    eng = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    messages = [{"role": "user", "content": "no session id"}]
    eng.handle_tool_call("lcm_grep", {}, messages=messages)
    # No state mutated — the prelude short-circuited.
    assert eng._last_seen_message_idx == {}


def test_empty_session_id_short_circuits(tmp_home: Path) -> None:
    """``session_id=""`` (empty string) is falsy → prelude is a no-op.

    Defensive: the spec override uses ``or`` which treats ``""`` as
    falsy; sender_id is not set so the chain returns falsy and the
    prelude skips. No DB open required (the prelude short-circuits
    before any lock/store access).
    """
    eng = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    messages = [{"role": "user", "content": "empty session"}]
    eng.handle_tool_call("lcm_grep", {}, messages=messages, session_id="")
    assert eng._last_seen_message_idx == {}


def test_no_kwargs_at_all_returns_json_error(tmp_home: Path) -> None:
    """Existing 02-01 test shape — ``handle_tool_call("lcm_grep", {})``.

    Issue 06-02 lifted dispatch from the 03-03 ``NotImplementedError``
    stub to the real dispatch table. ``lcm_grep`` is still unknown
    (06-07 not yet landed) so the call returns the structured JSON
    error string. The prelude is a no-op when ``messages`` is absent.
    No DB open required.
    """
    import json as _json

    eng = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    result = eng.handle_tool_call("lcm_grep", {})
    assert _json.loads(result) == {"error": "Unknown LCM tool: lcm_grep"}


# ---------------------------------------------------------------------------
# Observer-only contract — prelude never raises into tool dispatch
# ---------------------------------------------------------------------------


@_skip_no_extension_loading
def test_prelude_per_message_error_does_not_break_dispatch_raise(
    engine: LCMEngine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A per-message store failure is isolated; the dispatch still returns.

    The shared ``_ingest_batch`` body wraps each ``_ingest_single`` call
    in its own try/except (per-message error isolation). When the store
    explodes on the first message:

    * The batch logs the inner error and continues with the rest.
    * The cursor does NOT advance (no messages successfully landed).
    * The outer prelude's catch-all is NOT triggered (batch swallowed
      the error).
    * The dispatch returns its JSON error string (06-02 contract;
      ``lcm_grep`` is unknown until 06-07 lands).

    This test exercises the "ingest never breaks tool dispatch"
    contract via the per-message error-isolation path that the
    Wave-4 atomic-txn fix relies on (see ``_ingest_batch`` body).
    """
    import logging

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated store failure")

    monkeypatch.setattr(engine._conversation_store, "get_or_create_conversation", _boom)

    messages = [{"role": "user", "content": "trigger"}]
    with caplog.at_level(logging.ERROR, logger="lossless_hermes.engine.ingest"):
        # The dispatch returns JSON error (Epic 06 per-tool ports not in).
        result = engine.handle_tool_call("lcm_grep", {}, messages=messages, session_id="sess-A")
    assert isinstance(result, str)

    # An ingest error was logged — the per-message isolation logs
    # ``_ingest_batch: single ingest failed for session=sess-A …``.
    assert any(
        "single ingest failed" in rec.getMessage() and "sess-A" in rec.getMessage()
        for rec in caplog.records
    ), [r.getMessage() for r in caplog.records]
    # Cursor did NOT advance (no messages landed).
    assert "sess-A" not in engine._last_seen_message_idx


@_skip_no_extension_loading
def test_prelude_outer_exception_does_not_break_dispatch_raise(
    engine: LCMEngine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failure OUTSIDE per-message batch isolation triggers the outer catch.

    Simulates a lock-acquisition failure (which the per-message
    isolation in ``_ingest_batch`` would NOT catch). The outer
    :meth:`_ingest_from_handle_tool_call` try/except fires, logs the
    ``handle_tool_call ingest failed`` breadcrumb, and the dispatch
    still returns its JSON error string (06-02 contract).
    """
    import logging
    from contextlib import contextmanager

    real_registry = engine._session_locks

    class _BoomRegistry:
        @contextmanager
        def acquire_sync(self, session_id: str) -> Iterator[None]:
            raise RuntimeError("simulated lock acquisition failure")
            yield  # pragma: no cover — unreachable

        def __getattr__(self, name: str) -> Any:
            return getattr(real_registry, name)

    monkeypatch.setattr(engine, "_session_locks", _BoomRegistry())

    messages = [{"role": "user", "content": "trigger"}]
    with caplog.at_level(logging.ERROR, logger="lossless_hermes.engine.ingest"):
        result = engine.handle_tool_call("lcm_grep", {}, messages=messages, session_id="sess-A")
    assert isinstance(result, str)

    # The outer prelude catch logged its breadcrumb.
    assert any("handle_tool_call ingest failed" in rec.getMessage() for rec in caplog.records), [
        r.getMessage() for r in caplog.records
    ]


def test_prelude_with_no_stores_is_safe(tmp_home: Path) -> None:
    """A handle_tool_call BEFORE ``on_session_start`` doesn't crash.

    Mirrors ``test_pre_bootstrap_hook_call_is_safe`` for the post_llm_call
    seam. Stores are ``None``; the prelude's
    ``_do_ingest_history_diff`` short-circuits with a warning log.
    """
    import json as _json

    eng = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    assert eng._conversation_store is None  # pre-condition

    messages = [{"role": "user", "content": "hi"}]
    # The prelude is a no-op when stores are missing; the dispatch
    # returns the structured JSON error (lcm_grep unknown — 06-07
    # not yet landed). The test asserts NO unexpected exception leaks
    # from the prelude.
    result = eng.handle_tool_call("lcm_grep", {}, messages=messages, session_id="sess-A")
    assert _json.loads(result) == {"error": "Unknown LCM tool: lcm_grep"}
    assert "sess-A" not in eng._last_seen_message_idx


# ---------------------------------------------------------------------------
# Parametrized hook-source — same body, both entry seams
# ---------------------------------------------------------------------------


@_skip_no_extension_loading
@pytest.mark.parametrize("hook_source", ["post_llm_call", "handle_tool_call"])
def test_same_body_both_entry_seams(engine: LCMEngine, hook_source: str) -> None:
    """Both entry seams produce identical DB state for the same history.

    Spec AC: "All TS tests for ingest replay / dedup pass against the
    new entry seam — parametrize over
    ``(hook_source: "post_llm_call" | "handle_tool_call")``".

    The shared body (:meth:`_IngestMixin._do_ingest_history_diff`)
    means the only observable difference between the two seams should
    be log attribution. Cursor, row count, content all match.
    """
    history: List[Dict[str, Any]] = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
    ]
    session_id = f"sess-{hook_source}"

    if hook_source == "post_llm_call":
        engine._on_post_llm_call(session_id=session_id, conversation_history=history)
    elif hook_source == "handle_tool_call":
        _invoke(engine, "lcm_grep", {}, messages=history, session_id=session_id)
    else:  # pragma: no cover — parametrize values are exhaustive
        pytest.fail(f"Unknown hook_source: {hook_source}")

    # Both seams advance the cursor + ingest all 3 messages.
    assert engine._last_seen_message_idx[session_id] == 3
    conv = engine._conversation_store.get_conversation_by_session_id(session_id)
    assert conv is not None
    msgs = engine._conversation_store.get_messages(conv.conversation_id)
    assert [m.content for m in msgs] == ["u1", "a1", "u2"]


# ---------------------------------------------------------------------------
# Session-filter gates apply equally to the new seam
# ---------------------------------------------------------------------------


@_skip_no_extension_loading
def test_ignored_session_pattern_skips_prelude(tmp_home: Path) -> None:
    """Sessions matching ``ignore_session_patterns`` skip the prelude ingest.

    The shared body's gates fire identically for either entry seam.
    """
    cfg = LcmConfig(ignore_session_patterns=["^bench-"])
    eng = LCMEngine(hermes_home=tmp_home / ".hermes", config=cfg)
    eng.on_session_start("bench-001")
    try:
        messages = [{"role": "user", "content": "ignored"}]
        eng.handle_tool_call("lcm_grep", {}, messages=messages, session_id="bench-001")
        # No conversation row landed.
        assert eng._conversation_store.list_active_conversations() == []
        assert "bench-001" not in eng._last_seen_message_idx
    finally:
        eng.on_session_end("bench-001", [])


# ---------------------------------------------------------------------------
# Distinct sessions get distinct locks (parallelism preserved across seams)
# ---------------------------------------------------------------------------


@_skip_no_extension_loading
def test_distinct_sessions_use_distinct_sync_locks(engine: LCMEngine) -> None:
    """The prelude acquires the per-session sync lock — distinct sessions parallelize.

    Acquires ``sess-A``'s lock manually then ingests under ``sess-B``
    via the new seam — proves the sync lock is per-session, not global.
    """
    with engine._session_locks.acquire_sync("sess-A"):
        messages = [{"role": "user", "content": "hi-B"}]
        engine.handle_tool_call("lcm_grep", {}, messages=messages, session_id="sess-B")
        conv_b = engine._conversation_store.get_conversation_by_session_id("sess-B")
        assert conv_b is not None
        msgs_b = engine._conversation_store.get_messages(conv_b.conversation_id)
        assert len(msgs_b) == 1
        assert msgs_b[0].content == "hi-B"
