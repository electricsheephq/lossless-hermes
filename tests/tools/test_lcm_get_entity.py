"""Tests for :mod:`lossless_hermes.tools.get_entity` — ``lcm_get_entity`` tool.

Mirrors ``lossless-claw/test/lcm-get-entity-tool.test.ts`` (480 LOC TS
→ ~430 LOC Python). Covers:

* Entity exists with mentions → markdown with metadata + ordered
  mention list.
* Entity exists but ALL mentions are suppressed → returns
  ``{found: False, fallback_suggestions: [...]}`` (identical shape to
  entity-not-found — pin this).
* Entity does not exist → ``{found: False, fallback_suggestions: [...]}``
  with the 3 concrete suggestions.
* ``entityType`` filter restricts results.
* Case-folding: ``name="Foo"`` matches canonical ``"foo"`` (COLLATE
  NOCASE).
* ``alternateSurfaces`` display strips canonical form.
* ``mentionLimit`` caps results (test 100 boundary).
* ``mentioned_at DESC`` ordering verified.
* Wave-12 P1 regression: aggregates (count, surfaces, first_seen_in,
  first/last_seen_at) recompute from unsuppressed mentions only.
* No session_key resolved → error.
* Empty name → error.

Source pin: ``lossless-claw`` at commit ``1f07fbd`` on branch ``pr-613``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterator

import pytest

from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.tools.get_entity import (
    DEFAULT_MENTION_LIMIT,
    LCM_GET_ENTITY_SCHEMA,
    MAX_MENTION_LIMIT,
    MIN_MENTION_LIMIT,
    GetEntityContext,
    handle_lcm_get_entity,
)


# ===========================================================================
# Fixtures
# ===========================================================================


@dataclass
class _Ctx:
    """Concrete :class:`GetEntityContext` for tests."""

    conn: sqlite3.Connection
    timezone: str


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    """In-memory SQLite with migrations + FK on + Row factory."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(conn, fts5_available=False, seed_default_prompts=False)
    # Seed a conversation row referenced by the summaries used in tests.
    conn.execute(
        "INSERT INTO conversations (session_id, session_key) VALUES ('s1', 'sk1')",
    )
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def ctx(db: sqlite3.Connection) -> _Ctx:
    return _Ctx(conn=db, timezone="UTC")


# ===========================================================================
# Test data helpers
# ===========================================================================


def _insert_summary(
    conn: sqlite3.Connection,
    summary_id: str,
    *,
    session_key: str = "sk1",
    conv_id: int = 1,
    suppressed_at: str | None = None,
    created_at: str | None = None,
) -> None:
    """Insert a minimal summary row.

    Mirrors the TS ``insertSummary`` helper. The columns mirror the
    v4.1 ``summaries`` table — ``suppressed_at`` left ``NULL`` for the
    common visible-leaf case.
    """
    if created_at is None:
        conn.execute(
            "INSERT INTO summaries (summary_id, conversation_id, kind, content, "
            "token_count, session_key, suppressed_at) "
            "VALUES (?, ?, 'leaf', 'x', 1, ?, ?)",
            (summary_id, conv_id, session_key, suppressed_at),
        )
    else:
        conn.execute(
            "INSERT INTO summaries (summary_id, conversation_id, kind, content, "
            "token_count, session_key, suppressed_at, created_at) "
            "VALUES (?, ?, 'leaf', 'x', 1, ?, ?, ?)",
            (summary_id, conv_id, session_key, suppressed_at, created_at),
        )


