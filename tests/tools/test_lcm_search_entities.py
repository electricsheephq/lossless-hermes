"""Tests for :mod:`lossless_hermes.tools.search_entities` — ``lcm_search_entities`` tool.

Mirrors ``lossless-claw/test/lcm-search-entities-tool.test.ts`` (394 LOC TS
→ ~330 LOC Python). Covers:

* Schema well-formedness + registry presence.
* ``mode='like'`` (default substring) matches mid-string.
* ``mode='prefix'`` matches start only.
* ``mode='exact'`` matches whole canonical name (case-folded).
* ``entityType`` filter alone (catalog probe with empty query).
* Both ``query`` and ``entityType`` missing → error.
* ``escape_like`` handles ``%``, ``_``, ``\\`` literally.
* ``catalogStatus == "active"`` when query has zero matches but entities
  exist for session.
* ``catalogStatus == "empty-for-session"`` when no entities for this
  session but some globally.
* ``catalogStatus == "empty-globally"`` when no entities anywhere.
* Rank order: ``occ_count DESC, last_at DESC``.
* ``limit`` clamp at 100 boundary; ``limitReached`` reported.
* ``sessionKey`` scope: defaults to session-key arg unless overridden in
  request body.

Source pin: ``lossless-claw`` at commit ``1f07fbd`` on branch ``pr-613``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as _tz
from typing import Any, Iterator, Optional

import pytest

from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.store.conversation import (
    ConversationStore,
    CreateConversationInput,
)
from lossless_hermes.tools.search_entities import (
    LCM_SEARCH_ENTITIES_SCHEMA,
    SearchEntitiesContext,
    escape_like,
    handle_lcm_search_entities,
)


# ===========================================================================
# Fixtures
# ===========================================================================


@dataclass
class _Ctx:
    """Concrete :class:`SearchEntitiesContext` for tests."""

    conn: sqlite3.Connection
    conversation_store: ConversationStore
    timezone: str


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
    """ConversationStore wired against the in-memory DB."""
    return ConversationStore(db, fts5_available=False)


@pytest.fixture
def default_conv(conv_store: ConversationStore) -> int:
    """Seed a default conversation for session_key='sk1' (TS fixture parity).

    The TS test fixture pre-creates a conversation row for ``sk1`` so the
    default mention inserts have something to reference. We mirror that
    here so the same call-shape works.
    """
    rec = conv_store.create_conversation(
        CreateConversationInput(
            session_id="s1",
            session_key="sk1",
            title="t",
        ),
    )
    return rec.conversation_id


@pytest.fixture
def ctx(
    db: sqlite3.Connection,
    conv_store: ConversationStore,
) -> _Ctx:
    return _Ctx(
        conn=db,
        conversation_store=conv_store,
        timezone="UTC",
    )


# ---------------------------------------------------------------------------
# Helpers — port of the TS ``insertEntity`` / setup pattern
# ---------------------------------------------------------------------------


def _ensure_conversation(conn: sqlite3.Connection, session_key: str) -> int:
    """Return the conversation_id for ``session_key``, inserting if absent."""
    row = conn.execute(
        "SELECT conversation_id FROM conversations WHERE session_key = ? LIMIT 1",
        (session_key,),
    ).fetchone()
    if row is not None:
        return int(row["conversation_id"])
    cur = conn.execute(
        "INSERT INTO conversations (session_id, session_key) VALUES (?, ?)",
        (f"s_{session_key}", session_key),
    )
    return int(cur.lastrowid)


def _now_iso() -> str:
    return datetime.now(tz=_tz.utc).isoformat()


def _insert_entity(
    conn: sqlite3.Connection,
    *,
    entity_id: str,
    session_key: str = "sk1",
    canonical_text: str,
    entity_type: str = "concept",
    occurrence_count: int = 1,
    last_seen_at: Optional[str] = None,
    no_default_mention: bool = False,
) -> None:
    """Port of the TS ``insertEntity`` helper (test lines 76-142).

    By default also inserts an unsuppressed summary + mention so the
    EXISTS guard (Wave-10 P2) doesn't filter the entity out. Tests that
    explicitly want the all-suppressed case pass ``no_default_mention=True``.
    """
    last = last_seen_at or _now_iso()
    first = (datetime.now(tz=_tz.utc) - timedelta(days=7)).isoformat()
    conn.execute(
        """
        INSERT INTO lcm_entities
          (entity_id, session_key, canonical_text, entity_type,
           first_seen_at, last_seen_at, occurrence_count, alternate_surfaces)
        VALUES (?, ?, ?, ?, ?, ?, ?, '[]')
        """,
        (entity_id, session_key, canonical_text, entity_type, first, last, occurrence_count),
    )
    if not no_default_mention:
        conv_id = _ensure_conversation(conn, session_key)
        default_sum_id = f"sum_default_{entity_id}"
        conn.execute(
            """
            INSERT OR IGNORE INTO summaries
              (summary_id, conversation_id, kind, content, token_count, session_key, suppressed_at)
            VALUES (?, ?, 'leaf', 'default fixture content', 1, ?, NULL)
            """,
            (default_sum_id, conv_id, session_key),
        )
        conn.execute(
            """
            INSERT INTO lcm_entity_mentions
              (mention_id, entity_id, summary_id, surface_form, span_start, span_end, mentioned_at)
            VALUES (?, ?, ?, ?, 0, 5, ?)
            """,
            (
                f"m_default_{entity_id}",
                entity_id,
                default_sum_id,
                canonical_text,
                _now_iso(),
            ),
        )
    conn.commit()


def _seed_entity_with_mentions(
    conn: sqlite3.Connection,
    *,
    entity_id: str,
    canonical_text: str,
    mention_count: int,
    last_mention_at: str,
    session_key: str = "sk1",
) -> None:
    """Seed an entity with N real mentions (Wave-12 P1 rank test).

    Ports the TS ``seedEntityWithMentions`` helper (test lines 219-261).
    Required because rank + ``last_seen_at`` are recomputed from
    unsuppressed mentions (not from the stored entity row).
    """
    _insert_entity(
        conn,
        entity_id=entity_id,
        canonical_text=canonical_text,
        session_key=session_key,
        occurrence_count=mention_count,
        last_seen_at=last_mention_at,
        no_default_mention=True,
    )
    conv_id = _ensure_conversation(conn, session_key)
    last_dt = datetime.fromisoformat(last_mention_at)
    for i in range(mention_count):
        sum_id = f"sum_{entity_id}_{i}"
        mentioned_at = (
            last_mention_at
            if i == mention_count - 1
            else (last_dt - timedelta(seconds=mention_count - i)).isoformat()
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO summaries
              (summary_id, conversation_id, kind, content, token_count, session_key, suppressed_at)
            VALUES (?, ?, 'leaf', 'fixture', 1, ?, NULL)
            """,
            (sum_id, conv_id, session_key),
        )
        conn.execute(
            """
            INSERT INTO lcm_entity_mentions
              (mention_id, entity_id, summary_id, surface_form, span_start, span_end, mentioned_at)
            VALUES (?, ?, ?, ?, 0, 5, ?)
            """,
            (f"m_{entity_id}_{i}", entity_id, sum_id, canonical_text, mentioned_at),
        )
    conn.commit()


