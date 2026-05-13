"""Tests for :mod:`lossless_hermes.tools.expand` — ``lcm_expand`` tool.

Mirrors ``lossless-claw/test/lcm-expand-tool.test.ts`` (496 LOC TS →
~400 LOC Python). Covers:

* Main-agent session key → refusal error (pinned prose).
* Sub-agent session, no grant → refusal error (pinned prose).
* Sub-agent session, valid grant → ``summaryIds`` direct entry path.
* Sub-agent session, valid grant → ``query`` grep-then-expand entry
  path.
* Empty ``query`` results → empty expansion result.
* Conversation scope errors propagate.
* ``maxDepth`` cap respected (passed through to orchestrator).
* ``tokenCap`` cap respected (passed through; truncation flag set).
* ``includeMessages=True`` hydrates leaf messages.
* Neither summaryIds nor query provided → structured error.
* ``default_is_subagent_session_key`` substring predicate works.
* Schema is well-formed, registered, name matches.

Per ADR-012 the delegated dispatch (``lcm_expand_query``) is deferred
to v2 — tests for the policy/observability branches in the TS source
(lines 264-302, 381-411) are out of scope.

Source pin: ``lossless-claw`` at commit ``1f07fbd`` on branch ``pr-613``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

import pytest

from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.store.conversation import (
    ConversationStore,
    CreateConversationInput,
)
from lossless_hermes.tools.conversation_scope import LcmDependencies
from lossless_hermes.tools.expand import (
    LCM_EXPAND_DESCRIPTION,
    LCM_EXPAND_SCHEMA,
    ExpansionResult,
    GrepResult,
    GrepSummaryMatch,
    default_is_subagent_session_key,
    handle_lcm_expand,
)


# ===========================================================================
# Fixtures: DB, stores, contexts, stubs
# ===========================================================================


@dataclass
class _ExpandCalls:
    """Records calls to the orchestrator stub for assertions."""

    args: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _StubOrchestrator:
    """Test stub satisfying :class:`ExpansionOrchestrator`.

    Records each ``expand`` invocation in ``calls.args`` and returns the
    pre-configured ``result`` (or a default-empty one).
    """

    result: ExpansionResult = field(default_factory=ExpansionResult)
    calls: _ExpandCalls = field(default_factory=_ExpandCalls)
    raise_exc: Optional[Exception] = None

    def expand(
        self,
        *,
        summary_ids: list[str],
        conversation_id: int,
        max_depth: Optional[int] = None,
        token_cap: Optional[int] = None,
        include_messages: bool = False,
    ) -> ExpansionResult:
        self.calls.args.append(
            {
                "summary_ids": summary_ids,
                "conversation_id": conversation_id,
                "max_depth": max_depth,
                "token_cap": token_cap,
                "include_messages": include_messages,
            },
        )
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.result


@dataclass
class _GrepCalls:
    """Records calls to the retrieval stub."""

    args: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _StubRetrieval:
    """Test stub satisfying :class:`Retrieval`."""

    result: GrepResult = field(default_factory=GrepResult)
    calls: _GrepCalls = field(default_factory=_GrepCalls)
    raise_exc: Optional[Exception] = None

    def grep(
        self,
        *,
        query: str,
        mode: str,
        scope: str,
        conversation_id: Optional[int],
    ) -> GrepResult:
        self.calls.args.append(
            {
                "query": query,
                "mode": mode,
                "scope": scope,
                "conversation_id": conversation_id,
            },
        )
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.result


@dataclass
class _Ctx:
    """Concrete :class:`ExpandContext` for tests."""

    conn: sqlite3.Connection
    conversation_store: ConversationStore
    orchestrator: _StubOrchestrator
    retrieval: _StubRetrieval


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
def conv_store(db: sqlite3.Connection) -> ConversationStore:
    return ConversationStore(db, fts5_available=False)


@pytest.fixture
def main_conv_id(conv_store: ConversationStore) -> int:
    """Seed a main-agent conversation."""
    rec = conv_store.create_conversation(
        CreateConversationInput(
            session_id="main-sess",
            session_key="agent:main:main",
            title="main",
        ),
    )
    return rec.conversation_id


@pytest.fixture
def subagent_conv_id(conv_store: ConversationStore) -> int:
    """Seed a sub-agent (delegated) conversation."""
    rec = conv_store.create_conversation(
        CreateConversationInput(
            session_id="subagent-sess",
            session_key="agent:main:subagent:foo",
            title="sub",
        ),
    )
    return rec.conversation_id


@pytest.fixture
def orchestrator() -> _StubOrchestrator:
    return _StubOrchestrator()


@pytest.fixture
def retrieval() -> _StubRetrieval:
    return _StubRetrieval()


@pytest.fixture
def ctx(
    db: sqlite3.Connection,
    conv_store: ConversationStore,
    orchestrator: _StubOrchestrator,
    retrieval: _StubRetrieval,
) -> _Ctx:
    return _Ctx(
        conn=db,
        conversation_store=conv_store,
        orchestrator=orchestrator,
        retrieval=retrieval,
    )


@pytest.fixture
def deps() -> LcmDependencies:
    return LcmDependencies(resolve_session_id_from_session_key=lambda _k: None)


def _grant(_session_key: str) -> Optional[str]:
    """Default grant resolver that always returns a valid grant id."""
    return "grant-1"


def _no_grant(_session_key: str) -> Optional[str]:
    """Grant resolver that always returns None."""
    return None


def _call(
    args: dict[str, Any],
    *,
    ctx: _Ctx,
    deps: LcmDependencies,
    session_key: str = "agent:main:subagent:foo",
    is_subagent: Optional[Any] = None,
    grant_resolver: Optional[Any] = _grant,
) -> dict[str, Any]:
    """Invoke :func:`handle_lcm_expand` and parse the JSON result."""
    raw = handle_lcm_expand(
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
# Schema
# ===========================================================================


class TestSchema:
    """The schema is well-formed and lives in the registry."""

    def test_name_and_required(self) -> None:
        assert LCM_EXPAND_SCHEMA["name"] == "lcm_expand"
        params = LCM_EXPAND_SCHEMA["parameters"]
        # Per the issue AC: required array is empty (runtime validates
        # at-least-one-of summaryIds / query).
        assert params["required"] == []

    def test_optional_properties_present(self) -> None:
        props = LCM_EXPAND_SCHEMA["parameters"]["properties"]
        for key in (
            "summaryIds",
            "query",
            "maxDepth",
            "tokenCap",
            "includeMessages",
            "conversationId",
            "allConversations",
        ):
            assert key in props, f"missing schema property {key!r}"

    def test_max_depth_minimum(self) -> None:
        assert LCM_EXPAND_SCHEMA["parameters"]["properties"]["maxDepth"]["minimum"] == 1

    def test_token_cap_minimum(self) -> None:
        assert LCM_EXPAND_SCHEMA["parameters"]["properties"]["tokenCap"]["minimum"] == 1

    def test_summary_ids_is_array_of_strings(self) -> None:
        prop = LCM_EXPAND_SCHEMA["parameters"]["properties"]["summaryIds"]
        assert prop["type"] == "array"
        assert prop["items"]["type"] == "string"

    def test_description_verbatim_marker(self) -> None:
        """The tool description carries the SUB-AGENT ONLY prose."""
        assert LCM_EXPAND_DESCRIPTION.startswith("SUB-AGENT ONLY.")
        assert "lcm_expand_query" in LCM_EXPAND_DESCRIPTION
        assert "lcm_describe with expandChildren/expandMessages flags" in LCM_EXPAND_DESCRIPTION


# ===========================================================================
# Default sub-agent predicate
# ===========================================================================


class TestDefaultIsSubagentPredicate:
    """``default_is_subagent_session_key`` substring match."""

    @pytest.mark.parametrize(
        "key",
        [
            "agent:main:subagent:foo",
            "agent:lcm:subagent:bar",
            "agent:main:subagent:nested:more",
            ":subagent:",  # bare substring
        ],
    )
    def test_returns_true_for_subagent_keys(self, key: str) -> None:
        assert default_is_subagent_session_key(key) is True

    @pytest.mark.parametrize(
        "key",
        [
            "agent:main:main",
            "agent:lcm:lcm",
            "",
            "subagent",  # no surrounding colons
            "agent:subagent_foo",  # underscore, not colon
        ],
    )
    def test_returns_false_for_main_keys(self, key: str) -> None:
        assert default_is_subagent_session_key(key) is False

    def test_handles_non_string_input(self) -> None:
        """Defensive against caller misuse — ``None`` / non-string → False."""
        assert default_is_subagent_session_key(None) is False  # type: ignore[arg-type]
        assert default_is_subagent_session_key(123) is False  # type: ignore[arg-type]


# ===========================================================================
# Main-agent refusal
# ===========================================================================


class TestMainAgentRefusal:
    """Main-agent sessions get a pinned structured error."""

    def test_main_agent_session_refused(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        main_conv_id: int,
    ) -> None:
        del main_conv_id  # conversation seeded for symmetry; gate fires first
        result = _call(
            {"summaryIds": ["sum_a"]},
            ctx=ctx,
            deps=deps,
            session_key="agent:main:main",
        )
        assert result["error"] == (
            "lcm_expand is only available in sub-agent sessions. Use "
            "lcm_expand_query to ask a focused question against expanded "
            "summaries, or lcm_describe/lcm_grep for lighter lookups."
        )
        # The gate fires BEFORE any orchestrator or retrieval call.
        assert ctx.orchestrator.calls.args == []
        assert ctx.retrieval.calls.args == []

    def test_main_agent_via_session_id_fallback_refused(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
    ) -> None:
        """When session_key is blank, falls through to session_id for the gate."""
        raw = handle_lcm_expand(
            {"summaryIds": ["sum_a"]},
            ctx=ctx,
            deps=deps,
            session_key=None,
            session_id="agent:main:main",  # not a subagent
            grant_id_resolver=_grant,
        )
        result = json.loads(raw)
        assert "only available in sub-agent sessions" in result["error"]

    def test_empty_session_key_refused(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
    ) -> None:
        """Empty session_key (anonymous caller) is treated as main-agent."""
        result = _call(
            {"summaryIds": ["sum_a"]},
            ctx=ctx,
            deps=deps,
            session_key="",
        )
        assert "only available in sub-agent sessions" in result["error"]

    def test_injected_predicate_overrides_default(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        subagent_conv_id: int,
    ) -> None:
        """A custom predicate can flip the gate logic (tests pass it in)."""
        del subagent_conv_id
        # Even though the session_key contains :subagent:, the custom
        # predicate says "never a subagent" → refused.
        result = _call(
            {"summaryIds": ["sum_a"]},
            ctx=ctx,
            deps=deps,
            session_key="agent:main:subagent:foo",
            is_subagent=lambda _k: False,
        )
        assert "only available in sub-agent sessions" in result["error"]


# ===========================================================================
# No-grant refusal
# ===========================================================================


class TestNoGrantRefusal:
    """Sub-agent session with no propagated grant → structured error."""

    def test_no_grant_resolver_refuses(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        subagent_conv_id: int,
    ) -> None:
        del subagent_conv_id
        result = _call(
            {"summaryIds": ["sum_a"]},
            ctx=ctx,
            deps=deps,
            grant_resolver=None,  # no resolver at all
        )
        assert result["error"] == (
            "Delegated expansion requires a valid grant. This sub-agent "
            "session has no propagated expansion grant."
        )

    def test_resolver_returning_none_refuses(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        subagent_conv_id: int,
    ) -> None:
        del subagent_conv_id
        result = _call(
            {"summaryIds": ["sum_a"]},
            ctx=ctx,
            deps=deps,
            grant_resolver=_no_grant,
        )
        assert "requires a valid grant" in result["error"]
        assert ctx.orchestrator.calls.args == []

    def test_resolver_returning_empty_string_refuses(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        subagent_conv_id: int,
    ) -> None:
        del subagent_conv_id
        result = _call(
            {"summaryIds": ["sum_a"]},
            ctx=ctx,
            deps=deps,
            grant_resolver=lambda _k: "",  # empty string → falsy
        )
        assert "requires a valid grant" in result["error"]


# ===========================================================================
# summaryIds direct entry path
# ===========================================================================


class TestSummaryIdsPath:
    """Direct expansion via the ``summaryIds`` shape."""

    def test_calls_orchestrator_with_summary_ids(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        subagent_conv_id: int,
    ) -> None:
        ctx.orchestrator.result = ExpansionResult(
            expansions=[
                {
                    "summaryId": "sum_a",
                    "children": [],
                    "messages": [],
                },
            ],
            cited_ids=["sum_a"],
            total_tokens=40,
            truncated=False,
        )
        result = _call(
            {
                "summaryIds": ["sum_a"],
                "conversationId": subagent_conv_id,
            },
            ctx=ctx,
            deps=deps,
        )
        assert len(ctx.orchestrator.calls.args) == 1
        call = ctx.orchestrator.calls.args[0]
        assert call["summary_ids"] == ["sum_a"]
        assert call["conversation_id"] == subagent_conv_id
        assert result["expansionCount"] == 1
        assert result["totalTokens"] == 40
        assert result["truncated"] is False
        assert result["citedIds"] == ["sum_a"]

    def test_passes_max_depth_to_orchestrator(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        subagent_conv_id: int,
    ) -> None:
        _call(
            {
                "summaryIds": ["sum_a"],
                "maxDepth": 5,
                "conversationId": subagent_conv_id,
            },
            ctx=ctx,
            deps=deps,
        )
        assert ctx.orchestrator.calls.args[0]["max_depth"] == 5

    def test_passes_token_cap_to_orchestrator(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        subagent_conv_id: int,
    ) -> None:
        _call(
            {
                "summaryIds": ["sum_a"],
                "tokenCap": 200,
                "conversationId": subagent_conv_id,
            },
            ctx=ctx,
            deps=deps,
        )
        assert ctx.orchestrator.calls.args[0]["token_cap"] == 200

    def test_passes_include_messages_to_orchestrator(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        subagent_conv_id: int,
    ) -> None:
        _call(
            {
                "summaryIds": ["sum_a"],
                "includeMessages": True,
                "conversationId": subagent_conv_id,
            },
            ctx=ctx,
            deps=deps,
        )
        assert ctx.orchestrator.calls.args[0]["include_messages"] is True

    def test_dedupes_summary_ids(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        subagent_conv_id: int,
    ) -> None:
        """Duplicate ``summaryIds`` entries are filtered preserving order."""
        _call(
            {
                "summaryIds": ["sum_a", "sum_b", "sum_a", "sum_c", "sum_b"],
                "conversationId": subagent_conv_id,
            },
            ctx=ctx,
            deps=deps,
        )
        assert ctx.orchestrator.calls.args[0]["summary_ids"] == ["sum_a", "sum_b", "sum_c"]

    def test_filters_empty_strings_from_summary_ids(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        subagent_conv_id: int,
    ) -> None:
        _call(
            {
                "summaryIds": ["sum_a", "  ", "", "sum_b"],
                "conversationId": subagent_conv_id,
            },
            ctx=ctx,
            deps=deps,
        )
        assert ctx.orchestrator.calls.args[0]["summary_ids"] == ["sum_a", "sum_b"]

    def test_truncated_flag_propagates(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        subagent_conv_id: int,
    ) -> None:
        ctx.orchestrator.result = ExpansionResult(
            expansions=[],
            cited_ids=[],
            total_tokens=999,
            truncated=True,
        )
        result = _call(
            {
                "summaryIds": ["sum_a"],
                "conversationId": subagent_conv_id,
            },
            ctx=ctx,
            deps=deps,
        )
        assert result["truncated"] is True
        assert "[Truncated: yes]" in result["text"]

    def test_orchestrator_value_error_returns_tool_error(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        subagent_conv_id: int,
    ) -> None:
        """A ValueError from the orchestrator becomes a tool-error payload."""
        ctx.orchestrator.raise_exc = ValueError("bad summary id")
        result = _call(
            {
                "summaryIds": ["sum_a"],
                "conversationId": subagent_conv_id,
            },
            ctx=ctx,
            deps=deps,
        )
        assert result["error"] == "bad summary id"


# ===========================================================================
# query grep-then-expand entry path
# ===========================================================================


class TestQueryPath:
    """``query`` shape — grep first, then expand top matches."""

    def test_grep_then_expand_top_matches(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        subagent_conv_id: int,
    ) -> None:
        ctx.retrieval.result = GrepResult(
            summaries=[
                GrepSummaryMatch(summary_id="sum_match_1"),
                GrepSummaryMatch(summary_id="sum_match_2"),
            ],
        )
        ctx.orchestrator.result = ExpansionResult(
            expansions=[
                {"summaryId": "sum_match_1", "children": [], "messages": []},
                {"summaryId": "sum_match_2", "children": [], "messages": []},
            ],
            cited_ids=["sum_match_1", "sum_match_2"],
            total_tokens=50,
        )
        result = _call(
            {
                "query": "auth issues",
                "conversationId": subagent_conv_id,
            },
            ctx=ctx,
            deps=deps,
        )
        # Retrieval was called with full_text mode + summaries scope.
        assert len(ctx.retrieval.calls.args) == 1
        grep_call = ctx.retrieval.calls.args[0]
        assert grep_call["query"] == "auth issues"
        assert grep_call["mode"] == "full_text"
        assert grep_call["scope"] == "summaries"
        assert grep_call["conversation_id"] == subagent_conv_id
        # Orchestrator was called with grep results.
        assert len(ctx.orchestrator.calls.args) == 1
        assert ctx.orchestrator.calls.args[0]["summary_ids"] == [
            "sum_match_1",
            "sum_match_2",
        ]
        # The query path forces include_messages=False (TS line 310).
        assert ctx.orchestrator.calls.args[0]["include_messages"] is False
        assert result["expansionCount"] == 2

    def test_query_with_no_grep_matches_returns_empty(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        subagent_conv_id: int,
    ) -> None:
        """Empty grep result → empty expansion, no orchestrator call."""
        ctx.retrieval.result = GrepResult(summaries=[])
        result = _call(
            {
                "query": "nothing matches",
                "conversationId": subagent_conv_id,
            },
            ctx=ctx,
            deps=deps,
        )
        assert len(ctx.retrieval.calls.args) == 1
        # The orchestrator is NOT called when grep returns no matches.
        assert ctx.orchestrator.calls.args == []
        assert result["expansionCount"] == 0
        assert result["citedIds"] == []
        assert result["totalTokens"] == 0
        assert result["truncated"] is False

    def test_query_path_ignores_summary_ids(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        subagent_conv_id: int,
    ) -> None:
        """Per the description: if query is set, summaryIds is ignored."""
        ctx.retrieval.result = GrepResult(
            summaries=[GrepSummaryMatch(summary_id="sum_from_grep")],
        )
        _call(
            {
                "query": "x",
                "summaryIds": ["sum_ignored"],
                "conversationId": subagent_conv_id,
            },
            ctx=ctx,
            deps=deps,
        )
        assert ctx.orchestrator.calls.args[0]["summary_ids"] == ["sum_from_grep"]

    def test_query_passes_max_depth_and_token_cap(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        subagent_conv_id: int,
    ) -> None:
        ctx.retrieval.result = GrepResult(
            summaries=[GrepSummaryMatch(summary_id="sum_x")],
        )
        _call(
            {
                "query": "x",
                "maxDepth": 7,
                "tokenCap": 999,
                "conversationId": subagent_conv_id,
            },
            ctx=ctx,
            deps=deps,
        )
        call = ctx.orchestrator.calls.args[0]
        assert call["max_depth"] == 7
        assert call["token_cap"] == 999

    def test_retrieval_value_error_returns_tool_error(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        subagent_conv_id: int,
    ) -> None:
        ctx.retrieval.raise_exc = ValueError("grep failed")
        result = _call(
            {
                "query": "x",
                "conversationId": subagent_conv_id,
            },
            ctx=ctx,
            deps=deps,
        )
        assert result["error"] == "grep failed"

    def test_whitespace_only_query_falls_through_to_summary_ids(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        subagent_conv_id: int,
    ) -> None:
        """Empty-after-strip query is treated as absent — summaryIds wins."""
        ctx.orchestrator.result = ExpansionResult(
            expansions=[{"summaryId": "sum_a", "children": [], "messages": []}],
            cited_ids=["sum_a"],
        )
        _call(
            {
                "query": "   ",
                "summaryIds": ["sum_a"],
                "conversationId": subagent_conv_id,
            },
            ctx=ctx,
            deps=deps,
        )
        # Retrieval NOT called (query was effectively absent).
        assert ctx.retrieval.calls.args == []
        assert ctx.orchestrator.calls.args[0]["summary_ids"] == ["sum_a"]


# ===========================================================================
# Conversation scope error paths
# ===========================================================================


class TestConversationScope:
    """No conversation scope → structured error."""

    def test_no_scope_no_conv_no_flag_errors(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
    ) -> None:
        """No session_key match + no conversationId + no allConversations → error.

        Subagent_conv_id is NOT seeded here; the conversation_store lookup
        finds nothing.
        """
        result = _call(
            {"summaryIds": ["sum_a"]},
            ctx=ctx,
            deps=deps,
            session_key="agent:main:subagent:unknown",
        )
        assert "No LCM conversation found" in result["error"]
        assert ctx.orchestrator.calls.args == []

    def test_all_conversations_flag_allows_proceed(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
    ) -> None:
        """``allConversations=true`` bypasses the conversation-scope refusal."""
        ctx.orchestrator.result = ExpansionResult(
            expansions=[{"summaryId": "sum_a", "children": [], "messages": []}],
            cited_ids=["sum_a"],
        )
        result = _call(
            {
                "summaryIds": ["sum_a"],
                "allConversations": True,
            },
            ctx=ctx,
            deps=deps,
            session_key="agent:main:subagent:unknown",
        )
        # Tool didn't error — orchestrator was called with conversation_id=0
        # (sentinel for "any conversation" per the TS contract).
        assert "error" not in result
        assert ctx.orchestrator.calls.args[0]["conversation_id"] == 0


# ===========================================================================
# Neither summaryIds nor query
# ===========================================================================


class TestNeitherShape:
    """At least one of summaryIds / query must be present."""

    def test_neither_provided_returns_error(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        subagent_conv_id: int,
    ) -> None:
        del subagent_conv_id
        result = _call({}, ctx=ctx, deps=deps)
        assert result["error"] == "Either summaryIds or query must be provided."

    def test_empty_summary_ids_array_returns_error(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        subagent_conv_id: int,
    ) -> None:
        """Empty array (after dedup/filter) is the same as absent."""
        del subagent_conv_id
        result = _call({"summaryIds": []}, ctx=ctx, deps=deps)
        assert result["error"] == "Either summaryIds or query must be provided."

    def test_summary_ids_all_blank_strings_returns_error(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        subagent_conv_id: int,
    ) -> None:
        del subagent_conv_id
        result = _call(
            {"summaryIds": ["", "  ", "\t"]},
            ctx=ctx,
            deps=deps,
        )
        assert result["error"] == "Either summaryIds or query must be provided."


# ===========================================================================
# distill_for_subagent rendering
# ===========================================================================


class TestDistillRendering:
    """The output ``text`` mirrors TS ``distillForSubagent``."""

    def test_header_includes_count_and_total_tokens(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        subagent_conv_id: int,
    ) -> None:
        ctx.orchestrator.result = ExpansionResult(
            expansions=[
                {"summaryId": "sum_a", "children": [], "messages": []},
                {"summaryId": "sum_b", "children": [], "messages": []},
            ],
            cited_ids=["sum_a", "sum_b"],
            total_tokens=100,
        )
        result = _call(
            {"summaryIds": ["sum_a", "sum_b"], "conversationId": subagent_conv_id},
            ctx=ctx,
            deps=deps,
        )
        assert "## Expansion Results (2 summaries, 100 total tokens)" in result["text"]

    def test_entry_renders_kind_and_token_sum(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        subagent_conv_id: int,
    ) -> None:
        ctx.orchestrator.result = ExpansionResult(
            expansions=[
                {
                    "summaryId": "sum_cond",
                    "children": [
                        {
                            "summaryId": "sum_c1",
                            "kind": "leaf",
                            "snippet": "first child content",
                            "tokenCount": 30,
                        },
                    ],
                    "messages": [],
                },
            ],
            cited_ids=["sum_cond", "sum_c1"],
            total_tokens=30,
        )
        result = _call(
            {"summaryIds": ["sum_cond"], "conversationId": subagent_conv_id},
            ctx=ctx,
            deps=deps,
        )
        # Children present -> kind="condensed"; token sum from child.
        assert "### sum_cond (condensed, 30 tokens)" in result["text"]
        assert "Children: sum_c1" in result["text"]
        assert "[Snippet: first child content]" in result["text"]

    def test_leaf_entry_with_messages(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        subagent_conv_id: int,
    ) -> None:
        ctx.orchestrator.result = ExpansionResult(
            expansions=[
                {
                    "summaryId": "sum_leaf",
                    "children": [],
                    "messages": [
                        {"messageId": 42, "role": "user", "tokenCount": 10},
                        {"messageId": 43, "role": "assistant", "tokenCount": 15},
                    ],
                },
            ],
            cited_ids=["sum_leaf"],
            total_tokens=25,
        )
        result = _call(
            {
                "summaryIds": ["sum_leaf"],
                "includeMessages": True,
                "conversationId": subagent_conv_id,
            },
            ctx=ctx,
            deps=deps,
        )
        # Empty children -> kind="leaf"; token sum from messages.
        assert "### sum_leaf (leaf, 25 tokens)" in result["text"]
        assert "msg#42 (user, 10 tokens)" in result["text"]
        assert "msg#43 (assistant, 15 tokens)" in result["text"]

    def test_footer_truncated_flag(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        subagent_conv_id: int,
    ) -> None:
        ctx.orchestrator.result = ExpansionResult(
            expansions=[],
            cited_ids=[],
            total_tokens=0,
            truncated=True,
        )
        result = _call(
            {"summaryIds": ["sum_a"], "conversationId": subagent_conv_id},
            ctx=ctx,
            deps=deps,
        )
        assert "[Truncated: yes]" in result["text"]

    def test_cited_ids_listed(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        subagent_conv_id: int,
    ) -> None:
        ctx.orchestrator.result = ExpansionResult(
            expansions=[],
            cited_ids=["sum_a", "sum_b", "sum_c"],
        )
        result = _call(
            {"summaryIds": ["sum_a"], "conversationId": subagent_conv_id},
            ctx=ctx,
            deps=deps,
        )
        assert "Cited IDs for follow-up: sum_a, sum_b, sum_c" in result["text"]


# ===========================================================================
# Numeric param coercion
# ===========================================================================


class TestNumericCoercion:
    """``maxDepth`` / ``tokenCap`` accept ints + floats, reject bools."""

    def test_float_max_depth_truncates(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        subagent_conv_id: int,
    ) -> None:
        _call(
            {
                "summaryIds": ["sum_a"],
                "maxDepth": 3.7,  # truncates to 3
                "conversationId": subagent_conv_id,
            },
            ctx=ctx,
            deps=deps,
        )
        assert ctx.orchestrator.calls.args[0]["max_depth"] == 3

    def test_negative_token_cap_clamps_to_one(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        subagent_conv_id: int,
    ) -> None:
        """tokenCap < 1 clamps to 1 (TS Math.max(1, ...))."""
        _call(
            {
                "summaryIds": ["sum_a"],
                "tokenCap": -5,
                "conversationId": subagent_conv_id,
            },
            ctx=ctx,
            deps=deps,
        )
        assert ctx.orchestrator.calls.args[0]["token_cap"] == 1

    def test_bool_max_depth_ignored(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        subagent_conv_id: int,
    ) -> None:
        """Bools (an int subclass) are rejected, not coerced."""
        _call(
            {
                "summaryIds": ["sum_a"],
                "maxDepth": True,  # not a real number — ignored
                "conversationId": subagent_conv_id,
            },
            ctx=ctx,
            deps=deps,
        )
        assert ctx.orchestrator.calls.args[0]["max_depth"] is None

    def test_nan_token_cap_ignored(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        subagent_conv_id: int,
    ) -> None:
        _call(
            {
                "summaryIds": ["sum_a"],
                "tokenCap": float("nan"),
                "conversationId": subagent_conv_id,
            },
            ctx=ctx,
            deps=deps,
        )
        assert ctx.orchestrator.calls.args[0]["token_cap"] is None