def _insert_entity(
    conn: sqlite3.Connection,
    *,
    entity_id: str,
    canonical_text: str,
    session_key: str = "sk1",
    entity_type: str = "concept",
    occurrence_count: int = 1,
    alternate_surfaces: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    no_default_mention: bool = False,
) -> None:
    """Insert an entity row + (by default) an auto-paired visible mention.

    Mirrors the TS ``insertEntity`` helper. Per Wave-10 reviewer P2 fix:
    ``lcm_get_entity`` requires at least one unsuppressed mention to
    return the entity. For tests that don't care about mentions, this
    helper inserts a default unsuppressed summary + mention so the
    entity is findable. Tests that EXPLICITLY want the all-suppressed
    case pass ``no_default_mention=True`` and insert their own state.
    """
    conn.execute(
        """
        INSERT INTO lcm_entities
            (entity_id, session_key, canonical_text, entity_type,
             first_seen_at, last_seen_at, occurrence_count,
             alternate_surfaces, metadata)
        VALUES (?, ?, ?, ?,
                datetime('now', '-3 days'), datetime('now'),
                ?, ?, ?)
        """,
        (
            entity_id,
            session_key,
            canonical_text,
            entity_type,
            occurrence_count,
            json.dumps(alternate_surfaces or []),
            json.dumps(metadata) if metadata is not None else None,
        ),
    )
    if not no_default_mention:
        # Auto-create one unsuppressed summary + mention so the
        # VISIBLE_MENTIONS_CTE guard in lcm_get_entity sees a visible
        # mention.
        default_sum_id = f"sum_default_{entity_id}"
        conn.execute(
            """
            INSERT OR IGNORE INTO summaries
                (summary_id, conversation_id, kind, content, token_count,
                 session_key, suppressed_at)
            VALUES (?, 1, 'leaf', 'default fixture content', 1, ?, NULL)
            """,
            (default_sum_id, session_key),
        )
        conn.execute(
            """
            INSERT INTO lcm_entity_mentions
                (mention_id, entity_id, summary_id, surface_form,
                 span_start, span_end, mentioned_at)
            VALUES (?, ?, ?, ?, 0, 5, datetime('now'))
            """,
            (
                f"m_default_{entity_id}",
                entity_id,
                default_sum_id,
                canonical_text,
            ),
        )


def _insert_mention(
    conn: sqlite3.Connection,
    *,
    mention_id: str,
    entity_id: str,
    summary_id: str,
    surface_form: str,
    mentioned_at: str | None = None,
) -> None:
    """Insert a mention row (deterministic id columns)."""
    if mentioned_at is None:
        conn.execute(
            """
            INSERT INTO lcm_entity_mentions
                (mention_id, entity_id, summary_id, surface_form,
                 span_start, span_end, mentioned_at)
            VALUES (?, ?, ?, ?, 0, 5, datetime('now'))
            """,
            (mention_id, entity_id, summary_id, surface_form),
        )
    else:
        conn.execute(
            """
            INSERT INTO lcm_entity_mentions
                (mention_id, entity_id, summary_id, surface_form,
                 span_start, span_end, mentioned_at)
            VALUES (?, ?, ?, ?, 0, 5, ?)
            """,
            (mention_id, entity_id, summary_id, surface_form, mentioned_at),
        )


def _call(
    args: dict[str, Any],
    *,
    ctx: _Ctx,
    session_key: str | None = "sk1",
) -> dict[str, Any]:
    """Invoke :func:`handle_lcm_get_entity` and parse the JSON result."""
    raw = handle_lcm_get_entity(args, ctx=ctx, session_key=session_key)
    return json.loads(raw)


# ===========================================================================
# Schema sanity
# ===========================================================================


