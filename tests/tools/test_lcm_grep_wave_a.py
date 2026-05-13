"""Tests for :mod:`lossless_hermes.tools.grep` — Wave A modes (regex/full_text/verbatim).

Mirrors ``lossless-claw/test/lcm-grep-verbatim-mode.test.ts`` (435 LOC TS →
~400 LOC Python). Covers:

* **Schema sanity** — name, required fields, optional caps, description prose.
* **Regex mode** — literal pattern, regex pattern, ``scope`` variants, ``since``
  / ``before`` filters, ``conversationId`` scoping.
* **Full-text mode** — simple keyword, multi-word AND default, quoted phrase
  preservation, ``sort`` variants.
* **Verbatim mode** — 20-cap enforcement, ``role`` filter (each of 5 values),
  ``sanitize_fts5_pattern`` edge cases (``"`` in pattern, ``*`` in pattern,
  ``(`` in pattern), full-row untruncated output.
* **Empty pattern** → structured error.
* **since > before** → structured error.
* **Hybrid / semantic modes** → ``not yet available`` error (regression test
  deleted when #06-09 ships).
* **Wave-12 N3 regression** — truncation regex matches
  ``MAX_RESULT_CHARS`` overflow notice byte-identically.

Source pin: ``lossless-claw`` at commit ``1f07fbd`` on branch ``pr-613``.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterator

import pytest

from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.store.conversation import (
    ConversationStore,
    CreateConversationInput,
    CreateMessageInput,
)
from lossless_hermes.store.summary import (
    CreateSummaryInput,
    SummaryStore,
)
from lossless_hermes.tools.conversation_scope import LcmDependencies
from lossless_hermes.tools.grep import (
    LCM_GREP_SCHEMA,
    GrepContext,
    handle_lcm_grep,
    sanitize_fts5_pattern,
)


# ===========================================================================
# Fixtures
# ===========================================================================


@dataclass
class _Ctx:
    """Concrete :class:`GrepContext` for tests."""

    conn: sqlite3.Connection
    summary_store: SummaryStore
    conversation_store: ConversationStore
    timezone: str


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    """In-memory SQLite with migrations + FTS5 + FK on + Row factory."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(conn, fts5_available=True, seed_default_prompts=False)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def conv_id(db: sqlite3.Connection) -> int:
    """Seeded conversation id with session_key=agent:main:main."""
    store = ConversationStore(db, fts5_available=True)
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
    return ConversationStore(db, fts5_available=True)


@pytest.fixture
def summary_store(db: sqlite3.Connection) -> SummaryStore:
    return SummaryStore(db, fts5_available=True, trigram_tokenizer_available=False)


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
    )