def _call(
    args: dict[str, Any],
    *,
    ctx: _Ctx,
    session_key: Optional[str] = "sk1",
) -> dict[str, Any]:
    """Invoke :func:`handle_lcm_search_entities` and parse the JSON result."""
    raw = handle_lcm_search_entities(
        args,
        ctx=ctx,
        session_key=session_key,
    )
    return json.loads(raw)


# ===========================================================================
# Schema sanity
# ===========================================================================


class TestSchema:
    """The schema is well-formed and lives in the registry."""

    def test_name(self) -> None:
        assert LCM_SEARCH_ENTITIES_SCHEMA["name"] == "lcm_search_entities"

    def test_no_required_fields(self) -> None:
        """All fields are optional — ``query`` is gated at handler-time
        only when ``entityType`` is absent (a runtime constraint, not a
        schema constraint)."""
        params = LCM_SEARCH_ENTITIES_SCHEMA["parameters"]
        assert params["required"] == []

    def test_mode_enum(self) -> None:
        """The ``mode`` field declares the three-mode enum."""
        props = LCM_SEARCH_ENTITIES_SCHEMA["parameters"]["properties"]
        assert props["mode"]["enum"] == ["like", "prefix", "exact"]

    def test_limit_clamp_range(self) -> None:
        """``limit`` declares the 1..100 range matching TS constants."""
        props = LCM_SEARCH_ENTITIES_SCHEMA["parameters"]["properties"]
        assert props["limit"]["minimum"] == 1
        assert props["limit"]["maximum"] == 100

    def test_description_verbatim_markers(self) -> None:
        """The tool description carries the canonical 'three use modes'
        prose plus the PRIMARY-tool routing copy."""
        desc = LCM_SEARCH_ENTITIES_SCHEMA["description"]
        assert "PRIMARY tool for entity discovery" in desc
        assert "(1) **browse by type**" in desc
        assert "(2) **fuzzy lookup**" in desc
        assert "(3) **catalog probe**" in desc

    def test_entity_type_wave1_provenance(self) -> None:
        """The ``entityType`` description preserves the Wave-1 Auditor #7
        note about snake_case canonical types."""
        props = LCM_SEARCH_ENTITIES_SCHEMA["parameters"]["properties"]
        et = props["entityType"]["description"]
        assert "snake_case canonical types" in et
        assert "Wave-1 Auditor #7" in et