class TestSchema:
    """The schema is well-formed and lives in the registry."""

    def test_name_and_required(self) -> None:
        assert LCM_GET_ENTITY_SCHEMA["name"] == "lcm_get_entity"
        params = LCM_GET_ENTITY_SCHEMA["parameters"]
        assert params["required"] == ["name"]

    def test_mention_limit_bounds(self) -> None:
        """``mentionLimit`` carries the 1-100 bounds from TS lines 60-65."""
        props = LCM_GET_ENTITY_SCHEMA["parameters"]["properties"]
        assert props["mentionLimit"]["minimum"] == MIN_MENTION_LIMIT
        assert props["mentionLimit"]["maximum"] == MAX_MENTION_LIMIT

    def test_description_verbatim_markers(self) -> None:
        """Tool description carries the load-bearing Type-D + fallback prose."""
        desc = LCM_GET_ENTITY_SCHEMA["description"]
        # TS lines 123-136 — Type-D PRIMARY routing prose.
        assert "PRIMARY tool for Type D pattern-anchored entity queries" in desc
        # Fallback-to-hybrid hint (tools.md lines 401-402 — load-bearing).
        assert "prefer lcm_grep --mode hybrid instead" in desc
        # Sibling-tool routing prose.
        assert "lcm_search_entities" in desc
        assert "lcm_grep --mode semantic" in desc

    def test_name_field_description_collate_nocase_marker(self) -> None:
        """``name`` field description references COLLATE NOCASE — TS line 42."""
        props = LCM_GET_ENTITY_SCHEMA["parameters"]["properties"]
        assert "COLLATE NOCASE" in props["name"]["description"]


# ===========================================================================
# Error paths
# ===========================================================================


class TestErrorPaths:
    """Failure-mode payloads match the TS source."""

    def test_empty_name_errors(self, ctx: _Ctx) -> None:
        """TS lines 153-156: empty / whitespace-only name → required-error."""
        result = _call({"name": ""}, ctx=ctx)
        assert "error" in result
        assert "`name` is required" in result["error"]

    def test_whitespace_only_name_errors(self, ctx: _Ctx) -> None:
        """Whitespace-only name strips to empty → same error."""
        result = _call({"name": "   "}, ctx=ctx)
        assert "`name` is required" in result["error"]

    def test_missing_name_errors(self, ctx: _Ctx) -> None:
        """Missing the ``name`` key → required-error."""
        result = _call({}, ctx=ctx)
        assert "`name` is required" in result["error"]

    def test_no_session_key_errors(self, ctx: _Ctx) -> None:
        """TS lines 161-166: neither param nor input.sessionKey → error."""
        result = _call({"name": "Voyage"}, ctx=ctx, session_key=None)
        assert "No session_key resolved" in result["error"]

    def test_empty_input_session_key_errors(self, ctx: _Ctx) -> None:
        """Empty-string input.sessionKey treated as absent (TS line 161)."""
        result = _call({"name": "Voyage"}, ctx=ctx, session_key="")
        assert "No session_key resolved" in result["error"]


# ===========================================================================
# Happy path — entity found
# ===========================================================================


