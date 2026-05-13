"""Tests for :mod:`lossless_hermes.tools.conversation_scope` (issue 06-05).

Ports parity checks for ``lossless-claw/src/tools/lcm-conversation-scope.ts``
(LCM commit ``1f07fbd`` on branch ``pr-613``, 162 LOC TS → ~190 LOC Python).

Coverage:

* :func:`parse_iso_timestamp_param` — absent key, empty string, non-string
  value, invalid ISO string (ValueError), valid ISO with/without Z suffix,
  whitespace trimming, and a smoke for the ``since > before`` ordering
  (the caller's concern, but the parser is shared).
* :func:`resolve_lcm_conversation_scope` — each branch of the resolution
  priority order:

  1. Explicit ``conversationId`` (int + float + bool-rejection + NaN
     rejection).
  2. ``allConversations: True``.
  3. Session-key → ``get_conversation_by_session_key`` + family expansion.
  4. Session-id → ``get_conversation_for_session`` + family expansion.
  5. ``deps.resolve_session_id_from_session_key`` fallback.
  6. No match → empty scope.

* Family expansion — seeds a parent + 2 child conversations sharing one
  ``session_key``; asserts the returned ``conversation_ids`` lists all 3.

Setup mirrors ``tests/test_conversation_store.py``: an in-memory SQLite
with the migration ladder applied, then a ``ConversationStore`` wrapped
in a tiny stub object that exposes a ``_conversation_store`` attribute
(satisfying the ``_LcmLike`` Protocol the resolver consumes).

References:

* :mod:`lossless_hermes.tools.conversation_scope` — implementation.
* ``/Volumes/LEXAR/Claude/lossless-claw/src/tools/lcm-conversation-scope.ts`` — TS source.
* ``epics/06-tools/06-05-conversation-scope.md`` — issue spec.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

import pytest

from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.store.conversation import (
    ConversationStore,
    CreateConversationInput,
)
from lossless_hermes.tools.conversation_scope import (
    LcmConversationScope,
    LcmDependencies,
    parse_iso_timestamp_param,
    resolve_lcm_conversation_scope,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class _LcmStub:
    """Minimal stand-in satisfying the ``_LcmLike`` protocol."""

    _conversation_store: Optional[ConversationStore]


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    """In-memory SQLite with the migration ladder applied + FK on."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(conn, seed_default_prompts=False)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def store(db: sqlite3.Connection) -> ConversationStore:
    """ConversationStore over the in-memory DB."""
    return ConversationStore(db, fts5_available=False)


@pytest.fixture
def lcm(store: ConversationStore) -> _LcmStub:
    """LcmStub wrapping the store — satisfies the resolver's protocol."""
    return _LcmStub(_conversation_store=store)


# ===========================================================================
# parse_iso_timestamp_param
# ===========================================================================