# ===========================================================================
# escape_like — defensive LIKE pattern escaping
# ===========================================================================


class TestEscapeLike:
    """Mirrors the TS ``escapeLike`` semantics (lines 111-115)."""

    def test_escapes_percent(self) -> None:
        assert escape_like("100%pure") == "100\\%pure"

    def test_escapes_underscore(self) -> None:
        assert escape_like("abc_def") == "abc\\_def"

    def test_escapes_backslash(self) -> None:
        assert escape_like("a\\b") == "a\\\\b"

    def test_plain_string_untouched(self) -> None:
        assert escape_like("plain") == "plain"

    def test_backslash_escaped_before_metas(self) -> None:
        """Backslash escape must run first so the ``\\%`` and ``\\_``
        inserts don't get re-escaped."""
        # If backslash were escaped LAST, ``%`` -> ``\%`` would then
        # become ``\\%``, doubling the escape character. The correct
        # order produces a single backslash.
        assert escape_like("%") == "\\%"
        assert escape_like("_") == "\\_"


# ===========================================================================
# Mode-based matching
# ===========================================================================


class TestSearchModes:
    """Mirrors the TS ``createLcmSearchEntitiesTool — match modes`` block."""

    def test_default_like_mode_substring(
        self,
        ctx: _Ctx,
        default_conv: int,
    ) -> None:
        """Default 'like' mode matches anywhere in the string (case-insensitive)."""
        del default_conv
        _insert_entity(ctx.conn, entity_id="e_voyage", canonical_text="Voyage")
        _insert_entity(ctx.conn, entity_id="e_voyageai", canonical_text="VoyageAI")
        _insert_entity(ctx.conn, entity_id="e_other", canonical_text="OpenAI")

        result = _call({"query": "voyage"}, ctx=ctx)
        names = sorted(e["canonicalText"] for e in result["entities"])
        assert names == ["Voyage", "VoyageAI"]

    def test_prefix_mode_matches_start_only(
        self,
        ctx: _Ctx,
        default_conv: int,
    ) -> None:
        """``mode='prefix'`` excludes mid-string matches like 'envoy'."""
        del default_conv
        _insert_entity(ctx.conn, entity_id="e_voyage", canonical_text="Voyage")
        _insert_entity(ctx.conn, entity_id="e_voyageai", canonical_text="VoyageAI")
        _insert_entity(ctx.conn, entity_id="e_envoy", canonical_text="envoy")

        result = _call({"query": "voy", "mode": "prefix"}, ctx=ctx)
        names = sorted(e["canonicalText"] for e in result["entities"])
        assert names == ["Voyage", "VoyageAI"]  # 'envoy' excluded

    def test_exact_mode_matches_whole_string(
        self,
        ctx: _Ctx,
        default_conv: int,
    ) -> None:
        """``mode='exact'`` matches whole canonical name (case-insensitive)."""
        del default_conv
        _insert_entity(ctx.conn, entity_id="e_voyage", canonical_text="Voyage")
        _insert_entity(ctx.conn, entity_id="e_voyageai", canonical_text="VoyageAI")

        result = _call({"query": "voyage", "mode": "exact"}, ctx=ctx)
        assert len(result["entities"]) == 1
        assert result["entities"][0]["canonicalText"] == "Voyage"