class TestHappyPath:
    """Mirror TS ``createLcmGetEntityTool — happy path`` block."""

    def test_returns_entity_with_mentions(self, ctx: _Ctx) -> None:
        """TS 177-251: entity + 3 mentions, aggregates recomputed."""
        _insert_summary(ctx.conn, "sum_1")
        _insert_summary(ctx.conn, "sum_2")
        _insert_summary(ctx.conn, "sum_3")
        # Wave-12 P1: aggregates recompute from unsuppressed mentions,
        # so stored occurrence_count/alternate_surfaces are no longer
        # authoritative. Opt out of default mention; insert our own.
        _insert_entity(
            ctx.conn,
            entity_id="ent_voyage",
            canonical_text="Voyage",
            entity_type="tool",
            occurrence_count=99,  # ignored — recomputed
            alternate_surfaces=["stale-pre-recompute"],  # ignored
            no_default_mention=True,
        )
        _insert_mention(
            ctx.conn,
            mention_id="m1",
            entity_id="ent_voyage",
            summary_id="sum_1",
            surface_form="Voyage",
        )
        _insert_mention(
            ctx.conn,
            mention_id="m2",
            entity_id="ent_voyage",
            summary_id="sum_2",
            surface_form="voyage",
        )
        _insert_mention(
            ctx.conn,
            mention_id="m3",
            entity_id="ent_voyage",
            summary_id="sum_3",
            surface_form="VoyageAI",
        )

        result = _call({"name": "Voyage"}, ctx=ctx)
        details = result["details"]
        assert details["found"] is True
        assert details["entityId"] == "ent_voyage"
        assert details["name"] == "Voyage"
        assert details["entityType"] == "tool"
        assert len(details["mentions"]) == 3
        assert sorted(m["mentionId"] for m in details["mentions"]) == ["m1", "m2", "m3"]
        # Aggregates recomputed from mentions, not from stored entity row.
        assert details["totalOccurrences"] == 3
        # Alternate surfaces strips canonical "Voyage" (case-insensitive),
        # leaving distinct non-canonical forms.
        assert sorted(details["alternateSurfaces"]) == ["VoyageAI"]

        text = result["text"]
        assert "## Entity: Voyage" in text
        assert "**Total occurrences**: 3" in text
        assert "**Alternate surfaces**: VoyageAI" in text

    def test_matches_case_insensitively(self, ctx: _Ctx) -> None:
        """TS 253-265: name='EVA' matches canonical 'Eva' (COLLATE NOCASE)."""
        _insert_entity(ctx.conn, entity_id="e1", canonical_text="Eva")
        result = _call({"name": "EVA"}, ctx=ctx)
        assert result["details"]["found"] is True

    def test_filters_by_entity_type(self, ctx: _Ctx) -> None:
        """TS 267-283: entityType filter restricts results."""
        _insert_entity(
            ctx.conn,
            entity_id="e_main_proj",
            canonical_text="main-project",
            entity_type="project",
        )
        _insert_entity(
            ctx.conn,
            entity_id="e_main_branch",
            canonical_text="main-branch",
            entity_type="git-branch",
        )
        result = _call(
            {"name": "main-branch", "entityType": "git-branch"},
            ctx=ctx,
        )
        assert result["details"]["entityId"] == "e_main_branch"

    def test_entity_type_filter_lowercased(self, ctx: _Ctx) -> None:
        """TS line 171 — entityType filter is .toLowerCase()'d before matching."""
        _insert_entity(
            ctx.conn,
            entity_id="e_caps",
            canonical_text="caps-target",
            entity_type="git-branch",
        )
        # Caller passes uppercase; handler folds before query.
        result = _call(
            {"name": "caps-target", "entityType": "GIT-BRANCH"},
            ctx=ctx,
        )
        assert result["details"]["entityId"] == "e_caps"

    def test_alternate_surfaces_strips_canonical(self, ctx: _Ctx) -> None:
        """TS 270-273: canonical form removed from alternate-surfaces display."""
        _insert_summary(ctx.conn, "sum_a")
        _insert_summary(ctx.conn, "sum_b")
        _insert_entity(
            ctx.conn,
            entity_id="ent_alpha",
            canonical_text="alpha",
            no_default_mention=True,
        )
        # Both surface forms; canonical "alpha" must NOT show up in
        # alternateSurfaces (case-insensitive strip).
        _insert_mention(
            ctx.conn,
            mention_id="m_a",
            entity_id="ent_alpha",
            summary_id="sum_a",
            surface_form="ALPHA",
        )
        _insert_mention(
            ctx.conn,
            mention_id="m_b",
            entity_id="ent_alpha",
            summary_id="sum_b",
            surface_form="Alpha-Beta",
        )
        result = _call({"name": "alpha"}, ctx=ctx)
        alts = result["details"]["alternateSurfaces"]
        # "ALPHA" matches canonical "alpha" case-insensitively → stripped.
        # "Alpha-Beta" is distinct → kept.
        assert "ALPHA" not in alts
        assert "alpha" not in alts
        assert "Alpha-Beta" in alts


# ===========================================================================
# Suppression — Wave-12 P1 regression
# ===========================================================================