class TestParseIsoTimestampParam:
    """Coverage for :func:`parse_iso_timestamp_param`."""

    def test_absent_key_returns_none(self) -> None:
        """Missing key → ``None``."""
        assert parse_iso_timestamp_param({}, "since") is None

    def test_empty_string_returns_none(self) -> None:
        """Empty string → ``None``."""
        assert parse_iso_timestamp_param({"since": ""}, "since") is None

    def test_whitespace_only_returns_none(self) -> None:
        """All-whitespace → ``None`` (after .strip())."""
        assert parse_iso_timestamp_param({"since": "   \t\n  "}, "since") is None

    def test_non_string_returns_none(self) -> None:
        """Non-string values (number, None, list) silently → ``None``."""
        assert parse_iso_timestamp_param({"since": 12345}, "since") is None
        assert parse_iso_timestamp_param({"since": None}, "since") is None
        assert parse_iso_timestamp_param({"since": [1, 2]}, "since") is None
        assert parse_iso_timestamp_param({"since": {"k": "v"}}, "since") is None

    def test_valid_iso_no_tz(self) -> None:
        """Valid ISO without timezone — returns naive datetime."""
        result = parse_iso_timestamp_param({"since": "2025-06-15T12:30:00"}, "since")
        assert result == datetime(2025, 6, 15, 12, 30, 0)

    def test_valid_iso_with_offset(self) -> None:
        """Valid ISO with explicit offset — returns aware datetime."""
        result = parse_iso_timestamp_param({"before": "2025-06-15T12:30:00+02:00"}, "before")
        assert result is not None
        assert result.tzinfo is not None

    def test_valid_iso_with_z_suffix(self) -> None:
        """Valid ISO with ``Z`` suffix — Python 3.11+ accepts directly."""
        result = parse_iso_timestamp_param({"since": "2025-06-15T12:30:00Z"}, "since")
        assert result is not None
        assert result.tzinfo == timezone.utc

    def test_valid_iso_trims_whitespace(self) -> None:
        """Leading/trailing whitespace is stripped before parsing."""
        result = parse_iso_timestamp_param({"since": "  2025-06-15T12:30:00Z  "}, "since")
        assert result is not None
        assert result.year == 2025

    def test_invalid_iso_raises_with_key_in_message(self) -> None:
        """Garbage value → :class:`ValueError` mentioning the key name."""
        with pytest.raises(ValueError, match="since must be a valid ISO timestamp."):
            parse_iso_timestamp_param({"since": "not-a-timestamp"}, "since")

    def test_invalid_iso_error_mentions_correct_key(self) -> None:
        """Error message contains the supplied key (``before``, not ``since``)."""
        with pytest.raises(ValueError, match="before must be a valid ISO timestamp."):
            parse_iso_timestamp_param({"before": "garbage"}, "before")

    def test_since_before_ordering_smoke(self) -> None:
        """Caller-level concern: ``since > before`` is detectable by the caller.

        The parser itself does not enforce ordering — it only returns
        the parsed timestamps. The caller compares them and surfaces a
        structured error. This test exists to lock down the expected
        public contract that the parser's output supports plain
        ``datetime`` comparison.
        """
        since = parse_iso_timestamp_param({"since": "2025-06-15T00:00:00Z"}, "since")
        before = parse_iso_timestamp_param({"before": "2025-06-14T00:00:00Z"}, "before")
        assert since is not None and before is not None
        assert since > before  # caller would surface a structured error here


# ===========================================================================
# resolve_lcm_conversation_scope — priority 1: explicit conversationId
# ===========================================================================


class TestExplicitConversationId:
    """Priority 1 of the resolution ladder."""

    def test_int_returns_single_id_scope(self, lcm: _LcmStub) -> None:
        """``{"conversationId": 42}`` → single-id scope."""
        result = resolve_lcm_conversation_scope(lcm=lcm, params={"conversationId": 42})
        assert result == LcmConversationScope(
            conversation_id=42, conversation_ids=[42], all_conversations=False
        )

    def test_finite_float_truncates_to_int(self, lcm: _LcmStub) -> None:
        """``{"conversationId": 7.9}`` → truncates to 7 (TS ``Math.trunc`` parity)."""
        result = resolve_lcm_conversation_scope(lcm=lcm, params={"conversationId": 7.9})
        assert result.conversation_id == 7
        assert result.conversation_ids == [7]

    def test_negative_float_truncates_toward_zero(self, lcm: _LcmStub) -> None:
        """Negative float truncates toward zero per TS ``Math.trunc``."""
        result = resolve_lcm_conversation_scope(lcm=lcm, params={"conversationId": -3.7})
        assert result.conversation_id == -3

    def test_nan_falls_through(self, lcm: _LcmStub, store: ConversationStore) -> None:
        """``float("nan")`` is rejected (TS ``Number.isFinite`` parity)."""
        # NaN should fall through to the no-match branch when no other
        # signal is supplied.
        result = resolve_lcm_conversation_scope(lcm=lcm, params={"conversationId": float("nan")})
        assert result.conversation_id is None
        assert result.all_conversations is False

    def test_infinity_falls_through(self, lcm: _LcmStub) -> None:
        """``+Infinity`` is rejected (TS ``Number.isFinite`` parity)."""
        result = resolve_lcm_conversation_scope(lcm=lcm, params={"conversationId": float("inf")})
        assert result.conversation_id is None

    def test_bool_rejected(self, lcm: _LcmStub) -> None:
        """``{"conversationId": True}`` does NOT truncate to 1.

        Python-specific: ``bool`` is a subclass of ``int``, so
        ``isinstance(True, int)`` is True. The implementation must
        explicitly reject bool values.
        """
        result = resolve_lcm_conversation_scope(lcm=lcm, params={"conversationId": True})
        assert result.conversation_id is None
        assert result.all_conversations is False

    def test_string_rejected(self, lcm: _LcmStub) -> None:
        """Strings are not numbers — fall through."""
        result = resolve_lcm_conversation_scope(lcm=lcm, params={"conversationId": "42"})
        assert result.conversation_id is None