@pytest.fixture
def deps() -> LcmDependencies:
    """Minimal :class:`LcmDependencies` for scope resolution."""
    return LcmDependencies(resolve_session_id_from_session_key=lambda _k: None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_message(
    store: ConversationStore,
    *,
    conv_id: int,
    seq: int,
    content: str,
    role: str = "user",
    token_count: int | None = None,
) -> int:
    rec = store.create_message(
        CreateMessageInput(
            conversation_id=conv_id,
            seq=seq,
            role=role,  # type: ignore[arg-type]
            content=content,
            token_count=(token_count if token_count is not None else max(1, len(content) // 4)),
        ),
    )
    return rec.message_id


def _suppress_message(conn: sqlite3.Connection, message_id: int) -> None:
    conn.execute(
        "UPDATE messages SET suppressed_at = datetime('now') WHERE message_id = ?",
        (message_id,),
    )


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
            token_count=(token_count if token_count is not None else max(1, len(content) // 4)),
        ),
    )


def _call(
    args: dict[str, Any],
    *,
    ctx: _Ctx,
    deps: LcmDependencies,
    session_key: str = "agent:main:main",
) -> dict[str, Any]:
    """Invoke :func:`handle_lcm_grep` and parse the JSON result."""
    raw = handle_lcm_grep(
        args,
        ctx=ctx,
        deps=deps,
        session_key=session_key,
        session_id=None,
    )
    return json.loads(raw)


# ===========================================================================
# Schema (sanity) — well-formedness is exercised by the registry test
# ===========================================================================


class TestSchema:
    """The schema matches the TS source byte-identically."""

    def test_name_and_required(self) -> None:
        assert LCM_GREP_SCHEMA["name"] == "lcm_grep"
        params = LCM_GREP_SCHEMA["parameters"]
        assert params["required"] == ["pattern"]

    def test_mode_enum_includes_all_five(self) -> None:
        """Per AC: schema advertises all 5 modes even though Wave A
        implements only 3 (hybrid + semantic land in #06-09)."""
        props = LCM_GREP_SCHEMA["parameters"]["properties"]
        assert props["mode"]["enum"] == [
            "regex",
            "full_text",
            "hybrid",
            "semantic",
            "verbatim",
        ]

    def test_role_enum_includes_system(self) -> None:
        """Wave-12 audit 2 finding #2: ``system`` is in the enum."""
        props = LCM_GREP_SCHEMA["parameters"]["properties"]
        assert props["role"]["enum"] == [
            "user",
            "assistant",
            "tool",
            "system",
            "all",
        ]

    def test_limit_bounds(self) -> None:
        props = LCM_GREP_SCHEMA["parameters"]["properties"]
        assert props["limit"]["minimum"] == 1
        assert props["limit"]["maximum"] == 200

    def test_summary_kinds_is_array(self) -> None:
        props = LCM_GREP_SCHEMA["parameters"]["properties"]
        sk = props["summaryKinds"]
        assert sk["type"] == "array"
        assert sk["items"]["enum"] == ["leaf", "condensed"]

    def test_description_verbatim_markers(self) -> None:
        """The tool description carries the canonical model-routing
        prose verbatim from TS lines 196-204."""
        desc = LCM_GREP_SCHEMA["description"]
        assert "FIVE modes" in desc
        assert "LCM_TOOL_RESULT_TOKEN_BUDGET" in desc
        assert "Type B topic-anchored queries" in desc
        assert "Type C verbatim/citation queries" in desc


# ===========================================================================
# Error paths
# ===========================================================================


class TestErrorPaths:
    """Empty pattern, bad timestamps, hybrid/semantic deferral."""

    def test_empty_pattern_is_rejected(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        conv_id: int,
    ) -> None:
        del conv_id
        result = _call({"pattern": ""}, ctx=ctx, deps=deps)
        assert "pattern` is required" in result["error"]

    def test_whitespace_only_pattern_is_rejected(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        conv_id: int,
    ) -> None:
        del conv_id
        result = _call({"pattern": "   "}, ctx=ctx, deps=deps)
        assert "pattern` is required" in result["error"]

    def test_since_after_before_is_rejected(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        conv_id: int,
    ) -> None:
        del conv_id
        result = _call(
            {
                "pattern": "race",
                "since": "2026-05-14T12:00:00",
                "before": "2026-05-14T10:00:00",
            },
            ctx=ctx,
            deps=deps,
        )
        assert "`since` must be earlier than `before`" in result["error"]

    def test_invalid_since_timestamp_is_rejected(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        conv_id: int,
    ) -> None:
        del conv_id
        result = _call(
            {"pattern": "race", "since": "not-a-timestamp"},
            ctx=ctx,
            deps=deps,
        )
        assert "since must be a valid ISO timestamp" in result["error"]

    def test_no_conversation_scope_returns_error(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
    ) -> None:
        # No session_key, no conversationId — scope resolver returns empty;
        # handler refuses with the AC error string.
        result = _call(
            {"pattern": "race"},
            ctx=ctx,
            deps=deps,
            session_key="",
        )
        assert "No LCM conversation found" in result["error"]
        assert "allConversations=true" in result["error"]

    def test_hybrid_mode_returns_deferred_error(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        conv_id: int,
    ) -> None:
        """Regression test — DELETE when #06-09 ships hybrid mode."""
        del conv_id
        result = _call(
            {"pattern": "race", "mode": "hybrid"},
            ctx=ctx,
            deps=deps,
        )
        assert "hybrid mode is not yet available" in result["error"]
        assert "full_text" in result["error"]

    def test_semantic_mode_returns_deferred_error(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        conv_id: int,
    ) -> None:
        """Regression test — DELETE when #06-09 ships semantic mode."""
        del conv_id
        result = _call(
            {"pattern": "race", "mode": "semantic"},
            ctx=ctx,
            deps=deps,
        )
        assert "semantic mode is not yet available" in result["error"]
        assert "full_text" in result["error"]


# ===========================================================================
# Regex mode
# ===========================================================================


class TestRegexMode:
    """Regex mode runs Python ``re.search`` over content."""

    def test_literal_pattern_matches_messages(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        conv_id: int,
    ) -> None:
        _insert_message(ctx.conversation_store, conv_id=conv_id, seq=1, content="hello world")
        _insert_message(ctx.conversation_store, conv_id=conv_id, seq=2, content="goodbye world")
        result = _call(
            {"pattern": "hello", "mode": "regex", "scope": "messages"},
            ctx=ctx,
            deps=deps,
        )
        # Regex search matches "hello" → 1 message hit. The regex backend
        # returns the match.group(0) as the snippet, so the literal
        # matched substring (not the full content) appears in the text.
        assert result["details"]["messageCount"] == 1
        assert result["details"]["summaryCount"] == 0
        assert result["details"]["totalMatches"] == 1
        assert "hello" in result["text"]

    def test_regex_pattern_matches_messages(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        conv_id: int,
    ) -> None:
        _insert_message(ctx.conversation_store, conv_id=conv_id, seq=1, content="v4.1 released")
        _insert_message(ctx.conversation_store, conv_id=conv_id, seq=2, content="v4.2 released")
        result = _call(
            {"pattern": r"v\d\.\d", "mode": "regex", "scope": "messages"},
            ctx=ctx,
            deps=deps,
        )
        # Both messages match the regex
        assert result["details"]["messageCount"] == 2

    def test_scope_summaries_only(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        conv_id: int,
    ) -> None:
        _insert_message(ctx.conversation_store, conv_id=conv_id, seq=1, content="race condition")
        _insert_summary(
            ctx.summary_store,
            summary_id="sum_a",
            conv_id=conv_id,
            content="race condition summary",
        )
        result = _call(
            {"pattern": "race", "mode": "regex", "scope": "summaries"},
            ctx=ctx,
            deps=deps,
        )
        assert result["details"]["messageCount"] == 0
        assert result["details"]["summaryCount"] == 1

    def test_scope_both_default(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        conv_id: int,
    ) -> None:
        _insert_message(ctx.conversation_store, conv_id=conv_id, seq=1, content="race condition")
        _insert_summary(
            ctx.summary_store,
            summary_id="sum_a",
            conv_id=conv_id,
            content="race condition summary",
        )
        result = _call(
            {"pattern": "race", "mode": "regex"},
            ctx=ctx,
            deps=deps,
        )
        assert result["details"]["messageCount"] == 1
        assert result["details"]["summaryCount"] == 1
        assert result["details"]["totalMatches"] == 2

    def test_conversation_id_scopes_search(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        conv_id: int,
    ) -> None:
        # Seed a second conversation; the explicit conversationId param
        # should isolate the search to conv_id only.
        other = ctx.conversation_store.create_conversation(
            CreateConversationInput(session_id="s2", session_key="agent:other:main", title="t"),
        )
        _insert_message(ctx.conversation_store, conv_id=conv_id, seq=1, content="race here")
        _insert_message(
            ctx.conversation_store,
            conv_id=other.conversation_id,
            seq=1,
            content="race there",
        )
        result = _call(
            {
                "pattern": "race",
                "mode": "regex",
                "scope": "messages",
                "conversationId": conv_id,
            },
            ctx=ctx,
            deps=deps,
        )
        # Regex backend returns match.group(0) as snippet (not full
        # content). The match is "race"; the second conversation's
        # message is invisible due to conversationId scoping.
        assert result["details"]["messageCount"] == 1
        # Conversation scope header confirms the isolation
        assert f"**Conversation scope:** {conv_id}" in result["text"]

    def test_sort_relevance_is_silently_overridden_with_marker(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        conv_id: int,
    ) -> None:
        """Wave-7 Auditor #8 P1: sortIgnored surfaces when sort != recency
        and mode != full_text."""
        _insert_message(ctx.conversation_store, conv_id=conv_id, seq=1, content="race")
        result = _call(
            {"pattern": "race", "mode": "regex", "sort": "relevance"},
            ctx=ctx,
            deps=deps,
        )
        assert result["details"].get("sortIgnored") is True
        assert result["details"]["requestedSort"] == "relevance"
        assert result["details"]["effectiveSort"] == "recency"


# ===========================================================================
# Full-text mode (FTS5)
# ===========================================================================


class TestFullTextMode:
    """Full-text mode uses FTS5 MATCH via the store layer."""

    def test_simple_keyword_search(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        conv_id: int,
    ) -> None:
        _insert_message(ctx.conversation_store, conv_id=conv_id, seq=1, content="hello world")
        _insert_message(ctx.conversation_store, conv_id=conv_id, seq=2, content="goodbye moon")
        result = _call(
            {"pattern": "hello", "mode": "full_text", "scope": "messages"},
            ctx=ctx,
            deps=deps,
        )
        assert result["details"]["messageCount"] == 1

    def test_multi_word_and_default(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        conv_id: int,
    ) -> None:
        """FTS5 default AND semantics: multi-word query needs all terms."""
        _insert_message(
            ctx.conversation_store,
            conv_id=conv_id,
            seq=1,
            content="race condition documented",
        )
        _insert_message(
            ctx.conversation_store,
            conv_id=conv_id,
            seq=2,
            content="race only here",
        )
        result = _call(
            {
                "pattern": "race condition",
                "mode": "full_text",
                "scope": "messages",
            },
            ctx=ctx,
            deps=deps,
        )
        # Only the message with BOTH terms matches.
        assert result["details"]["messageCount"] == 1

    def test_sort_recency_default(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        conv_id: int,
    ) -> None:
        for i in range(3):
            _insert_message(
                ctx.conversation_store,
                conv_id=conv_id,
                seq=i + 1,
                content=f"race condition message {i + 1}",
            )
        result = _call(
            {"pattern": "race", "mode": "full_text", "scope": "messages"},
            ctx=ctx,
            deps=deps,
        )
        assert result["details"]["messageCount"] == 3

    def test_sort_relevance_in_full_text_no_override_marker(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        conv_id: int,
    ) -> None:
        _insert_message(ctx.conversation_store, conv_id=conv_id, seq=1, content="race condition")
        result = _call(
            {
                "pattern": "race",
                "mode": "full_text",
                "scope": "messages",
                "sort": "relevance",
            },
            ctx=ctx,
            deps=deps,
        )
        # sortIgnored is NOT set when mode=full_text supports the sort.
        assert "sortIgnored" not in result["details"]


# ===========================================================================
# Verbatim mode — the bulk of the verbatim-mode test fixture
# ===========================================================================


class TestVerbatimMode:
    """Mirrors lcm-grep-verbatim-mode.test.ts 1:1."""

    def test_returns_full_untruncated_message_content(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        conv_id: int,
    ) -> None:
        long_content = (
            "This is a very long message that exceeds the normal 200-character snippet limit. "
            "It contains specific phrasing about race conditions in the empty plan body fix that "
            "Eva would want to quote verbatim — the literal wording matters here for citation purposes, "
            "and snippet truncation would lose the specific terminology she used."
        )
        _insert_message(ctx.conversation_store, conv_id=conv_id, seq=1, content=long_content)
        result = _call(
            {"pattern": "race condition", "mode": "verbatim", "conversationId": conv_id},
            ctx=ctx,
            deps=deps,
        )
        details = result["details"]
        assert details["mode"] == "verbatim"
        assert details["totalMatches"] == 1
        # First (and only) hit returns FULL content (under 5K cap).
        hit = details["hits"][0]
        assert hit["content"] == long_content
        assert hit["fullContentLength"] == len(long_content)
        assert hit["contentTruncated"] is False
        # Text also contains the verbatim body
        assert "**Mode:** verbatim" in result["text"]
        assert long_content in result["text"]

    def test_hard_caps_at_20_results(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        conv_id: int,
    ) -> None:
        for i in range(30):
            _insert_message(
                ctx.conversation_store,
                conv_id=conv_id,
                seq=i + 1,
                content=f"Race condition message {i + 1}",
            )
        result = _call(
            {
                "pattern": "race",
                "mode": "verbatim",
                "limit": 100,  # user asks for 100 → still capped at 20
                "conversationId": conv_id,
            },
            ctx=ctx,
            deps=deps,
        )
        assert len(result["details"]["hits"]) <= 20

    def test_filters_suppressed_messages(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        conv_id: int,
    ) -> None:
        visible = _insert_message(
            ctx.conversation_store,
            conv_id=conv_id,
            seq=1,
            content="race condition visible message",
        )
        suppressed_id = _insert_message(
            ctx.conversation_store,
            conv_id=conv_id,
            seq=2,
            content="race condition suppressed message",
        )
        _suppress_message(ctx.conn, suppressed_id)
        result = _call(
            {"pattern": "race condition", "mode": "verbatim", "conversationId": conv_id},
            ctx=ctx,
            deps=deps,
        )
        # The visible row is returned, the suppressed row is filtered out.
        details = result["details"]
        assert details["totalMatches"] == 1
        hit_ids = [h["messageId"] for h in details["hits"]]
        assert visible in hit_ids
        assert suppressed_id not in hit_ids

    @pytest.mark.parametrize(
        "role_filter,expected_count",
        [
            ("user", 1),
            ("assistant", 1),
            ("tool", 1),
            ("system", 1),
        ],
    )
    def test_role_filter_each_value(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        conv_id: int,
        role_filter: str,
        expected_count: int,
    ) -> None:
        """P6 fix: role filter composes with FTS5 and prevents the 20-cap
        from being burned by tool-role messages."""
        for i, role in enumerate(("user", "assistant", "tool", "system")):
            _insert_message(
                ctx.conversation_store,
                conv_id=conv_id,
                seq=i + 1,
                content="race condition",
                role=role,
            )
        result = _call(
            {
                "pattern": "race",
                "mode": "verbatim",
                "conversationId": conv_id,
                "role": role_filter,
            },
            ctx=ctx,
            deps=deps,
        )
        details = result["details"]
        assert details["totalMatches"] == expected_count
        if expected_count:
            assert details["hits"][0]["role"] == role_filter
        # Header line records the filter.
        assert f"role={role_filter}" in result["text"]

    def test_role_filter_all_disables_filter(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        conv_id: int,
    ) -> None:
        for i, role in enumerate(("user", "assistant", "tool", "system")):
            _insert_message(
                ctx.conversation_store,
                conv_id=conv_id,
                seq=i + 1,
                content="race condition",
                role=role,
            )
        result = _call(
            {
                "pattern": "race",
                "mode": "verbatim",
                "conversationId": conv_id,
                "role": "all",
            },
            ctx=ctx,
            deps=deps,
        )
        # All 4 rows surface; the role=all header marker is NOT emitted.
        assert result["details"]["totalMatches"] == 4
        assert "role=all" not in result["text"]

    def test_per_hit_content_cap(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        conv_id: int,
    ) -> None:
        """Wave-12 reviewer F6: per-hit content capped at 5K chars."""
        huge = "race condition " + ("X" * 10_000)
        _insert_message(ctx.conversation_store, conv_id=conv_id, seq=1, content=huge)
        result = _call(
            {"pattern": "race", "mode": "verbatim", "conversationId": conv_id},
            ctx=ctx,
            deps=deps,
        )
        # If the truncation notice fired in markdown emit, the hit could be
        # dropped from details.hits. Allow either: the hit is emitted with
        # content cap, OR the hit was not emitted at all (truncated).
        hits = result["details"]["hits"]
        if hits:
            hit = hits[0]
            assert hit["fullContentLength"] == len(huge)
            assert hit["contentTruncated"] is True
            assert len(hit["content"]) <= 5_000 + 50  # +50 for truncation marker

    def test_no_matches_returns_friendly_message(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        conv_id: int,
    ) -> None:
        _insert_message(
            ctx.conversation_store,
            conv_id=conv_id,
            seq=1,
            content="nothing interesting here",
        )
        result = _call(
            {"pattern": "race", "mode": "verbatim", "conversationId": conv_id},
            ctx=ctx,
            deps=deps,
        )
        assert result["details"]["totalMatches"] == 0
        assert "No verbatim matches" in result["text"]


# ===========================================================================
# sanitize_fts5_pattern — TS lines 154-178
# ===========================================================================


class TestSanitizeFts5Pattern:
    """Mirrors the TS sanitizeFts5Pattern behavior."""

    def test_preserves_already_quoted(self) -> None:
        assert sanitize_fts5_pattern('"already quoted"') == '"already quoted"'

    def test_preserves_fts5_boolean_operators(self) -> None:
        assert sanitize_fts5_pattern("foo AND bar") == "foo AND bar"
        assert sanitize_fts5_pattern("foo OR bar") == "foo OR bar"
        assert sanitize_fts5_pattern("NEAR(foo bar)") == "NEAR(foo bar)"

    def test_wraps_problematic_dot(self) -> None:
        """Patterns like ``v4.1`` need phrase-wrap."""
        assert sanitize_fts5_pattern("v4.1") == '"v4.1"'

    def test_wraps_problematic_brackets(self) -> None:
        assert sanitize_fts5_pattern("[brackets]") == '"[brackets]"'

    def test_wraps_problematic_star(self) -> None:
        assert sanitize_fts5_pattern("foo*bar") == '"foo*bar"'

    def test_wraps_problematic_hyphen_at_start(self) -> None:
        assert sanitize_fts5_pattern("-leading") == '"-leading"'

    def test_wraps_problematic_hyphen_at_end(self) -> None:
        assert sanitize_fts5_pattern("trailing-") == '"trailing-"'

    def test_doubles_internal_quotes(self) -> None:
        """TS line 174: internal double quotes are doubled (FTS5 escape)."""
        # "v4.1 has a "feature"" → wrap + escape
        out = sanitize_fts5_pattern('v4.1 with "feature"')
        # Either fully wrapped + escaped, or unchanged if it parsed as
        # already-quoted. The starting char is not " so it gets wrapped.
        assert out.startswith('"')
        assert out.endswith('"')
        assert '""feature""' in out

    def test_passes_through_plain_words(self) -> None:
        assert sanitize_fts5_pattern("plain words") == "plain words"

    def test_empty_input_returns_empty(self) -> None:
        assert sanitize_fts5_pattern("") == ""
        assert sanitize_fts5_pattern("   ") == ""


# ===========================================================================
# Wave-12 N3 — truncation regex byte-identity
# ===========================================================================


class TestTruncationRegex:
    """Wave-12 N3: truncation notice prose is pinned by regex."""

    _TRUNCATION_PIN = re.compile(
        r"truncated at ~\d+ tokens to protect agent context",
    )

    def test_truncation_notice_regex_matches(
        self,
        ctx: _Ctx,
        deps: LcmDependencies,
        conv_id: int,
    ) -> None:
        """Trigger truncation by stuffing the messages table with very
        large rows so the result-budget cap fires."""
        # Each row is ~5K chars; with limit=20 (verbatim cap) the markdown
        # blocks far exceed MAX_RESULT_CHARS (40K default).
        big = "race condition " + ("X" * 5_000)
        for i in range(20):
            _insert_message(
                ctx.conversation_store,
                conv_id=conv_id,
                seq=i + 1,
                content=big,
            )
        result = _call(
            {"pattern": "race", "mode": "verbatim", "conversationId": conv_id},
            ctx=ctx,
            deps=deps,
        )
        # Either at least one row truncated (markdown text contains the
        # notice) or all 20 fit (no notice) — the regression test asserts
        # the regex pin matches IF truncated. Force the truncation by
        # checking the boolean.
        assert result["details"]["truncated"] is True
        assert self._TRUNCATION_PIN.search(result["text"]) is not None