class TestSuppression:
    """Mirror TS ``createLcmGetEntityTool — suppression`` block."""

    def test_filters_suppressed_mentions(self, ctx: _Ctx) -> None:
        """TS 287-332: suppressed-parent mentions filtered; totalOccurrences=1."""
        _insert_summary(ctx.conn, "sum_visible")
        _insert_summary(
            ctx.conn,
            "sum_suppressed",
            suppressed_at="2026-05-14T00:00:00",
        )
        _insert_entity(
            ctx.conn,
            entity_id="e1",
            canonical_text="TestEntity",
            occurrence_count=2,
            no_default_mention=True,
        )
        _insert_mention(
            ctx.conn,
            mention_id="m_visible",
            entity_id="e1",
            summary_id="sum_visible",
            surface_form="TestEntity",
        )
        _insert_mention(
            ctx.conn,
            mention_id="m_hidden",
            entity_id="e1",
            summary_id="sum_suppressed",
            surface_form="TestEntity",
        )

        result = _call({"name": "TestEntity"}, ctx=ctx)
        details = result["details"]
        assert len(details["mentions"]) == 1
        assert details["mentions"][0]["mentionId"] == "m_visible"
        # Wave-12 P1: totalOccurrences is recomputed from unsuppressed
        # mentions only. Previously this read the stored entity-row
        # column (which includes suppressed counts) — which leaks an
        # oracle handle revealing that hidden mentions exist.
        assert details["totalOccurrences"] == 1

    def test_wave12_p1_aggregates_recomputed(self, ctx: _Ctx) -> None:
        """TS 334-393: every aggregate column recomputed from unsuppressed mentions.

        Pre-fix: entity row aggregates included suppressed-mention data,
        leaking surface forms first introduced in suppressed leaves and
        exposing summary IDs of suppressed leaves via
        first_seen_in_summary_id.
        Post-fix: every aggregate is computed live from the JOIN with
        unsuppressed summaries.
        """
        # Suppressed leaf 7 days ago; surface form only used here.
        _insert_summary(
            ctx.conn,
            "sum_suppressed_old",
            suppressed_at="2026-05-14T00:00:00",
        )
        ctx.conn.execute(
            "UPDATE summaries SET created_at = datetime('now', '-7 days') "
            "WHERE summary_id = 'sum_suppressed_old'",
        )
        # Visible leaf 1 day ago.
        _insert_summary(ctx.conn, "sum_visible_recent")
        ctx.conn.execute(
            "UPDATE summaries SET created_at = datetime('now', '-1 day') "
            "WHERE summary_id = 'sum_visible_recent'",
        )
        _insert_entity(
            ctx.conn,
            entity_id="ent_x",
            canonical_text="ProjectAlpha",
            occurrence_count=99,  # stale stored count; ignored after fix
            no_default_mention=True,
        )
        # Hidden mention with a unique surface form — must NOT appear post-fix.
        _insert_mention(
            ctx.conn,
            mention_id="m_hidden_old",
            entity_id="ent_x",
            summary_id="sum_suppressed_old",
            surface_form="alpha-secret-codename",
        )
        ctx.conn.execute(
            "UPDATE lcm_entity_mentions SET mentioned_at = datetime('now', '-7 days') "
            "WHERE mention_id = 'm_hidden_old'",
        )
        # Visible mention with canonical surface form.
        _insert_mention(
            ctx.conn,
            mention_id="m_visible_recent",
            entity_id="ent_x",
            summary_id="sum_visible_recent",
            surface_form="ProjectAlpha",
        )
        ctx.conn.execute(
            "UPDATE lcm_entity_mentions SET mentioned_at = datetime('now', '-1 day') "
            "WHERE mention_id = 'm_visible_recent'",
        )

        result = _call({"name": "ProjectAlpha"}, ctx=ctx)
        details = result["details"]
        # Aggregates should reflect ONLY the visible mention.
        assert details["totalOccurrences"] == 1
        assert details["firstSeenInSummaryId"] == "sum_visible_recent"
        # 'alpha-secret-codename' was only in the suppressed leaf — must not appear.
        assert "alpha-secret-codename" not in details["alternateSurfaces"]
        # first_seen_at == last_seen_at == visible mention's mentioned_at
        # (1d ago). The 7d-ago suppressed mention must not pull
        # first_seen_at backward.
        assert details["firstSeenAt"] == details["lastSeenAt"]

    def test_all_mentions_suppressed_indistinguishable_from_not_found(
        self,
        ctx: _Ctx,
    ) -> None:
        """Wave-12 existence-probing defense (load-bearing per spec).

        When all of an entity's mentions are suppressed, the payload
        MUST be byte-identical in shape to "no such entity" — otherwise
        an attacker could probe by querying and observing differences.
        Per TS lines 226-244, the "not found" message is intentionally
        silent about suppression.
        """
        # Seed an entity whose ONLY mention is in a suppressed leaf.
        _insert_summary(
            ctx.conn,
            "sum_only_suppressed",
            suppressed_at="2026-05-14T00:00:00",
        )
        _insert_entity(
            ctx.conn,
            entity_id="ent_hidden",
            canonical_text="HiddenEntity",
            no_default_mention=True,
        )
        _insert_mention(
            ctx.conn,
            mention_id="m_only_hidden",
            entity_id="ent_hidden",
            summary_id="sum_only_suppressed",
            surface_form="HiddenEntity",
        )

        suppressed_result = _call({"name": "HiddenEntity"}, ctx=ctx)
        # No entity at all — same surface should look identical (modulo
        # the name and prefix-token in the suggestion strings).
        nonexistent_result = _call({"name": "DoesNotExistAtAll"}, ctx=ctx)

        # Same top-level keys (no `text` / `details`, only `found` etc.).
        assert set(suppressed_result.keys()) == set(nonexistent_result.keys())
        # Both have found=False.
        assert suppressed_result["found"] is False
        assert nonexistent_result["found"] is False
        # Both expose fallback_suggestions of length 3.
        assert len(suppressed_result["fallback_suggestions"]) == 3
        assert len(nonexistent_result["fallback_suggestions"]) == 3
        # Message does NOT mention "suppression" — that would leak the
        # oracle handle the attacker probes for.
        assert "suppress" not in suppressed_result["message"].lower()