# ===========================================================================
# resolve_lcm_conversation_scope — priority 2: allConversations=True
# ===========================================================================


class TestAllConversationsFlag:
    """Priority 2 of the resolution ladder."""

    def test_all_conversations_true(self, lcm: _LcmStub) -> None:
        """``{"allConversations": True}`` → cross-conversation scope."""
        result = resolve_lcm_conversation_scope(lcm=lcm, params={"allConversations": True})
        assert result == LcmConversationScope(
            conversation_id=None, conversation_ids=None, all_conversations=True
        )

    def test_all_conversations_false_falls_through(self, lcm: _LcmStub) -> None:
        """``{"allConversations": False}`` does NOT trigger priority 2."""
        result = resolve_lcm_conversation_scope(lcm=lcm, params={"allConversations": False})
        # No other signals → no-match branch.
        assert result.all_conversations is False
        assert result.conversation_id is None

    def test_truthy_non_true_falls_through(self, lcm: _LcmStub) -> None:
        """Only the literal ``True`` triggers priority 2 (TS ``=== true`` parity)."""
        result = resolve_lcm_conversation_scope(lcm=lcm, params={"allConversations": 1})
        # 1 is truthy but not literally True — fall through.
        assert result.all_conversations is False

    def test_explicit_id_takes_precedence(self, lcm: _LcmStub) -> None:
        """Priority 1 (explicit id) wins over priority 2 (allConversations)."""
        result = resolve_lcm_conversation_scope(
            lcm=lcm,
            params={"conversationId": 17, "allConversations": True},
        )
        assert result.conversation_id == 17
        assert result.all_conversations is False


# ===========================================================================
# resolve_lcm_conversation_scope — priority 3: session_key path
# ===========================================================================


class TestSessionKeyPath:
    """Priority 3 of the resolution ladder."""

    def test_session_key_no_match_falls_through(
        self, lcm: _LcmStub, store: ConversationStore
    ) -> None:
        """Unknown session_key → fall through to no-match (no session_id)."""
        result = resolve_lcm_conversation_scope(lcm=lcm, params={}, session_key="unknown-key")
        assert result.conversation_id is None
        assert result.all_conversations is False

    def test_session_key_single_conversation(self, lcm: _LcmStub, store: ConversationStore) -> None:
        """Known session_key → resolves to that conversation."""
        record = store.create_conversation(
            CreateConversationInput(session_id="sess-1", session_key="key-A", title="Test")
        )
        result = resolve_lcm_conversation_scope(lcm=lcm, params={}, session_key="key-A")
        assert result.conversation_id == record.conversation_id
        assert result.conversation_ids == [record.conversation_id]
        assert result.all_conversations is False

    def test_session_key_with_whitespace_normalized(
        self, lcm: _LcmStub, store: ConversationStore
    ) -> None:
        """Leading/trailing whitespace on session_key is stripped before lookup."""
        record = store.create_conversation(
            CreateConversationInput(session_id="sess-1", session_key="key-trim", title="T")
        )
        result = resolve_lcm_conversation_scope(lcm=lcm, params={}, session_key="   key-trim   ")
        assert result.conversation_id == record.conversation_id


# ===========================================================================
# resolve_lcm_conversation_scope — family expansion
# ===========================================================================


