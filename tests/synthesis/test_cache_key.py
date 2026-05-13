"""Tests for :mod:`lossless_hermes.synthesis.cache_key` (issue 07-06).

Ports the cache-key + single-flight slice of
``lossless-claw/test/lcm-synthesize-around-tool.test.ts`` plus the
Wave-10 P1 regression in ``v41-wave10-reviewer-regressions.test.ts``
(commit ``1f07fbd`` on branch ``pr-613``).

### Case mapping (TS → Python)

| TS describe block / case | Python test |
|---|---|
| ``fingerprintLeaves`` (TS:550-557, helper-level) | :class:`TestLeafFingerprint` |
| Wave-10 #2 distinct ``tier_label`` / ``prompt_id`` rows | :class:`TestWave10TierPromptCollision` |
| Wave-10 #2 collision on identical 7-tuple | :class:`TestWave10TierPromptCollision` |
| 4-step ``session_key`` fallback (TS:775-814) | :class:`TestResolveSessionKey` |
| INSERT OR IGNORE + SELECT-back single-flight | :class:`TestSingleFlight` |

### Additional port tests

* :class:`TestLeafFingerprintByteEqualitySnapshot` — SHA-256 byte-for-
  byte snapshot pins so Python implementation cannot drift from the TS
  source's fingerprinting bytes. Pattern matches PR #70 / #85.
* :class:`TestCacheIdShape` — every ``cache_id`` matches a 24-hex-char
  shape (per AC: ``secrets.token_hex(12)``).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator

import pytest

from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.synthesis.cache_key import (
    DEFAULT_SESSION_KEY,
    LEAF_FINGERPRINT_HEX_LEN,
    CacheKey,
    InvalidLeafIdError,
    generate_cache_id,
    insert_cache_row_single_flight,
    leaf_fingerprint,
    lookup_cache_row,
    resolve_session_key,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _setup_db() -> sqlite3.Connection:
    """Build an in-memory DB with FK enforcement + v4.1 schema applied.

    ``isolation_level=None`` (autocommit) mirrors ``test_dispatch.py`` —
    register_prompt issues its own BEGIN IMMEDIATE so the default
    ``isolation_level=""`` would conflict.
    """
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(db, fts5_available=False, seed_default_prompts=False)
    return db


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    """Migrated in-memory DB, FK enforcement on."""
    conn = _setup_db()
    try:
        yield conn
    finally:
        conn.close()


def _insert_prompt(
    db: sqlite3.Connection,
    *,
    prompt_id: str = "p_test",
    memory_type: str = "episodic-condensed",
    tier_label: str | None = "custom",
    pass_kind: str = "single",
    version: int = 1,
) -> str:
    """Insert a prompt row + return its prompt_id.

    Match the helper used in ``test_migration_v41.py`` but allow caller
    to vary all four fields so multiple distinct prompts can coexist.
    """
    db.execute(
        "INSERT INTO lcm_prompt_registry"
        " (prompt_id, memory_type, tier_label, pass_kind, version, template, active)"
        " VALUES (?, ?, ?, ?, ?, ?, 1)",
        (prompt_id, memory_type, tier_label, pass_kind, version, "T"),
    )
    return prompt_id


def _make_key(
    *,
    session_key: str = "sk1",
    range_start: str = "2026-05-01T00:00:00Z",
    range_end: str = "2026-05-02T00:00:00Z",
    leaf_fp: str = "fp1",
    grep_filter: str | None = None,
    tier_label: str = "custom",
    prompt_id: str = "p_test",
) -> CacheKey:
    """Convenience constructor for test keys."""
    return CacheKey(
        session_key=session_key,
        range_start=range_start,
        range_end=range_end,
        leaf_fingerprint=leaf_fp,
        grep_filter=grep_filter,
        tier_label=tier_label,
        prompt_id=prompt_id,
    )


# ---------------------------------------------------------------------------
# TestLeafFingerprint — port of TS fingerprintLeaves helper
# ---------------------------------------------------------------------------


class TestLeafFingerprint:
    """Ports the TS ``fingerprintLeaves`` helper at
    ``lossless-claw/src/tools/lcm-synthesize-around-tool.ts:550-557``."""

    def test_returns_24_hex_chars(self) -> None:
        """Output length matches :data:`LEAF_FINGERPRINT_HEX_LEN`."""
        fp = leaf_fingerprint(["s_a", "s_b"])
        assert len(fp) == LEAF_FINGERPRINT_HEX_LEN == 24
        assert re.fullmatch(r"[0-9a-f]{24}", fp), f"expected 24 hex chars, got {fp!r}"

    def test_order_sensitive_AC(self) -> None:
        """AC: different ordering produces different fingerprint.

        ``leaf_fingerprint(["a", "b"]) != leaf_fingerprint(["b", "a"])``.
        """
        a_b = leaf_fingerprint(["s_a", "s_b"])
        b_a = leaf_fingerprint(["s_b", "s_a"])
        assert a_b != b_a, (
            "fingerprint must be order-sensitive — leaf-selection paths "
            "produce IDs in deterministic-but-mode-specific order; "
            "the cache key must distinguish (a, b) from (b, a)"
        )

    def test_empty_list_returns_24_hex_chars(self) -> None:
        """Empty input returns SHA-256 of empty string, truncated."""
        fp = leaf_fingerprint([])
        # SHA-256("") truncated to 24 hex.
        expected = hashlib.sha256(b"").hexdigest()[:24]
        assert fp == expected

    def test_single_leaf_uses_nul_separator(self) -> None:
        """One ID still produces the same shape (ID + NUL byte)."""
        fp = leaf_fingerprint(["s_solo"])
        expected = hashlib.sha256(b"s_solo\0").hexdigest()[:24]
        assert fp == expected

    def test_AC_distinct_from_concat(self) -> None:
        """NUL separator means ``["ab", "cd"] != ["abcd"]`` and the like.

        Without the NUL between elements, ``["ab", "cd"]`` and
        ``["a", "bcd"]`` would collide trivially. Verify the NUL
        boundary is enforced.
        """
        with_nul = leaf_fingerprint(["ab", "cd"])
        # Same bytes, no NUL — manually computed reference.
        no_nul = hashlib.sha256(b"abcd").hexdigest()[:24]
        assert with_nul != no_nul

    def test_AC_rejects_nul_in_leaf_id(self) -> None:
        """A leaf_id with literal NUL must raise (residual risk per spec)."""
        with pytest.raises(InvalidLeafIdError, match="NUL byte"):
            leaf_fingerprint(["s_a", "s_b\0evil", "s_c"])

    def test_accepts_iterable_not_just_list(self) -> None:
        """API signature is :class:`Iterable`; a generator must work."""
        ids_gen = (f"s_{i}" for i in range(3))
        fp = leaf_fingerprint(ids_gen)
        # Compare to list form.
        fp_list = leaf_fingerprint(["s_0", "s_1", "s_2"])
        assert fp == fp_list


# ---------------------------------------------------------------------------
# TestLeafFingerprintByteEqualitySnapshot — TS parity pins
# ---------------------------------------------------------------------------


class TestLeafFingerprintByteEqualitySnapshot:
    """SHA-256 byte-for-byte snapshot pins versus the TS source.

    Hashes computed via the TS source at commit ``1f07fbd`` —
    ``lossless-claw/src/tools/lcm-synthesize-around-tool.ts:550-557``::

        function fingerprintLeaves(ids: string[]): string {
          const hash = createHash("sha256");
          for (const id of ids) {
            hash.update(id);
            hash.update("\\0");
          }
          return hash.digest("hex").slice(0, 24);
        }

    Drift in the Python implementation (intentional or accidental)
    surfaces as a test failure; reviewers cross-check against the TS
    source and update the snapshot deliberately, NOT quietly.

    Pattern mirrors PR #70 ``test_summarize_prompts.py`` and PR #85
    ``test_seed_prompts.py``.
    """

    EXPECTED_FINGERPRINTS: dict[tuple[str, ...], str] = {
        # Two-element ordered list (used by composite tests below).
        ("s_a", "s_b"): "248d81f90c821aa54b448e4f",
        # Three-element canonical ordered list.
        ("s_alpha", "s_beta", "s_gamma"): "e4e3f9e4b65ebaeddd915488",
        # Reversed order — different fingerprint, proves order-sensitivity.
        ("s_gamma", "s_beta", "s_alpha"): "cf8ae18d08557f13f3f951f6",
        # Single element with NUL trailer.
        ("s_solo",): "72bb2ef024e193bcb3ed40c8",
        # Empty list -> SHA-256("") truncated.
        (): "e3b0c44298fc1c149afbf4c8",
    }

    def test_byte_equality_pins_match_ts(self) -> None:
        """Each pinned input hashes to its TS-source pin.

        If Python's :func:`hashlib.sha256` ever changes encoding
        behaviour or the helper's NUL-separator logic drifts, exactly
        one of these will break and tell us which.
        """
        for inputs, expected in self.EXPECTED_FINGERPRINTS.items():
            actual = leaf_fingerprint(list(inputs))
            assert actual == expected, (
                f"leaf_fingerprint{inputs!r} drifted from TS pin (commit 1f07fbd). "
                f"actual={actual}, expected={expected}. If intentional, recompute "
                f"the TS hash and update EXPECTED_FINGERPRINTS."
            )


# ---------------------------------------------------------------------------
# TestResolveSessionKey — port of TS 4-step fallback (TS:775-814)
# ---------------------------------------------------------------------------


class TestResolveSessionKey:
    """Ports the 4-step session_key fallback at
    ``lossless-claw/src/tools/lcm-synthesize-around-tool.ts:775-814``
    (Wave-7 Auditor #6 P0).

    AC: ``session_key 4-step fallback unit test``.
    """

    def test_step_1_prefers_target_summary_session_key(self, db: sqlite3.Connection) -> None:
        """Step 1: target_summary's session_key wins when present."""
        result = resolve_session_key(
            db,
            target_summary_session_key="agent:claude-3:work-1",
            input_session_key="agent:claude-3:work-2",  # would be step 2
        )
        assert result == "agent:claude-3:work-1"

    def test_step_2_falls_back_to_input_session_key(self, db: sqlite3.Connection) -> None:
        """Step 2: input.session_key when target_summary lacks one."""
        result = resolve_session_key(
            db,
            target_summary_session_key=None,
            input_session_key="agent:claude-3:work-1",
        )
        assert result == "agent:claude-3:work-1"

    def test_step_2_skips_target_summary_empty_after_trim(self, db: sqlite3.Connection) -> None:
        """Step 1 with whitespace-only key falls through to step 2."""
        result = resolve_session_key(
            db,
            target_summary_session_key="   ",
            input_session_key="agent:claude-3:work-1",
        )
        assert result == "agent:claude-3:work-1"

    def test_step_3_falls_back_to_conversation_session_key(self, db: sqlite3.Connection) -> None:
        """Step 3: lookup ``conversations.session_key`` by first conv id."""
        db.execute(
            "INSERT INTO conversations (conversation_id, session_id, session_key)"
            " VALUES (42, 's', 'agent:claude-3:work-from-conv')"
        )
        result = resolve_session_key(
            db,
            target_summary_session_key=None,
            input_session_key=None,
            conversation_ids=[42],
        )
        assert result == "agent:claude-3:work-from-conv"

    def test_step_3_skips_conversations_with_null_session_key(self, db: sqlite3.Connection) -> None:
        """A conversation row with NULL session_key falls through to step 4."""
        db.execute(
            "INSERT INTO conversations (conversation_id, session_id, session_key)"
            " VALUES (43, 's', NULL)"
        )
        result = resolve_session_key(
            db,
            target_summary_session_key=None,
            input_session_key=None,
            conversation_ids=[43],
        )
        assert result == DEFAULT_SESSION_KEY

    def test_step_3_tries_each_conversation_id_in_order(self, db: sqlite3.Connection) -> None:
        """If first conv has no session_key, try the next one."""
        db.execute(
            "INSERT INTO conversations (conversation_id, session_id, session_key)"
            " VALUES (44, 's', NULL)"
        )
        db.execute(
            "INSERT INTO conversations (conversation_id, session_id, session_key)"
            " VALUES (45, 's', 'agent:claude-3:second-conv')"
        )
        result = resolve_session_key(
            db,
            target_summary_session_key=None,
            input_session_key=None,
            conversation_ids=[44, 45],
        )
        assert result == "agent:claude-3:second-conv"

    def test_step_4_default_when_all_else_empty(self, db: sqlite3.Connection) -> None:
        """Step 4: ``agent:main:main`` for shell/CLI callers w/o identity."""
        result = resolve_session_key(db)
        assert result == DEFAULT_SESSION_KEY
        assert result == "agent:main:main"

    def test_strips_whitespace_from_target_summary_key(self, db: sqlite3.Connection) -> None:
        """Padded keys are trimmed before returning (TS ``.trim()`` parity)."""
        result = resolve_session_key(
            db,
            target_summary_session_key="  agent:claude-3:padded  ",
        )
        assert result == "agent:claude-3:padded"


# ---------------------------------------------------------------------------
# TestWave10TierPromptCollision — Wave-10 P1 regression
# ---------------------------------------------------------------------------


class TestWave10TierPromptCollision:
    """LCM Wave-10 (2026-03-22) regression: ``tier_label`` + ``prompt_id``
    in the cache UNIQUE index.

    Ports ``lossless-claw/test/v41-wave10-reviewer-regressions.test.ts``
    lines 39-99 (the ``#2 — synthesis cache UNIQUE index includes
    tier_label + prompt_id`` describe block).

    Without these two columns in the UNIQUE constraint, ``tier='custom'``
    then ``tier='filtered'`` for the same ``(session_key, range,
    leaf_fingerprint)`` collapsed to one row, silently returning wrong-
    tier text. The fix widened both the migration UNIQUE index and the
    application-level lookup (this module).
    """

    def test_AC_distinct_tier_label_produces_distinct_rows(self, db: sqlite3.Connection) -> None:
        """AC: ``tier='custom'`` and ``tier='filtered'`` for the same leaf set
        produce two distinct cache rows (Wave-10 P1)."""
        _insert_prompt(db, prompt_id="p_custom", tier_label="custom", version=1)
        _insert_prompt(db, prompt_id="p_filtered", tier_label="filtered", version=2)

        # Two inserts, same (session_key, range, leaf_fingerprint),
        # different (tier_label, prompt_id). Pre-Wave-10 these would
        # collide on the 5-field UNIQUE; post-fix they're distinct rows.
        key_custom = _make_key(tier_label="custom", prompt_id="p_custom", leaf_fp="fp_shared")
        key_filtered = _make_key(tier_label="filtered", prompt_id="p_filtered", leaf_fp="fp_shared")

        result_a = insert_cache_row_single_flight(
            db,
            cache_id="c_custom",
            key=key_custom,
            model_used="m",
            source_leaf_ids_json="[]",
            source_token_count=0,
            actual_range_covered="r1..r2",
            leaf_count_synthesized=0,
        )
        result_b = insert_cache_row_single_flight(
            db,
            cache_id="c_filtered",
            key=key_filtered,
            model_used="m",
            source_leaf_ids_json="[]",
            source_token_count=0,
            actual_range_covered="r1..r2",
            leaf_count_synthesized=0,
        )

        assert result_a.won_latch is True, "first INSERT must win"
        assert result_b.won_latch is True, (
            "second INSERT with distinct tier+prompt MUST win — Wave-10 "
            "widened the UNIQUE index from 5 to 7 fields"
        )

        rows = db.execute(
            "SELECT cache_id, tier_label, prompt_id FROM lcm_synthesis_cache ORDER BY cache_id"
        ).fetchall()
        assert len(rows) == 2
        assert (rows[0][0], rows[0][1], rows[0][2]) == (
            "c_custom",
            "custom",
            "p_custom",
        )
        assert (rows[1][0], rows[1][1], rows[1][2]) == (
            "c_filtered",
            "filtered",
            "p_filtered",
        )

    def test_distinct_prompt_id_produces_distinct_rows(self, db: sqlite3.Connection) -> None:
        """Wave-10 #2 second leg: a fresh ``prompt_id`` for the same tier
        gets its own cache row (no stale-prompt service)."""
        _insert_prompt(db, prompt_id="p_v1", tier_label="custom", version=1)
        _insert_prompt(db, prompt_id="p_v2", tier_label="custom", version=2)

        key_v1 = _make_key(prompt_id="p_v1")
        key_v2 = _make_key(prompt_id="p_v2")

        result_a = insert_cache_row_single_flight(
            db,
            cache_id="c_v1",
            key=key_v1,
            model_used="m",
            source_leaf_ids_json="[]",
            source_token_count=0,
            actual_range_covered="r",
            leaf_count_synthesized=0,
        )
        result_b = insert_cache_row_single_flight(
            db,
            cache_id="c_v2",
            key=key_v2,
            model_used="m",
            source_leaf_ids_json="[]",
            source_token_count=0,
            actual_range_covered="r",
            leaf_count_synthesized=0,
        )
        assert result_a.won_latch and result_b.won_latch
        count = db.execute("SELECT COUNT(*) FROM lcm_synthesis_cache").fetchone()[0]
        assert count == 2

    def test_identical_7_tuple_collides_via_or_ignore(self, db: sqlite3.Connection) -> None:
        """Wave-10 #2 first leg: identical (session_key, range,
        leaf_fingerprint, tier, prompt) goes through the UNIQUE latch."""
        _insert_prompt(db, prompt_id="p_x", tier_label="custom", version=1)
        key = _make_key(tier_label="custom", prompt_id="p_x", leaf_fp="fp")

        result_a = insert_cache_row_single_flight(
            db,
            cache_id="c1",
            key=key,
            model_used="m",
            source_leaf_ids_json="[]",
            source_token_count=0,
            actual_range_covered="r",
            leaf_count_synthesized=0,
        )
        result_b = insert_cache_row_single_flight(
            db,
            cache_id="c2",  # different PK
            key=key,  # identical 7-tuple
            model_used="m",
            source_leaf_ids_json="[]",
            source_token_count=0,
            actual_range_covered="r",
            leaf_count_synthesized=0,
        )

        assert result_a.won_latch is True
        assert result_b.won_latch is False, (
            "second INSERT with identical 7-tuple MUST lose the latch (OR IGNORE no-op)"
        )
        # SELECT-back returns the winner's cache_id, not the loser's.
        assert result_b.cache_id == "c1"

        count = db.execute("SELECT COUNT(*) FROM lcm_synthesis_cache").fetchone()[0]
        assert count == 1


# ---------------------------------------------------------------------------
# TestSingleFlight — port of TS INSERT OR IGNORE + SELECT-back loop
# ---------------------------------------------------------------------------


class TestSingleFlight:
    """Ports the single-flight INSERT + SELECT-back semantics at
    ``lossless-claw/src/tools/lcm-synthesize-around-tool.ts:1182-1280``."""

    def test_winner_inserts_status_building(self, db: sqlite3.Connection) -> None:
        """A winning INSERT lands the row at ``status='building'``."""
        _insert_prompt(db, prompt_id="p_test")
        key = _make_key(prompt_id="p_test")
        result = insert_cache_row_single_flight(
            db,
            cache_id="cache_alpha",
            key=key,
            model_used="claude-3-haiku",
            source_leaf_ids_json=json.dumps(["s_a", "s_b"]),
            source_token_count=42,
            actual_range_covered="2026-05-01..2026-05-02",
            leaf_count_synthesized=2,
        )
        assert result.won_latch
        assert result.cache_id == "cache_alpha"

        row = db.execute(
            "SELECT status, model_used, source_leaf_ids, source_token_count,"
            " leaf_count_synthesized, prompt_id, tier_label, content"
            " FROM lcm_synthesis_cache WHERE cache_id = 'cache_alpha'"
        ).fetchone()
        assert row[0] == "building"
        assert row[1] == "claude-3-haiku"
        assert json.loads(row[2]) == ["s_a", "s_b"]
        assert row[3] == 42
        assert row[4] == 2
        assert row[5] == "p_test"
        assert row[6] == "custom"
        assert row[7] is None  # content NULL until ready

    def test_loser_sees_won_latch_false(self, db: sqlite3.Connection) -> None:
        """Concurrent caller's INSERT OR IGNORE returns ``won_latch=False``."""
        _insert_prompt(db, prompt_id="p_test")
        key = _make_key(prompt_id="p_test")
        first = insert_cache_row_single_flight(
            db,
            cache_id="c_first",
            key=key,
            model_used="m",
            source_leaf_ids_json="[]",
            source_token_count=0,
            actual_range_covered="r",
            leaf_count_synthesized=0,
        )
        second = insert_cache_row_single_flight(
            db,
            cache_id="c_second",
            key=key,
            model_used="m",
            source_leaf_ids_json="[]",
            source_token_count=0,
            actual_range_covered="r",
            leaf_count_synthesized=0,
        )
        assert first.won_latch is True
        assert second.won_latch is False
        assert second.cache_id == "c_first"  # SELECT-back returns winner's ID

    def test_lookup_cache_row_returns_winner(self, db: sqlite3.Connection) -> None:
        """:func:`lookup_cache_row` returns the matching row."""
        _insert_prompt(db, prompt_id="p_test")
        key = _make_key(prompt_id="p_test")
        result = insert_cache_row_single_flight(
            db,
            cache_id="cache_lookup",
            key=key,
            model_used="m",
            source_leaf_ids_json="[]",
            source_token_count=0,
            actual_range_covered="r",
            leaf_count_synthesized=0,
        )
        assert result.won_latch

        existing = lookup_cache_row(db, key)
        assert existing is not None
        assert existing.cache_id == "cache_lookup"
        assert existing.status == "building"
        assert existing.content is None
        assert existing.output_token_count == 0
        assert existing.building_started_at is not None
        assert existing.failure_reason is None

    def test_lookup_cache_row_returns_none_for_unknown_key(self, db: sqlite3.Connection) -> None:
        """A non-existent 7-tuple returns ``None``, not an empty row."""
        existing = lookup_cache_row(db, _make_key(prompt_id="p_missing"))
        assert existing is None

    def test_lookup_returns_ready_content_after_update(self, db: sqlite3.Connection) -> None:
        """After the winner UPDATEs to ``status='ready'`` with content, the
        loser-path lookup returns it (parity item: cache hit returns cached
        content without LLM call)."""
        _insert_prompt(db, prompt_id="p_test")
        key = _make_key(prompt_id="p_test")
        winner = insert_cache_row_single_flight(
            db,
            cache_id="c_ready",
            key=key,
            model_used="m",
            source_leaf_ids_json="[]",
            source_token_count=10,
            actual_range_covered="r",
            leaf_count_synthesized=1,
        )
        assert winner.won_latch
        db.execute(
            "UPDATE lcm_synthesis_cache SET status='ready', content=?, output_token_count=?"
            " WHERE cache_id=?",
            ("THE SYNTHESIS", 5, "c_ready"),
        )

        # Second caller arrives, INSERT OR IGNORE no-ops, SELECT-back finds ready row.
        loser = insert_cache_row_single_flight(
            db,
            cache_id="c_loser",
            key=key,
            model_used="m",
            source_leaf_ids_json="[]",
            source_token_count=10,
            actual_range_covered="r",
            leaf_count_synthesized=1,
        )
        assert loser.won_latch is False
        assert loser.cache_id == "c_ready"

        existing = lookup_cache_row(db, key)
        assert existing is not None
        assert existing.status == "ready"
        assert existing.content == "THE SYNTHESIS"
        assert existing.output_token_count == 5

    def test_null_grep_filter_and_empty_string_coalesce_match(self, db: sqlite3.Connection) -> None:
        """``COALESCE(grep_filter, '') = COALESCE(?, '')`` parity: ``None``
        and ``""`` both collide on the UNIQUE index (the index uses
        ``COALESCE(grep_filter, '')``)."""
        _insert_prompt(db, prompt_id="p_test")
        # NULL grep_filter
        key_null = _make_key(prompt_id="p_test", grep_filter=None)
        # Empty-string grep_filter
        key_empty = _make_key(prompt_id="p_test", grep_filter="")

        first = insert_cache_row_single_flight(
            db,
            cache_id="c_null",
            key=key_null,
            model_used="m",
            source_leaf_ids_json="[]",
            source_token_count=0,
            actual_range_covered="r",
            leaf_count_synthesized=0,
        )
        second = insert_cache_row_single_flight(
            db,
            cache_id="c_empty",
            key=key_empty,
            model_used="m",
            source_leaf_ids_json="[]",
            source_token_count=0,
            actual_range_covered="r",
            leaf_count_synthesized=0,
        )
        assert first.won_latch is True
        assert second.won_latch is False, (
            "NULL grep_filter and '' grep_filter must coalesce identically — "
            "COALESCE(grep_filter, '') in BOTH the UNIQUE index AND the SELECT "
            "WHERE clause is load-bearing for cache-hit parity"
        )
        # SELECT-back from the empty-string side finds the NULL row.
        existing = lookup_cache_row(db, key_empty)
        assert existing is not None
        assert existing.cache_id == "c_null"

    def test_non_null_grep_filter_keys_distinctly(self, db: sqlite3.Connection) -> None:
        """Different grep patterns are distinct cache rows."""
        _insert_prompt(db, prompt_id="p_test")
        key_a = _make_key(prompt_id="p_test", grep_filter="ERROR.*")
        key_b = _make_key(prompt_id="p_test", grep_filter="WARN.*")

        r1 = insert_cache_row_single_flight(
            db,
            cache_id="c_err",
            key=key_a,
            model_used="m",
            source_leaf_ids_json="[]",
            source_token_count=0,
            actual_range_covered="r",
            leaf_count_synthesized=0,
        )
        r2 = insert_cache_row_single_flight(
            db,
            cache_id="c_warn",
            key=key_b,
            model_used="m",
            source_leaf_ids_json="[]",
            source_token_count=0,
            actual_range_covered="r",
            leaf_count_synthesized=0,
        )
        assert r1.won_latch and r2.won_latch
        assert db.execute("SELECT COUNT(*) FROM lcm_synthesis_cache").fetchone()[0] == 2


# ---------------------------------------------------------------------------
# TestCacheIdShape
# ---------------------------------------------------------------------------


class TestCacheIdShape:
    """AC: ``cache_id`` generated as :func:`secrets.token_hex(12)` —
    24 hex chars / 96 bits of entropy."""

    def test_generate_cache_id_returns_24_hex(self) -> None:
        cid = generate_cache_id()
        assert len(cid) == 24
        assert re.fullmatch(r"[0-9a-f]{24}", cid), (
            f"cache_id must be 24 lowercase-hex chars (secrets.token_hex(12)), got {cid!r}"
        )

    def test_generate_cache_id_unique_across_many_calls(self) -> None:
        """96 bits of entropy → collision-free in any realistic test loop."""
        ids = {generate_cache_id() for _ in range(1000)}
        assert len(ids) == 1000