# ===========================================================================
# Not-found fallback suggestions
# ===========================================================================


class TestNotFoundFallbacks:
    """Spec AC: 3 concrete fallback suggestions in the not-found payload."""

    def test_not_found_returns_three_suggestions(self, ctx: _Ctx) -> None:
        result = _call({"name": "NoSuchEntity"}, ctx=ctx)
        assert result["found"] is False
        assert "No entity matching" in result["message"]
        suggestions = result["fallback_suggestions"]
        assert len(suggestions) == 3
        # Each suggestion is a concrete tool call string.
        assert "lcm_search_entities" in suggestions[0]
        assert "mode='prefix'" in suggestions[0]
        assert "lcm_grep" in suggestions[1]
        assert "mode='hybrid'" in suggestions[1]
        assert "lcm_grep" in suggestions[2]
        assert "mode='verbatim'" in suggestions[2]

    def test_not_found_includes_entity_type_in_message(self, ctx: _Ctx) -> None:
        """TS line 236: not-found message includes the entity_type filter."""
        result = _call(
            {"name": "Unknown", "entityType": "person_name"},
            ctx=ctx,
        )
        assert result["found"] is False
        assert "person_name" in result["message"]


# ===========================================================================
# Mention list — limit + ordering
# ===========================================================================


class TestMentionLimit:
    """Mirror TS ``createLcmGetEntityTool — mention limit`` block."""

    def test_respects_mention_limit_and_truncated_flag(self, ctx: _Ctx) -> None:
        """TS 436-462: mentionLimit caps; mentionsTruncated=True when capped."""
        _insert_summary(ctx.conn, "sum_x")
        _insert_entity(
            ctx.conn,
            entity_id="e_many",
            canonical_text="Lots",
            occurrence_count=10,
            no_default_mention=True,
        )
        for i in range(10):
            _insert_mention(
                ctx.conn,
                mention_id=f"m_{i}",
                entity_id="e_many",
                summary_id="sum_x",
                surface_form="Lots",
            )

        result = _call({"name": "Lots", "mentionLimit": 3}, ctx=ctx)
        details = result["details"]
        assert len(details["mentions"]) == 3
        assert details["mentionsTruncated"] is True

    def test_mention_limit_under_count_not_truncated(self, ctx: _Ctx) -> None:
        """TS 464-479: when mentions fit under the limit, mentionsTruncated=False."""
        _insert_summary(ctx.conn, "sum_x")
        _insert_entity(
            ctx.conn,
            entity_id="e_few",
            canonical_text="Few",
            occurrence_count=2,
            no_default_mention=True,
        )
        _insert_mention(
            ctx.conn,
            mention_id="m1",
            entity_id="e_few",
            summary_id="sum_x",
            surface_form="Few",
        )
        _insert_mention(
            ctx.conn,
            mention_id="m2",
            entity_id="e_few",
            summary_id="sum_x",
            surface_form="Few",
        )
        result = _call({"name": "Few", "mentionLimit": 50}, ctx=ctx)
        assert result["details"]["mentionsTruncated"] is False

    def test_mention_limit_clamped_to_max_100(self, ctx: _Ctx) -> None:
        """``mentionLimit=999`` clamps to MAX (100). Boundary check."""
        _insert_summary(ctx.conn, "sum_x")
        _insert_entity(
            ctx.conn,
            entity_id="e_clamp",
            canonical_text="ClampTarget",
            no_default_mention=True,
        )
        for i in range(120):
            _insert_mention(
                ctx.conn,
                mention_id=f"mc_{i}",
                entity_id="e_clamp",
                summary_id="sum_x",
                surface_form="ClampTarget",
            )
        result = _call({"name": "ClampTarget", "mentionLimit": 999}, ctx=ctx)
        # Max 100 — clamped.
        assert len(result["details"]["mentions"]) == 100
        assert result["details"]["mentionsTruncated"] is True

    def test_mention_limit_clamped_to_min_1(self, ctx: _Ctx) -> None:
        """``mentionLimit=0`` clamps to MIN (1)."""
        _insert_summary(ctx.conn, "sum_x")
        _insert_entity(
            ctx.conn,
            entity_id="e_min",
            canonical_text="MinTarget",
            no_default_mention=True,
        )
        for i in range(3):
            _insert_mention(
                ctx.conn,
                mention_id=f"mn_{i}",
                entity_id="e_min",
                summary_id="sum_x",
                surface_form="MinTarget",
            )
        result = _call({"name": "MinTarget", "mentionLimit": 0}, ctx=ctx)
        assert len(result["details"]["mentions"]) == 1

    def test_default_mention_limit_when_omitted(self, ctx: _Ctx) -> None:
        """Omitting mentionLimit uses DEFAULT (20)."""
        _insert_summary(ctx.conn, "sum_x")
        _insert_entity(
            ctx.conn,
            entity_id="e_default",
            canonical_text="DefaultTarget",
            no_default_mention=True,
        )
        for i in range(25):
            _insert_mention(
                ctx.conn,
                mention_id=f"md_{i}",
                entity_id="e_default",
                summary_id="sum_x",
                surface_form="DefaultTarget",
            )
        result = _call({"name": "DefaultTarget"}, ctx=ctx)
        assert len(result["details"]["mentions"]) == DEFAULT_MENTION_LIMIT