class TestFamilyExpansion:
    """Family expansion via :meth:`ConversationStore.get_conversation_family_ids`."""

    def test_session_key_family_three_members(
        self, lcm: _LcmStub, store: ConversationStore, db: sqlite3.Connection
    ) -> None:
        """Three conversations sharing a session_key → all three returned.

        Setup: one active conversation + two archived conversations
        sharing the same session_key. The store's
        ``get_conversation_family_ids`` returns rows ordered by
        ``active DESC, created_at DESC, conversation_id DESC``.
        """
        parent = store.create_conversation(
            CreateConversationInput(
                session_id="sess-parent",
                session_key="family-key",
                title="parent",
            )
        )
        # Archive the parent before creating children: only one row may
        # be active per session_key (the partial UNIQUE index enforces
        # this). The family-expansion logic returns active + archived
        # rows alike when the session_key matches.
        store.archive_conversation(parent.conversation_id)
        child1 = store.create_conversation(
            CreateConversationInput(
                session_id="sess-child-1",
                session_key="family-key",
                title="child1",
            )
        )
        store.archive_conversation(child1.conversation_id)
        child2 = store.create_conversation(
            CreateConversationInput(
                session_id="sess-child-2",
                session_key="family-key",
                title="child2",
            )
        )

        # Resolve via session_key — should pick up the still-active
        # child2 as the anchor, but family_ids includes all 3.
        result = resolve_lcm_conversation_scope(lcm=lcm, params={}, session_key="family-key")
        assert result.conversation_id == child2.conversation_id
        assert result.conversation_ids is not None
        assert set(result.conversation_ids) == {
            parent.conversation_id,
            child1.conversation_id,
            child2.conversation_id,
        }
        assert len(result.conversation_ids) == 3
        assert result.all_conversations is False


# ===========================================================================
# resolve_lcm_conversation_scope — priority 4: session_id path
# ===========================================================================


class TestSessionIdPath:
    """Priority 4 of the resolution ladder."""

    def test_session_id_single_conversation(self, lcm: _LcmStub, store: ConversationStore) -> None:
        """Known session_id, no session_key → resolves via session_id."""
        record = store.create_conversation(
            CreateConversationInput(session_id="sess-by-id", title="T")
        )
        result = resolve_lcm_conversation_scope(lcm=lcm, params={}, session_id="sess-by-id")
        assert result.conversation_id == record.conversation_id
        assert result.conversation_ids == [record.conversation_id]

    def test_session_id_unknown_returns_no_match(
        self, lcm: _LcmStub, store: ConversationStore
    ) -> None:
        """Unknown session_id → no-match branch."""
        result = resolve_lcm_conversation_scope(lcm=lcm, params={}, session_id="unknown-session")
        assert result.conversation_id is None
        assert result.conversation_ids is None
        assert result.all_conversations is False

    def test_session_id_whitespace_normalized(
        self, lcm: _LcmStub, store: ConversationStore
    ) -> None:
        """Leading/trailing whitespace on session_id is stripped."""
        record = store.create_conversation(
            CreateConversationInput(session_id="sess-trim", title="T")
        )
        result = resolve_lcm_conversation_scope(lcm=lcm, params={}, session_id="  sess-trim  ")
        assert result.conversation_id == record.conversation_id


# ===========================================================================
# resolve_lcm_conversation_scope — deps fallback
# ===========================================================================


