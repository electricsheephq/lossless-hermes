"""Tests for :mod:`lossless_hermes.tools.describe` — ``lcm_describe`` tool.

Mirrors ``lossless-claw/test/lcm-describe-expand-flags.test.ts`` (415 LOC TS
→ ~350 LOC Python). Covers:

* ``expandChildren`` happy path — first-hop child summaries inline.
* ``expandChildren`` with suppression filter — raw count exposed.
* ``expandChildren`` capped at ``expandChildrenLimit`` (max 50).
* ``expandChildren`` omitted → no expansion section.
* ``expandMessages`` on leaf with ``expandMessagesOffset`` pagination.
* ``expandMessages`` filters suppressed messages.
* ``expandMessages`` on non-leaf (condensed) → ``not-leaf`` status, no
  messages.
* Delegated-grant redaction when ``remaining < base summary tokens``.
* Delegated-grant budget exhaustion (``resolved_token_cap == 0``) →
  refuses to expand.
* Grant-ledger consumption AFTER successful emit (Wave-9 P1).
* ``not found`` and ``not in scope`` error shapes.
* Output exceeds :data:`MAX_RESULT_CHARS` → truncation notice appended;
  regex ``truncated at ~\\d+ tokens to protect agent context`` matches
  (Wave-12 N3 pin).
* File-path branch with the same truncation semantics.
* No conversation scope → structured error.

Source pin: ``lossless-claw`` at commit ``1f07fbd`` on branch ``pr-613``.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterator, Optional

import pytest

from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.plugin import result_budget as _result_budget
from lossless_hermes.store.conversation import (
    ConversationStore,
    CreateConversationInput,
)
from lossless_hermes.store.summary import (
    CreateLargeFileInput,
    CreateSummaryInput,
    SummaryStore,
)
from lossless_hermes.tools.conversation_scope import LcmDependencies
from lossless_hermes.tools.describe import (
    LCM_DESCRIBE_SCHEMA,
    DescribeContext,
    handle_lcm_describe,
    set_grant_budget_lookup,
)


# ===========================================================================
# Fixtures
# ===========================================================================


@dataclass
class _Ctx:
    """Concrete :class:`DescribeContext` for tests."""

    conn: sqlite3.Connection
    summary_store: SummaryStore
    conversation_store: ConversationStore
    timezone: str
    max_expand_tokens: int


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    """In-memory SQLite with migrations + FK on + Row factory."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(conn, fts5_available=False, seed_default_prompts=False)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def conv_id(db: sqlite3.Connection) -> int:
    """Seeded conversation id with session_key=agent:main:main."""
    store = ConversationStore(db, fts5_available=False)
    rec = store.create_conversation(
        CreateConversationInput(
            session_id="s1",
            session_key="agent:main:main",
            title="t",
        ),
    )
    return rec.conversation_id


@pytest.fixture
def conv_store(db: sqlite3.Connection) -> ConversationStore:
    return ConversationStore(db, fts5_available=False)


@pytest.fixture
def summary_store(db: sqlite3.Connection) -> SummaryStore:
    return SummaryStore(db, fts5_available=False, trigram_tokenizer_available=False)


@pytest.fixture
def ctx(
    db: sqlite3.Connection,
    summary_store: SummaryStore,
    conv_store: ConversationStore,
) -> _Ctx:
    return _Ctx(
        conn=db,
        summary_store=summary_store,
        conversation_store=conv_store,
        timezone="UTC",
        max_expand_tokens=4000,
    )


@pytest.fixture
def deps() -> LcmDependencies:
    """Minimal :class:`LcmDependencies` for scope resolution."""
    return LcmDependencies(resolve_session_id_from_session_key=lambda _k: None)