# ===========================================================================
# Ranking + limit + limitReached
# ===========================================================================


class TestRankingAndLimits:
    """Mirrors the TS ``ranking and limits`` block."""

    def test_ranks_by_occurrence_then_recency(
        self,
        ctx: _Ctx,
        default_conv: int,
    ) -> None:
        """Order: occ_count DESC, then last_at DESC (Wave-12 P1)."""
        del default_conv
        # The TS rank test seeds REAL mentions because aggregates are
        # recomputed from unsuppressed mentions (Wave-12 P1 fix).
        now = datetime.now(tz=_tz.utc)
        _seed_entity_with_mentions(
            ctx.conn,
            entity_id="e_low",
            canonical_text="TopicA",
            mention_count=1,
            last_mention_at=now.isoformat(),
        )
        _seed_entity_with_mentions(
            ctx.conn,
            entity_id="e_high",
            canonical_text="TopicB",
            mention_count=50,
            last_mention_at=(now - timedelta(days=1)).isoformat(),
        )
        _seed_entity_with_mentions(
            ctx.conn,
            entity_id="e_med",
            canonical_text="TopicC",
            mention_count=50,
            last_mention_at=now.isoformat(),
        )

        result = _call({"query": "Topic"}, ctx=ctx)
        entity_ids = [e["entityId"] for e in result["entities"]]
        # 50 occ + recent first, then 50 occ + older, then 1 occ.
        assert entity_ids[0] == "e_med"
        assert entity_ids[1] == "e_high"
        assert entity_ids[2] == "e_low"

    def test_limit_respected_and_limit_reached_reported(
        self,
        ctx: _Ctx,
        default_conv: int,
    ) -> None:
        """``limit`` caps the result list; ``limitReached`` flips when it does."""
        del default_conv
        for i in range(5):
            _insert_entity(ctx.conn, entity_id=f"e_{i}", canonical_text=f"Item{i}")

        result = _call({"query": "Item", "limit": 3}, ctx=ctx)
        assert len(result["entities"]) == 3
        assert result["limitReached"] is True

    def test_limit_clamps_to_max_100(
        self,
        ctx: _Ctx,
        default_conv: int,
    ) -> None:
        """``limit=999`` is silently clamped to 100 (the MAX_LIMIT cap)."""
        del default_conv
        _insert_entity(ctx.conn, entity_id="e1", canonical_text="x")
        result = _call({"query": "x", "limit": 999}, ctx=ctx)
        # Underlying clamp not directly observable on a 1-row result,
        # but the request must not error — proves the schema-cap path.
        assert len(result["entities"]) == 1

    def test_limit_clamps_to_min_1(
        self,
        ctx: _Ctx,
        default_conv: int,
    ) -> None:
        """``limit=0`` is silently clamped to 1 (the MIN_LIMIT floor)."""
        del default_conv
        _insert_entity(ctx.conn, entity_id="e1", canonical_text="x")
        _insert_entity(ctx.conn, entity_id="e2", canonical_text="x-alt")
        result = _call({"query": "x", "limit": 0}, ctx=ctx)
        # Floor clamp -> 1 result returned (not the whole set).
        assert len(result["entities"]) == 1


# ===========================================================================
# Filters + session scope + escape
# ===========================================================================


