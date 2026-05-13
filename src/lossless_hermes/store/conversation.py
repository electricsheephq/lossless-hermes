"""ConversationStore — conversations + messages + message_parts CRUD + search.

Port of ``lossless-claw/src/store/conversation-store.ts`` (LCM commit
``1f07fbd``, 1,071 LOC TS → ~1,100 LOC Python).

Per ADR-017 the Python port is **synchronous**: every method is a plain
``def`` over :class:`sqlite3.Connection`. The TS source's ``async``
markings + ``await`` keywords are dropped; the underlying ``sqlite3``
calls are already blocking and benefit from the 30 s busy-timeout
configured by :func:`lossless_hermes.db.connection.open_lcm_db`.

Public surface (29 methods, per storage.md §4.1):

* ``create_conversation`` / ``get_conversation`` /
  ``get_conversation_by_session_id`` / ``get_conversation_by_session_key`` /
  ``get_conversation_family_ids`` / ``get_conversation_for_session`` /
  ``list_active_conversations`` / ``get_or_create_conversation`` /
  ``mark_conversation_bootstrapped`` / ``archive_conversation``.
* ``create_message`` / ``create_messages_bulk`` / ``get_messages`` /
  ``get_last_message`` / ``has_message`` /
  ``count_messages_by_identity`` / ``get_message_by_id`` /
  ``get_message_count`` / ``get_max_seq``.
* ``create_message_parts`` / ``get_message_parts``.
* ``delete_messages``.
* ``search_messages`` (dispatcher) + ``_search_full_text`` /
  ``_search_like`` / ``_search_regex`` (backends).
* ``with_transaction`` (re-entrant wrapper).
* ``_index_message_for_full_text`` / ``_delete_message_from_full_text``
  (FTS5 maintenance).

Design notes (Python deviations from TS):

1. **BigInt → int.** Python ``int`` is arbitrary-precision; we drop the
   defensive ``Number(row.message_id)`` casts. (spike-001 §INTEGER/INT64
   confirms ``sqlite3.Row`` returns native Python ``int``.)

2. **JSON.parse for message_parts.metadata.** The TS source leaves
   ``metadata`` as a string and lets callers ``JSON.parse`` it. We mirror
   that: :meth:`get_message_parts` returns the raw string (or ``None``);
   any JSON parsing is the caller's job. The issue-spec acceptance test
   asserts the row survives with ``metadata`` unchanged on invalid JSON
   — that's automatic since we don't parse it here.

3. **Regex flags.** The TS ``RegExp(pattern)`` is the Python ``re.compile``
   default (no flags). We mirror that — case-sensitive, byte-friendly
   (string) regex. A parity test in :file:`tests/test_conversation_store.py`
   spot-checks 10 representative patterns against the TS RegExp behavior.

4. **CJK snippet offsets.** TS ``string.slice`` is UTF-16 unit-based;
   Python ``str`` slicing is code-point-based. For non-surrogate-pair
   content (which is everything in LCM) the two are equivalent. The
   :func:`create_fallback_snippet` helper uses code-point slicing.

5. **`fts5_available` flag.** Probed once per DB by
   :func:`lossless_hermes.db.features.get_lcm_db_features`; cached on the
   store instance. Used to skip ``messages_fts`` writes when FTS5 is
   unavailable.

See:

* TS source: ``/Volumes/LEXAR/Claude/lossless-claw/src/store/conversation-store.ts``
* Issue spec: ``epics/01-storage/01-08-conversation-store.md``
* ADR-017 — synchronous-by-design.
* :mod:`lossless_hermes.store.message_identity` — the SHA-256 identity hash.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable, List, Literal, Sequence, TypeVar

from lossless_hermes.store.conversation_scope import (
    append_conversation_scope_constraint,
)
from lossless_hermes.store.fts5_sanitize import sanitize_fts5_query
from lossless_hermes.store.full_text_fallback import (
    build_like_search_plan,
    contains_cjk,
    create_fallback_snippet,
)
from lossless_hermes.store.full_text_sort import SearchSort, build_fts_order_by
from lossless_hermes.store.message_identity import build_message_identity_hash
from lossless_hermes.store.parse_utc_timestamp import (
    parse_utc_timestamp,
    parse_utc_timestamp_or_null,
)
# NOTE: Earlier drafts of this file imported a sync helper
# `with_database_transaction` from `lossless_hermes.transaction_mutex`. PR #19
# (issue 01-13) shipped the ABC-018-correct **async** `ConversationLockManager`
# in that module instead, so the sync helper no longer exists. Until a sync
# wrapper around the async manager lands (tracked as a follow-up; the
# `ConversationStore` callers are all in synchronous request paths today),
# this store opens its own `BEGIN IMMEDIATE` against the connection. The
# behavior is equivalent to the dropped `with_database_transaction` for the
# in-process single-writer scenario; cross-process serialization is provided
# by SQLite's `BEGIN IMMEDIATE` itself.

__all__ = [
    "ConversationId",
    "ConversationRecord",
    "ConversationStore",
    "CreateConversationInput",
    "CreateMessageInput",
    "CreateMessagePartInput",
    "MessageId",
    "MessagePartRecord",
    "MessagePartType",
    "MessageRecord",
    "MessageRole",
    "MessageSearchInput",
    "MessageSearchResult",
]

_log = logging.getLogger("lossless_hermes.store.conversation")

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Public type aliases (mirrors TS top-level exports lines 11-27)
# ---------------------------------------------------------------------------

ConversationId = int
MessageId = int
SummaryId = str
MessageRole = Literal["system", "user", "assistant", "tool"]
MessagePartType = Literal[
    "text",
    "reasoning",
    "tool",
    "patch",
    "file",
    "subtask",
    "compaction",
    "step_start",
    "step_finish",
    "snapshot",
    "agent",
    "retry",
]


# ---------------------------------------------------------------------------
# Input/Record dataclasses (mirrors TS type aliases lines 29-112)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CreateConversationInput:
    """Inputs for :meth:`ConversationStore.create_conversation`.

    Mirrors TS ``CreateConversationInput`` (lines 74-80). All fields
    except ``session_id`` are optional.

    Attributes:
        session_id: Stable session identifier (typically Hermes session
            UUID or LCM-internal token).
        session_key: Cross-conversation identity for session-family
            grouping. Subject to the partial UNIQUE on active rows.
        title: Optional human-readable conversation title.
        active: ``True`` (default) = active conversation; ``False`` =
            create as archived.
        archived_at: Optional explicit archival timestamp. If ``active``
            is False but ``archived_at`` is None, the row is still
            inserted (matches TS behavior).
    """

    session_id: str
    session_key: str | None = None
    title: str | None = None
    active: bool = True
    archived_at: datetime | None = None


@dataclass(frozen=True)
class ConversationRecord:
    """The shaped conversation row returned by query methods.

    Mirrors TS ``ConversationRecord`` (lines 82-92).
    """

    conversation_id: ConversationId
    session_id: str
    session_key: str | None
    active: bool
    archived_at: datetime | None
    title: str | None
    bootstrapped_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class CreateMessageInput:
    """Inputs for :meth:`ConversationStore.create_message`.

    Mirrors TS ``CreateMessageInput`` (lines 29-36).

    Attributes:
        conversation_id: Owning conversation.
        seq: Per-conversation sequence number (unique within a conv).
        role: One of the four roles in :data:`MessageRole`.
        content: The full message text.
        token_count: Caller-computed token count for budget accounting.
        identity_hash: Optional pre-computed dedup hash. When omitted,
            :meth:`create_message` computes
            ``build_message_identity_hash(role, content)``.
    """

    conversation_id: ConversationId
    seq: int
    role: MessageRole
    content: str
    token_count: int
    identity_hash: str | None = None


@dataclass(frozen=True)
class MessageRecord:
    """The shaped message row returned by query methods.

    Mirrors TS ``MessageRecord`` (lines 38-46).
    """

    message_id: MessageId
    conversation_id: ConversationId
    seq: int
    role: MessageRole
    content: str
    token_count: int
    created_at: datetime


@dataclass(frozen=True)
class CreateMessagePartInput:
    """Inputs for :meth:`ConversationStore.create_message_parts`.

    Mirrors TS ``CreateMessagePartInput`` (lines 48-58). Most fields are
    optional; the sparse 12-part-type schema means each part_type uses
    only a few of the available columns.
    """

    session_id: str
    part_type: MessagePartType
    ordinal: int
    text_content: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_input: str | None = None
    tool_output: str | None = None
    metadata: str | None = None


@dataclass(frozen=True)
class MessagePartRecord:
    """The shaped message_part row returned by :meth:`get_message_parts`.

    Mirrors TS ``MessagePartRecord`` (lines 60-72).
    """

    part_id: str
    message_id: MessageId
    session_id: str
    part_type: MessagePartType
    ordinal: int
    text_content: str | None
    tool_call_id: str | None
    tool_name: str | None
    tool_input: str | None
    tool_output: str | None
    metadata: str | None


@dataclass(frozen=True)
class MessageSearchInput:
    """Inputs for :meth:`ConversationStore.search_messages`.

    Mirrors TS ``MessageSearchInput`` (lines 94-103).

    Attributes:
        query: User search query.
        mode: ``"full_text"`` (FTS5 / LIKE) or ``"regex"`` (Python
            ``re.search`` against message content).
        conversation_id: Optional single-conversation scope.
        conversation_ids: Optional multi-conversation scope.
        since: Optional lower bound on ``created_at``.
        before: Optional upper bound on ``created_at`` (exclusive).
        limit: Optional result limit (default 50).
        sort: Optional sort mode for FTS5 backend (see
            :data:`lossless_hermes.store.full_text_sort.SearchSort`).
    """

    query: str
    mode: Literal["regex", "full_text"]
    conversation_id: ConversationId | None = None
    conversation_ids: Sequence[ConversationId] | None = None
    since: datetime | None = None
    before: datetime | None = None
    limit: int | None = None
    sort: SearchSort | None = None


@dataclass(frozen=True)
class MessageSearchResult:
    """A single match returned by :meth:`search_messages`.

    Mirrors TS ``MessageSearchResult`` (lines 105-112).
    """

    message_id: MessageId
    conversation_id: ConversationId
    role: MessageRole
    snippet: str
    created_at: datetime
    rank: float | None = None


# ---------------------------------------------------------------------------
# Row mappers (mirrors TS lines 171-222)
# ---------------------------------------------------------------------------


def _to_conversation_record(row: sqlite3.Row | tuple) -> ConversationRecord:
    """Map a ``conversations`` row to :class:`ConversationRecord`.

    Row tuple order (from SELECT in this module):
    ``(conversation_id, session_id, session_key, active, archived_at,
       title, bootstrapped_at, created_at, updated_at)``
    """
    return ConversationRecord(
        conversation_id=row[0],
        session_id=row[1],
        session_key=row[2] if row[2] is not None else None,
        active=row[3] == 1,
        archived_at=parse_utc_timestamp_or_null(row[4]),
        title=row[5],
        bootstrapped_at=parse_utc_timestamp_or_null(row[6]),
        created_at=parse_utc_timestamp(row[7]),
        updated_at=parse_utc_timestamp(row[8]),
    )


def _to_message_record(row: sqlite3.Row | tuple) -> MessageRecord:
    """Map a ``messages`` row to :class:`MessageRecord`.

    Row tuple order (from SELECT in this module):
    ``(message_id, conversation_id, seq, role, content, token_count, created_at)``
    """
    return MessageRecord(
        message_id=row[0],
        conversation_id=row[1],
        seq=row[2],
        role=row[3],
        content=row[4],
        token_count=row[5],
        created_at=parse_utc_timestamp(row[6]),
    )


def _to_message_part_record(row: sqlite3.Row | tuple) -> MessagePartRecord:
    """Map a ``message_parts`` row to :class:`MessagePartRecord`.

    Row tuple order:
    ``(part_id, message_id, session_id, part_type, ordinal, text_content,
       tool_call_id, tool_name, tool_input, tool_output, metadata)``
    """
    return MessagePartRecord(
        part_id=row[0],
        message_id=row[1],
        session_id=row[2],
        part_type=row[3],
        ordinal=row[4],
        text_content=row[5],
        tool_call_id=row[6],
        tool_name=row[7],
        tool_input=row[8],
        tool_output=row[9],
        metadata=row[10],
    )


def _normalize_message_content_for_full_text_index(content: str) -> str | None:
    """Strip ``[LCM File:`` / ``[LCM Tool Output:`` boilerplate for FTS indexing.

    Ports ``normalizeMessageContentForFullTextIndex`` (TS lines 224-264).

    For externalized references (large files / tool outputs that LCM
    stored elsewhere and replaced inline with a header + exploration
    summary), only the header line + the ``Exploration Summary:`` block
    is indexed — not the boilerplate ``Use lcm_describe ...`` helper
    text. For all other content the input is returned unchanged.

    Returns ``None`` when the input is empty/whitespace (so the caller
    skips the FTS INSERT).
    """
    if not isinstance(content, str):
        return None
    trimmed = content.strip()
    if not trimmed:
        return None

    is_externalized = trimmed.startswith("[LCM File:") or trimmed.startswith("[LCM Tool Output:")
    if not is_externalized:
        return content

    raw_lines = re.split(r"\r?\n", trimmed)
    lines: List[str] = []
    for raw in raw_lines:
        stripped = raw.strip()
        if stripped:
            lines.append(stripped)
    if not lines:
        return None

    header = lines[0] if lines else ""
    summary_lines: List[str] = []
    in_summary = False
    for line in lines[1:]:
        if line == "Exploration Summary:":
            in_summary = True
            continue
        if line.startswith("Use lcm_describe"):
            continue
        if in_summary:
            summary_lines.append(line)

    normalized = "\n".join(item for item in [header, *summary_lines] if item)
    return normalized or None


# ---------------------------------------------------------------------------
# ConversationStore
# ---------------------------------------------------------------------------


class ConversationStore:
    """CRUD layer for conversations + messages + message_parts + search.

    Per ADR-017 the store is **synchronous**: every method is a plain
    ``def`` over :class:`sqlite3.Connection`. There is no ``async`` /
    ``await`` / ``aiosqlite``.

    Instances are cheap — the only state is the connection reference and
    the cached ``fts5_available`` flag. Tests can instantiate one store
    per test with negligible overhead.

    Args:
        db: An open :class:`sqlite3.Connection` configured via
            :func:`lossless_hermes.db.connection.open_lcm_db`.
        fts5_available: Whether FTS5 is available on this connection.
            Defaults to ``True``; pass ``False`` to skip FTS5 writes
            (the LIKE-fallback path is used for searches in this case).
            Production callers resolve this from
            :func:`lossless_hermes.db.features.get_lcm_db_features`.
    """

    def __init__(
        self,
        db: sqlite3.Connection,
        *,
        fts5_available: bool = True,
    ) -> None:
        self._db = db
        self._fts5_available = fts5_available

    @property
    def fts5_available(self) -> bool:
        """Whether FTS5 writes/reads are enabled on this store."""
        return self._fts5_available

    # -----------------------------------------------------------------
    # Transaction helpers
    # -----------------------------------------------------------------

    def with_transaction(self, operation: Callable[[], T]) -> T:
        """Run ``operation`` inside a serialized DB transaction.

        Opens ``BEGIN IMMEDIATE`` for the outermost call (same semantics as
        TS source lines 280-282), or a SAVEPOINT for nested calls. On
        exception, the outermost call ROLLBACKs and re-raises; nested calls
        ROLLBACK TO SAVEPOINT then re-raise so the caller's outer txn keeps
        going.

        Follow-up: replace with a sync wrapper around
        :class:`lossless_hermes.transaction_mutex.ConversationLockManager`
        once that wrapper lands (see PR #19 module docstring + #01-13 spec).
        """
        # Python stdlib sqlite3's default `isolation_level=""` auto-opens
        # a deferred transaction on the first DML. Flush any pending implicit
        # txn before BEGIN IMMEDIATE to avoid "cannot start a transaction
        # within a transaction" errors.
        if self._db.in_transaction:
            # Nested savepoint path.
            savepoint = f"cs_{id(operation):x}"
            self._db.execute(f"SAVEPOINT {savepoint}")
            try:
                result = operation()
            except BaseException:
                self._db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self._db.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            self._db.execute(f"RELEASE SAVEPOINT {savepoint}")
            return result
        # Outermost path: explicit BEGIN IMMEDIATE.
        self._db.execute("BEGIN IMMEDIATE")
        try:
            result = operation()
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")
        return result

    # -----------------------------------------------------------------
    # Conversation operations
    # -----------------------------------------------------------------

    def create_conversation(self, input: CreateConversationInput) -> ConversationRecord:
        """Insert a new conversation row.

        Mirrors TS ``createConversation`` (lines 286-324). Handles the
        UNIQUE-race recovery: if the insert hits the partial UNIQUE on
        ``active session_key``, return the existing row instead of
        raising.

        Args:
            input: :class:`CreateConversationInput`.

        Returns:
            The newly inserted :class:`ConversationRecord` (or the
            existing row in the UNIQUE-race recovery branch).
        """
        try:
            cursor = self._db.execute(
                "INSERT INTO conversations "
                "(session_id, session_key, active, archived_at, title) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    input.session_id,
                    input.session_key,
                    0 if input.active is False else 1,
                    input.archived_at.isoformat() if input.archived_at else None,
                    input.title,
                ),
            )
            inserted_id = cursor.lastrowid
        except sqlite3.IntegrityError as exc:
            # Handle UNIQUE constraint race: another writer created the
            # conversation first. Match the TS error-string regex.
            msg = str(exc).lower()
            if "unique constraint failed" in msg or "sqlite_constraint_unique" in msg:
                if input.session_key:
                    existing = self.get_conversation_by_session_key(input.session_key)
                    if existing is not None:
                        return existing
                existing = self.get_conversation_by_session_id(input.session_id)
                if existing is not None:
                    return existing
            raise

        row = self._db.execute(
            "SELECT conversation_id, session_id, session_key, active, "
            "archived_at, title, bootstrapped_at, created_at, updated_at "
            "FROM conversations WHERE conversation_id = ?",
            (inserted_id,),
        ).fetchone()
        return _to_conversation_record(row)

    def get_conversation(self, conversation_id: ConversationId) -> ConversationRecord | None:
        """Fetch a conversation by primary key.

        Mirrors TS ``getConversation`` (lines 326-335).
        """
        row = self._db.execute(
            "SELECT conversation_id, session_id, session_key, active, "
            "archived_at, title, bootstrapped_at, created_at, updated_at "
            "FROM conversations WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        return _to_conversation_record(row) if row else None

    def get_conversation_by_session_id(self, session_id: str) -> ConversationRecord | None:
        """Fetch the newest active conversation for ``session_id``.

        Mirrors TS ``getConversationBySessionId`` (lines 337-349). Falls
        through to archived rows if no active row exists (the
        ``ORDER BY active DESC, created_at DESC`` clause).
        """
        row = self._db.execute(
            "SELECT conversation_id, session_id, session_key, active, "
            "archived_at, title, bootstrapped_at, created_at, updated_at "
            "FROM conversations "
            "WHERE session_id = ? "
            "ORDER BY active DESC, created_at DESC "
            "LIMIT 1",
            (session_id,),
        ).fetchone()
        return _to_conversation_record(row) if row else None

    def get_conversation_by_session_key(self, session_key: str) -> ConversationRecord | None:
        """Fetch the active conversation matching ``session_key``.

        Mirrors TS ``getConversationBySessionKey`` (lines 351-364).
        Only active rows are considered (the partial UNIQUE index
        ``conversations_active_session_key_idx`` guarantees at most one).
        """
        row = self._db.execute(
            "SELECT conversation_id, session_id, session_key, active, "
            "archived_at, title, bootstrapped_at, created_at, updated_at "
            "FROM conversations "
            "WHERE session_key = ? AND active = 1 "
            "ORDER BY created_at DESC "
            "LIMIT 1",
            (session_key,),
        ).fetchone()
        return _to_conversation_record(row) if row else None

    def get_conversation_family_ids(
        self,
        *,
        conversation_id: ConversationId | None = None,
        session_id: str | None = None,
        session_key: str | None = None,
    ) -> List[ConversationId]:
        """Return all conversation_ids in the same session-family.

        Mirrors TS ``getConversationFamilyIds`` (lines 366-404). The
        resolution order is:

        1. If ``conversation_id`` provided, look it up via
           :meth:`get_conversation`. Otherwise resolve via
           :meth:`get_conversation_for_session` from session_id /
           session_key.
        2. If the base conversation has a non-empty ``session_key``,
           return all conversations sharing that key.
        3. Otherwise return all conversations sharing the ``session_id``.

        Args:
            conversation_id: Resolve the family from a specific conv id.
            session_id: Resolve via session id (used if conv_id absent).
            session_key: Resolve via session key (preferred when present).

        Returns:
            Ordered list of conversation_ids: active first, then by
            ``created_at DESC``, then by ``conversation_id DESC``.
        """
        if conversation_id is not None:
            base = self.get_conversation(conversation_id)
        else:
            base = self.get_conversation_for_session(
                session_id=session_id,
                session_key=session_key,
            )
        if base is None:
            return []

        normalized_key = (base.session_key or "").strip()
        if normalized_key:
            rows = self._db.execute(
                "SELECT conversation_id FROM conversations "
                "WHERE session_key = ? "
                "ORDER BY active DESC, created_at DESC, conversation_id DESC",
                (normalized_key,),
            ).fetchall()
            return [row[0] for row in rows]

        rows = self._db.execute(
            "SELECT conversation_id FROM conversations "
            "WHERE session_id = ? "
            "ORDER BY active DESC, created_at DESC, conversation_id DESC",
            (base.session_id,),
        ).fetchall()
        return [row[0] for row in rows]

    def get_conversation_for_session(
        self,
        *,
        session_id: str | None = None,
        session_key: str | None = None,
    ) -> ConversationRecord | None:
        """Resolve which conversation to ingest into.

        Mirrors TS ``getConversationForSession`` (lines 406-425). Tries
        session_key first; falls back to session_id.

        Args:
            session_id: Stable session identifier.
            session_key: Cross-conversation identity (preferred).

        Returns:
            A :class:`ConversationRecord` or ``None`` if none matches.
        """
        normalized_key = (session_key or "").strip()
        if normalized_key:
            by_key = self.get_conversation_by_session_key(normalized_key)
            if by_key is not None:
                return by_key

        normalized_id = (session_id or "").strip()
        if not normalized_id:
            return None
        return self.get_conversation_by_session_id(normalized_id)

    def list_active_conversations(self, limit: int | None = None) -> List[ConversationRecord]:
        """List active conversations, newest first.

        Mirrors TS ``listActiveConversations`` (lines 427-444). Default
        limit is 1,000 if not specified.
        """
        normalized_limit = int(limit) if isinstance(limit, int) and limit > 0 else 1000
        rows = self._db.execute(
            "SELECT conversation_id, session_id, session_key, active, "
            "archived_at, title, bootstrapped_at, created_at, updated_at "
            "FROM conversations "
            "WHERE active = 1 "
            "ORDER BY updated_at DESC, conversation_id DESC "
            "LIMIT ?",
            (normalized_limit,),
        ).fetchall()
        return [_to_conversation_record(row) for row in rows]

    def get_or_create_conversation(
        self,
        session_id: str,
        *,
        title: str | None = None,
        session_key: str | None = None,
    ) -> ConversationRecord:
        """Atomic find-or-insert by ``session_id`` (and optional ``session_key``).

        Mirrors TS ``getOrCreateConversation`` (lines 446-487). Lookup
        order:

        1. If ``session_key`` is provided AND an active conversation
           matches it, return that row (updating its ``session_id`` if
           drifted).
        2. Else if an active conversation matches ``session_id`` and the
           caller didn't ask for a different ``session_key``, return it
           (filling in a missing ``session_key`` on the row if needed).
        3. Otherwise create a new conversation.

        Args:
            session_id: Stable session identifier.
            title: Optional human-readable title for newly created rows.
            session_key: Optional cross-conversation identity.

        Returns:
            The found or freshly created :class:`ConversationRecord`.
        """
        normalized_key = (session_key or "").strip() or None
        if normalized_key:
            by_key = self.get_conversation_by_session_key(normalized_key)
            if by_key is not None:
                if by_key.session_id != session_id:
                    self._db.execute(
                        "UPDATE conversations "
                        "SET session_id = ?, updated_at = datetime('now') "
                        "WHERE conversation_id = ?",
                        (session_id, by_key.conversation_id),
                    )
                    # Refresh from DB so the returned record reflects the new id.
                    refreshed = self.get_conversation(by_key.conversation_id)
                    if refreshed is not None:
                        return refreshed
                return by_key

        existing = self.get_conversation_by_session_id(session_id)
        if existing is not None:
            if normalized_key is None:
                return existing
            if existing.active and existing.session_key is None:
                self._db.execute(
                    "UPDATE conversations "
                    "SET session_key = ?, updated_at = datetime('now') "
                    "WHERE conversation_id = ?",
                    (normalized_key, existing.conversation_id),
                )
                refreshed = self.get_conversation(existing.conversation_id)
                if refreshed is not None:
                    return refreshed
                return existing
            if existing.active and existing.session_key == normalized_key:
                return existing

        return self.create_conversation(
            CreateConversationInput(
                session_id=session_id,
                title=title,
                session_key=normalized_key,
            )
        )

    def mark_conversation_bootstrapped(self, conversation_id: ConversationId) -> None:
        """Set ``bootstrapped_at`` if not already set.

        Mirrors TS ``markConversationBootstrapped`` (lines 489-498).
        Idempotent — ``COALESCE`` keeps the existing timestamp on
        re-bootstrap.
        """
        self._db.execute(
            "UPDATE conversations "
            "SET bootstrapped_at = COALESCE(bootstrapped_at, datetime('now')), "
            "    updated_at = datetime('now') "
            "WHERE conversation_id = ?",
            (conversation_id,),
        )

    def archive_conversation(self, conversation_id: ConversationId) -> None:
        """Set ``active = 0`` and ``archived_at`` if not already set.

        Mirrors TS ``archiveConversation`` (lines 500-510). Idempotent —
        re-archiving keeps the original archival timestamp.
        """
        self._db.execute(
            "UPDATE conversations "
            "SET active = 0, "
            "    archived_at = COALESCE(archived_at, datetime('now')), "
            "    updated_at = datetime('now') "
            "WHERE conversation_id = ?",
            (conversation_id,),
        )

    # -----------------------------------------------------------------
    # Message operations
    # -----------------------------------------------------------------

    def create_message(self, input: CreateMessageInput) -> MessageRecord:
        """Insert a single message and index it for full-text search.

        Mirrors TS ``createMessage`` (lines 514-541). Auto-computes
        ``identity_hash = build_message_identity_hash(role, content)`` if
        not supplied. Writes to ``messages_fts`` in the same transaction
        when FTS5 is available.

        Args:
            input: :class:`CreateMessageInput`.

        Returns:
            The newly inserted :class:`MessageRecord`.
        """
        identity_hash = input.identity_hash or build_message_identity_hash(
            input.role, input.content
        )
        cursor = self._db.execute(
            "INSERT INTO messages "
            "(conversation_id, seq, role, content, token_count, identity_hash) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                input.conversation_id,
                input.seq,
                input.role,
                input.content,
                input.token_count,
                identity_hash,
            ),
        )
        message_id = cursor.lastrowid
        assert message_id is not None

        self._index_message_for_full_text(message_id, input.content)

        row = self._db.execute(
            "SELECT message_id, conversation_id, seq, role, content, "
            "token_count, created_at "
            "FROM messages WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        return _to_message_record(row)

    def create_messages_bulk(self, inputs: Sequence[CreateMessageInput]) -> List[MessageRecord]:
        """Insert multiple messages transactionally.

        Mirrors TS ``createMessagesBulk`` (lines 543-574). Returns an
        empty list for empty input. Each insert auto-computes the
        identity hash and writes to ``messages_fts``.

        Args:
            inputs: Sequence of :class:`CreateMessageInput`.

        Returns:
            List of :class:`MessageRecord` in the same order as inputs.
        """
        if not inputs:
            return []

        records: List[MessageRecord] = []
        for input in inputs:
            identity_hash = input.identity_hash or build_message_identity_hash(
                input.role, input.content
            )
            cursor = self._db.execute(
                "INSERT INTO messages "
                "(conversation_id, seq, role, content, token_count, identity_hash) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    input.conversation_id,
                    input.seq,
                    input.role,
                    input.content,
                    input.token_count,
                    identity_hash,
                ),
            )
            message_id = cursor.lastrowid
            assert message_id is not None
            self._index_message_for_full_text(message_id, input.content)
            row = self._db.execute(
                "SELECT message_id, conversation_id, seq, role, content, "
                "token_count, created_at "
                "FROM messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            records.append(_to_message_record(row))
        return records

    def get_messages(
        self,
        conversation_id: ConversationId,
        *,
        after_seq: int | None = None,
        limit: int | None = None,
    ) -> List[MessageRecord]:
        """Return messages for ``conversation_id``, optionally bounded by seq.

        Mirrors TS ``getMessages`` (lines 576-605). Returns messages with
        ``seq > after_seq`` (default ``-1`` so all rows match) in
        ascending ``seq`` order.

        Args:
            conversation_id: Conversation to read from.
            after_seq: Optional lower bound (exclusive) on ``seq``.
            limit: Optional row limit.

        Returns:
            List of :class:`MessageRecord` in ``seq ASC`` order.
        """
        seq_bound = after_seq if after_seq is not None else -1
        if limit is not None:
            rows = self._db.execute(
                "SELECT message_id, conversation_id, seq, role, content, "
                "token_count, created_at "
                "FROM messages "
                "WHERE conversation_id = ? AND seq > ? "
                "ORDER BY seq "
                "LIMIT ?",
                (conversation_id, seq_bound, limit),
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT message_id, conversation_id, seq, role, content, "
                "token_count, created_at "
                "FROM messages "
                "WHERE conversation_id = ? AND seq > ? "
                "ORDER BY seq",
                (conversation_id, seq_bound),
            ).fetchall()
        return [_to_message_record(row) for row in rows]

    def get_last_message(self, conversation_id: ConversationId) -> MessageRecord | None:
        """Return the highest-seq message for ``conversation_id``.

        Mirrors TS ``getLastMessage`` (lines 607-619).
        """
        row = self._db.execute(
            "SELECT message_id, conversation_id, seq, role, content, "
            "token_count, created_at "
            "FROM messages "
            "WHERE conversation_id = ? "
            "ORDER BY seq DESC "
            "LIMIT 1",
            (conversation_id,),
        ).fetchone()
        return _to_message_record(row) if row else None

    def has_message(
        self,
        conversation_id: ConversationId,
        role: MessageRole,
        content: str,
    ) -> bool:
        """Return ``True`` if a message with this ``(role, content)`` exists.

        Mirrors TS ``hasMessage`` (lines 621-637). Computes the identity
        hash and checks for an existing row with matching hash + role +
        content. Used by ingest to dedup on replay.
        """
        identity_hash = build_message_identity_hash(role, content)
        row = self._db.execute(
            "SELECT 1 AS count "
            "FROM messages "
            "WHERE conversation_id = ? AND identity_hash = ? "
            "  AND role = ? AND content = ? "
            "LIMIT 1",
            (conversation_id, identity_hash, role, content),
        ).fetchone()
        return row is not None and row[0] == 1

    def count_messages_by_identity(
        self,
        conversation_id: ConversationId,
        role: MessageRole,
        content: str,
    ) -> int:
        """Return the count of messages with this ``(role, content)`` hash.

        Mirrors TS ``countMessagesByIdentity`` (lines 639-654). Used for
        diagnostic counting (vs. boolean :meth:`has_message`).
        """
        identity_hash = build_message_identity_hash(role, content)
        row = self._db.execute(
            "SELECT COUNT(*) AS count "
            "FROM messages "
            "WHERE conversation_id = ? AND identity_hash = ? "
            "  AND role = ? AND content = ?",
            (conversation_id, identity_hash, role, content),
        ).fetchone()
        return row[0] if row else 0

    def get_message_by_id(
        self,
        message_id: MessageId,
        *,
        include_suppressed: bool = False,
    ) -> MessageRecord | None:
        """Return a message by its primary key.

        Mirrors TS ``getMessageById`` (lines 656-675).

        By default, suppressed messages (``suppressed_at IS NOT NULL``)
        are excluded — every agent-facing read path filters them per the
        v4.1 Final.review.3 fix (Loop 2 Leak 2.1+2.2 BLOCKER). Internal
        callers (integrity check, compaction, doctor) opt in via
        ``include_suppressed=True``.

        Args:
            message_id: The message primary key.
            include_suppressed: When ``True``, return the row even if
                ``suppressed_at`` is set. Default ``False``.

        Returns:
            A :class:`MessageRecord` or ``None`` if no matching row.
        """
        # v4.1 Final.review.3 fix (Loop 2 Leak 2.1+2.2 BLOCKER):
        # assembler.resolveMessageItem + retrieval.expandRecursive +
        # compaction.leafPass all called this without filtering
        # suppressed_at. After an operator suppress, the assembler hot
        # path was re-emitting suppressed message content to the agent
        # prompt. The §10 invariant requires every agent-facing read path
        # to filter suppressed_at IS NULL by default; internal callers
        # (integrity, compaction, doctor) opt-in via includeSuppressed=true.
        suppressed_clause = "" if include_suppressed else " AND suppressed_at IS NULL"
        row = self._db.execute(
            "SELECT message_id, conversation_id, seq, role, content, "
            "token_count, created_at "
            f"FROM messages WHERE message_id = ?{suppressed_clause}",
            (message_id,),
        ).fetchone()
        return _to_message_record(row) if row else None

    def create_message_parts(
        self,
        message_id: MessageId,
        parts: Sequence[CreateMessagePartInput],
    ) -> None:
        """Bulk insert message_parts rows.

        Mirrors TS ``createMessageParts`` (lines 677-713). Each part gets
        a freshly generated ``part_id`` (UUID4). Empty input is a no-op.

        Args:
            message_id: The owning message.
            parts: Sequence of :class:`CreateMessagePartInput`.
        """
        if not parts:
            return
        rows: List[tuple] = []
        for part in parts:
            rows.append((
                str(uuid.uuid4()),
                message_id,
                part.session_id,
                part.part_type,
                part.ordinal,
                part.text_content,
                part.tool_call_id,
                part.tool_name,
                part.tool_input,
                part.tool_output,
                part.metadata,
            ))
        self._db.executemany(
            "INSERT INTO message_parts ("
            "part_id, message_id, session_id, part_type, ordinal, "
            "text_content, tool_call_id, tool_name, tool_input, "
            "tool_output, metadata"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )

    def get_message_parts(self, message_id: MessageId) -> List[MessagePartRecord]:
        """Return all message_parts for ``message_id`` in ordinal order.

        Mirrors TS ``getMessageParts`` (lines 715-737).
        """
        rows = self._db.execute(
            "SELECT part_id, message_id, session_id, part_type, ordinal, "
            "text_content, tool_call_id, tool_name, tool_input, "
            "tool_output, metadata "
            "FROM message_parts "
            "WHERE message_id = ? "
            "ORDER BY ordinal",
            (message_id,),
        ).fetchall()
        return [_to_message_part_record(row) for row in rows]

    def get_message_count(self, conversation_id: ConversationId) -> int:
        """Return the number of messages in ``conversation_id``.

        Mirrors TS ``getMessageCount`` (lines 739-744).
        """
        row = self._db.execute(
            "SELECT COUNT(*) AS count FROM messages WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        return row[0] if row else 0

    def get_max_seq(self, conversation_id: ConversationId) -> int:
        """Return the largest ``seq`` in ``conversation_id``, or 0 if empty.

        Mirrors TS ``getMaxSeq`` (lines 746-754).
        """
        row = self._db.execute(
            "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM messages WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        return row[0] if row else 0

    # -----------------------------------------------------------------
    # Deletion
    # -----------------------------------------------------------------

    def delete_messages(self, message_ids: Sequence[MessageId]) -> int:
        """Delete messages and cascade to context_items, message_parts, FTS.

        Mirrors TS ``deleteMessages`` (lines 764-793). Returns the count
        of actually deleted messages.

        Skips messages referenced by ``summary_messages`` (already
        compacted) to avoid breaking the summary DAG — the
        ``ON DELETE RESTRICT`` would fail anyway.

        For each non-skipped message:

        1. Delete corresponding ``context_items`` rows (RESTRICT).
        2. Delete from ``messages_fts`` (manual — FTS5 doesn't cascade).
        3. Delete the ``messages`` row (``message_parts`` cascade via FK).

        Args:
            message_ids: Sequence of message primary keys to delete.

        Returns:
            Count of actually deleted messages (excluding skipped ones).
        """
        if not message_ids:
            return 0

        deleted = 0
        for message_id in message_ids:
            # Skip if referenced by a summary (ON DELETE RESTRICT would fail).
            ref_row = self._db.execute(
                "SELECT 1 AS found FROM summary_messages WHERE message_id = ? LIMIT 1",
                (message_id,),
            ).fetchone()
            if ref_row is not None:
                continue

            # Remove from context_items first (RESTRICT constraint).
            self._db.execute(
                "DELETE FROM context_items WHERE item_type = 'message' AND message_id = ?",
                (message_id,),
            )

            self._delete_message_from_full_text(message_id)

            # Delete the message (message_parts cascade via FK).
            self._db.execute(
                "DELETE FROM messages WHERE message_id = ?",
                (message_id,),
            )
            deleted += 1
        return deleted

    # -----------------------------------------------------------------
    # Search dispatcher + backends
    # -----------------------------------------------------------------

    def search_messages(self, input: MessageSearchInput) -> List[MessageSearchResult]:
        """Dispatch to the appropriate search backend.

        Mirrors TS ``searchMessages`` (lines 797-852).

        Routing:

        * ``mode="full_text"``:
          - If query contains CJK → :meth:`_search_like` (FTS5 unicode61
            tokenizer cannot index CJK reliably).
          - Else if FTS5 available → :meth:`_search_full_text`, falling
            back to :meth:`_search_like` on any exception.
          - Else → :meth:`_search_like`.
        * ``mode="regex"`` → :meth:`_search_regex`.

        Args:
            input: :class:`MessageSearchInput`.

        Returns:
            List of :class:`MessageSearchResult` ranked by the backend.
        """
        limit = input.limit if input.limit is not None else 50

        if input.mode == "full_text":
            if contains_cjk(input.query):
                return self._search_like(
                    input.query,
                    limit,
                    conversation_id=input.conversation_id,
                    conversation_ids=input.conversation_ids,
                    since=input.since,
                    before=input.before,
                )
            if self._fts5_available:
                try:
                    return self._search_full_text(
                        input.query,
                        limit,
                        conversation_id=input.conversation_id,
                        conversation_ids=input.conversation_ids,
                        since=input.since,
                        before=input.before,
                        sort=input.sort,
                    )
                except sqlite3.DatabaseError:
                    return self._search_like(
                        input.query,
                        limit,
                        conversation_id=input.conversation_id,
                        conversation_ids=input.conversation_ids,
                        since=input.since,
                        before=input.before,
                    )
            return self._search_like(
                input.query,
                limit,
                conversation_id=input.conversation_id,
                conversation_ids=input.conversation_ids,
                since=input.since,
                before=input.before,
            )

        return self._search_regex(
            input.query,
            limit,
            conversation_id=input.conversation_id,
            conversation_ids=input.conversation_ids,
            since=input.since,
            before=input.before,
        )

    # -----------------------------------------------------------------
    # FTS5 maintenance + backends
    # -----------------------------------------------------------------

    def _index_message_for_full_text(self, message_id: MessageId, content: str) -> None:
        """Insert ``message_id`` + normalized content into ``messages_fts``.

        Mirrors TS ``indexMessageForFullText`` (lines 854-869). No-op
        when:

        * ``fts5_available`` is False.
        * The normalized content is empty (after externalized-reference
          stripping).
        * The ``messages_fts`` table doesn't exist (FTS5 issue #01-05
          hasn't landed yet — best-effort fail-silent).

        Failures are swallowed because FTS indexing is optional;
        persistence of the message row is authoritative.
        """
        if not self._fts5_available:
            return
        normalized = _normalize_message_content_for_full_text_index(content)
        if normalized is None:
            return
        try:
            self._db.execute(
                "INSERT INTO messages_fts(rowid, content) VALUES (?, ?)",
                (message_id, normalized),
            )
        except sqlite3.DatabaseError:
            # FTS table missing or other failure — message persistence is
            # authoritative; FTS is best-effort.
            pass

    def _delete_message_from_full_text(self, message_id: MessageId) -> None:
        """Remove ``message_id`` from ``messages_fts``.

        Mirrors TS ``deleteMessageFromFullText`` (lines 871-880).
        Best-effort: failures are swallowed.
        """
        if not self._fts5_available:
            return
        try:
            self._db.execute(
                "DELETE FROM messages_fts WHERE rowid = ?",
                (message_id,),
            )
        except sqlite3.DatabaseError:
            pass

    def _search_full_text(
        self,
        query: str,
        limit: int,
        *,
        conversation_id: ConversationId | None = None,
        conversation_ids: Sequence[ConversationId] | None = None,
        since: datetime | None = None,
        before: datetime | None = None,
        sort: SearchSort | None = None,
    ) -> List[MessageSearchResult]:
        """FTS5 backend for :meth:`search_messages`.

        Mirrors TS ``searchFullText`` (lines 882-927). Sanitizes the
        query via :func:`sanitize_fts5_query`, joins ``messages_fts``
        against ``messages``, filters out suppressed messages
        (``m.suppressed_at IS NULL``), and applies the conversation/
        time/sort scope.
        """
        where: List[str] = ["messages_fts MATCH ?"]
        args: List[object] = [sanitize_fts5_query(query)]
        # v4.1 Final.review P1 #2: filter suppressed messages.
        where.append("m.suppressed_at IS NULL")
        append_conversation_scope_constraint(
            where=where,
            args=args,
            column_expr="m.conversation_id",
            conversation_id=conversation_id,
            conversation_ids=conversation_ids,
        )
        if since:
            where.append("julianday(m.created_at) >= julianday(?)")
            args.append(since.isoformat())
        if before:
            where.append("julianday(m.created_at) < julianday(?)")
            args.append(before.isoformat())
        args.append(limit)
        order_by = build_fts_order_by(sort, "m.created_at")

        sql = (
            "SELECT m.message_id, m.conversation_id, m.role, "
            "snippet(messages_fts, 0, '', '', '...', 32) AS snippet, "
            "rank, m.created_at "
            "FROM messages_fts "
            "JOIN messages m ON m.message_id = messages_fts.rowid "
            f"WHERE {' AND '.join(where)} "
            f"ORDER BY {order_by} "
            "LIMIT ?"
        )
        rows = self._db.execute(sql, args).fetchall()
        return [
            MessageSearchResult(
                message_id=row[0],
                conversation_id=row[1],
                role=row[2],
                snippet=row[3],
                created_at=parse_utc_timestamp(row[5]),
                rank=row[4],
            )
            for row in rows
        ]

    def _search_like(
        self,
        query: str,
        limit: int,
        *,
        conversation_id: ConversationId | None = None,
        conversation_ids: Sequence[ConversationId] | None = None,
        since: datetime | None = None,
        before: datetime | None = None,
    ) -> List[MessageSearchResult]:
        """LIKE-fallback backend for :meth:`search_messages`.

        Mirrors TS ``searchLike`` (lines 929-992). Selects messages
        matching all normalized terms (AND semantics across the LIKE
        clauses) then filters in Python to confirm all terms appear in
        the (normalized) content. Snippet is built via
        :func:`create_fallback_snippet`.
        """
        plan = build_like_search_plan("content", query)
        if not plan.terms:
            return []

        where: List[str] = list(plan.where)
        args: List[object] = list(plan.args)
        # v4.1 Final.review P1 #2: filter suppressed messages.
        where.append("suppressed_at IS NULL")
        append_conversation_scope_constraint(
            where=where,
            args=args,
            column_expr="conversation_id",
            conversation_id=conversation_id,
            conversation_ids=conversation_ids,
        )
        if since:
            where.append("julianday(created_at) >= julianday(?)")
            args.append(since.isoformat())
        if before:
            where.append("julianday(created_at) < julianday(?)")
            args.append(before.isoformat())
        args.append(limit)

        where_clause = f"WHERE {' AND '.join(where)}" if where else ""
        rows = self._db.execute(
            "SELECT message_id, conversation_id, seq, role, content, "
            "token_count, created_at "
            "FROM messages "
            f"{where_clause} "
            "ORDER BY created_at DESC "
            "LIMIT ?",
            args,
        ).fetchall()

        results: List[MessageSearchResult] = []
        for row in rows:
            content = row[4]
            normalized_content = _normalize_message_content_for_full_text_index(content) or content
            haystack = normalized_content.lower()
            matches_all = all(term in haystack for term in plan.terms)
            if not matches_all:
                continue
            results.append(
                MessageSearchResult(
                    message_id=row[0],
                    conversation_id=row[1],
                    role=row[3],
                    snippet=create_fallback_snippet(normalized_content, plan.terms),
                    created_at=parse_utc_timestamp(row[6]),
                    rank=0,
                )
            )
        return results

    def _search_regex(
        self,
        pattern: str,
        limit: int,
        *,
        conversation_id: ConversationId | None = None,
        conversation_ids: Sequence[ConversationId] | None = None,
        since: datetime | None = None,
        before: datetime | None = None,
    ) -> List[MessageSearchResult]:
        """Regex backend for :meth:`search_messages`.

        Mirrors TS ``searchRegex`` (lines 994-1069). SQLite has no native
        POSIX regex, so we fetch candidate rows (bounded by
        ``SQL_SCAN_BOUND = 10_000``) and filter via Python ``re.search``.

        ReDoS guard: reject patterns longer than 500 chars or containing
        nested-quantifier-like sequences (``+)+``, ``*)?``, etc.).

        Wave-8 Auditor #7-12 P1 fix: bound the SQL scan. Previously the
        SELECT had no LIMIT and the JS-side MAX_ROW_SCAN was the only
        brake — meaning the whole ``messages`` table (potentially
        millions of rows × content blobs) materialized into memory
        before the scan fired.
        """
        # ReDoS guard: reject patterns with nested quantifiers or excessive length.
        if len(pattern) > 500 or re.search(r"(\+|\*|\?)\)(\+|\*|\?|\{\d)", pattern):
            return []
        try:
            re_pattern = re.compile(pattern)
        except re.error:
            return []

        where: List[str] = ["suppressed_at IS NULL"]  # v4.1 Final.review P1 #2
        args: List[object] = []
        append_conversation_scope_constraint(
            where=where,
            args=args,
            column_expr="conversation_id",
            conversation_id=conversation_id,
            conversation_ids=conversation_ids,
        )
        if since:
            where.append("julianday(created_at) >= julianday(?)")
            args.append(since.isoformat())
        if before:
            where.append("julianday(created_at) < julianday(?)")
            args.append(before.isoformat())
        where_clause = f"WHERE {' AND '.join(where)}" if where else ""
        # Wave-8 Auditor #7-12 P1 fix: bound the SQL scan. Previously the
        # SELECT had no LIMIT and JS-side MAX_ROW_SCAN=10K was the only
        # brake — meaning the whole `messages` table (potentially
        # millions of rows × content blobs) materialized into Node memory
        # before the JS scan fires. Mirror the summary-store fix from
        # W8 R1: bind a SQL LIMIT at the JS scan ceiling.
        SQL_SCAN_BOUND = 10_000
        args.append(SQL_SCAN_BOUND)
        rows = self._db.execute(
            "SELECT message_id, conversation_id, seq, role, content, "
            "token_count, created_at "
            "FROM messages "
            f"{where_clause} "
            "ORDER BY created_at DESC "
            "LIMIT ?",
            args,
        ).fetchall()

        MAX_ROW_SCAN = 10_000
        results: List[MessageSearchResult] = []
        scanned = 0
        for row in rows:
            if len(results) >= limit or scanned >= MAX_ROW_SCAN:
                break
            scanned += 1
            match = re_pattern.search(row[4])
            if match:
                results.append(
                    MessageSearchResult(
                        message_id=row[0],
                        conversation_id=row[1],
                        role=row[3],
                        snippet=match.group(0),
                        created_at=parse_utc_timestamp(row[6]),
                        rank=0,
                    )
                )
        return results
