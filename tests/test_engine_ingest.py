"""Tests for :class:`_IngestMixin` bodies (issue 03-02).

Covers the diff-on-each-turn ingest path filled in by issue 03-02:

* :meth:`_IngestMixin._on_post_llm_call` — the Hermes ``post_llm_call``
  hook handler that diffs ``conversation_history`` against
  ``_last_seen_message_idx[session_id]`` and ingests each new entry.
* :meth:`_IngestMixin._ingest_single` — single-message persistence
  (skip ladder → atomic three-write txn).
* :meth:`_IngestMixin._ingest_batch` — loop of ``_ingest_single`` under
  the caller's lock; per-message error isolation.

The 02-07 hook-registration tests (``tests/test_hook_registrations.py``)
still apply — those exercise the wiring (``register_hook`` calls + bound
methods + the no-op-stub contract). This file exercises the BODY filled
in at 03-02 on top of that wiring.

References:

* ``epics/03-ingest-assembly/03-02-ingest-diff-on-turn.md`` — spec.
* ``docs/adr/009-per-message-ingest.md`` — ``post_llm_call`` as the
  per-turn ingest seam.
* ``docs/adr/018-concurrency-model.md`` — per-session lock invariant.
* ``docs/adr/029-wave-fix-provenance.md`` — Wave-4 atomic-txn comment.
* ``lossless-claw/src/engine.ts`` lines 5899-6134 — TS source.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator

import pytest

from lossless_hermes.db.config import LcmConfig
from lossless_hermes.engine import LCMEngine

# ---------------------------------------------------------------------------
# Skip marker: actions/setup-python macOS builds lack enable_load_extension
# ---------------------------------------------------------------------------
#
# Mirrors ``_skip_no_extension_loading`` in ``tests/test_lifecycle.py``.
# The ingest path runs on top of ``on_session_start``'s opened DB, so
# we need a full ``open_lcm_db`` connection (which loads sqlite-vec). On
# Apple's system Python that's not possible.
_skip_no_extension_loading = pytest.mark.skipif(
    not hasattr(sqlite3.Connection, "enable_load_extension"),
    reason=(
        "actions/setup-python on macOS ships a CPython build without "
        "--enable-loadable-sqlite-extensions; sqlite-vec cannot load. "
        "Ingest tests require the full lifecycle DB so they skip here."
    ),
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine(tmp_home: Path) -> Iterator[LCMEngine]:
    """An :class:`LCMEngine` with ``on_session_start`` already run.

    Tests get a real DB + the four stores wired. Teardown closes the DB
    via ``on_session_end`` symmetrically.
    """
    eng = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    eng.on_session_start("test-session")
    try:
        yield eng
    finally:
        eng.on_session_end("test-session", [])


# ---------------------------------------------------------------------------
# Happy path — single message ingest
# ---------------------------------------------------------------------------


@_skip_no_extension_loading
def test_single_user_message_ingest(engine: LCMEngine) -> None:
    """A single user message ingests and advances the cursor."""
    history = [{"role": "user", "content": "hello"}]
    engine._on_post_llm_call(
        session_id="sess-A",
        user_message="hello",
        assistant_response="",
        conversation_history=history,
        model="claude-haiku",
        platform="anthropic",
    )
    # Cursor advanced.
    assert engine._last_seen_message_idx["sess-A"] == 1
    # Message landed in the DB.
    conv = engine._conversation_store.get_conversation_by_session_id("sess-A")
    assert conv is not None
    msgs = engine._conversation_store.get_messages(conv.conversation_id)
    assert len(msgs) == 1
    assert msgs[0].role == "user"
    assert msgs[0].content == "hello"


@_skip_no_extension_loading
def test_user_plus_assistant_turn(engine: LCMEngine) -> None:
    """A user+assistant turn ingests both messages."""
    history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]
    engine._on_post_llm_call(
        session_id="sess-A",
        conversation_history=history,
    )
    assert engine._last_seen_message_idx["sess-A"] == 2
    conv = engine._conversation_store.get_conversation_by_session_id("sess-A")
    msgs = engine._conversation_store.get_messages(conv.conversation_id)
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert [m.content for m in msgs] == ["hello", "hi there"]


@_skip_no_extension_loading
def test_multi_message_tool_turn(engine: LCMEngine) -> None:
    """A user → multiple tool calls → final assistant turn ingests all 5."""
    history = [
        {"role": "user", "content": "do the thing"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call-1",
                    "name": "search",
                    "input": {"q": "x"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": "result-1",
        },
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call-2",
                    "name": "fetch",
                    "input": {"url": "y"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-2",
            "content": "result-2",
        },
        {"role": "assistant", "content": "done"},
    ]
    engine._on_post_llm_call(
        session_id="sess-A",
        conversation_history=history,
    )
    assert engine._last_seen_message_idx["sess-A"] == 6
    conv = engine._conversation_store.get_conversation_by_session_id("sess-A")
    msgs = engine._conversation_store.get_messages(conv.conversation_id)
    assert len(msgs) == 6
    # ``tool`` role passes through (toolResult → tool collapse in DB).
    assert [m.role for m in msgs] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
    ]


# ---------------------------------------------------------------------------
# Idempotency — re-running with the same history is a no-op
# ---------------------------------------------------------------------------


@_skip_no_extension_loading
def test_idempotent_replay_same_history(engine: LCMEngine) -> None:
    """Calling the hook twice with the same history doesn't double-write.

    Spec AC: "Re-running the hook with the same conversation_history is
    a no-op (idempotent)."

    On the second call the diff returns an empty window (cursor already
    at ``len(history)``), so we hit the fast-path no-op path. The
    identity_hash UNIQUE constraint mentioned in ADR-009 §"Identity
    hash invariant" is a v0.2 schema enhancement — not yet on the
    table at v0.1. The cursor advance IS the dedup mechanism for the
    in-process hook path at v0.1 (per ADR-009 §Decision "Option B
    primary path").
    """
    history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]
    engine._on_post_llm_call(session_id="sess-A", conversation_history=history)
    engine._on_post_llm_call(session_id="sess-A", conversation_history=history)
    # Same row count after two calls — second call hit the no-new-
    # messages fast-path.
    conv = engine._conversation_store.get_conversation_by_session_id("sess-A")
    msgs = engine._conversation_store.get_messages(conv.conversation_id)
    assert len(msgs) == 2


@_skip_no_extension_loading
def test_subsequent_call_with_no_new_messages_is_no_op(
    engine: LCMEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """The cursor at len(history) means the next call is a no-op fast-path."""
    history = [{"role": "user", "content": "first"}]
    engine._on_post_llm_call(session_id="sess-A", conversation_history=history)
    assert engine._last_seen_message_idx["sess-A"] == 1
    # Second call with same history — no new messages.
    import logging

    with caplog.at_level(logging.DEBUG, logger="lossless_hermes.engine.ingest"):
        engine._on_post_llm_call(session_id="sess-A", conversation_history=history)
    # No ingest log fired; the "no new messages" debug breadcrumb did.
    assert any("no new messages" in rec.getMessage() for rec in caplog.records), [
        r.getMessage() for r in caplog.records
    ]


@_skip_no_extension_loading
def test_empty_conversation_history_is_no_op(engine: LCMEngine) -> None:
    """Empty history → no ingest, no cursor mutation."""
    engine._on_post_llm_call(session_id="sess-A", conversation_history=[])
    assert "sess-A" not in engine._last_seen_message_idx
    # The conversation row may or may not exist depending on whether
    # ``get_or_create_conversation`` was called — at 03-02 it ISN'T
    # (we early-return before the lock acquisition).
    conv = engine._conversation_store.get_conversation_by_session_id("sess-A")
    assert conv is None


@_skip_no_extension_loading
def test_none_conversation_history_is_no_op(engine: LCMEngine) -> None:
    """``None`` conversation_history is treated as empty (no raise)."""
    engine._on_post_llm_call(session_id="sess-A", conversation_history=None)
    assert "sess-A" not in engine._last_seen_message_idx


# ---------------------------------------------------------------------------
# Cursor mechanics — diff against last_seen_message_idx
# ---------------------------------------------------------------------------


@_skip_no_extension_loading
def test_first_call_with_last_seen_zero_ingests_all(engine: LCMEngine) -> None:
    """First call with no recorded cursor ingests every history entry."""
    assert "sess-A" not in engine._last_seen_message_idx
    history = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
        {"role": "user", "content": "third"},
    ]
    engine._on_post_llm_call(session_id="sess-A", conversation_history=history)
    assert engine._last_seen_message_idx["sess-A"] == 3
    conv = engine._conversation_store.get_conversation_by_session_id("sess-A")
    assert len(engine._conversation_store.get_messages(conv.conversation_id)) == 3


@_skip_no_extension_loading
def test_subsequent_call_only_ingests_delta(engine: LCMEngine) -> None:
    """Calls 2..N ingest only the new tail past the cursor."""
    # Turn 1.
    history_t1 = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
    ]
    engine._on_post_llm_call(session_id="sess-A", conversation_history=history_t1)

    # Turn 2: same prefix + 2 new messages.
    history_t2 = history_t1 + [
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
    ]
    engine._on_post_llm_call(session_id="sess-A", conversation_history=history_t2)

    assert engine._last_seen_message_idx["sess-A"] == 4
    conv = engine._conversation_store.get_conversation_by_session_id("sess-A")
    msgs = engine._conversation_store.get_messages(conv.conversation_id)
    assert [m.content for m in msgs] == ["u1", "a1", "u2", "a2"]


@_skip_no_extension_loading
def test_on_session_reset_clears_cursor(engine: LCMEngine) -> None:
    """``on_session_reset`` clears ``_last_seen_message_idx`` per ADR-009."""
    history = [{"role": "user", "content": "hi"}]
    engine._on_post_llm_call(session_id="sess-A", conversation_history=history)
    assert engine._last_seen_message_idx["sess-A"] == 1

    engine.on_session_reset()
    assert "sess-A" not in engine._last_seen_message_idx


# ---------------------------------------------------------------------------
# Skip ladder — TS engine.ts:5906-5938 + persistable-role gate
# ---------------------------------------------------------------------------


@_skip_no_extension_loading
def test_failed_empty_assistant_is_skipped(engine: LCMEngine) -> None:
    """Assistant w/ ``stopReason=error|aborted`` + empty content → not ingested.

    Spec AC: "Assistant messages with ``stopReason=error|aborted`` and
    empty content are dropped (regression: prevents retry pollution loop)."
    """
    history = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": "",
            "stopReason": "error",
        },
        {"role": "user", "content": "retry"},
    ]
    engine._on_post_llm_call(session_id="sess-A", conversation_history=history)
    # Cursor still advances (idempotency) but only the 2 valid rows landed.
    assert engine._last_seen_message_idx["sess-A"] == 3
    conv = engine._conversation_store.get_conversation_by_session_id("sess-A")
    msgs = engine._conversation_store.get_messages(conv.conversation_id)
    assert [m.content for m in msgs] == ["go", "retry"]


@_skip_no_extension_loading
def test_failed_empty_assistant_aborted_variant_is_skipped(
    engine: LCMEngine,
) -> None:
    """``stop_reason: aborted`` is the OpenAI-Codex variant — also skipped."""
    history = [
        {
            "role": "assistant",
            "content": [],
            "stop_reason": "aborted",
        },
    ]
    engine._on_post_llm_call(session_id="sess-A", conversation_history=history)
    # No messages landed.
    conv = engine._conversation_store.get_conversation_by_session_id("sess-A")
    if conv is not None:
        # Conversation row may have been auto-created; just verify no
        # messages landed.
        assert engine._conversation_store.get_messages(conv.conversation_id) == []


@_skip_no_extension_loading
def test_assistant_error_with_nonempty_content_is_kept(engine: LCMEngine) -> None:
    """Error stopReason + non-empty content is NOT skipped (still useful).

    Only the empty-content variant is the retry-pollution risk.
    """
    history = [
        {
            "role": "assistant",
            "content": "I tried but failed",
            "stopReason": "error",
        }
    ]
    engine._on_post_llm_call(session_id="sess-A", conversation_history=history)
    conv = engine._conversation_store.get_conversation_by_session_id("sess-A")
    assert conv is not None
    msgs = engine._conversation_store.get_messages(conv.conversation_id)
    assert len(msgs) == 1


@_skip_no_extension_loading
def test_unknown_role_is_skipped(engine: LCMEngine) -> None:
    """Roles outside the persistable set are silently dropped."""
    history = [
        {"role": "frobnicator", "content": "weird"},
        {"role": "user", "content": "actual"},
    ]
    engine._on_post_llm_call(session_id="sess-A", conversation_history=history)
    conv = engine._conversation_store.get_conversation_by_session_id("sess-A")
    msgs = engine._conversation_store.get_messages(conv.conversation_id)
    assert len(msgs) == 1
    assert msgs[0].content == "actual"


@_skip_no_extension_loading
def test_message_parts_are_written(engine: LCMEngine) -> None:
    """Each ingested message has a corresponding ``message_parts`` row."""
    history = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "thinking..."},
                {
                    "type": "tool_use",
                    "id": "call-1",
                    "name": "search",
                    "input": {"q": "x"},
                },
            ],
        },
    ]
    engine._on_post_llm_call(session_id="sess-A", conversation_history=history)
    conv = engine._conversation_store.get_conversation_by_session_id("sess-A")
    msgs = engine._conversation_store.get_messages(conv.conversation_id)
    # First message: 1 text part.
    parts_0 = engine._conversation_store.get_message_parts(msgs[0].message_id)
    assert len(parts_0) == 1
    assert parts_0[0].part_type == "text"
    # Second message: 2 parts (text + tool).
    parts_1 = engine._conversation_store.get_message_parts(msgs[1].message_id)
    assert len(parts_1) == 2
    assert parts_1[0].part_type == "text"
    assert parts_1[1].part_type == "tool"
    assert parts_1[1].tool_call_id == "call-1"
    assert parts_1[1].tool_name == "search"


# ---------------------------------------------------------------------------
# Context items wiring
# ---------------------------------------------------------------------------


@_skip_no_extension_loading
def test_ingested_message_appears_in_context_items(engine: LCMEngine) -> None:
    """Each ingest path appends a ``context_items`` row (assembler input).

    Queries ``context_items`` directly (not via
    :meth:`SummaryStore.get_context_items`) because that store's reader
    methods require ``conn.row_factory = sqlite3.Row``, which is set
    by the store's own test fixture but not by :func:`open_lcm_db` at
    v0.1. The schema-level invariant (one ``context_items`` row per
    ingested message) is the load-bearing assertion here; the row-
    factory plumbing is a Epic-01 follow-up.
    """
    history = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
    ]
    engine._on_post_llm_call(session_id="sess-A", conversation_history=history)
    conv = engine._conversation_store.get_conversation_by_session_id("sess-A")
    # Direct SQL — avoid the SummaryStore reader-API row_factory dep.
    rows = engine._db.execute(
        "SELECT item_type, message_id FROM context_items WHERE conversation_id = ? ORDER BY ordinal",
        (conv.conversation_id,),
    ).fetchall()
    assert len(rows) == 2
    assert all(row[0] == "message" for row in rows)


# ---------------------------------------------------------------------------
# Atomic transaction — Wave-4 P0 fix invariant
# ---------------------------------------------------------------------------


@_skip_no_extension_loading
def test_transaction_rollback_leaves_no_orphan_rows(
    engine: LCMEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If ``create_message_parts`` throws AFTER ``create_message``, the
    transaction rolls back so no orphan ``messages`` row survives.

    This is the Wave-4 P0 invariant — without ``BEGIN IMMEDIATE``
    wrapping the three-write sequence, a failure mid-sequence would
    leave a message row with no parts (and the assembler would emit a
    malformed turn).
    """
    # Inject a failure into create_message_parts.
    real_create_parts = engine._conversation_store.create_message_parts

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated mid-ingest failure")

    monkeypatch.setattr(engine._conversation_store, "create_message_parts", _boom)

    history = [{"role": "user", "content": "doomed"}]
    # The hook's observer-only contract swallows the exception; the
    # DB state is the load-bearing assertion.
    engine._on_post_llm_call(session_id="sess-A", conversation_history=history)

    # Restore real implementation so the rest of the assertions work.
    monkeypatch.setattr(engine._conversation_store, "create_message_parts", real_create_parts)

    # Conversation row exists (created OUTSIDE the txn per spec), but
    # NO message row landed — the txn rolled back the ``create_message``
    # insert when ``create_message_parts`` threw.
    conv = engine._conversation_store.get_conversation_by_session_id("sess-A")
    assert conv is not None
    msgs = engine._conversation_store.get_messages(conv.conversation_id)
    assert msgs == [], f"Wave-4 atomic-txn invariant violated: orphan rows remained: {msgs}"
    # Cursor was NOT advanced (ingest_count was 0 → cursor stays).
    assert "sess-A" not in engine._last_seen_message_idx


@_skip_no_extension_loading
def test_transaction_rollback_on_append_context_message_failure(
    engine: LCMEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If ``append_context_message`` throws (the 3rd write), the
    message + parts rows roll back too.

    This catches the failure mode "message persisted but invisible to
    assembler → permanent context gap" (TS engine.ts:6032).
    """
    real_append = engine._summary_store.append_context_message

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated append failure")

    monkeypatch.setattr(engine._summary_store, "append_context_message", _boom)

    history = [{"role": "user", "content": "doomed"}]
    engine._on_post_llm_call(session_id="sess-A", conversation_history=history)

    monkeypatch.setattr(engine._summary_store, "append_context_message", real_append)

    conv = engine._conversation_store.get_conversation_by_session_id("sess-A")
    assert conv is not None
    msgs = engine._conversation_store.get_messages(conv.conversation_id)
    assert msgs == [], f"Wave-4 atomic-txn invariant violated: orphan rows remained: {msgs}"


# ---------------------------------------------------------------------------
# Cross-session durability — issue #144 P0 regression
# ---------------------------------------------------------------------------
#
# These tests exercise the durability property a within-session
# write-then-read test cannot: ingest through the real engine path,
# CLOSE the connection, then reopen ``lcm.db`` on a FRESH connection and
# assert the rows are still there.
#
# The pre-#144 bug: ``open_lcm_db`` opened the stdlib connection with
# Python's default ``isolation_level=""``, which silently opens an
# implicit deferred transaction on the first DML (the bare ``INSERT`` in
# ``ConversationStore.create_conversation``). Nothing on the ingest path
# ever committed that implicit transaction, so ``conn.close()`` rolled
# the entire session back — a fresh reopen showed 0 rows. The fix opens
# the connection with ``isolation_level=None`` (autocommit /
# explicit-transactions). 4,070 tests passed pre-fix because none did a
# close-then-reopen.


def _count_rows(db_file: Path, table: str) -> int:
    """Open a fresh stdlib connection to ``db_file`` and count ``table`` rows.

    The fresh connection is the load-bearing part: it does not share the
    engine's connection or its (pre-fix) uncommitted implicit transaction,
    so it sees only what was durably committed to the file.
    """
    fresh = sqlite3.connect(str(db_file))
    try:
        return int(fresh.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608
    finally:
        fresh.close()


@_skip_no_extension_loading
def test_ingested_data_survives_connection_close(tmp_home: Path) -> None:
    """Issue #144: a single-turn ingest is durable across ``on_session_end``.

    Ingest one user+assistant turn, end the session (which calls
    ``close_lcm_db`` → ``conn.close()``), then reopen ``lcm.db`` on a
    fresh connection. The conversation row + both message rows must still
    be present. Pre-#144 this reopened to 0 rows.
    """
    db_file = tmp_home / ".hermes" / "lossless-hermes" / "lcm.db"

    eng = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    eng.on_session_start("durable-sess")
    eng._on_post_llm_call(
        session_id="durable-sess",
        conversation_history=[
            {"role": "user", "content": "will I survive a close?"},
            {"role": "assistant", "content": "yes, after the #144 fix"},
        ],
    )
    # No write transaction may be left dangling at close time — a True
    # here is the exact pre-#144 symptom (uncommitted implicit txn).
    assert eng._db is not None
    assert eng._db.in_transaction is False, (
        "issue #144: an uncommitted transaction is open at close time — "
        "ingested data will roll back on conn.close()"
    )
    eng.on_session_end("durable-sess", [])

    # Fresh connection — sees only durably-committed data.
    assert _count_rows(db_file, "conversations") == 1, (
        "issue #144: the conversation row did not survive connection close"
    )
    assert _count_rows(db_file, "messages") == 2, (
        "issue #144: ingested messages did not survive connection close"
    )


@_skip_no_extension_loading
def test_multi_turn_ingest_survives_connection_close(tmp_home: Path) -> None:
    """Issue #144: a multi-turn conversation is durable across close.

    Turn 1 creates the conversation (via ``create_conversation``'s bare
    INSERT — the statement that opened the uncommitted implicit txn
    pre-fix); turns 2-3 append to it. After ``on_session_end`` a fresh
    reopen must show the conversation row + all 6 message rows, in order.
    """
    db_file = tmp_home / ".hermes" / "lossless-hermes" / "lcm.db"

    eng = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    eng.on_session_start("multi-sess")

    # Turn 1 — creates the conversation row.
    eng._on_post_llm_call(
        session_id="multi-sess",
        conversation_history=[
            {"role": "user", "content": "turn 1 user"},
            {"role": "assistant", "content": "turn 1 assistant"},
        ],
    )
    # Turns 2-3 — append to the existing conversation. The full history
    # is replayed each turn (Hermes hook contract); the engine diffs
    # against its cursor and ingests only the delta.
    eng._on_post_llm_call(
        session_id="multi-sess",
        conversation_history=[
            {"role": "user", "content": "turn 1 user"},
            {"role": "assistant", "content": "turn 1 assistant"},
            {"role": "user", "content": "turn 2 user"},
            {"role": "assistant", "content": "turn 2 assistant"},
            {"role": "user", "content": "turn 3 user"},
            {"role": "assistant", "content": "turn 3 assistant"},
        ],
    )
    conv_id = eng._conversation_store.get_conversation_by_session_id("multi-sess").conversation_id
    eng.on_session_end("multi-sess", [])

    # Fresh connection — assert the conversation + every message row and
    # its ordering survived.
    assert _count_rows(db_file, "conversations") == 1
    fresh = sqlite3.connect(str(db_file))
    try:
        rows = fresh.execute(
            "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY seq",
            (conv_id,),
        ).fetchall()
    finally:
        fresh.close()
    assert [r[0] for r in rows] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
    ], f"issue #144: multi-turn messages did not survive close: {rows}"
    assert [r[1] for r in rows] == [
        "turn 1 user",
        "turn 1 assistant",
        "turn 2 user",
        "turn 2 assistant",
        "turn 3 user",
        "turn 3 assistant",
    ]


@_skip_no_extension_loading
def test_ingest_data_survives_reopen_via_new_engine(tmp_home: Path) -> None:
    """Issue #144: a second ``LCMEngine`` reads the prior session's data.

    The end-to-end durability contract: session A ingests + closes, then
    a brand-new engine instance on the same ``hermes_home`` runs
    ``on_session_start`` (reopening the same ``lcm.db``) and reads the
    rows back through the normal store API. This is the real
    restart-survives-data scenario the bug broke.
    """
    # Session A — ingest and close.
    eng_a = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    eng_a.on_session_start("sess-A")
    eng_a._on_post_llm_call(
        session_id="sess-A",
        conversation_history=[
            {"role": "user", "content": "persisted across restart"},
        ],
    )
    eng_a.on_session_end("sess-A", [])

    # Session B — a fresh engine reopens the same DB and reads it back.
    eng_b = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    eng_b.on_session_start("sess-B")
    try:
        conv = eng_b._conversation_store.get_conversation_by_session_id("sess-A")
        assert conv is not None, "issue #144: conversation from a prior session was lost on close"
        msgs = eng_b._conversation_store.get_messages(conv.conversation_id)
        assert [m.content for m in msgs] == ["persisted across restart"], (
            "issue #144: messages from a prior session were lost on close"
        )
    finally:
        eng_b.on_session_end("sess-B", [])


# ---------------------------------------------------------------------------
# Per-session lock — acquisition, isolation, cross-session parallelism
# ---------------------------------------------------------------------------


@_skip_no_extension_loading
def test_lock_is_held_during_ingest(engine: LCMEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-session sync lock is acquired around the ingest critical section.

    Wraps the entire registry in a thin spy proxy (the registry has
    ``__slots__`` so we can't ``setattr`` on it directly).
    """
    from contextlib import contextmanager

    acquire_calls: list[str] = []
    real_registry = engine._session_locks

    class _SpyRegistry:
        """Forwarding proxy that records ``acquire_sync`` calls."""

        def __init__(self, target):
            self._target = target

        @contextmanager
        def acquire_sync(self, session_id: str):
            acquire_calls.append(session_id)
            with self._target.acquire_sync(session_id):
                yield

        def __getattr__(self, name):
            return getattr(self._target, name)

    monkeypatch.setattr(engine, "_session_locks", _SpyRegistry(real_registry))

    history = [{"role": "user", "content": "hi"}]
    engine._on_post_llm_call(session_id="sess-A", conversation_history=history)
    assert acquire_calls == ["sess-A"]


@_skip_no_extension_loading
def test_distinct_sessions_use_distinct_locks(engine: LCMEngine) -> None:
    """Two distinct session_ids get independent per-session locks.

    Verified by acquiring one session's lock manually and confirming the
    other session can still ingest.
    """
    with engine._session_locks.acquire_sync("sess-A"):
        # Inside the lock for sess-A, we should still be able to ingest
        # under sess-B (different session, different lock).
        history = [{"role": "user", "content": "hi-B"}]
        engine._on_post_llm_call(session_id="sess-B", conversation_history=history)
        # ingest landed under sess-B.
        conv_b = engine._conversation_store.get_conversation_by_session_id("sess-B")
        assert conv_b is not None
        assert len(engine._conversation_store.get_messages(conv_b.conversation_id)) == 1


# ---------------------------------------------------------------------------
# Observer-only contract — exceptions never leak from the hook
# ---------------------------------------------------------------------------


@_skip_no_extension_loading
def test_exception_in_store_does_not_raise(
    engine: LCMEngine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Any store-side exception is caught + logged; ``None`` is returned.

    Per ``docs/reference/hermes-hooks.md`` line 92 and ADR-009 §
    Consequences, observer-only hooks MUST NOT raise. The body catches
    every exception, logs it, and returns ``None``.
    """
    import logging

    def _boom(*args, **kwargs):
        raise RuntimeError("boom from get_or_create")

    monkeypatch.setattr(engine._conversation_store, "get_or_create_conversation", _boom)

    history = [{"role": "user", "content": "trigger"}]
    with caplog.at_level(logging.ERROR, logger="lossless_hermes.engine.ingest"):
        result = engine._on_post_llm_call(session_id="sess-A", conversation_history=history)
    assert result is None
    # The error was logged.
    assert any(
        "ingest" in rec.getMessage().lower() and "failed" in rec.getMessage().lower()
        for rec in caplog.records
    ), [r.getMessage() for r in caplog.records]


# ---------------------------------------------------------------------------
# Hermes-shape-compat: forward-compat kwargs + empty session_id
# ---------------------------------------------------------------------------


@_skip_no_extension_loading
def test_empty_session_id_short_circuits(engine: LCMEngine) -> None:
    """Empty session_id → no DB writes, no exception.

    Defensive: a malformed hook fire (empty session_id) must not blow up
    the conversation_store path.
    """
    engine._on_post_llm_call(session_id="", conversation_history=[{"role": "user", "content": "x"}])
    # No conversation row was written.
    assert engine._conversation_store.list_active_conversations() == []


@_skip_no_extension_loading
def test_extra_kwargs_are_tolerated(engine: LCMEngine) -> None:
    """Forward-compat: future Hermes kwargs do not break the hook."""
    history = [{"role": "user", "content": "hi"}]
    engine._on_post_llm_call(
        session_id="sess-A",
        conversation_history=history,
        # Future-only kwargs
        future_added_in_hermes_v999="ignored",
        another_thing=42,
    )
    assert engine._last_seen_message_idx["sess-A"] == 1


# ---------------------------------------------------------------------------
# Session-filter gates — ignore + stateless patterns
# ---------------------------------------------------------------------------


@_skip_no_extension_loading
def test_ignored_session_pattern_skips_ingest(tmp_home: Path) -> None:
    """Sessions matching ``ignore_session_patterns`` skip the entire ingest pipeline."""
    cfg = LcmConfig(ignore_session_patterns=["^bench-"])
    eng = LCMEngine(hermes_home=tmp_home / ".hermes", config=cfg)
    eng.on_session_start("bench-001")
    try:
        history = [{"role": "user", "content": "ignored"}]
        eng._on_post_llm_call(session_id="bench-001", conversation_history=history)
        # No conversation row, no messages.
        assert eng._conversation_store.list_active_conversations() == []
        assert "bench-001" not in eng._last_seen_message_idx
    finally:
        eng.on_session_end("bench-001", [])


# ---------------------------------------------------------------------------
# Pre-bootstrap defense (stores not yet initialized)
# ---------------------------------------------------------------------------


def test_pre_bootstrap_hook_call_is_safe(tmp_home: Path) -> None:
    """A hook fire BEFORE ``on_session_start`` doesn't crash; just logs.

    Engine state: stores are ``None``. The hook degrades gracefully.
    """
    eng = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    assert eng._conversation_store is None  # pre-condition
    # Should NOT raise.
    result = eng._on_post_llm_call(
        session_id="sess-A",
        conversation_history=[{"role": "user", "content": "hi"}],
    )
    assert result is None
    # No state was mutated.
    assert "sess-A" not in eng._last_seen_message_idx


# ---------------------------------------------------------------------------
# Mock-based unit tests (no DB) — for the inner gate helpers
# ---------------------------------------------------------------------------


def test_should_ignore_session_no_patterns_is_fast_path(tmp_home: Path) -> None:
    """No patterns configured → fast-path ``False`` (no regex search)."""
    eng = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    # No patterns by default.
    assert eng.ignore_session_patterns == []
    assert eng._should_ignore_session(session_id="anything") is False


def test_should_ignore_session_matches_pattern(tmp_home: Path) -> None:
    """Compiled pattern that matches the session_id returns True."""
    cfg = LcmConfig(ignore_session_patterns=["^test-"])
    eng = LCMEngine(hermes_home=tmp_home / ".hermes", config=cfg)
    assert eng._should_ignore_session(session_id="test-1") is True
    assert eng._should_ignore_session(session_id="prod-1") is False


def test_should_ignore_session_session_key_preferred_over_id(tmp_home: Path) -> None:
    """Non-empty session_key takes precedence over session_id."""
    cfg = LcmConfig(ignore_session_patterns=["^key-"])
    eng = LCMEngine(hermes_home=tmp_home / ".hermes", config=cfg)
    assert eng._should_ignore_session(session_id="id-not-matching", session_key="key-x") is True