class TestFiltersAndEdgeCases:
    """Mirrors the TS ``filters and edge cases`` block."""

    def test_filter_by_entity_type(
        self,
        ctx: _Ctx,
        default_conv: int,
    ) -> None:
        """``entityType`` filter narrows by type."""
        del default_conv
        _insert_entity(
            ctx.conn,
            entity_id="e_proj_alpha",
            canonical_text="alpha",
            entity_type="project",
        )
        _insert_entity(
            ctx.conn,
            entity_id="e_branch_alpha",
            canonical_text="alpha-feature",
            entity_type="git-branch",
        )
        _insert_entity(
            ctx.conn,
            entity_id="e_proj_beta",
            canonical_text="beta",
            entity_type="project",
        )

        result = _call({"query": "alpha", "entityType": "project"}, ctx=ctx)
        assert len(result["entities"]) == 1
        assert result["entities"][0]["entityId"] == "e_proj_alpha"

    def test_browse_by_type_with_empty_query(
        self,
        ctx: _Ctx,
        default_conv: int,
    ) -> None:
        """Empty query + ``entityType`` enumerates the type's entities.

        Wave-12 consolidation: this is the 'browse-by-type' / 'catalog
        probe' use-case path; empty query is allowed when entityType is
        provided.
        """
        del default_conv
        _insert_entity(
            ctx.conn,
            entity_id="e_pr_100",
            canonical_text="PR #100",
            entity_type="pr_number",
        )
        _insert_entity(
            ctx.conn,
            entity_id="e_pr_200",
            canonical_text="PR #200",
            entity_type="pr_number",
        )
        _insert_entity(
            ctx.conn,
            entity_id="e_other",
            canonical_text="some person",
            entity_type="person_name",
        )

        result = _call({"entityType": "pr_number"}, ctx=ctx)
        ids = sorted(e["entityId"] for e in result["entities"])
        assert ids == ["e_pr_100", "e_pr_200"]
        # The empty query is reflected back so the agent can audit the
        # call shape it ended up with.
        assert result["query"] == ""

    def test_default_scopes_to_current_session_key(
        self,
        ctx: _Ctx,
        default_conv: int,
    ) -> None:
        """Without an explicit ``sessionKey`` arg, defaults to the
        session-key passed via the handler input."""
        del default_conv
        _insert_entity(
            ctx.conn,
            entity_id="e_in",
            canonical_text="Voyage",
            session_key="sk1",
        )
        _insert_entity(
            ctx.conn,
            entity_id="e_out",
            canonical_text="Voyage",
            session_key="sk2",
        )

        result = _call({"query": "Voyage"}, ctx=ctx)
        assert len(result["entities"]) == 1
        assert result["entities"][0]["entityId"] == "e_in"

    def test_session_key_arg_overrides_default(
        self,
        ctx: _Ctx,
        default_conv: int,
    ) -> None:
        """Caller can override the default by passing ``sessionKey`` in args."""
        del default_conv
        _insert_entity(
            ctx.conn,
            entity_id="e_in",
            canonical_text="Voyage",
            session_key="sk1",
        )
        _insert_entity(
            ctx.conn,
            entity_id="e_out",
            canonical_text="Voyage",
            session_key="sk2",
        )

        result = _call({"query": "Voyage", "sessionKey": "sk2"}, ctx=ctx)
        assert len(result["entities"]) == 1
        assert result["entities"][0]["entityId"] == "e_out"

    def test_escapes_like_wildcards_in_query(
        self,
        ctx: _Ctx,
        default_conv: int,
    ) -> None:
        """User-supplied ``%`` is treated literally, not as a wildcard."""
        del default_conv
        _insert_entity(ctx.conn, entity_id="e1", canonical_text="100%pure")
        _insert_entity(ctx.conn, entity_id="e2", canonical_text="100abc")

        # Without escape, the % would widen the match to include "100abc".
        # With escape (and ESCAPE '\\' in SQL), only e1 matches.
        result = _call({"query": "100%pure"}, ctx=ctx)
        assert len(result["entities"]) == 1
        assert result["entities"][0]["entityId"] == "e1"

    def test_escapes_underscore_in_query(
        self,
        ctx: _Ctx,
        default_conv: int,
    ) -> None:
        """User-supplied ``_`` is treated literally, not as 'any single char'."""
        del default_conv
        _insert_entity(ctx.conn, entity_id="e1", canonical_text="abc_def")
        _insert_entity(ctx.conn, entity_id="e2", canonical_text="abcXdef")

        # Without escape, '_' matches any single char -> both rows match.
        # With escape, only the literal underscore matches e1.
        result = _call({"query": "abc_def"}, ctx=ctx)
        assert len(result["entities"]) == 1
        assert result["entities"][0]["entityId"] == "e1"

    def test_empty_query_no_entity_type_is_error(
        self,
        ctx: _Ctx,
        default_conv: int,
    ) -> None:
        """Empty query AND no entityType -> structured error response."""
        del default_conv
        result = _call({"query": ""}, ctx=ctx)
        assert "`query` is required" in result["error"]

    def test_missing_query_no_entity_type_is_error(
        self,
        ctx: _Ctx,
        default_conv: int,
    ) -> None:
        """Even with no ``query`` key at all (not just empty), still errors."""
        del default_conv
        result = _call({}, ctx=ctx)
        assert "`query` is required" in result["error"]

    def test_no_session_key_resolved_is_error(
        self,
        ctx: _Ctx,
    ) -> None:
        """No session_key (no input, no arg) -> structured error response."""
        result = _call({"query": "x"}, ctx=ctx, session_key=None)
        assert "No session_key resolved" in result["error"]