@pytest.fixture(autouse=True)
def reset_grant_resolver() -> Iterator[None]:
    """Reset the grant-budget lookup / consumer to no-op defaults per test."""
    set_grant_budget_lookup(lookup=None, consumer=None)
    try:
        yield
    finally:
        set_grant_budget_lookup(lookup=None, consumer=None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_summary(
    store: SummaryStore,
    *,
    summary_id: str,
    conv_id: int,
    kind: str = "leaf",
    content: str = "x",
    token_count: int | None = None,
) -> None:
    store.insert_summary(
        CreateSummaryInput(
            summary_id=summary_id,
            conversation_id=conv_id,
            kind=kind,  # type: ignore[arg-type]
            content=content,
            token_count=token_count if token_count is not None else max(1, len(content) // 4),
        ),
    )


def _link_parent(conn: sqlite3.Connection, parent: str, child: str, ordinal: int = 0) -> None:
    conn.execute(
        "INSERT INTO summary_parents (summary_id, parent_summary_id, ordinal) VALUES (?, ?, ?)",
        (child, parent, ordinal),
    )


def _suppress(conn: sqlite3.Connection, summary_id: str) -> None:
    conn.execute(
        "UPDATE summaries SET suppressed_at = datetime('now') WHERE summary_id = ?",
        (summary_id,),
    )


def _insert_message(
    conn: sqlite3.Connection,
    *,
    message_id: int,
    conv_id: int,
    seq: int,
    role: str = "user",
    content: str = "msg",
    token_count: int | None = None,
    suppressed: bool = False,
) -> None:
    conn.execute(
        "INSERT INTO messages (message_id, conversation_id, seq, role, content, "
        "token_count, suppressed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            message_id,
            conv_id,
            seq,
            role,
            content,
            token_count if token_count is not None else max(1, len(content) // 4),
            "2026-05-14T00:00:00" if suppressed else None,
        ),
    )


def _link_summary_message(
    conn: sqlite3.Connection,
    *,
    summary_id: str,
    message_id: int,
    ordinal: int = 0,
) -> None:
    conn.execute(
        "INSERT INTO summary_messages (summary_id, message_id, ordinal) VALUES (?, ?, ?)",
        (summary_id, message_id, ordinal),
    )


def _call(
    args: dict[str, Any],
    *,
    ctx: _Ctx,
    deps: LcmDependencies,
    session_key: str = "agent:main:main",
    is_subagent: Optional[Any] = None,
    grant_resolver: Optional[Any] = None,
) -> dict[str, Any]:
    """Invoke :func:`handle_lcm_describe` and parse the JSON result."""
    raw = handle_lcm_describe(
        args,
        ctx=ctx,
        deps=deps,
        session_key=session_key,
        session_id=None,
        is_subagent_session=is_subagent,
        grant_id_resolver=grant_resolver,
    )
    return json.loads(raw)


# ===========================================================================
# Schema (sanity) — well-formedness is exercised by the registry test
# ===========================================================================


class TestSchema:
    """The schema is well-formed and lives in the registry."""

    def test_name_and_required(self) -> None:
        assert LCM_DESCRIBE_SCHEMA["name"] == "lcm_describe"
        params = LCM_DESCRIBE_SCHEMA["parameters"]
        assert params["required"] == ["id"]

    def test_optional_property_caps(self) -> None:
        """``expandChildrenLimit`` / ``expandMessagesLimit`` ≤ 50;
        ``expandMessagesOffset`` ≥ 0."""
        props = LCM_DESCRIBE_SCHEMA["parameters"]["properties"]
        assert props["expandChildrenLimit"]["minimum"] == 1
        assert props["expandChildrenLimit"]["maximum"] == 50
        assert props["expandMessagesLimit"]["minimum"] == 1
        assert props["expandMessagesLimit"]["maximum"] == 50
        assert props["expandMessagesOffset"]["minimum"] == 0
        assert props["tokenCap"]["minimum"] == 1

    def test_description_verbatim_marker(self) -> None:
        """The tool description carries the canonical Type-E prose."""
        desc = LCM_DESCRIBE_SCHEMA["description"]
        assert "PRIMARY tool for Type E queries" in desc
        assert (
            "sum_xxx for summaries, file_xxx for files"
            in (LCM_DESCRIBE_SCHEMA["parameters"]["properties"]["id"]["description"])
        )


# ===========================================================================
# Error paths
# ===========================================================================


class TestErrorPaths:
    """Failure-mode payloads match the TS source (and the issue AC)."""

    def test_not_found(self, ctx: _Ctx, deps: LcmDependencies, conv_id: int) -> None:
        del conv_id  # fixture seeds the conversation; the lookup is by id
        result = _call({"id": "sum_does_not_exist"}, ctx=ctx, deps=deps)
        assert result["error"] == "Not found: sum_does_not_exist"
        assert "sum_xxx" in result["hint"] and "file_xxx" in result["hint"]

    def test_not_in_scope_when_conversation_id_param_doesnt_match(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        conv_id: int,
    ) -> None:
        # Seed a summary in this conversation, but ask for it via a
        # different conversationId param. TS: returns "Not found in this
        # session scope: <id>".
        _insert_summary(
            ctx.summary_store,
            summary_id="sum_isolated",
            conv_id=conv_id,
            content="x",
        )
        result = _call(
            {"id": "sum_isolated", "conversationId": 9999},
            ctx=ctx,
            deps=deps,
            session_key="",
        )
        assert "Not found in this session scope" in result["error"]
        assert "allConversations=true" in result["hint"]

    def test_no_conversation_scope(self, ctx: _Ctx, deps: LcmDependencies) -> None:
        # No session_key, no session_id, no conversationId — scope resolver
        # returns the empty scope; handler refuses with the AC error string.
        result = _call(
            {"id": "anything"},
            ctx=ctx,
            deps=deps,
            session_key="",
        )
        assert "No LCM conversation found" in result["error"]
        assert "conversationId" in result["error"]
        assert "allConversations=true" in result["error"]


# ===========================================================================
# Summary path — expandChildren flag
# ===========================================================================


class TestExpandChildren:
    """Mirrors ``test/lcm-describe-expand-flags.test.ts`` — children."""

    def test_returns_first_hop_child_summaries_inline(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        conv_id: int,
    ) -> None:
        _insert_summary(
            ctx.summary_store,
            summary_id="sum_parent",
            conv_id=conv_id,
            kind="condensed",
            content="parent",
        )
        _insert_summary(
            ctx.summary_store,
            summary_id="sum_child_a",
            conv_id=conv_id,
            content="First child content with race-condition fix details",
        )
        _insert_summary(
            ctx.summary_store,
            summary_id="sum_child_b",
            conv_id=conv_id,
            content="Second child content with another concrete topic",
        )
        _link_parent(ctx.conn, "sum_parent", "sum_child_a", 0)
        _link_parent(ctx.conn, "sum_parent", "sum_child_b", 1)

        result = _call(
            {"id": "sum_parent", "conversationId": conv_id, "expandChildren": True},
            ctx=ctx,
            deps=deps,
        )
        assert result["type"] == "summary"
        assert "expanded children: 2/2" in result["text"]
        assert "First child content with race-condition fix details" in result["text"]
        assert "Second child content with another concrete topic" in result["text"]
        assert len(result["expansion"]["children"]) == 2
        assert result["expansion"]["childrenStatus"] == "ok"

    def test_filters_suppressed_children(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        conv_id: int,
    ) -> None:
        _insert_summary(
            ctx.summary_store,
            summary_id="sum_parent",
            conv_id=conv_id,
            kind="condensed",
            content="parent",
        )
        _insert_summary(
            ctx.summary_store,
            summary_id="sum_visible",
            conv_id=conv_id,
            content="visible child content",
        )
        _insert_summary(
            ctx.summary_store,
            summary_id="sum_suppressed",
            conv_id=conv_id,
            content="suppressed child content",
        )
        _suppress(ctx.conn, "sum_suppressed")
        _link_parent(ctx.conn, "sum_parent", "sum_visible", 0)
        _link_parent(ctx.conn, "sum_parent", "sum_suppressed", 1)

        result = _call(
            {"id": "sum_parent", "conversationId": conv_id, "expandChildren": True},
            ctx=ctx,
            deps=deps,
        )
        children = result["expansion"]["children"]
        assert len(children) == 1
        assert children[0]["summaryId"] == "sum_visible"
        # Header line exposes raw count (2) vs visible (1).
        assert "expansion (children): 1 of 2 raw" in result["text"]

    def test_respects_expandChildrenLimit_max_50(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        conv_id: int,
    ) -> None:
        _insert_summary(
            ctx.summary_store,
            summary_id="sum_parent",
            conv_id=conv_id,
            kind="condensed",
            content="parent",
        )
        for i in range(1, 61):
            _insert_summary(
                ctx.summary_store,
                summary_id=f"sum_c{i}",
                conv_id=conv_id,
                content=f"child {i}",
            )
            _link_parent(ctx.conn, "sum_parent", f"sum_c{i}", i)

        # Ask for 100; tool caps at 50.
        result = _call(
            {
                "id": "sum_parent",
                "conversationId": conv_id,
                "expandChildren": True,
                "expandChildrenLimit": 100,
            },
            ctx=ctx,
            deps=deps,
        )
        assert len(result["expansion"]["children"]) <= 50
        assert result["expansion"]["childrenStatus"] == "capped"

    def test_no_expand_when_flag_omitted(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        conv_id: int,
    ) -> None:
        _insert_summary(
            ctx.summary_store,
            summary_id="sum_parent",
            conv_id=conv_id,
            kind="condensed",
            content="parent",
        )
        _insert_summary(
            ctx.summary_store,
            summary_id="sum_child",
            conv_id=conv_id,
            content="child",
        )
        _link_parent(ctx.conn, "sum_parent", "sum_child", 0)

        result = _call(
            {"id": "sum_parent", "conversationId": conv_id},
            ctx=ctx,
            deps=deps,
        )
        assert result["expansion"]["children"] == []
        assert "expanded children" not in result["text"]


# ===========================================================================
# Summary path — expandMessages flag
# ===========================================================================


class TestExpandMessages:
    """Mirrors ``test/lcm-describe-expand-flags.test.ts`` — messages."""

    def test_returns_first_hop_messages_for_leaf(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        conv_id: int,
    ) -> None:
        _insert_summary(
            ctx.summary_store,
            summary_id="sum_leaf",
            conv_id=conv_id,
            content="leaf summary text",
        )
        _insert_message(
            ctx.conn,
            message_id=100,
            conv_id=conv_id,
            seq=0,
            content="First raw message verbatim",
            role="user",
        )
        _insert_message(
            ctx.conn,
            message_id=101,
            conv_id=conv_id,
            seq=1,
            content="Second raw message verbatim",
            role="assistant",
        )
        _link_summary_message(ctx.conn, summary_id="sum_leaf", message_id=100, ordinal=0)
        _link_summary_message(ctx.conn, summary_id="sum_leaf", message_id=101, ordinal=1)

        result = _call(
            {"id": "sum_leaf", "conversationId": conv_id, "expandMessages": True},
            ctx=ctx,
            deps=deps,
        )
        text = result["text"]
        assert "expanded source messages" in text
        assert "First raw message verbatim" in text
        assert "Second raw message verbatim" in text
        assert len(result["expansion"]["messages"]) == 2

    def test_filters_suppressed_messages(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        conv_id: int,
    ) -> None:
        _insert_summary(
            ctx.summary_store,
            summary_id="sum_leaf",
            conv_id=conv_id,
            content="leaf",
        )
        _insert_message(
            ctx.conn,
            message_id=100,
            conv_id=conv_id,
            seq=0,
            content="visible message",
        )
        _insert_message(
            ctx.conn,
            message_id=101,
            conv_id=conv_id,
            seq=1,
            content="suppressed message",
            suppressed=True,
        )
        _link_summary_message(ctx.conn, summary_id="sum_leaf", message_id=100, ordinal=0)
        _link_summary_message(ctx.conn, summary_id="sum_leaf", message_id=101, ordinal=1)

        result = _call(
            {"id": "sum_leaf", "conversationId": conv_id, "expandMessages": True},
            ctx=ctx,
            deps=deps,
        )
        messages = result["expansion"]["messages"]
        assert len(messages) == 1
        assert messages[0]["messageId"] == 100

    def test_no_messages_for_condensed(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        conv_id: int,
    ) -> None:
        _insert_summary(
            ctx.summary_store,
            summary_id="sum_condensed",
            conv_id=conv_id,
            kind="condensed",
            content="condensed text",
        )
        _insert_message(
            ctx.conn,
            message_id=100,
            conv_id=conv_id,
            seq=0,
            content="raw message",
        )
        _link_summary_message(ctx.conn, summary_id="sum_condensed", message_id=100, ordinal=0)

        result = _call(
            {"id": "sum_condensed", "conversationId": conv_id, "expandMessages": True},
            ctx=ctx,
            deps=deps,
        )
        assert result["expansion"]["messages"] == []
        assert result["expansion"]["messagesStatus"] == "not-leaf"

    def test_offset_pagination(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        conv_id: int,
    ) -> None:
        """``expandMessagesOffset`` paginates a long leaf."""
        _insert_summary(
            ctx.summary_store,
            summary_id="sum_long_leaf",
            conv_id=conv_id,
            content="leaf with many messages",
        )
        # Insert 5 messages, ask for limit=2 offset=2 → returns messages
        # 3 and 4 (1-indexed in the range label).
        for i in range(5):
            _insert_message(
                ctx.conn,
                message_id=200 + i,
                conv_id=conv_id,
                seq=i,
                content=f"msg-{i}",
            )
            _link_summary_message(
                ctx.conn,
                summary_id="sum_long_leaf",
                message_id=200 + i,
                ordinal=i,
            )
        result = _call(
            {
                "id": "sum_long_leaf",
                "conversationId": conv_id,
                "expandMessages": True,
                "expandMessagesLimit": 2,
                "expandMessagesOffset": 2,
            },
            ctx=ctx,
            deps=deps,
        )
        messages = result["expansion"]["messages"]
        assert len(messages) == 2
        assert messages[0]["messageId"] == 202
        assert messages[1]["messageId"] == 203
        # 5 total, offset 2 + 2 returned = 4; 1 more remains.
        assert "1 more after this window" in result["text"]
        assert "expandMessagesOffset=4" in result["text"]
        assert result["expansion"]["messagesStatus"] == "capped"


# ===========================================================================
# Delegated-grant: redaction + budget exhaustion + ledger consumption
# ===========================================================================


class TestDelegatedGrantRedaction:
    """Wave-11 + Wave-9 + Wave-4 delegated-grant invariants."""

    def test_redacts_content_when_remaining_below_base_tokens(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        conv_id: int,
    ) -> None:
        """Wave-11 P1: base summary tokens > remaining budget → REDACT before emit."""
        # Insert a leaf with 500 tokens of content.
        _insert_summary(
            ctx.summary_store,
            summary_id="sum_big",
            conv_id=conv_id,
            content="x" * 2000,  # ~500 tokens at 4 chars/token
            token_count=500,
        )

        # Configure the grant lookup to return remaining=100 — well below
        # the 500 tokens the leaf has.
        consumed_log: list[tuple[str, int]] = []
        set_grant_budget_lookup(
            lookup=lambda gid: 100 if gid == "grant-1" else None,
            consumer=lambda gid, n: consumed_log.append((gid, n)),
        )

        result = _call(
            {"id": "sum_big", "conversationId": conv_id},
            ctx=ctx,
            deps=deps,
            session_key="agent:subagent:abc",
            is_subagent=lambda k: ":subagent:" in k,
            grant_resolver=lambda _k: "grant-1",
        )
        # Content is redacted, NOT emitted.
        assert "REDACTED" in result["text"]
        # The original 2000-x payload must not appear in the rendered text.
        assert "x" * 2000 not in result["text"]
        # Ledger: base tokens charge is 0 because base was redacted.
        # expandedChildren / expandedMessages are also 0. Net: no consume
        # call OR consume(0) → handler skips when sum is 0.
        assert all(amount == 0 for _, amount in consumed_log)

    def test_budget_exhausted_refuses_expansion(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        conv_id: int,
    ) -> None:
        """Wave-4 P1: when resolved_token_cap == 0, refuse to expand."""
        _insert_summary(
            ctx.summary_store,
            summary_id="sum_parent",
            conv_id=conv_id,
            kind="condensed",
            content="parent",
            token_count=10,  # small so base isn't over budget
        )
        _insert_summary(
            ctx.summary_store,
            summary_id="sum_child",
            conv_id=conv_id,
            content="child content",
        )
        _link_parent(ctx.conn, "sum_parent", "sum_child", 0)

        # Remaining=0 → cap clamps to 0.
        set_grant_budget_lookup(lookup=lambda gid: 0 if gid == "grant-empty" else None)

        result = _call(
            {
                "id": "sum_parent",
                "conversationId": conv_id,
                "expandChildren": True,
            },
            ctx=ctx,
            deps=deps,
            session_key="agent:subagent:abc",
            is_subagent=lambda k: ":subagent:" in k,
            grant_resolver=lambda _k: "grant-empty",
        )
        # Wave-8 P1 distinct status string
        assert result["expansion"]["childrenStatus"] == "budget-exhausted"
        # Expansion array empty
        assert result["expansion"]["children"] == []
        # Status line in body
        assert "delegated grant has 0 tokens remaining" in result["text"]
        # Meta line surfaces the budget-exhausted banner
        assert "budget exhausted" in result["text"]

    def test_ledger_consumed_after_successful_emit(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        conv_id: int,
    ) -> None:
        """Wave-9 P1: charge grant ledger AFTER successful emit, summed
        across base + children + messages."""
        # Base + 1 child summary + 1 message linked to base
        _insert_summary(
            ctx.summary_store,
            summary_id="sum_leaf",
            conv_id=conv_id,
            content="leaf content",
            token_count=20,
        )
        _insert_summary(
            ctx.summary_store,
            summary_id="sum_unrelated_child",
            conv_id=conv_id,
            content="child content",
            token_count=15,
        )
        _link_parent(ctx.conn, "sum_leaf", "sum_unrelated_child", 0)
        _insert_message(
            ctx.conn,
            message_id=900,
            conv_id=conv_id,
            seq=0,
            content="msg text",
            token_count=8,
        )
        _link_summary_message(
            ctx.conn,
            summary_id="sum_leaf",
            message_id=900,
            ordinal=0,
        )

        consumed_log: list[tuple[str, int]] = []
        set_grant_budget_lookup(
            lookup=lambda gid: 1000 if gid == "grant-ok" else None,
            consumer=lambda gid, n: consumed_log.append((gid, n)),
        )
        _call(
            {
                "id": "sum_leaf",
                "conversationId": conv_id,
                "expandChildren": True,
                "expandMessages": True,
            },
            ctx=ctx,
            deps=deps,
            session_key="agent:subagent:abc",
            is_subagent=lambda k: ":subagent:" in k,
            grant_resolver=lambda _k: "grant-ok",
        )
        # Expect: base 20 + child 15 + msg 8 = 43.
        assert consumed_log == [("grant-ok", 43)]


# ===========================================================================
# Truncation — Wave-12 N3 regex pin
# ===========================================================================


class TestTruncation:
    """The truncation notice format is the Wave-12 N3 pinned regex."""

    def test_summary_truncated_when_lines_exceed_max_chars(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        conv_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Forcibly shrink MAX_RESULT_CHARS and verify the notice appears."""
        # Insert a summary whose content alone would overflow the cap.
        _insert_summary(
            ctx.summary_store,
            summary_id="sum_huge",
            conv_id=conv_id,
            content="LONG " * 500,  # ~2500 chars
            token_count=200,
        )
        # Cap small enough to force trim.
        monkeypatch.setattr(_result_budget, "MAX_RESULT_CHARS", 200)

        # Patch the cap used inside describe.py — _truncate_lines_to_cap
        # imports MAX_RESULT_CHARS at module load. Patch the name
        # imported into describe too.
        from lossless_hermes.tools import describe as _describe_mod

        monkeypatch.setattr(_describe_mod, "MAX_RESULT_CHARS", 200)

        result = _call(
            {"id": "sum_huge", "conversationId": conv_id},
            ctx=ctx,
            deps=deps,
        )
        assert result["truncated"] is True
        # Wave-12 N3 regex pin — the exact substring agents may
        # match for "did this tool truncate?" detection.
        assert re.search(
            r"truncated at ~\d+ tokens to protect agent context",
            result["text"],
        ), result["text"]
        # The tool-specific reason hint is included.
        assert "lower expandChildrenLimit / expandMessagesLimit" in result["text"]

    def test_file_truncated_with_same_regex(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        conv_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Insert a large file with an exploration summary that overflows
        # the (small) cap.
        ctx.summary_store.insert_large_file(
            CreateLargeFileInput(
                file_id="file_big",
                conversation_id=conv_id,
                storage_uri="file:///tmp/big.txt",
                file_name="big.txt",
                mime_type="text/plain",
                byte_size=999_999,
                exploration_summary="LONG " * 500,
            ),
        )
        from lossless_hermes.tools import describe as _describe_mod

        monkeypatch.setattr(_describe_mod, "MAX_RESULT_CHARS", 200)

        result = _call(
            {"id": "file_big", "conversationId": conv_id},
            ctx=ctx,
            deps=deps,
        )
        assert result["type"] == "file"
        assert result["truncated"] is True
        assert re.search(
            r"truncated at ~\d+ tokens to protect agent context",
            result["text"],
        )


# ===========================================================================
# File path
# ===========================================================================


class TestFilePath:
    """``file_xxx`` IDs emit file metadata + exploration summary."""

    def test_file_with_exploration_summary(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        conv_id: int,
    ) -> None:
        ctx.summary_store.insert_large_file(
            CreateLargeFileInput(
                file_id="file_alpha",
                conversation_id=conv_id,
                storage_uri="file:///tmp/alpha.txt",
                file_name="alpha.txt",
                mime_type="text/plain",
                byte_size=1024,
                exploration_summary="A 1024-byte file containing alpha text.",
            ),
        )
        result = _call(
            {"id": "file_alpha", "conversationId": conv_id},
            ctx=ctx,
            deps=deps,
        )
        assert result["type"] == "file"
        text = result["text"]
        assert "## LCM File: file_alpha" in text
        assert "**Name:** alpha.txt" in text
        assert "**Type:** text/plain" in text
        assert "**Size:** 1,024 bytes" in text
        assert "## Exploration Summary" in text
        assert "A 1024-byte file containing alpha text." in text
        assert result["truncated"] is False

    def test_file_without_exploration_summary(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        conv_id: int,
    ) -> None:
        ctx.summary_store.insert_large_file(
            CreateLargeFileInput(
                file_id="file_naked",
                conversation_id=conv_id,
                storage_uri="file:///tmp/naked.bin",
            ),
        )
        result = _call(
            {"id": "file_naked", "conversationId": conv_id},
            ctx=ctx,
            deps=deps,
        )
        assert "*No exploration summary available.*" in result["text"]
        assert "**Name:** (no name)" in result["text"]
        assert "**Type:** unknown" in result["text"]