class TestMentionOrdering:
    """Mentions are returned in ``mentioned_at DESC`` order (TS line 257)."""

    def test_descending_mentioned_at(self, ctx: _Ctx) -> None:
        _insert_summary(ctx.conn, "sum_a")
        _insert_summary(ctx.conn, "sum_b")
        _insert_summary(ctx.conn, "sum_c")
        _insert_entity(
            ctx.conn,
            entity_id="e_order",
            canonical_text="OrderTarget",
            no_default_mention=True,
        )
        _insert_mention(
            ctx.conn,
            mention_id="m_old",
            entity_id="e_order",
            summary_id="sum_a",
            surface_form="OrderTarget",
            mentioned_at="2026-01-01T00:00:00",
        )
        _insert_mention(
            ctx.conn,
            mention_id="m_mid",
            entity_id="e_order",
            summary_id="sum_b",
            surface_form="OrderTarget",
            mentioned_at="2026-03-01T00:00:00",
        )
        _insert_mention(
            ctx.conn,
            mention_id="m_new",
            entity_id="e_order",
            summary_id="sum_c",
            surface_form="OrderTarget",
            mentioned_at="2026-05-01T00:00:00",
        )

        result = _call({"name": "OrderTarget"}, ctx=ctx)
        mention_ids = [m["mentionId"] for m in result["details"]["mentions"]]
        # Newest first.
        assert mention_ids == ["m_new", "m_mid", "m_old"]