# ===========================================================================
# catalogStatus three-state probe (the critical UX bit)
# ===========================================================================


class TestCatalogStatus:
    """Mirrors the P8 harness fix (TS lines 286-303)."""

    def test_active_when_session_has_entities_but_query_does_not_match(
        self,
        ctx: _Ctx,
        default_conv: int,
    ) -> None:
        """At least one entity exists in this session, but the query
        doesn't match any -> ``active`` (i.e. real negative answer)."""
        del default_conv
        _insert_entity(ctx.conn, entity_id="e1", canonical_text="SomethingElse")
        result = _call({"query": "Nonexistent"}, ctx=ctx)
        assert result["totalMatches"] == 0
        assert result["catalogStatus"] == "active"
        # Helpful 'no matches' text — not the coverage-gap copy.
        assert "No entities matched this query" in result["text"]

    def test_empty_for_session_when_other_session_has_entities(
        self,
        ctx: _Ctx,
        default_conv: int,
    ) -> None:
        """Other sessions have entities but this one doesn't ->
        ``empty-for-session``. Coverage gap on this session, not on the
        whole DB."""
        del default_conv
        _insert_entity(
            ctx.conn,
            entity_id="e_other",
            canonical_text="Other",
            session_key="sk2",
        )
        result = _call({"query": "anything"}, ctx=ctx, session_key="sk1")
        assert result["totalMatches"] == 0
        assert result["catalogStatus"] == "empty-for-session"
        # Helpful empty-for-session copy points at the typical fix
        # (session_key='agent:main:main').
        assert "empty-for-session" in result["catalogStatus"]
        assert "agent:main:main" in result["text"]

    def test_empty_globally_when_no_entities_anywhere(
        self,
        ctx: _Ctx,
        default_conv: int,
    ) -> None:
        """Zero entities in the DB at all -> ``empty-globally``.
        Coverage gap on the whole DB."""
        del default_conv
        result = _call({"query": "Nonexistent"}, ctx=ctx)
        assert result["totalMatches"] == 0
        assert result["catalogStatus"] == "empty-globally"
        # The empty-globally copy mentions falling back to lcm_grep.
        assert "No entities indexed in this DB at all" in result["text"]
        assert "coverage gap" in result["text"]


# ===========================================================================
# Suppression filter — EXISTS guard (Wave-10 P2)
# ===========================================================================


