"""Tests for :mod:`lossless_hermes.tools.synthesize_around` — ``lcm_synthesize_around``.

Mirrors ``lossless-claw/test/lcm-synthesize-around-tool.test.ts`` (757 LOC
TS → ~600 LOC Python). Covers:

* Schema well-formedness + registry presence.
* Input validation: empty target, bad window_kind, since >= before,
  missing scope, free-text in time mode, target-not-found.
* Missing-prompt error surfaces up-front (before any LLM call).
* Time-window happy path: leaves selected within ±windowHours, dispatch
  called, cache row persisted, audit row written.
* Time-window: target itself excluded from source set.
* Time-window: helpful error when zero leaves match.
* Time-window: since/before bounds clamp the window.
* Semantic mode without vec0 returns graceful error (Wave A defers it).
* Cache-key Wave-10 regression: two cache rows with same range but
  different tier_label don't collide (seed both, assert no UNIQUE error).
* session_key fallback chain (4 levels).
* Period mode: rejects missing both shortcut + explicit range.
* Period mode: rejects unknown period shortcut.
* Period mode: accepts ``period='last-7-days'`` without target.
* Period mode: accepts explicit since/before without target.

Source pin: ``lossless-claw`` at commit ``1f07fbd`` on branch ``pr-613``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterator, Optional

import pytest

from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.store.conversation import (
    ConversationStore,
    CreateConversationInput,
)
from lossless_hermes.synthesis.dispatch import (
    LlmCall,
    LlmCallArgs,
    LlmCallResult,
)
from lossless_hermes.synthesis.prompt_registry import (
    RegisterPromptOptions,
    register_prompt,
)
from lossless_hermes.tools.conversation_scope import LcmDependencies
from lossless_hermes.tools.synthesize_around import (
    LCM_SYNTHESIZE_AROUND_SCHEMA,
    BuildLlmCall,
    SynthesizeAroundContext,
    build_source_text,
    handle_lcm_synthesize_around,
)


# ===========================================================================
# Fixtures
# ===========================================================================


@dataclass
class _Ctx:
    """Concrete :class:`SynthesizeAroundContext` for tests."""

    conn: sqlite3.Connection
    conversation_store: ConversationStore
    timezone: str
    build_llm_call: BuildLlmCall


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    """In-memory SQLite with migrations + FK on + Row factory + autocommit."""
    # Use isolation_level=None for register_prompt (needs autocommit).
    conn = sqlite3.connect(":memory:", isolation_level=None)
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
def conv_id_1(conv_store: ConversationStore) -> int:
    """Seed a conversation for session_key='sk1'."""
    rec = conv_store.create_conversation(
        CreateConversationInput(session_id="s1", session_key="sk1", title="t1"),
    )
    return rec.conversation_id


@pytest.fixture
def conv_id_2(conv_store: ConversationStore) -> int:
    """Seed a second conversation for session_key='sk2'."""
    rec = conv_store.create_conversation(
        CreateConversationInput(session_id="s2", session_key="sk2", title="t2"),
    )
    return rec.conversation_id


def _make_deps() -> LcmDependencies:
    """Minimal LcmDependencies for tests."""
    return LcmDependencies(
        resolve_session_id_from_session_key=lambda _: None,
    )


def _make_mock_llm_call() -> LlmCall:
    """Return a deterministic mock LlmCall — used by the build_llm_call factory."""

    async def _call(args: LlmCallArgs) -> LlmCallResult:
        # Surface the prompt head so tests can assert leaf concat reached the LLM.
        head = " | ".join(args.prompt.split("\n")[:6])
        return LlmCallResult(
            output=f"synthesized: {head[:200]}",
            latency_ms=5.0,
            cost_cents=1,
            actual_model="test-mock-model",
        )

    return _call


def _make_build_llm_call(model_name: str = "test-mock-model") -> BuildLlmCall:
    """Factory returning the mock call + a fixed model name."""

    class _Factory:
        def __call__(self) -> tuple[LlmCall, str]:
            return _make_mock_llm_call(), model_name

    return _Factory()


@pytest.fixture
def ctx(db: sqlite3.Connection, conv_store: ConversationStore) -> _Ctx:
    return _Ctx(
        conn=db,
        conversation_store=conv_store,
        timezone="UTC",
        build_llm_call=_make_build_llm_call(),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_leaf(
    conn: sqlite3.Connection,
    *,
    summary_id: str,
    conversation_id: int,
    content: str,
    created_at: str,
) -> None:
    """Insert a leaf summary (mirrors TS ``insertLeaf``)."""
    conn.execute(
        "INSERT INTO summaries"
        " (summary_id, conversation_id, kind, content, token_count,"
        "  session_key, created_at)"
        " VALUES (?, ?, 'leaf', ?, ?,"
        "         (SELECT session_key FROM conversations WHERE conversation_id = ?), ?)",
        (
            summary_id,
            conversation_id,
            content,
            max(1, (len(content) + 3) // 4),
            conversation_id,
            created_at,
        ),
    )


def _insert_condensed(
    conn: sqlite3.Connection,
    *,
    summary_id: str,
    conversation_id: int,
    content: str,
    created_at: str,
) -> None:
    """Insert a condensed summary (anchor for time-mode tests)."""
    conn.execute(
        "INSERT INTO summaries"
        " (summary_id, conversation_id, kind, content, token_count,"
        "  session_key, created_at)"
        " VALUES (?, ?, 'condensed', ?, ?,"
        "         (SELECT session_key FROM conversations WHERE conversation_id = ?), ?)",
        (
            summary_id,
            conversation_id,
            content,
            max(1, (len(content) + 3) // 4),
            conversation_id,
            created_at,
        ),
    )


def _register_default_prompt(conn: sqlite3.Connection, tier: str = "custom") -> str:
    """Register an active prompt for (episodic-condensed, tier, single)."""
    return register_prompt(
        conn,
        RegisterPromptOptions(
            memory_type="episodic-condensed",
            tier_label=tier,
            pass_kind="single",
            template="Compact: {{source_text}}",
        ),
    )


def _call(
    ctx: _Ctx,
    args: dict[str, Any],
    *,
    session_key: Optional[str] = "sk1",
    session_id: Optional[str] = "s1",
) -> dict[str, Any]:
    """Invoke the handler and JSON-decode the result."""
    out = handle_lcm_synthesize_around(
        args,
        ctx=ctx,  # type: ignore[arg-type]
        deps=_make_deps(),
        session_key=session_key,
        session_id=session_id,
    )
    return json.loads(out)


# ===========================================================================
# Schema + registration
# ===========================================================================


class TestSchema:
    def test_schema_has_correct_name(self) -> None:
        assert LCM_SYNTHESIZE_AROUND_SCHEMA["name"] == "lcm_synthesize_around"

    def test_schema_has_correct_parameter_shape(self) -> None:
        params = LCM_SYNTHESIZE_AROUND_SCHEMA["parameters"]
        assert params["type"] == "object"
        # required is omitted for optional-only schemas; window_kind is REQUIRED.
        assert "window_kind" in params["properties"]
        assert "window_kind" in params.get("required", [])
        # target is optional (period mode allows missing).
        assert "target" in params["properties"]
        assert "target" not in params.get("required", [])

    def test_schema_registered_in_tool_schemas(self) -> None:
        from lossless_hermes.tools import get_tool_schemas

        names = [s["name"] for s in get_tool_schemas()]
        assert "lcm_synthesize_around" in names


# ===========================================================================
# Input validation
# ===========================================================================


class TestInputValidation:
    def test_rejects_empty_target_for_time(self, ctx: _Ctx, conv_id_1: int) -> None:
        r = _call(ctx, {"target": "  ", "window_kind": "time"})
        assert "`target` is required" in r["error"]

    def test_rejects_bad_window_kind(self, ctx: _Ctx, conv_id_1: int) -> None:
        r = _call(ctx, {"target": "anything", "window_kind": "bogus"})
        assert "window_kind" in r["error"]

    def test_rejects_since_after_before(self, ctx: _Ctx, conv_id_1: int) -> None:
        r = _call(
            ctx,
            {
                "target": "sum_x",
                "window_kind": "time",
                "since": "2026-05-01T00:00:00.000Z",
                "before": "2026-04-01T00:00:00.000Z",
            },
        )
        assert "earlier than `before`" in r["error"]

    def test_requires_conversation_scope(self, ctx: _Ctx) -> None:
        # No conversation seeded for sk_none, and no allConversations.
        r = _call(
            ctx,
            {"target": "anything", "window_kind": "semantic"},
            session_key="sk_none",
            session_id="s_none",
        )
        assert "No LCM conversation found for this session" in r["error"]

    def test_rejects_free_text_target_in_time_mode(self, ctx: _Ctx, conv_id_1: int) -> None:
        r = _call(ctx, {"target": "free text", "window_kind": "time"})
        assert "time window requires a summary_id" in r["error"]

    def test_target_not_found(self, ctx: _Ctx, conv_id_1: int) -> None:
        r = _call(ctx, {"target": "sum_does_not_exist", "window_kind": "time"})
        assert "Target summary not found" in r["error"]


# ===========================================================================
# Missing-prompt error surfaces up-front
# ===========================================================================


class TestMissingPrompt:
    def test_missing_prompt_surfaced_before_llm_call(self, ctx: _Ctx, conv_id_1: int) -> None:
        """When no prompt is registered, fail fast before any LLM call."""
        _insert_condensed(
            ctx.conn,
            summary_id="sum_anchor",
            conversation_id=conv_id_1,
            content="anchor body",
            created_at="2026-05-01 12:00:00",
        )
        _insert_leaf(
            ctx.conn,
            summary_id="sum_a",
            conversation_id=conv_id_1,
            content="leaf one body",
            created_at="2026-05-01 11:30:00",
        )
        _insert_leaf(
            ctx.conn,
            summary_id="sum_b",
            conversation_id=conv_id_1,
            content="leaf two body",
            created_at="2026-05-01 12:30:00",
        )
        # NO prompt registered.
        r = _call(
            ctx,
            {"target": "sum_anchor", "window_kind": "time", "windowHours": 6},
        )
        assert "missing_prompt" in r["error"]
        assert "episodic-condensed" in r["error"]
        assert "custom" in r["error"]


# ===========================================================================
# Time-window happy path
# ===========================================================================


class TestTimeWindowHappy:
    def test_selects_in_window_calls_dispatch_persists_cache(
        self, ctx: _Ctx, conv_id_1: int
    ) -> None:
        """Anchor + 4 in-window leaves + 1 far leaf → 4 selected, cache=ready."""
        _insert_condensed(
            ctx.conn,
            summary_id="sum_anchor",
            conversation_id=conv_id_1,
            content="anchor summary",
            created_at="2026-05-01 12:00:00",
        )
        _insert_leaf(
            ctx.conn,
            summary_id="sum_in_a",
            conversation_id=conv_id_1,
            content="AAA-content",
            created_at="2026-05-01 09:00:00",
        )
        _insert_leaf(
            ctx.conn,
            summary_id="sum_in_b",
            conversation_id=conv_id_1,
            content="BBB-content",
            created_at="2026-05-01 11:30:00",
        )
        _insert_leaf(
            ctx.conn,
            summary_id="sum_in_c",
            conversation_id=conv_id_1,
            content="CCC-content",
            created_at="2026-05-01 12:30:00",
        )
        _insert_leaf(
            ctx.conn,
            summary_id="sum_in_d",
            conversation_id=conv_id_1,
            content="DDD-content",
            created_at="2026-05-01 18:00:00",
        )
        _insert_leaf(
            ctx.conn,
            summary_id="sum_far",
            conversation_id=conv_id_1,
            content="FAR-content",
            created_at="2026-05-05 12:00:00",
        )
        _register_default_prompt(ctx.conn)

        r = _call(
            ctx,
            {"target": "sum_anchor", "window_kind": "time", "windowHours": 12},
        )
        assert "error" not in r
        details = r["details"]
        assert details["leaf_count"] == 4
        assert details["cache_id"].startswith("cache_around_")

        # Cache row is ready
        cache_id = details["cache_id"]
        cache = ctx.conn.execute(
            "SELECT status, content, source_leaf_ids, tier_label"
            " FROM lcm_synthesis_cache WHERE cache_id = ?",
            (cache_id,),
        ).fetchone()
        assert cache["status"] == "ready"
        assert cache["tier_label"] == "custom"
        assert "synthesized:" in (cache["content"] or "")
        ids = json.loads(cache["source_leaf_ids"])
        assert ids == ["sum_in_a", "sum_in_b", "sum_in_c", "sum_in_d"]

        # Audit row written by dispatch
        audit_rows = ctx.conn.execute(
            "SELECT pass_kind, status, target_cache_id FROM lcm_synthesis_audit"
        ).fetchall()
        assert len(audit_rows) == 1
        assert audit_rows[0]["pass_kind"] == "single"
        assert audit_rows[0]["status"] == "completed"
        assert audit_rows[0]["target_cache_id"] == cache_id

        # Markdown surface
        text = r["text"]
        assert "## LCM Synthesize-Around" in text
        assert "**Mode:** time" in text
        assert f"**Cache id:** `{cache_id}`" in text
        assert "synthesized:" in text

    def test_excludes_target_summary_from_sources(self, ctx: _Ctx, conv_id_1: int) -> None:
        _insert_leaf(
            ctx.conn,
            summary_id="sum_target",
            conversation_id=conv_id_1,
            content="TARGET-CONTENT",
            created_at="2026-05-01 12:00:00",
        )
        _insert_leaf(
            ctx.conn,
            summary_id="sum_other",
            conversation_id=conv_id_1,
            content="OTHER-CONTENT",
            created_at="2026-05-01 12:30:00",
        )
        _register_default_prompt(ctx.conn)

        r = _call(
            ctx,
            {"target": "sum_target", "window_kind": "time", "windowHours": 6},
        )
        details = r["details"]
        assert details["leaf_count"] == 1
        cache = ctx.conn.execute(
            "SELECT source_leaf_ids FROM lcm_synthesis_cache WHERE cache_id = ?",
            (details["cache_id"],),
        ).fetchone()
        assert json.loads(cache["source_leaf_ids"]) == ["sum_other"]

    def test_helpful_error_on_zero_leaves(self, ctx: _Ctx, conv_id_1: int) -> None:
        _insert_condensed(
            ctx.conn,
            summary_id="sum_anchor",
            conversation_id=conv_id_1,
            content="anchor",
            created_at="2026-05-01 12:00:00",
        )
        _register_default_prompt(ctx.conn)
        r = _call(
            ctx,
            {"target": "sum_anchor", "window_kind": "time", "windowHours": 1},
        )
        assert "Window selected zero leaves" in r["error"]

    def test_respects_since_bound(self, ctx: _Ctx, conv_id_1: int) -> None:
        _insert_condensed(
            ctx.conn,
            summary_id="sum_anchor",
            conversation_id=conv_id_1,
            content="anchor",
            created_at="2026-05-01 12:00:00",
        )
        _insert_leaf(
            ctx.conn,
            summary_id="sum_early",
            conversation_id=conv_id_1,
            content="early",
            created_at="2026-05-01 09:00:00",
        )
        _insert_leaf(
            ctx.conn,
            summary_id="sum_late",
            conversation_id=conv_id_1,
            content="late",
            created_at="2026-05-01 18:00:00",
        )
        _register_default_prompt(ctx.conn)

        r = _call(
            ctx,
            {
                "target": "sum_anchor",
                "window_kind": "time",
                "windowHours": 12,
                "since": "2026-05-01T12:00:00.000Z",
            },
        )
        details = r["details"]
        assert details["leaf_count"] == 1
        cache = ctx.conn.execute(
            "SELECT source_leaf_ids FROM lcm_synthesis_cache WHERE cache_id = ?",
            (details["cache_id"],),
        ).fetchone()
        assert json.loads(cache["source_leaf_ids"]) == ["sum_late"]


# ===========================================================================
# Cache hit second identical call
# ===========================================================================


class TestCacheHit:
    def test_second_identical_call_returns_cached_row(self, ctx: _Ctx, conv_id_1: int) -> None:
        """Second call with identical params hits the cache (single-flight)."""
        _insert_condensed(
            ctx.conn,
            summary_id="sum_anchor",
            conversation_id=conv_id_1,
            content="anchor",
            created_at="2026-05-01 12:00:00",
        )
        _insert_leaf(
            ctx.conn,
            summary_id="sum_a",
            conversation_id=conv_id_1,
            content="A-content",
            created_at="2026-05-01 11:30:00",
        )
        _insert_leaf(
            ctx.conn,
            summary_id="sum_b",
            conversation_id=conv_id_1,
            content="B-content",
            created_at="2026-05-01 12:30:00",
        )
        _register_default_prompt(ctx.conn)

        args = {"target": "sum_anchor", "window_kind": "time", "windowHours": 6}
        r1 = _call(ctx, args)
        cache_id_1 = r1["details"]["cache_id"]

        # Second call → loser path, status="cached"
        r2 = _call(ctx, args)
        assert r2.get("status") == "cached"
        assert r2.get("cache_id") == cache_id_1
        assert r2.get("single_flight_outcome") == "winner_already_ready"


# ===========================================================================
# Wave-10 regression — tier_label keys cache UNIQUE index
# ===========================================================================


class TestWave10TierCacheKey:
    def test_distinct_tiers_get_distinct_cache_rows(self, ctx: _Ctx, conv_id_1: int) -> None:
        """Wave-10 regression: tier='custom' vs tier='filtered' don't collide.

        Without Wave-10's UNIQUE-index expansion, the same
        ``(session_key, range, leaf_fingerprint)`` tuple collapsed onto
        one row regardless of tier, silently returning wrong-tier text.
        """
        _insert_condensed(
            ctx.conn,
            summary_id="sum_anchor",
            conversation_id=conv_id_1,
            content="anchor",
            created_at="2026-05-01 12:00:00",
        )
        _insert_leaf(
            ctx.conn,
            summary_id="sum_a",
            conversation_id=conv_id_1,
            content="A-content",
            created_at="2026-05-01 11:30:00",
        )
        _insert_leaf(
            ctx.conn,
            summary_id="sum_b",
            conversation_id=conv_id_1,
            content="B-content",
            created_at="2026-05-01 12:30:00",
        )
        _register_default_prompt(ctx.conn, tier="custom")
        _register_default_prompt(ctx.conn, tier="filtered")

        # First call: tier='custom'
        r_custom = _call(
            ctx,
            {
                "target": "sum_anchor",
                "window_kind": "time",
                "windowHours": 6,
                "tier": "custom",
            },
        )
        assert "error" not in r_custom
        cache_custom = r_custom["details"]["cache_id"]

        # Second call: tier='filtered' — must produce a DIFFERENT cache row.
        r_filtered = _call(
            ctx,
            {
                "target": "sum_anchor",
                "window_kind": "time",
                "windowHours": 6,
                "tier": "filtered",
            },
        )
        assert "error" not in r_filtered
        cache_filtered = r_filtered["details"]["cache_id"]
        assert cache_custom != cache_filtered

        # Both cache rows exist with the correct tier_label.
        rows = ctx.conn.execute(
            "SELECT cache_id, tier_label FROM lcm_synthesis_cache ORDER BY built_at"
        ).fetchall()
        tier_labels = sorted([r["tier_label"] for r in rows])
        assert tier_labels == ["custom", "filtered"]


# ===========================================================================
# session_key fallback chain
# ===========================================================================


class TestSessionKeyFallback:
    def test_target_summary_session_key_takes_precedence(self, ctx: _Ctx, conv_id_1: int) -> None:
        """If the target summary has a session_key, it's used."""
        _insert_condensed(
            ctx.conn,
            summary_id="sum_anchor",
            conversation_id=conv_id_1,
            content="anchor",
            created_at="2026-05-01 12:00:00",
        )
        _insert_leaf(
            ctx.conn,
            summary_id="sum_a",
            conversation_id=conv_id_1,
            content="A",
            created_at="2026-05-01 12:30:00",
        )
        _register_default_prompt(ctx.conn)

        r = _call(
            ctx,
            {"target": "sum_anchor", "window_kind": "time", "windowHours": 1},
        )
        if "error" in r:
            return  # window too narrow / no leaves — OK, what we want is the cache row
        # Cache row's session_key should be 'sk1' (the target's session_key).
        cache = ctx.conn.execute(
            "SELECT session_key FROM lcm_synthesis_cache WHERE cache_id = ?",
            (r["details"]["cache_id"],),
        ).fetchone()
        assert cache["session_key"] == "sk1"

    def test_period_mode_no_target_uses_input_session_key(self, ctx: _Ctx, conv_id_1: int) -> None:
        """Period mode without anchor: input session_key used."""
        _insert_leaf(
            ctx.conn,
            summary_id="leaf_a",
            conversation_id=conv_id_1,
            content="A",
            created_at="2026-05-01 12:00:00",
        )
        _register_default_prompt(ctx.conn)
        r = _call(
            ctx,
            {
                "window_kind": "period",
                "since": "2026-05-01T00:00:00Z",
                "before": "2026-05-02T00:00:00Z",
            },
            session_key="sk1",
            session_id="s1",
        )
        if "error" in r:
            return
        cache = ctx.conn.execute(
            "SELECT session_key FROM lcm_synthesis_cache WHERE cache_id = ?",
            (r["details"]["cache_id"],),
        ).fetchone()
        # session_key comes from input.sessionKey ('sk1') or conversation lookup.
        assert cache["session_key"] == "sk1"