# ===========================================================================
# Markdown rendering
# ===========================================================================


class TestMarkdownRender:
    """Output text follows the TS markdown structure (lines 277-310)."""

    def test_header_lists_metadata(self, ctx: _Ctx) -> None:
        _insert_summary(ctx.conn, "sum_x")
        _insert_entity(
            ctx.conn,
            entity_id="ent_meta",
            canonical_text="MetaTarget",
            entity_type="person_name",
            no_default_mention=True,
        )
        _insert_mention(
            ctx.conn,
            mention_id="m_z",
            entity_id="ent_meta",
            summary_id="sum_x",
            surface_form="MetaTarget",
        )
        result = _call({"name": "MetaTarget"}, ctx=ctx)
        text = result["text"]
        assert "## Entity: MetaTarget" in text
        assert "- **Type**: person_name" in text
        assert "- **Entity ID**: `ent_meta`" in text
        assert "- **Session key**: `sk1`" in text
        assert "- **Total occurrences**: 1" in text
        # Mention section follows.
        assert "### Mentions (1)" in text
        assert 'surface: "MetaTarget"' in text

    def test_truncation_header_shows_n_of_m(self, ctx: _Ctx) -> None:
        """TS line 302 — ``### Mentions (N of M)`` when truncated."""
        _insert_summary(ctx.conn, "sum_x")
        _insert_entity(
            ctx.conn,
            entity_id="e_trunc",
            canonical_text="TruncTarget",
            no_default_mention=True,
        )
        for i in range(5):
            _insert_mention(
                ctx.conn,
                mention_id=f"mt_{i}",
                entity_id="e_trunc",
                summary_id="sum_x",
                surface_form="TruncTarget",
            )
        result = _call({"name": "TruncTarget", "mentionLimit": 2}, ctx=ctx)
        text = result["text"]
        # Two mentions returned out of five total visible.
        assert "### Mentions (2 of 5)" in text
        assert result["details"]["mentionsTruncated"] is True


# ===========================================================================
# Context: GetEntityContext satisfies Protocol
# ===========================================================================


def test_ctx_dataclass_satisfies_protocol(ctx: _Ctx) -> None:
    """The test dataclass is structurally a :class:`GetEntityContext`.

    Protocols are checked structurally — this test pins that the test
    fixture continues to satisfy the contract so a future Protocol
    field addition doesn't silently make the tests use a stale shape.
    """
    # isinstance check with @runtime_checkable would be needed, but the
    # Protocol isn't decorated. Instead assert by attribute access.
    assert isinstance(ctx.conn, sqlite3.Connection)
    assert isinstance(ctx.timezone, str)
    # Used to satisfy ty's structural assignment check too.
    _: GetEntityContext = ctx
    del _