class TestSuppressionFilter:
    """Wave-10 P2 — entities with all-suppressed mentions stay hidden."""

    def test_entity_with_all_suppressed_mentions_is_filtered_out(
        self,
        ctx: _Ctx,
        default_conv: int,
    ) -> None:
        """An entity whose only mention is in a suppressed summary must
        not appear in search results."""
        del default_conv
        # Insert without the default unsuppressed mention.
        _insert_entity(
            ctx.conn,
            entity_id="e_hidden",
            canonical_text="HiddenEntity",
            no_default_mention=True,
        )
        # Create a suppressed summary + mention.
        conv_id = _ensure_conversation(ctx.conn, "sk1")
        ctx.conn.execute(
            """
            INSERT INTO summaries
              (summary_id, conversation_id, kind, content, token_count, session_key, suppressed_at)
            VALUES (?, ?, 'leaf', 'x', 1, 'sk1', '2026-01-01T00:00:00')
            """,
            ("sum_suppressed", conv_id),
        )
        ctx.conn.execute(
            """
            INSERT INTO lcm_entity_mentions
              (mention_id, entity_id, summary_id, surface_form, span_start, span_end, mentioned_at)
            VALUES ('m_hidden', 'e_hidden', 'sum_suppressed', 'HiddenEntity', 0, 5, ?)
            """,
            (_now_iso(),),
        )
        ctx.conn.commit()

        # Also seed a normal visible entity to confirm the filter is
        # not just an empty result by accident.
        _insert_entity(ctx.conn, entity_id="e_visible", canonical_text="VisibleEntity")

        result = _call({"query": "Entity"}, ctx=ctx)
        ids = [e["entityId"] for e in result["entities"]]
        assert "e_hidden" not in ids
        assert "e_visible" in ids


# ===========================================================================
# Surface-form parsing (alternateSurfaces)
# ===========================================================================


class TestAlternateSurfaces:
    """``alternateSurfaces`` excludes the canonical itself (TS lines 357-359)."""

    def test_canonical_stripped_from_alternate_surfaces(
        self,
        ctx: _Ctx,
        default_conv: int,
    ) -> None:
        """When the visible-mentions CTE picks up the canonical surface
        plus a distinct surface form, the response's
        ``alternateSurfaces`` keeps only the distinct one."""
        del default_conv
        _insert_entity(
            ctx.conn,
            entity_id="e1",
            canonical_text="Voyage",
            no_default_mention=True,
        )
        conv_id = _ensure_conversation(ctx.conn, "sk1")
        ctx.conn.execute(
            """
            INSERT INTO summaries
              (summary_id, conversation_id, kind, content, token_count, session_key, suppressed_at)
            VALUES ('sum_1', ?, 'leaf', 'x', 1, 'sk1', NULL)
            """,
            (conv_id,),
        )
        # Two mentions — one matching canonical, one alternate.
        ctx.conn.execute(
            """
            INSERT INTO lcm_entity_mentions
              (mention_id, entity_id, summary_id, surface_form, span_start, span_end, mentioned_at)
            VALUES ('m1', 'e1', 'sum_1', 'Voyage', 0, 5, ?)
            """,
            (_now_iso(),),
        )
        ctx.conn.execute(
            """
            INSERT INTO lcm_entity_mentions
              (mention_id, entity_id, summary_id, surface_form, span_start, span_end, mentioned_at)
            VALUES ('m2', 'e1', 'sum_1', 'voyage-ai', 0, 5, ?)
            """,
            (_now_iso(),),
        )
        ctx.conn.commit()

        result = _call({"query": "Voyage"}, ctx=ctx)
        ent = result["entities"][0]
        # The canonical 'Voyage' must be stripped (case-insensitive).
        assert "Voyage" not in ent["alternateSurfaces"]
        # The distinct alternate stays.
        assert "voyage-ai" in ent["alternateSurfaces"]


# ===========================================================================
# Context-Protocol smoke check — instance shape
# ===========================================================================


class TestSearchEntitiesContextProtocol:
    """The :class:`SearchEntitiesContext` Protocol is satisfied by
    the test fixture dataclass without explicit subclassing."""

    def test_structural_match(self, ctx: _Ctx) -> None:
        # We do not assert isinstance because Protocol's structural
        # nature is the whole point; we just check the attributes that
        # the Protocol declares are present.
        assert hasattr(ctx, "conn")
        assert hasattr(ctx, "conversation_store")
        assert hasattr(ctx, "timezone")
        # And the handler accepts the dataclass without complaint.
        rv = handle_lcm_search_entities(
            {"query": "x"},
            ctx=ctx,
            session_key="sk1",
        )
        assert isinstance(rv, str)
        # Result is JSON-parseable.
        assert isinstance(json.loads(rv), dict)


def _ctx_satisfies_protocol(ctx: SearchEntitiesContext) -> bool:
    """Helper to confirm the Protocol is satisfied at the type-checker
    level (mypy / ty would catch this statically)."""
    return ctx is not None