class TestDepsFallback:
    """Priority 4's ``deps.resolve_session_id_from_session_key`` fallback."""

    def test_deps_resolves_session_id_from_key(
        self, lcm: _LcmStub, store: ConversationStore
    ) -> None:
        """No session_id but session_key + deps callback → resolves via callback.

        The session_key itself does NOT match an active conversation (so
        priority 3 falls through); deps then maps it to a session_id
        that DOES match a conversation.
        """
        record = store.create_conversation(
            CreateConversationInput(session_id="resolved-sess-id", title="T")
        )

        def fake_resolver(key: str) -> Optional[str]:
            assert key == "external-key"
            return "resolved-sess-id"

        deps = LcmDependencies(resolve_session_id_from_session_key=fake_resolver)
        result = resolve_lcm_conversation_scope(
            lcm=lcm,
            params={},
            session_key="external-key",
            deps=deps,
        )
        assert result.conversation_id == record.conversation_id

    def test_deps_returns_none_falls_through(self, lcm: _LcmStub, store: ConversationStore) -> None:
        """Deps callback returning ``None`` → no-match branch."""
        deps = LcmDependencies(resolve_session_id_from_session_key=lambda _: None)
        result = resolve_lcm_conversation_scope(
            lcm=lcm,
            params={},
            session_key="unresolvable-key",
            deps=deps,
        )
        assert result.conversation_id is None
        assert result.all_conversations is False

    def test_deps_none_when_session_id_present(
        self, lcm: _LcmStub, store: ConversationStore
    ) -> None:
        """Deps callback is not consulted when session_id is already present.

        Guards the implementation against accidentally always-consulting
        the deps callback (which would be a perf regression on hot
        paths).
        """
        record = store.create_conversation(
            CreateConversationInput(session_id="direct-sess", title="T")
        )

        called = []

        def watching_resolver(key: str) -> Optional[str]:
            called.append(key)
            return None

        deps = LcmDependencies(resolve_session_id_from_session_key=watching_resolver)
        result = resolve_lcm_conversation_scope(
            lcm=lcm,
            params={},
            session_id="direct-sess",
            session_key="some-key",  # also provided, but session_id wins for the lookup
            deps=deps,
        )
        # session_key took priority 3 and may have matched; either way
        # the deps callback should NOT have been called because session_id
        # is non-empty.
        assert called == []
        # Resolution itself: session_key didn't match (no row with that
        # session_key exists), session_id "direct-sess" did. The result
        # anchor is record.conversation_id.
        assert result.conversation_id == record.conversation_id

    def test_no_session_data_at_all_returns_empty(self, lcm: _LcmStub) -> None:
        """No params + no session info → empty scope."""
        result = resolve_lcm_conversation_scope(lcm=lcm, params={})
        assert result == LcmConversationScope(
            conversation_id=None, conversation_ids=None, all_conversations=False
        )


# ===========================================================================
# resolve_lcm_conversation_scope — error handling
# ===========================================================================


class TestErrorHandling:
    """Defensive checks."""

    def test_store_none_raises_runtime_error(self) -> None:
        """``lcm._conversation_store = None`` → :class:`RuntimeError`."""
        lcm = _LcmStub(_conversation_store=None)
        with pytest.raises(RuntimeError, match="_conversation_store is None"):
            resolve_lcm_conversation_scope(lcm=lcm, params={})


# ===========================================================================
# resolve_lcm_conversation_scope — priority ordering smoke
# ===========================================================================


class TestPriorityOrdering:
    """End-to-end smoke tests of the 5-step priority ladder."""

    def test_priority_1_beats_3_and_4(self, lcm: _LcmStub, store: ConversationStore) -> None:
        """Explicit conversationId wins over session_key + session_id."""
        store.create_conversation(
            CreateConversationInput(session_id="real-sess", session_key="real-key", title="T")
        )
        result = resolve_lcm_conversation_scope(
            lcm=lcm,
            params={"conversationId": 999},
            session_id="real-sess",
            session_key="real-key",
        )
        # Explicit-id branch — ignores everything else.
        assert result.conversation_id == 999
        assert result.conversation_ids == [999]

    def test_priority_3_beats_4(self, lcm: _LcmStub, store: ConversationStore) -> None:
        """Session_key (if it matches) wins over session_id."""
        rec_key = store.create_conversation(
            CreateConversationInput(
                session_id="sess-A", session_key="key-priority", title="from-key"
            )
        )
        # Different session row by session_id only (no session_key).
        store.create_conversation(CreateConversationInput(session_id="sess-B", title="from-id"))
        result = resolve_lcm_conversation_scope(
            lcm=lcm,
            params={},
            session_id="sess-B",
            session_key="key-priority",
        )
        # session_key wins.
        assert result.conversation_id == rec_key.conversation_id