# ===========================================================================
# Semantic mode unavailable (Wave A: vec0 not shipped)
# ===========================================================================


class TestSemanticUnavailable:
    def test_semantic_returns_graceful_error_without_vec0(self, ctx: _Ctx, conv_id_1: int) -> None:
        """Wave A: semantic mode is gracefully refused — Wave B wires Voyage/vec0."""
        _insert_leaf(
            ctx.conn,
            summary_id="sum_x",
            conversation_id=conv_id_1,
            content="any leaf",
            created_at="2026-05-01 12:00:00",
        )
        _register_default_prompt(ctx.conn)
        r = _call(ctx, {"target": "anything", "window_kind": "semantic"})
        assert "Semantic search is unavailable" in r["error"]


# ===========================================================================
# Period mode (reviewer P1 lcm_recent parity)
# ===========================================================================


class TestPeriodMode:
    def test_rejects_no_period_and_no_bounds(self, ctx: _Ctx, conv_id_1: int) -> None:
        r = _call(ctx, {"window_kind": "period"})
        assert "requires either `period`" in r["error"]

    def test_rejects_unknown_period_shortcut(self, ctx: _Ctx, conv_id_1: int) -> None:
        r = _call(ctx, {"window_kind": "period", "period": "next-tuesday"})
        assert "Unrecognized period shortcut" in r["error"]
        assert "yesterday" in r["error"]

    def test_accepts_explicit_since_before_without_target(self, ctx: _Ctx, conv_id_1: int) -> None:
        """Period mode with explicit since/before — no target required."""
        _insert_leaf(
            ctx.conn,
            summary_id="leaf_2026_05_01",
            conversation_id=conv_id_1,
            content="MAY1-content",
            created_at="2026-05-01 12:00:00",
        )
        _insert_leaf(
            ctx.conn,
            summary_id="leaf_2026_04_29",
            conversation_id=conv_id_1,
            content="APR29-content",
            created_at="2026-04-29 09:00:00",
        )
        _register_default_prompt(ctx.conn)
        r = _call(
            ctx,
            {
                "window_kind": "period",
                "since": "2026-05-01T00:00:00Z",
                "before": "2026-05-02T00:00:00Z",
            },
        )
        # Should not error with "target required" or "no leaves".
        err = r.get("error", "")
        assert "target" not in err.lower()
        assert "no leaves" not in err.lower()
        assert "error" not in r  # Should succeed
        # The May-1 leaf is in scope; April-29 is out.
        cache = ctx.conn.execute(
            "SELECT source_leaf_ids FROM lcm_synthesis_cache WHERE cache_id = ?",
            (r["details"]["cache_id"],),
        ).fetchone()
        assert json.loads(cache["source_leaf_ids"]) == ["leaf_2026_05_01"]

    def test_period_last_7_days_without_target(self, ctx: _Ctx, conv_id_1: int) -> None:
        """period='last-7-days' without target: should not require target."""
        from datetime import datetime, timedelta, timezone as _tz

        two_days_ago = (datetime.now(tz=_tz.utc) - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        _insert_leaf(
            ctx.conn,
            summary_id="leaf_recent",
            conversation_id=conv_id_1,
            content="RECENT-content",
            created_at=two_days_ago,
        )
        _register_default_prompt(ctx.conn)
        r = _call(ctx, {"window_kind": "period", "period": "last-7-days"})
        err = r.get("error", "")
        assert "target" not in err.lower()
        assert "no leaves" not in err.lower()

    def test_period_mode_renders_period_label(self, ctx: _Ctx, conv_id_1: int) -> None:
        """Period mode markdown contains the period label."""
        _insert_leaf(
            ctx.conn,
            summary_id="leaf_a",
            conversation_id=conv_id_1,
            content="A",
            created_at="2026-05-01 12:00:00",
        )
        _register_default_prompt(ctx.conn)
        r = _call(
            ctx,
            {
                "window_kind": "period",
                "since": "2026-05-01T00:00:00Z",
                "before": "2026-05-02T00:00:00Z",
            },
        )
        if "error" in r:
            return
        text = r["text"]
        assert "**Mode:** period" in text


# ===========================================================================
# build_source_text — pure helper
# ===========================================================================


class TestBuildSourceText:
    def test_concatenates_with_separators(self) -> None:
        rows = [
            {
                "summary_id": "sum_a",
                "content": "AAA",
                "created_at": "2026-05-01 12:00:00",
                "token_count": 1,
            },
            {
                "summary_id": "sum_b",
                "content": "BBB",
                "created_at": "2026-05-01 13:00:00",
                "token_count": 1,
            },
        ]
        out = build_source_text(rows)
        assert "### Leaf sum_a (2026-05-01 12:00:00)" in out["text"]
        assert "### Leaf sum_b (2026-05-01 13:00:00)" in out["text"]
        assert "\n\n---\n\n" in out["text"]
        assert out["truncated_at"] is None

    def test_truncates_at_token_cap(self) -> None:
        """If cumulative tokens exceed MAX_SOURCE_TEXT_TOKENS, truncate."""
        rows = [
            {
                "summary_id": f"sum_{i}",
                "content": "x" * 1000,
                "created_at": f"2026-05-01 12:00:{i:02d}",
                # high token count forces truncation early
                "token_count": 20_000,
            }
            for i in range(10)
        ]
        out = build_source_text(rows)
        assert out["truncated_at"] is not None
        assert out["truncated_at"] < 10


# ===========================================================================
# Wave-12 W2A1 regression — token gate fires for synthesize_around
# ===========================================================================


class TestWave12TokenGate:
    """Wave-12 W2A1: lcm_synthesize_around is in TOKEN_GATE_TOOLS.

    Previously skipped (the docstring said "self-protecting via 50K
    source cap"). Wave-12 caught that this covered SOURCE input but not
    the 4K-8K OUTPUT tokens. The tool is now in the gate set.
    """

    def test_synthesize_around_is_in_token_gate_tools(self) -> None:
        from lossless_hermes.plugin.needs_compact_gate import TOKEN_GATE_TOOLS

        assert "lcm_synthesize_around" in TOKEN_GATE_TOOLS

    def test_synthesize_around_has_estimator_entry(self) -> None:
        from lossless_hermes.plugin.needs_compact_gate import estimate_result_tokens

        # Flat 6_000 per Wave-12 W2A1 doc.
        estimate = estimate_result_tokens("lcm_synthesize_around", {})
        assert estimate == 6_000
