"""SummaryStore — summaries + context_items + large_files + bootstrap_state CRUD.

Ports ``lossless-claw/src/store/summary-store.ts`` (LCM commit ``1f07fbd``,
1,668 LOC TS → ~1,500 LOC Python). The single largest non-migration store in
the LCM codebase.

Per ADR-017, the Python port is **synchronous**: every method is a plain
``def`` returning a record (or list of records). The TS source's ``async`` is
decorative — ``node:sqlite`` wraps sync C calls in Promises at the binding
boundary, but the underlying work is sync. We drop the ``async`` here.

Surface:

* **Summary CRUD:** :meth:`SummaryStore.insert_summary`,
  :meth:`SummaryStore.get_summary`,
  :meth:`SummaryStore.get_summaries_by_conversation`.
* **Lineage:** :meth:`SummaryStore.link_summary_to_messages`,
  :meth:`SummaryStore.link_summary_to_parents`,
  :meth:`SummaryStore.get_summary_messages`,
  :meth:`SummaryStore.get_conversation_max_summary_depth`,
  :meth:`SummaryStore.get_leaf_summary_links_for_message_ids`,
  :meth:`SummaryStore.list_transcript_gc_candidates`,
  :meth:`SummaryStore.get_summary_children`,
  :meth:`SummaryStore.get_summary_parents`,
  :meth:`SummaryStore.get_summary_subtree`.
* **Context items:** :meth:`SummaryStore.get_context_items`,
  :meth:`SummaryStore.get_distinct_depths_in_context`,
  :meth:`SummaryStore.prune_for_new_session`,
  :meth:`SummaryStore.append_context_message`,
  :meth:`SummaryStore.append_context_messages`,
  :meth:`SummaryStore.append_context_summary`,
  :meth:`SummaryStore.replace_context_range_with_summary`,
  :meth:`SummaryStore.get_context_token_count`.
* **Search:** :meth:`SummaryStore.search_summaries` (dispatcher across FTS5,
  CJK-trigram, LIKE, LIKE-CJK, and regex paths).
* **Large files:** :meth:`SummaryStore.insert_large_file`,
  :meth:`SummaryStore.get_large_file`,
  :meth:`SummaryStore.get_large_files_by_conversation`.
* **Bootstrap state:** :meth:`SummaryStore.get_conversation_bootstrap_state`,
  :meth:`SummaryStore.upsert_conversation_bootstrap_state`.
* **Transaction helper:** :meth:`SummaryStore.with_transaction`.

Key invariants (preserved from TS):

* **v4.1 §10 suppression filter:** every agent-facing read excludes
  ``suppressed_at IS NOT NULL`` by default; internal-tool callers pass
  ``include_suppressed=True`` to bypass.
* **Atomic context replacement:** :meth:`replace_context_range_with_summary`
  is wrapped in :meth:`with_transaction` so a mid-operation crash leaves the
  context_items table consistent (no half-replaced ranges).
* **Recursive subtree walks** use ``WITH RECURSIVE`` with a 10,000-node hard
  cap (Wave-4 Auditor #7 P1 fix — prevents runaway memory on pathological
  subtrees).

See:

* ``/Volumes/LEXAR/Claude/lossless-claw/src/store/summary-store.ts`` — TS
  canonical (commit ``1f07fbd``, 1,668 LOC).
* ``docs/porting-guides/storage.md`` §4.2 — the SummaryStore method table.
* ``docs/adr/017-store-sync-vs-async.md`` — sync-stores decision.
* ``epics/01-storage/01-09-summary-store.md`` — this module's issue spec.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator, Literal

from .conversation_scope import append_conversation_scope_constraint
from .fts5_sanitize import sanitize_fts5_query
from .full_text_fallback import (
    build_like_search_plan,
    contains_cjk,
    create_fallback_snippet,
)
from .full_text_sort import SearchSort, build_fts_order_by
from .parse_utc_timestamp import parse_utc_timestamp, parse_utc_timestamp_or_null

__all__ = [
    "ContextItemRecord",
    "ContextItemType",
    "ConversationBootstrapStateRecord",
    "CreateLargeFileInput",
    "CreateSummaryInput",
    "LargeFileRecord",
    "MessageLeafSummaryLinkRecord",
    "ReplaceContextRangeInput",
    "SummaryKind",
    "SummaryRecord",
    "SummarySearchInput",
    "SummarySearchResult",
    "SummaryStore",
    "SummarySubtreeNodeRecord",
    "TranscriptGcCandidateRecord",
    "UpsertConversationBootstrapStateInput",
]

# ── Type aliases ──────────────────────────────────────────────────────────────

SummaryKind = Literal["leaf", "condensed"]
ContextItemType = Literal["message", "summary"]
SearchMode = Literal["regex", "full_text"]


# ── Record dataclasses ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CreateSummaryInput:
    """Input shape for :meth:`SummaryStore.insert_summary`.

    Mirrors TS ``CreateSummaryInput`` (lines 12-26).
    """

    summary_id: str
    conversation_id: int
    kind: SummaryKind
    content: str
    token_count: int
    depth: int | None = None
    file_ids: list[str] | None = None
    earliest_at: datetime | None = None
    latest_at: datetime | None = None
    descendant_count: int | None = None
    descendant_token_count: int | None = None
    source_message_token_count: int | None = None
    model: str | None = None


@dataclass(frozen=True)
class SummaryRecord:
    """A summary row + derived fields.

    Mirrors TS ``SummaryRecord`` (lines 28-43).
    """

    summary_id: str
    conversation_id: int
    kind: SummaryKind
    depth: int
    content: str
    token_count: int
    file_ids: list[str]
    earliest_at: datetime | None
    latest_at: datetime | None
    descendant_count: int
    descendant_token_count: int
    source_message_token_count: int
    model: str
    created_at: datetime


@dataclass(frozen=True)
class SummarySubtreeNodeRecord:
    """A summary plus the walk-relative fields from :meth:`get_summary_subtree`.

    Mirrors TS ``SummarySubtreeNodeRecord`` (lines 45-50). All ``SummaryRecord``
    fields plus the four CTE-derived metadata fields.
    """

    summary_id: str
    conversation_id: int
    kind: SummaryKind
    depth: int
    content: str
    token_count: int
    file_ids: list[str]
    earliest_at: datetime | None
    latest_at: datetime | None
    descendant_count: int
    descendant_token_count: int
    source_message_token_count: int
    model: str
    created_at: datetime
    depth_from_root: int
    parent_summary_id: str | None
    path: str
    child_count: int


@dataclass(frozen=True)
class MessageLeafSummaryLinkRecord:
    """A (message_id, summary_id) pair from leaf-summary expansion lookups.

    Mirrors TS ``MessageLeafSummaryLinkRecord`` (lines 52-55).
    """

    message_id: int
    summary_id: str


@dataclass(frozen=True)
class ContextItemRecord:
    """One row of the assembled prompt's context_items ordering.

    Mirrors TS ``ContextItemRecord`` (lines 57-64).
    """

    conversation_id: int
    ordinal: int
    item_type: ContextItemType
    message_id: int | None
    summary_id: str | None
    created_at: datetime


@dataclass(frozen=True)
class SummarySearchInput:
    """Input shape for :meth:`SummaryStore.search_summaries`.

    Mirrors TS ``SummarySearchInput`` (lines 66-75).
    """

    query: str
    mode: SearchMode
    conversation_id: int | None = None
    conversation_ids: list[int] | None = None
    since: datetime | None = None
    before: datetime | None = None
    limit: int | None = None
    sort: SearchSort | None = None


@dataclass(frozen=True)
class SummarySearchResult:
    """A search result row.

    Mirrors TS ``SummarySearchResult`` (lines 77-85). ``created_at`` is the
    *effective* search timestamp (``COALESCE(latest_at, created_at)``), not the
    row's literal ``created_at``.
    """

    summary_id: str
    conversation_id: int
    kind: SummaryKind
    snippet: str
    created_at: datetime
    rank: float = 0.0


@dataclass(frozen=True)
class CreateLargeFileInput:
    """Input shape for :meth:`SummaryStore.insert_large_file`.

    Mirrors TS ``CreateLargeFileInput`` (lines 87-95).
    """

    file_id: str
    conversation_id: int
    storage_uri: str
    file_name: str | None = None
    mime_type: str | None = None
    byte_size: int | None = None
    exploration_summary: str | None = None


@dataclass(frozen=True)
class LargeFileRecord:
    """A large_files row.

    Mirrors TS ``LargeFileRecord`` (lines 97-106).
    """

    file_id: str
    conversation_id: int
    file_name: str | None
    mime_type: str | None
    byte_size: int | None
    storage_uri: str
    exploration_summary: str | None
    created_at: datetime


@dataclass(frozen=True)
class UpsertConversationBootstrapStateInput:
    """Input shape for :meth:`SummaryStore.upsert_conversation_bootstrap_state`.

    Mirrors TS ``UpsertConversationBootstrapStateInput`` (lines 108-115).
    """

    conversation_id: int
    session_file_path: str
    last_seen_size: int
    last_seen_mtime_ms: int
    last_processed_offset: int
    last_processed_entry_hash: str | None = None


@dataclass(frozen=True)
class ConversationBootstrapStateRecord:
    """A conversation_bootstrap_state row.

    Mirrors TS ``ConversationBootstrapStateRecord`` (lines 117-125).
    """

    conversation_id: int
    session_file_path: str
    last_seen_size: int
    last_seen_mtime_ms: int
    last_processed_offset: int
    last_processed_entry_hash: str | None
    updated_at: datetime


@dataclass(frozen=True)
class TranscriptGcCandidateRecord:
    """A messages row that's safe to GC after summarization.

    Mirrors TS ``TranscriptGcCandidateRecord`` (lines 127-135).
    """

    message_id: int
    conversation_id: int
    seq: int
    tool_call_id: str
    tool_name: str | None
    externalized_file_id: str | None
    original_byte_size: int | None


@dataclass(frozen=True)
class ReplaceContextRangeInput:
    """Input shape for :meth:`SummaryStore.replace_context_range_with_summary`.

    Mirrors TS inline-object argument (lines 1001-1006).
    """

    conversation_id: int
    start_ordinal: int
    end_ordinal: int
    summary_id: str


# ── Row mapper helpers ────────────────────────────────────────────────────────


def _coerce_nonneg_int(value: Any) -> int:
    """Floor + clamp to non-negative integer.

    Mirrors the TS pattern
    ``Number.isFinite(x) && x >= 0 ? Math.floor(x) : 0`` used throughout the
    summary row mappers.
    """
    if isinstance(value, bool):
        # Bools are ints in Python; reject to match the TS ``typeof === 'number'``.
        return 0
    if isinstance(value, (int, float)):
        if value < 0:
            return 0
        # ``Math.floor`` rounds toward negative infinity; for non-negatives that
        # matches ``int()`` truncation.
        try:
            return int(value)
        except (OverflowError, ValueError):
            return 0
    return 0


def _to_summary_record(row: dict[str, Any]) -> SummaryRecord:
    """Map a ``summaries`` row dict to :class:`SummaryRecord`.

    Mirrors TS ``toSummaryRecord`` (lines 243-281).
    """
    raw_file_ids = row.get("file_ids", "[]")
    try:
        file_ids = json.loads(raw_file_ids) if isinstance(raw_file_ids, str) else []
        if not isinstance(file_ids, list):
            file_ids = []
    except (json.JSONDecodeError, TypeError):
        # Ignore malformed JSON — matches TS behavior (lines 244-249).
        file_ids = []

    return SummaryRecord(
        summary_id=row["summary_id"],
        conversation_id=row["conversation_id"],
        kind=row["kind"],
        depth=row["depth"],
        content=row["content"],
        token_count=row["token_count"],
        file_ids=file_ids,
        earliest_at=parse_utc_timestamp_or_null(row.get("earliest_at")),
        latest_at=parse_utc_timestamp_or_null(row.get("latest_at")),
        descendant_count=_coerce_nonneg_int(row.get("descendant_count", 0)),
        descendant_token_count=_coerce_nonneg_int(row.get("descendant_token_count", 0)),
        source_message_token_count=_coerce_nonneg_int(row.get("source_message_token_count", 0)),
        model=row["model"] if isinstance(row.get("model"), str) else "unknown",
        created_at=parse_utc_timestamp(row["created_at"]),
    )


def _to_context_item_record(row: dict[str, Any]) -> ContextItemRecord:
    """Map a ``context_items`` row dict to :class:`ContextItemRecord`.

    Mirrors TS ``toContextItemRecord`` (lines 283-292).
    """
    return ContextItemRecord(
        conversation_id=row["conversation_id"],
        ordinal=row["ordinal"],
        item_type=row["item_type"],
        message_id=row.get("message_id"),
        summary_id=row.get("summary_id"),
        created_at=parse_utc_timestamp(row["created_at"]),
    )


def _to_search_result(row: dict[str, Any]) -> SummarySearchResult:
    """Map a search-row dict to :class:`SummarySearchResult`.

    Mirrors TS ``toSearchResult`` (lines 294-303).
    """
    return SummarySearchResult(
        summary_id=row["summary_id"],
        conversation_id=row["conversation_id"],
        kind=row["kind"],
        snippet=row["snippet"],
        created_at=parse_utc_timestamp(row["created_at"]),
        rank=float(row.get("rank", 0) or 0),
    )


def _to_large_file_record(row: dict[str, Any]) -> LargeFileRecord:
    """Map a ``large_files`` row to :class:`LargeFileRecord`.

    Mirrors TS ``toLargeFileRecord`` (lines 305-316).
    """
    return LargeFileRecord(
        file_id=row["file_id"],
        conversation_id=row["conversation_id"],
        file_name=row.get("file_name"),
        mime_type=row.get("mime_type"),
        byte_size=row.get("byte_size"),
        storage_uri=row["storage_uri"],
        exploration_summary=row.get("exploration_summary"),
        created_at=parse_utc_timestamp(row["created_at"]),
    )


def _to_conversation_bootstrap_state_record(
    row: dict[str, Any],
) -> ConversationBootstrapStateRecord:
    """Map a ``conversation_bootstrap_state`` row to a record.

    Mirrors TS ``toConversationBootstrapStateRecord`` (lines 318-330).
    """
    return ConversationBootstrapStateRecord(
        conversation_id=row["conversation_id"],
        session_file_path=row["session_file_path"],
        last_seen_size=row["last_seen_size"],
        last_seen_mtime_ms=row["last_seen_mtime_ms"],
        last_processed_offset=row["last_processed_offset"],
        last_processed_entry_hash=row.get("last_processed_entry_hash"),
        updated_at=parse_utc_timestamp(row["updated_at"]),
    )


def _to_transcript_gc_candidate_record(
    row: dict[str, Any],
) -> TranscriptGcCandidateRecord | None:
    """Map a ``messages JOIN message_parts`` row to a transcript-GC candidate.

    Returns ``None`` when the row is not a tool-output candidate (no tool_call_id,
    metadata not flagged ``toolOutputExternalized``). Mirrors TS
    ``toTranscriptGcCandidateRecord`` (lines 332-366).
    """
    tool_call_id = row.get("tool_call_id")
    if not isinstance(tool_call_id, str) or len(tool_call_id) == 0:
        return None

    raw_metadata = row.get("metadata")
    metadata: dict[str, Any] | None
    if isinstance(raw_metadata, str) and raw_metadata:
        try:
            parsed = json.loads(raw_metadata)
            metadata = parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            metadata = None
    else:
        metadata = None

    if not metadata or metadata.get("toolOutputExternalized") is not True:
        return None

    externalized_file_id_raw = metadata.get("externalizedFileId")
    externalized_file_id = (
        externalized_file_id_raw if isinstance(externalized_file_id_raw, str) else None
    )
    original_byte_size_raw = metadata.get("originalByteSize")
    if isinstance(original_byte_size_raw, bool):
        original_byte_size: int | None = None
    elif isinstance(original_byte_size_raw, (int, float)):
        original_byte_size = max(0, int(original_byte_size_raw))
    else:
        original_byte_size = None

    return TranscriptGcCandidateRecord(
        message_id=row["message_id"],
        conversation_id=row["conversation_id"],
        seq=row["seq"],
        tool_call_id=tool_call_id,
        tool_name=row.get("tool_name"),
        externalized_file_id=externalized_file_id,
        original_byte_size=original_byte_size,
    )


# ── SQL fragments ─────────────────────────────────────────────────────────────

# Mirrors TS ``SUMMARY_SEARCH_TIME_EXPR`` (line 181). Effective search timestamp
# is the latest covered content time, falling back to row insert time.
_SUMMARY_SEARCH_TIME_EXPR = "COALESCE(s.latest_at, s.created_at)"
_SUMMARY_SEARCH_TIME_EXPR_UNQUALIFIED = "COALESCE(latest_at, created_at)"

# CJK query segment matcher — matches a run of CJK characters in the query.
# Mirrors TS ``CJK_QUERY_SEGMENT_RE`` (lines 230-231).
_CJK_QUERY_SEGMENT_RE = re.compile("[⺀-鿿㐀-䶿豈-﫿가-힯぀-ゟ゠-ヿ]+")

# Latin token matcher — alphanumerics + ``_./-``. Mirrors TS
# ``LATIN_QUERY_TOKEN_RE`` (line 232).
_LATIN_QUERY_TOKEN_RE = re.compile(r"[a-zA-Z0-9][\w./\-]*")

# Wave-4 Auditor #7 P1 fix: cap subtree recursion to prevent runaway memory.
# 10K nodes is ~10× the largest realistic synthesis tree in Eva's actual DB.
_SUBTREE_HARD_CAP = 10_000

# Wave-8 Auditor #1 P1 fix: bound the regex-search SQL scan so we never stream
# the entire summaries table through the Python row loop.
_REGEX_SQL_SCAN_BOUND = 10_000
_REGEX_MAX_ROW_SCAN = 10_000

# ReDoS guard for regex search. Patterns longer than 500 chars or containing
# nested quantifiers are rejected outright.
_REGEX_PATTERN_MAX_LEN = 500
_REGEX_REDOS_PATTERN = re.compile(r"[+*?]\)[+*?{]")


# ── SummaryStore ──────────────────────────────────────────────────────────────


class SummaryStore:
    """SQLite-backed CRUD + search for summaries and related tables.

    Ports ``lossless-claw/src/store/summary-store.ts`` (LCM commit ``1f07fbd``).
    All public methods are synchronous per ADR-017.

    Attributes:
        _conn: The :class:`sqlite3.Connection` to operate against. Caller is
            responsible for opening and configuring the connection (PRAGMAs,
            FK enforcement) — typically via
            :func:`lossless_hermes.db.connection.open_lcm_db`.
        _fts5_available: Whether ``summaries_fts`` exists on this DB. False
            short-circuits the FTS5 search paths to LIKE fallback.
        _trigram_tokenizer_available: Whether ``summaries_fts_cjk`` (trigram
            tokenizer) is available. False routes CJK searches to LIKE-CJK
            fallback.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        fts5_available: bool = True,
        trigram_tokenizer_available: bool = True,
    ) -> None:
        """Initialize the SummaryStore.

        Args:
            conn: An open :class:`sqlite3.Connection`. The caller must have
                applied PRAGMA settings (foreign_keys, busy_timeout, etc.)
                before passing the connection here.
            fts5_available: Whether ``summaries_fts`` is available. Defaults
                to ``True`` (matching the TS default
                ``options?.fts5Available ?? true``).
            trigram_tokenizer_available: Whether ``summaries_fts_cjk`` is
                available. Defaults to ``True``. False routes CJK searches
                straight to the LIKE-CJK fallback path.
        """
        self._conn = conn
        self._fts5_available = fts5_available
        self._trigram_tokenizer_available = trigram_tokenizer_available

    # ── Transaction helper ────────────────────────────────────────────────────

    @contextmanager
    def with_transaction(self) -> Iterator[None]:
        """Run a block inside a serialized DB transaction.

        Synchronous port of TS ``withTransaction``. Ports
        ``transaction-mutex.withDatabaseTransaction(db, 'BEGIN', operation)``
        as a simple ``BEGIN`` / ``COMMIT`` / ``ROLLBACK`` envelope. The
        async-mutex / savepoint reentrancy from the TS source lives in #01-13;
        per ADR-017 the sync DB does not need cross-task serialization.

        Nested calls are detected via the connection's ``in_transaction``
        attribute and downgrade to a SAVEPOINT (mirrors the TS
        ``withDatabaseTransaction`` nested-call branch).

        Usage::

            with store.with_transaction():
                store.insert_summary(...)
                store.link_summary_to_messages(...)
        """
        if self._conn.in_transaction:
            # Nested call: use a savepoint so the outer transaction stays open.
            sp_name = f"summary_store_txn_{int(time.time() * 1000)}_{id(self)}"
            self._conn.execute(f"SAVEPOINT {sp_name}")
            try:
                yield
                self._conn.execute(f"RELEASE SAVEPOINT {sp_name}")
            except BaseException:
                self._conn.execute(f"ROLLBACK TO SAVEPOINT {sp_name}")
                self._conn.execute(f"RELEASE SAVEPOINT {sp_name}")
                raise
            return

        self._conn.execute("BEGIN")
        try:
            yield
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    # ── Summary CRUD ──────────────────────────────────────────────────────────

    def insert_summary(self, input_: CreateSummaryInput) -> SummaryRecord:
        """Insert a leaf or condensed summary; index in FTS5 (best-effort).

        Mirrors TS ``insertSummary`` (lines 382-517).

        Atomicity contract:

        * The ``summaries`` INSERT and the ``lcm_extraction_queue`` enqueue
          (leaf only) and the FTS5 index INSERTs (``summaries_fts``,
          ``summaries_fts_cjk``) are best-effort independent operations.
        * Failure of any FTS or extraction-queue insert does NOT roll back the
          ``summaries`` row — search degrades gracefully (the summary is
          findable via LIKE fallback but not via FTS5/trigram).
        * The leaf-write-hook MUST run BEFORE the FTS-availability early-return
          so FTS-disabled installs and in-memory test DBs still get the queue
          write (per TS comment at lines 472-473).

        Args:
            input_: The summary fields to insert. ``file_ids`` is serialized as
                a JSON array; missing → ``"[]"``. ``earliest_at`` / ``latest_at``
                are ISO-8601 stringified for storage. ``descendant_count``,
                ``descendant_token_count``, ``source_message_token_count`` are
                clamped to non-negative integers (matches TS
                ``Number.isFinite && x >= 0`` checks). ``depth`` defaults to 0
                for leaf, 1 for condensed.

        Returns:
            The :class:`SummaryRecord` of the inserted row (re-read from the
            DB so server-default columns like ``created_at`` are populated).
        """
        file_ids_json = json.dumps(input_.file_ids if input_.file_ids is not None else [])
        earliest_at = input_.earliest_at.isoformat() if input_.earliest_at else None
        latest_at = input_.latest_at.isoformat() if input_.latest_at else None
        descendant_count = _coerce_nonneg_int(input_.descendant_count)
        descendant_token_count = _coerce_nonneg_int(input_.descendant_token_count)
        source_message_token_count = _coerce_nonneg_int(input_.source_message_token_count)

        # Default depth: 0 for leaf, 1 for condensed (matches TS lines 404-409).
        if (
            input_.depth is not None
            and isinstance(input_.depth, (int, float))
            and input_.depth >= 0
        ):
            depth = int(input_.depth)
        elif input_.kind == "leaf":
            depth = 0
        else:
            depth = 1

        model = input_.model if input_.model is not None else "unknown"

        # v4.1 Gap 8 (Group A adversarial review): atomically populate
        # session_key from conversations.session_key via sub-SELECT. Closes
        # the gap where new summaries inserted between gateway boots had
        # session_key='' until the next boot's JOIN-backfill step ran.
        # The COALESCE protects against a (theoretically impossible) case
        # where conversations.session_key is NULL — in that case the row
        # would be backfilled by the migration on next boot.
        self._conn.execute(
            """
            INSERT INTO summaries (
                summary_id, conversation_id, kind, depth, content, token_count, file_ids,
                earliest_at, latest_at, descendant_count, descendant_token_count,
                source_message_token_count, model, session_key
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                COALESCE((SELECT session_key FROM conversations WHERE conversation_id = ?), ''))
            """,
            (
                input_.summary_id,
                input_.conversation_id,
                input_.kind,
                depth,
                input_.content,
                input_.token_count,
                file_ids_json,
                earliest_at,
                latest_at,
                descendant_count,
                descendant_token_count,
                source_message_token_count,
                model,
                input_.conversation_id,
            ),
        )

        row = self._conn.execute(
            """
            SELECT summary_id, conversation_id, kind, depth, content, token_count, file_ids,
                   earliest_at, latest_at, descendant_count, created_at,
                   descendant_token_count, source_message_token_count, model
            FROM summaries WHERE summary_id = ?
            """,
            (input_.summary_id,),
        ).fetchone()

        record = _to_summary_record(_row_to_dict(row, self._conn))

        # v4.1 §0/§6.1 — Leaf-write hook: enqueue async entity coreference
        # extraction. NEVER inline (would couple gateway hot path to LLM
        # latency, per the v3.1 invariant). Just inserts a queue row so the
        # worker (Group F orchestrator) can pick it up later. Best-effort —
        # if lcm_extraction_queue doesn't exist (pre-migration) or the
        # insert fails, leaf-write still succeeds.
        #
        # MUST run BEFORE the FTS-availability early-return so FTS-disabled
        # installs (or in-memory test DBs) still get the queue write.
        if input_.kind == "leaf":
            try:
                queue_id = f"q_{input_.summary_id}_{_now_base36_ms()}"
                self._conn.execute(
                    """
                    INSERT INTO lcm_extraction_queue (queue_id, leaf_id, kind, queued_at)
                    VALUES (?, ?, 'entity', datetime('now'))
                    """,
                    (queue_id, input_.summary_id),
                )
            except sqlite3.DatabaseError:
                # lcm_extraction_queue not present (pre-migration) OR queue_id
                # collision (incredibly rare given the timestamp suffix).
                # Either way: leaf-write must succeed regardless.
                pass

        # Index in FTS5 as best-effort; compaction flow must continue even if
        # FTS indexing fails for any reason.
        if not self._fts5_available:
            return record

        try:
            self._conn.execute(
                "INSERT INTO summaries_fts(summary_id, content) VALUES (?, ?)",
                (input_.summary_id, input_.content),
            )
        except sqlite3.DatabaseError:
            # FTS indexing failed — search won't find this summary but
            # compaction and assembly will still work correctly.
            pass

        # Also index into the CJK trigram FTS table for CJK substring search.
        try:
            self._conn.execute(
                "INSERT INTO summaries_fts_cjk(summary_id, content) VALUES (?, ?)",
                (input_.summary_id, input_.content),
            )
        except sqlite3.DatabaseError:
            # CJK trigram FTS table may not exist yet (pre-migration); ignore.
            pass

        return record

    def get_summary(
        self,
        summary_id: str,
        *,
        include_suppressed: bool = False,
    ) -> SummaryRecord | None:
        """Look up a summary by id.

        Mirrors TS ``getSummary`` (lines 519-538).

        v4.1 §10 + Final adversarial Finding #1 (BLOCKER): exclude suppressed
        by default. Internal cleanup code (integrity.ts, compaction.ts internal
        paths) opts in to ``include_suppressed=True`` when it legitimately
        needs to inspect suppressed rows. Agent-facing surfaces (retrieval.ts,
        assembler.ts) must NOT pass the flag.

        Args:
            summary_id: The ``summaries.summary_id`` to fetch.
            include_suppressed: Pass ``True`` to return rows where
                ``suppressed_at IS NOT NULL``. Defaults to ``False``.

        Returns:
            :class:`SummaryRecord` or ``None`` if not found / suppressed (when
            ``include_suppressed=False``).
        """
        suppressed_clause = "" if include_suppressed else "AND suppressed_at IS NULL"
        row = self._conn.execute(
            f"""
            SELECT summary_id, conversation_id, kind, depth, content, token_count, file_ids,
                   earliest_at, latest_at, descendant_count, created_at,
                   descendant_token_count, source_message_token_count, model
            FROM summaries WHERE summary_id = ? {suppressed_clause}
            """,
            (summary_id,),
        ).fetchone()
        if row is None:
            return None
        return _to_summary_record(_row_to_dict(row, self._conn))

    def get_summaries_by_conversation(self, conversation_id: int) -> list[SummaryRecord]:
        """Fetch every summary for a conversation, ordered by ``created_at``.

        Mirrors TS ``getSummariesByConversation`` (lines 540-552). Note: this
        method does NOT filter ``suppressed_at`` — historical naming kept for
        parity. Callers that need agent-facing rows should filter on the
        returned records.
        """
        rows = self._conn.execute(
            """
            SELECT summary_id, conversation_id, kind, depth, content, token_count, file_ids,
                   earliest_at, latest_at, descendant_count, created_at,
                   descendant_token_count, source_message_token_count, model
            FROM summaries
            WHERE conversation_id = ?
            ORDER BY created_at
            """,
            (conversation_id,),
        ).fetchall()
        return [_to_summary_record(_row_to_dict(r, self._conn)) for r in rows]

    # ── Lineage ───────────────────────────────────────────────────────────────

    def link_summary_to_messages(
        self,
        summary_id: str,
        message_ids: list[int],
    ) -> None:
        """Insert ``summary_messages`` rows for each message_id, in order.

        Mirrors TS ``linkSummaryToMessages`` (lines 556-570). ``ON CONFLICT``
        skips duplicates so this method is idempotent against re-runs.

        Args:
            summary_id: The owning summary.
            message_ids: Source messages in narrative order (each row's
                ``ordinal`` is the index in this list).
        """
        if not message_ids:
            return
        self._conn.executemany(
            """
            INSERT INTO summary_messages (summary_id, message_id, ordinal)
            VALUES (?, ?, ?)
            ON CONFLICT (summary_id, message_id) DO NOTHING
            """,
            [(summary_id, mid, idx) for idx, mid in enumerate(message_ids)],
        )

    def link_summary_to_parents(
        self,
        summary_id: str,
        parent_summary_ids: list[str],
    ) -> None:
        """Insert ``summary_parents`` rows for each parent_summary_id, in order.

        Mirrors TS ``linkSummaryToParents`` (lines 572-586). The TS naming is
        historically confusing — see :meth:`get_summary_parents` for the
        direction-of-arrow comment. ``ON CONFLICT`` skips duplicates.

        Args:
            summary_id: The child summary being condensed.
            parent_summary_ids: Source summaries compacted into ``summary_id``.
        """
        if not parent_summary_ids:
            return
        self._conn.executemany(
            """
            INSERT INTO summary_parents (summary_id, parent_summary_id, ordinal)
            VALUES (?, ?, ?)
            ON CONFLICT (summary_id, parent_summary_id) DO NOTHING
            """,
            [(summary_id, parent_id, idx) for idx, parent_id in enumerate(parent_summary_ids)],
        )

    def get_summary_messages(self, summary_id: str) -> list[int]:
        """Return the ``message_id``s linked to ``summary_id``, in narrative order.

        Mirrors TS ``getSummaryMessages`` (lines 588-597).
        """
        rows = self._conn.execute(
            """
            SELECT message_id FROM summary_messages
            WHERE summary_id = ?
            ORDER BY ordinal
            """,
            (summary_id,),
        ).fetchall()
        return [r[0] for r in rows]

    def get_conversation_max_summary_depth(
        self,
        conversation_id: int,
    ) -> int | None:
        """Return the deepest persisted summary depth for a conversation.

        Mirrors TS ``getConversationMaxSummaryDepth`` (lines 599-611). Returns
        ``None`` when the conversation has no summaries at all.
        """
        row = self._conn.execute(
            """
            SELECT MAX(depth) AS max_depth
            FROM summaries
            WHERE conversation_id = ?
            """,
            (conversation_id,),
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return int(row[0])

    def get_leaf_summary_links_for_message_ids(
        self,
        conversation_id: int,
        message_ids: list[int],
    ) -> list[MessageLeafSummaryLinkRecord]:
        """Resolve message-id hits back to their linked leaf summaries.

        Mirrors TS ``getLeafSummaryLinksForMessageIds`` (lines 613-670).

        Wave-8 Auditor #1 P1 fix: was missing ``s.suppressed_at IS NULL``.
        Caller is ``lcm-expand-query-tool`` (agent-facing) — without this
        filter, suppressed leaves leaked through expand-query results
        even after a /lcm purge. Other agent-facing read paths in this
        store all default to exclude-suppressed; this method was the
        outlier.

        Args:
            conversation_id: Scope.
            message_ids: Message ids to look up. Duplicates and non-positive
                values are filtered out; the order of the input list is
                preserved in the output (matching TS).

        Returns:
            A list of (message_id, summary_id) records, ordered by
            ``message_ids`` input order. A single message_id can yield
            multiple summary_ids (one row per linked leaf); ordering within a
            group is ``sm.ordinal ASC, s.created_at ASC``.
        """
        # Filter + dedupe + preserve order (Python: dict-from-keys trick).
        seen: set[int] = set()
        normalized: list[int] = []
        for mid in message_ids:
            if isinstance(mid, int) and mid > 0 and mid not in seen:
                seen.add(mid)
                normalized.append(mid)
        if not normalized:
            return []

        placeholders = ", ".join("?" for _ in normalized)
        # Wave-8 Auditor #1 P1 fix: was missing `s.suppressed_at IS NULL`.
        # Caller is `lcm-expand-query-tool` (agent-facing) — without this
        # filter, suppressed leaves leaked through expand-query results
        # even after a /lcm purge.
        sql = f"""
            SELECT sm.message_id, sm.summary_id
            FROM summary_messages sm
            JOIN summaries s ON s.summary_id = sm.summary_id
            WHERE s.conversation_id = ?
              AND s.kind = 'leaf'
              AND s.suppressed_at IS NULL
              AND sm.message_id IN ({placeholders})
            ORDER BY sm.ordinal ASC, s.created_at ASC
        """
        rows = self._conn.execute(sql, (conversation_id, *normalized)).fetchall()

        summary_ids_by_message_id: dict[int, list[str]] = {}
        for row in rows:
            mid_val = row[0] if not isinstance(row, sqlite3.Row) else row["message_id"]
            sid_val = row[1] if not isinstance(row, sqlite3.Row) else row["summary_id"]
            bucket = summary_ids_by_message_id.setdefault(mid_val, [])
            if sid_val not in bucket:
                bucket.append(sid_val)

        ordered: list[MessageLeafSummaryLinkRecord] = []
        for mid in normalized:
            for sid in summary_ids_by_message_id.get(mid, []):
                ordered.append(MessageLeafSummaryLinkRecord(message_id=mid, summary_id=sid))
        return ordered

    def list_transcript_gc_candidates(
        self,
        conversation_id: int,
        *,
        limit: int = 25,
    ) -> list[TranscriptGcCandidateRecord]:
        """Return summarized tool-result messages safe for transcript GC.

        Mirrors TS ``listTranscriptGcCandidates`` (lines 671-735). "Safe"
        means: covered by ≥1 leaf summary AND not currently present as a
        raw context_items row. The candidate row's metadata must flag
        ``toolOutputExternalized=true`` (set by the externalization pass).

        Args:
            conversation_id: Scope.
            limit: Maximum candidates to return. Negative/zero falls back to
                25 (matches TS). Capped at the SQL row count after the
                metadata filter passes.

        Returns:
            A list of :class:`TranscriptGcCandidateRecord` ordered by
            ``(seq ASC, ordinal ASC)``.
        """
        # Clamp limit per TS lines 679-682.
        if not isinstance(limit, (int, float)) or limit <= 0:
            effective_limit = 25
        else:
            effective_limit = max(1, int(limit))

        rows = self._conn.execute(
            """
            SELECT
                m.message_id,
                m.conversation_id,
                m.seq,
                mp.tool_call_id,
                mp.tool_name,
                mp.metadata
            FROM messages m
            JOIN message_parts mp
                ON mp.message_id = m.message_id
            WHERE m.conversation_id = ?
              AND m.role = 'tool'
              AND mp.part_type = 'tool'
              AND mp.tool_call_id IS NOT NULL
              AND mp.tool_call_id != ''
              AND EXISTS (
                SELECT 1
                FROM summary_messages sm
                WHERE sm.message_id = m.message_id
              )
              AND NOT EXISTS (
                SELECT 1
                FROM context_items ci
                WHERE ci.conversation_id = m.conversation_id
                  AND ci.item_type = 'message'
                  AND ci.message_id = m.message_id
              )
            ORDER BY m.seq ASC, mp.ordinal ASC
            """,
            (conversation_id,),
        ).fetchall()

        seen_message_ids: set[int] = set()
        candidates: list[TranscriptGcCandidateRecord] = []
        for row in rows:
            row_dict = _row_to_dict(row, self._conn)
            mid = row_dict["message_id"]
            if mid in seen_message_ids:
                continue
            candidate = _to_transcript_gc_candidate_record(row_dict)
            if candidate is None:
                continue
            seen_message_ids.add(candidate.message_id)
            candidates.append(candidate)
            if len(candidates) >= effective_limit:
                break
        return candidates

    def get_summary_children(
        self,
        parent_summary_id: str,
        *,
        include_suppressed: bool = False,
    ) -> list[SummaryRecord]:
        """Return the summaries that have ``parent_summary_id`` as a parent.

        Mirrors TS ``getSummaryChildren`` (lines 739-756). v4.1 §10 + Final
        review #1 fix: excludes suppressed by default.
        """
        suppressed_clause = "" if include_suppressed else "AND s.suppressed_at IS NULL"
        rows = self._conn.execute(
            f"""
            SELECT s.summary_id, s.conversation_id, s.kind, s.depth, s.content, s.token_count,
                   s.file_ids, s.earliest_at, s.latest_at, s.descendant_count, s.created_at,
                   s.descendant_token_count, s.source_message_token_count, s.model
            FROM summaries s
            JOIN summary_parents sp ON sp.summary_id = s.summary_id
            WHERE sp.parent_summary_id = ? {suppressed_clause}
            ORDER BY sp.ordinal
            """,
            (parent_summary_id,),
        ).fetchall()
        return [_to_summary_record(_row_to_dict(r, self._conn)) for r in rows]

    def get_summary_parents(
        self,
        summary_id: str,
        *,
        include_suppressed: bool = False,
    ) -> list[SummaryRecord]:
        """Return the source summaries compacted into ``summary_id``.

        Mirrors TS ``getSummaryParents`` (lines 758-778).

        NOTE: historical naming is confusing here. ``getSummaryParents(id)``
        returns the source summaries compacted into ``id``. Expansion should
        use this direction for replay.

        v4.1 §10 + Final review #1 fix: excludes suppressed by default.
        """
        suppressed_clause = "" if include_suppressed else "AND s.suppressed_at IS NULL"
        rows = self._conn.execute(
            f"""
            SELECT s.summary_id, s.conversation_id, s.kind, s.depth, s.content, s.token_count,
                   s.file_ids, s.earliest_at, s.latest_at, s.descendant_count, s.created_at,
                   s.descendant_token_count, s.source_message_token_count, s.model
            FROM summaries s
            JOIN summary_parents sp ON sp.parent_summary_id = s.summary_id
            WHERE sp.summary_id = ? {suppressed_clause}
            ORDER BY sp.ordinal
            """,
            (summary_id,),
        ).fetchall()
        return [_to_summary_record(_row_to_dict(r, self._conn)) for r in rows]

    def get_summary_subtree(self, summary_id: str) -> list[SummarySubtreeNodeRecord]:
        """Walk the subtree rooted at ``summary_id`` via ``WITH RECURSIVE``.

        Mirrors TS ``getSummarySubtree`` (lines 780-853). Returns rows in
        depth-then-path order with a path label so caller can reconstruct the
        tree shape.

        Wave-4 Auditor #7 P1 fix: cap recursion to prevent runaway memory
        on pathological subtrees (deep condensation chains, doctor-recovered
        DBs with cycles, stress-test artifacts). 10K nodes is ~10× the
        largest realistic synthesis tree in Eva's actual DB; beyond that
        we truncate and the caller (lcm_describe) sees the truncation in
        the manifest length vs claimed descendant_count.

        Args:
            summary_id: The subtree root.

        Returns:
            A flat list of :class:`SummarySubtreeNodeRecord` ordered by
            ``(depth_from_root ASC, path ASC, created_at ASC)``. Excludes
            ``suppressed_at IS NOT NULL`` rows. Deduplicated by ``summary_id``
            so cycles don't yield duplicates.
        """
        rows = self._conn.execute(
            """
            WITH RECURSIVE subtree(summary_id, parent_summary_id, depth_from_root, path) AS (
                SELECT ?, NULL, 0, ''
                UNION ALL
                SELECT
                    sp.summary_id,
                    sp.parent_summary_id,
                    subtree.depth_from_root + 1,
                    CASE
                        WHEN subtree.path = '' THEN printf('%04d', sp.ordinal)
                        ELSE subtree.path || '.' || printf('%04d', sp.ordinal)
                    END
                FROM summary_parents sp
                JOIN subtree ON sp.parent_summary_id = subtree.summary_id
            )
            SELECT
                s.summary_id,
                s.conversation_id,
                s.kind,
                s.depth,
                s.content,
                s.token_count,
                s.file_ids,
                s.earliest_at,
                s.latest_at,
                s.descendant_count,
                s.descendant_token_count,
                s.source_message_token_count,
                s.model,
                s.created_at,
                subtree.depth_from_root,
                subtree.parent_summary_id,
                subtree.path,
                (
                    SELECT COUNT(*) FROM summary_parents sp2
                    WHERE sp2.parent_summary_id = s.summary_id
                ) AS child_count
            FROM subtree
            JOIN summaries s ON s.summary_id = subtree.summary_id
            WHERE s.suppressed_at IS NULL
            ORDER BY subtree.depth_from_root ASC, subtree.path ASC, s.created_at ASC
            LIMIT ?
            """,
            (summary_id, _SUBTREE_HARD_CAP),
        ).fetchall()

        seen: set[str] = set()
        output: list[SummarySubtreeNodeRecord] = []
        for row in rows:
            row_dict = _row_to_dict(row, self._conn)
            sid = row_dict["summary_id"]
            if sid in seen:
                continue
            seen.add(sid)
            base = _to_summary_record(row_dict)
            depth_from_root = max(0, int(row_dict.get("depth_from_root") or 0))
            parent_summary_id = row_dict.get("parent_summary_id")
            path_value = row_dict.get("path")
            path = path_value if isinstance(path_value, str) else ""
            child_count_raw = row_dict.get("child_count")
            if isinstance(child_count_raw, (int, float)) and not isinstance(child_count_raw, bool):
                child_count = max(0, int(child_count_raw))
            else:
                child_count = 0
            output.append(
                SummarySubtreeNodeRecord(
                    summary_id=base.summary_id,
                    conversation_id=base.conversation_id,
                    kind=base.kind,
                    depth=base.depth,
                    content=base.content,
                    token_count=base.token_count,
                    file_ids=base.file_ids,
                    earliest_at=base.earliest_at,
                    latest_at=base.latest_at,
                    descendant_count=base.descendant_count,
                    descendant_token_count=base.descendant_token_count,
                    source_message_token_count=base.source_message_token_count,
                    model=base.model,
                    created_at=base.created_at,
                    depth_from_root=depth_from_root,
                    parent_summary_id=parent_summary_id,
                    path=path,
                    child_count=child_count,
                )
            )
        return output

    # ── Context items ─────────────────────────────────────────────────────────

    def get_context_items(self, conversation_id: int) -> list[ContextItemRecord]:
        """Return the assembled prompt's context_items ordering.

        Mirrors TS ``getContextItems`` (lines 857-867).
        """
        rows = self._conn.execute(
            """
            SELECT conversation_id, ordinal, item_type, message_id, summary_id, created_at
            FROM context_items
            WHERE conversation_id = ?
            ORDER BY ordinal
            """,
            (conversation_id,),
        ).fetchall()
        return [_to_context_item_record(_row_to_dict(r, self._conn)) for r in rows]

    def get_distinct_depths_in_context(
        self,
        conversation_id: int,
        *,
        max_ordinal_exclusive: int | None = None,
    ) -> list[int]:
        """Return the distinct summary depths currently in the context window.

        Mirrors TS ``getDistinctDepthsInContext`` (lines 869-901).

        Args:
            conversation_id: Scope.
            max_ordinal_exclusive: When provided (and finite), restrict to
                rows with ``ordinal < max_ordinal_exclusive``. Used by
                assembler to inspect the depth distribution below a point.

        Returns:
            Depths in ascending order. ``int``s only; no record wrapper.
        """
        use_ordinal_bound = (
            max_ordinal_exclusive is not None
            and isinstance(max_ordinal_exclusive, (int, float))
            and not isinstance(max_ordinal_exclusive, bool)
        )

        if use_ordinal_bound:
            assert max_ordinal_exclusive is not None  # for type-checker
            sql = """
                SELECT DISTINCT s.depth
                FROM context_items ci
                JOIN summaries s ON s.summary_id = ci.summary_id
                WHERE ci.conversation_id = ?
                  AND ci.item_type = 'summary'
                  AND ci.ordinal < ?
                ORDER BY s.depth ASC
            """
            rows = self._conn.execute(sql, (conversation_id, int(max_ordinal_exclusive))).fetchall()
        else:
            sql = """
                SELECT DISTINCT s.depth
                FROM context_items ci
                JOIN summaries s ON s.summary_id = ci.summary_id
                WHERE ci.conversation_id = ?
                  AND ci.item_type = 'summary'
                ORDER BY s.depth ASC
            """
            rows = self._conn.execute(sql, (conversation_id,)).fetchall()

        return [r[0] for r in rows]

    def prune_for_new_session(
        self,
        conversation_id: int,
        retain_depth: float,
    ) -> None:
        """Truncate context_items at session boundary, keeping summaries ≥ depth.

        Mirrors TS ``pruneForNewSession`` (lines 908-945).

        Strategy:

        1. Delete every ``message`` context_item (raw messages are always
           cleared at boundary).
        2. Delete ``summary`` context_items whose linked summary has
           ``depth < retain_depth``. ``retain_depth=Infinity`` (or ``inf``)
           clears all summaries too — matches the TS branch at lines 921-930.
        3. Negative ``retain_depth`` is a no-op (matches TS guard at 909-911).

        Args:
            conversation_id: Scope.
            retain_depth: Depth threshold (e.g. 2.0 keeps depth>=2 summaries).
                ``math.inf`` / huge values keep everything; negative values
                are a no-op.
        """
        # Negative finite values: no-op (TS lines 909-911).
        try:
            is_finite = retain_depth == retain_depth and retain_depth not in (
                float("inf"),
                float("-inf"),
            )
        except (TypeError, ValueError):
            is_finite = False
        if is_finite and retain_depth < 0:
            return

        # 1. Always clear message rows.
        self._conn.execute(
            """
            DELETE FROM context_items
            WHERE conversation_id = ?
              AND item_type = 'message'
            """,
            (conversation_id,),
        )

        # 2a. Infinite retain_depth: also clear all summaries (TS 921-930).
        if not is_finite:
            self._conn.execute(
                """
                DELETE FROM context_items
                WHERE conversation_id = ?
                  AND item_type = 'summary'
                """,
                (conversation_id,),
            )
            return

        # 2b. Finite retain_depth: drop summaries below it (TS 932-944).
        self._conn.execute(
            """
            DELETE FROM context_items
            WHERE conversation_id = ?
              AND item_type = 'summary'
              AND summary_id IN (
                SELECT summary_id
                FROM summaries
                WHERE conversation_id = ?
                  AND depth < ?
              )
            """,
            (conversation_id, conversation_id, int(retain_depth)),
        )

    def append_context_message(
        self,
        conversation_id: int,
        message_id: int,
    ) -> None:
        """Append a message-type context_items row at ``max(ordinal)+1``.

        Mirrors TS ``appendContextMessage`` (lines 947-961).
        """
        row = self._conn.execute(
            """
            SELECT COALESCE(MAX(ordinal), -1) AS max_ordinal
            FROM context_items WHERE conversation_id = ?
            """,
            (conversation_id,),
        ).fetchone()
        next_ordinal = int(row[0]) + 1
        self._conn.execute(
            """
            INSERT INTO context_items (conversation_id, ordinal, item_type, message_id)
            VALUES (?, ?, 'message', ?)
            """,
            (conversation_id, next_ordinal, message_id),
        )

    def append_context_messages(
        self,
        conversation_id: int,
        message_ids: list[int],
    ) -> None:
        """Bulk-append message context_items rows.

        Mirrors TS ``appendContextMessages`` (lines 963-983).
        """
        if not message_ids:
            return
        row = self._conn.execute(
            """
            SELECT COALESCE(MAX(ordinal), -1) AS max_ordinal
            FROM context_items WHERE conversation_id = ?
            """,
            (conversation_id,),
        ).fetchone()
        base_ordinal = int(row[0]) + 1
        self._conn.executemany(
            """
            INSERT INTO context_items (conversation_id, ordinal, item_type, message_id)
            VALUES (?, ?, 'message', ?)
            """,
            [(conversation_id, base_ordinal + idx, mid) for idx, mid in enumerate(message_ids)],
        )

    def append_context_summary(
        self,
        conversation_id: int,
        summary_id: str,
    ) -> None:
        """Append a summary-type context_items row at ``max(ordinal)+1``.

        Mirrors TS ``appendContextSummary`` (lines 985-999).
        """
        row = self._conn.execute(
            """
            SELECT COALESCE(MAX(ordinal), -1) AS max_ordinal
            FROM context_items WHERE conversation_id = ?
            """,
            (conversation_id,),
        ).fetchone()
        next_ordinal = int(row[0]) + 1
        self._conn.execute(
            """
            INSERT INTO context_items (conversation_id, ordinal, item_type, summary_id)
            VALUES (?, ?, 'summary', ?)
            """,
            (conversation_id, next_ordinal, summary_id),
        )

    def replace_context_range_with_summary(
        self,
        input_: ReplaceContextRangeInput,
    ) -> None:
        """Atomically replace context_items[start..end] with a summary item.

        Mirrors TS ``replaceContextRangeWithSummary`` (lines 1001-1064).

        Atomicity: wraps the three-phase operation (DELETE range → INSERT
        summary at start_ordinal → resequence ordinals to contiguous) in a
        single transaction. A mid-operation crash leaves the context_items
        table consistent — no half-replaced ranges.

        The resequence pass uses negative-temp ordinals to dodge the
        ``(conversation_id, ordinal)`` UNIQUE constraint during the swap.
        """
        with self.with_transaction():
            self._replace_context_range_with_summary_in_transaction(input_)

    def _replace_context_range_with_summary_in_transaction(
        self,
        input_: ReplaceContextRangeInput,
    ) -> None:
        """Inner atomic body of ``replace_context_range_with_summary``.

        Mirrors TS ``replaceContextRangeWithSummaryInTransaction`` (lines
        1013-1064). Called with the txn already open.
        """
        conversation_id = input_.conversation_id
        start_ordinal = input_.start_ordinal
        end_ordinal = input_.end_ordinal
        summary_id = input_.summary_id

        # 1. Delete context items in the range [startOrdinal, endOrdinal]
        self._conn.execute(
            """
            DELETE FROM context_items
            WHERE conversation_id = ?
              AND ordinal >= ?
              AND ordinal <= ?
            """,
            (conversation_id, start_ordinal, end_ordinal),
        )

        # 2. Insert the replacement summary item at startOrdinal
        self._conn.execute(
            """
            INSERT INTO context_items (conversation_id, ordinal, item_type, summary_id)
            VALUES (?, ?, 'summary', ?)
            """,
            (conversation_id, start_ordinal, summary_id),
        )

        # 3. Resequence all ordinals to maintain contiguity (no gaps).
        #    Pre-compute ranks from a SELECT (safe snapshot), then apply
        #    via 2-pass UPDATE loop using negative temps to avoid UNIQUE
        #    constraint violations. The SELECT reads post-delete/insert
        #    state and provides a consistent snapshot for resequencing.
        items = self._conn.execute(
            """
            SELECT ordinal FROM context_items
            WHERE conversation_id = ?
            ORDER BY ordinal
            """,
            (conversation_id,),
        ).fetchall()

        if items and any(items[i][0] != i for i in range(len(items))):
            update_sql = """
                UPDATE context_items SET ordinal = ?
                WHERE conversation_id = ? AND ordinal = ?
            """
            for i, (current,) in enumerate(items):
                self._conn.execute(update_sql, (-(i + 1), conversation_id, current))
            for i in range(len(items)):
                self._conn.execute(update_sql, (i, conversation_id, -(i + 1)))

    def get_context_token_count(self, conversation_id: int) -> int:
        """Sum tokens across all context_items (messages + summaries).

        Mirrors TS ``getContextTokenCount`` (lines 1066-1088). Uses a UNION
        ALL across the two joins so messages and summaries contribute
        independently — no double-counting if a row somehow had both a
        message_id and summary_id (the CHECK constraint forbids that anyway).
        """
        row = self._conn.execute(
            """
            SELECT COALESCE(SUM(token_count), 0) AS total
            FROM (
                SELECT m.token_count
                FROM context_items ci
                JOIN messages m ON m.message_id = ci.message_id
                WHERE ci.conversation_id = ?
                  AND ci.item_type = 'message'

                UNION ALL

                SELECT s.token_count
                FROM context_items ci
                JOIN summaries s ON s.summary_id = ci.summary_id
                WHERE ci.conversation_id = ?
                  AND ci.item_type = 'summary'
            ) sub
            """,
            (conversation_id, conversation_id),
        ).fetchone()
        if row is None or row[0] is None:
            return 0
        return int(row[0])

    # ── Search ────────────────────────────────────────────────────────────────

    def search_summaries(self, input_: SummarySearchInput) -> list[SummarySearchResult]:
        """Dispatch to the right search path based on mode + content.

        Mirrors TS ``searchSummaries`` (lines 1092-1169). Dispatch table:

        1. ``mode='regex'`` → :meth:`_search_regex`.
        2. ``mode='full_text'`` + CJK content → :meth:`_search_cjk_trigram`
           first, then :meth:`_search_like_cjk` fallback. (FTS5 ``unicode61``
           cannot segment CJK, so CJK can NEVER take the standard FTS5 path.)
        3. ``mode='full_text'`` + non-CJK + FTS5 available →
           :meth:`_search_full_text`, falling back to :meth:`_search_like`
           on any exception.
        4. ``mode='full_text'`` + non-CJK + FTS5 unavailable →
           :meth:`_search_like`.

        Args:
            input_: Query + filters.

        Returns:
            Up to ``limit`` (default 50) results, ordered by the active
            ``sort`` mode (default "recency": most-recent first).
        """
        limit = input_.limit if input_.limit is not None else 50

        if input_.mode == "full_text":
            # FTS5 unicode61 cannot segment CJK ideographs, so CJK queries
            # route through the trigram FTS table first, then fall back to
            # LIKE with OR semantics.
            if contains_cjk(input_.query):
                cjk_segments = self._extract_cjk_segments(input_.query)
                has_short_cjk_segment = any(len(seg) < 3 for seg in cjk_segments)
                if not has_short_cjk_segment:
                    try:
                        trigram_results = self._search_cjk_trigram(
                            input_.query,
                            limit,
                            input_.conversation_id,
                            input_.conversation_ids,
                            input_.since,
                            input_.before,
                            input_.sort,
                        )
                        if trigram_results:
                            return trigram_results
                    except sqlite3.DatabaseError:
                        # trigram table may not exist; fall through to LIKE OR
                        pass
                return self._search_like_cjk(
                    input_.query,
                    limit,
                    input_.conversation_id,
                    input_.conversation_ids,
                    input_.since,
                    input_.before,
                )
            if self._fts5_available:
                try:
                    return self._search_full_text(
                        input_.query,
                        limit,
                        input_.conversation_id,
                        input_.conversation_ids,
                        input_.since,
                        input_.before,
                        input_.sort,
                    )
                except sqlite3.DatabaseError:
                    return self._search_like(
                        input_.query,
                        limit,
                        input_.conversation_id,
                        input_.conversation_ids,
                        input_.since,
                        input_.before,
                    )
            return self._search_like(
                input_.query,
                limit,
                input_.conversation_id,
                input_.conversation_ids,
                input_.since,
                input_.before,
            )

        return self._search_regex(
            input_.query,
            limit,
            input_.conversation_id,
            input_.conversation_ids,
            input_.since,
            input_.before,
        )

    def _search_full_text(
        self,
        query: str,
        limit: int,
        conversation_id: int | None,
        conversation_ids: list[int] | None,
        since: datetime | None,
        before: datetime | None,
        sort: SearchSort | None,
    ) -> list[SummarySearchResult]:
        """FTS5 ``MATCH`` against ``summaries_fts``.

        Mirrors TS ``searchFullText`` (lines 1171-1219).

        v4.1 §10 invariant: every retrieval surface defaults to
        exclude-suppressed. lcm_grep (all modes) flows through here;
        operator/admin tools that need to see suppressed should
        bypass searchSummaries entirely.
        """
        where: list[str] = ["summaries_fts MATCH ?"]
        args: list[Any] = [sanitize_fts5_query(query)]
        # v4.1 §10 invariant: every retrieval surface defaults to
        # exclude-suppressed. lcm_grep (all modes) flows through here;
        # operator/admin tools that need to see suppressed should
        # bypass searchSummaries entirely.
        where.append("s.suppressed_at IS NULL")
        append_conversation_scope_constraint(
            where=where,
            args=args,
            column_expr="s.conversation_id",
            conversation_id=conversation_id,
            conversation_ids=conversation_ids,
        )
        if since is not None:
            where.append(f"julianday({_SUMMARY_SEARCH_TIME_EXPR}) >= julianday(?)")
            args.append(since.isoformat())
        if before is not None:
            where.append(f"julianday({_SUMMARY_SEARCH_TIME_EXPR}) < julianday(?)")
            args.append(before.isoformat())
        args.append(limit)
        order_by = build_fts_order_by(sort, _SUMMARY_SEARCH_TIME_EXPR)

        sql = f"""
            SELECT
                summaries_fts.summary_id,
                s.conversation_id,
                s.kind,
                snippet(summaries_fts, 1, '', '', '...', 32) AS snippet,
                rank,
                {_SUMMARY_SEARCH_TIME_EXPR} AS created_at
            FROM summaries_fts
            JOIN summaries s ON s.summary_id = summaries_fts.summary_id
            WHERE {" AND ".join(where)}
            ORDER BY {order_by}
            LIMIT ?
        """
        rows = self._conn.execute(sql, args).fetchall()
        return [_to_search_result(_row_to_dict(r, self._conn)) for r in rows]

    def _search_like(
        self,
        query: str,
        limit: int,
        conversation_id: int | None,
        conversation_ids: list[int] | None,
        since: datetime | None,
        before: datetime | None,
    ) -> list[SummarySearchResult]:
        """LIKE-based fallback search (FTS5 unavailable / failed).

        Mirrors TS ``searchLike`` (lines 1221-1278).
        """
        plan = build_like_search_plan("content", query)
        if not plan.terms:
            return []

        where: list[str] = list(plan.where)
        args: list[Any] = list(plan.args)
        # v4.1 §10 invariant: exclude suppressed by default (LIKE fallback
        # for FTS — same surface from agent's POV).
        where.append("suppressed_at IS NULL")
        append_conversation_scope_constraint(
            where=where,
            args=args,
            column_expr="conversation_id",
            conversation_id=conversation_id,
            conversation_ids=conversation_ids,
        )
        if since is not None:
            where.append(f"julianday({_SUMMARY_SEARCH_TIME_EXPR_UNQUALIFIED}) >= julianday(?)")
            args.append(since.isoformat())
        if before is not None:
            where.append(f"julianday({_SUMMARY_SEARCH_TIME_EXPR_UNQUALIFIED}) < julianday(?)")
            args.append(before.isoformat())
        args.append(limit)

        where_clause = f"WHERE {' AND '.join(where)}" if where else ""
        sql = f"""
            SELECT summary_id, conversation_id, kind, depth, content, token_count, file_ids,
                   earliest_at, latest_at, descendant_count, descendant_token_count,
                   source_message_token_count, model,
                   {_SUMMARY_SEARCH_TIME_EXPR_UNQUALIFIED} AS created_at
            FROM summaries
            {where_clause}
            ORDER BY {_SUMMARY_SEARCH_TIME_EXPR_UNQUALIFIED} DESC
            LIMIT ?
        """
        rows = self._conn.execute(sql, args).fetchall()

        results: list[SummarySearchResult] = []
        for row in rows:
            row_dict = _row_to_dict(row, self._conn)
            results.append(
                SummarySearchResult(
                    summary_id=row_dict["summary_id"],
                    conversation_id=row_dict["conversation_id"],
                    kind=row_dict["kind"],
                    snippet=create_fallback_snippet(row_dict["content"], plan.terms),
                    created_at=parse_utc_timestamp(row_dict["created_at"]),
                    rank=0.0,
                )
            )
        return results

    # ── CJK helpers ───────────────────────────────────────────────────────────

    def _extract_cjk_segments(self, query: str) -> list[str]:
        """Return the CJK runs from ``query`` (in left-to-right order).

        Mirrors TS ``extractCjkSegments`` (lines 1280-1282).
        """
        return _CJK_QUERY_SEGMENT_RE.findall(query)

    def _extract_latin_tokens(self, query: str) -> list[str]:
        """Return the deduplicated, lowercased Latin tokens in ``query``.

        Mirrors TS ``extractLatinTokens`` (lines 1284-1287).
        """
        tokens = _LATIN_QUERY_TOKEN_RE.findall(query)
        seen: set[str] = set()
        out: list[str] = []
        for token in tokens:
            lowered = token.lower()
            if lowered not in seen:
                seen.add(lowered)
                out.append(lowered)
        return out

    def _escape_like_term(self, term: str) -> str:
        """Escape ``\\`` ``%`` ``_`` for LIKE patterns.

        Mirrors TS ``escapeLikeTerm`` (lines 1289-1291).
        """
        out_chars: list[str] = []
        for ch in term:
            if ch in ("\\", "%", "_"):
                out_chars.append("\\")
            out_chars.append(ch)
        return "".join(out_chars)

    def _split_cjk_chunks(self, text: str, size: int) -> list[str]:
        """Split ``text`` into overlapping windows of ``size`` chars.

        E.g. ``"端到端测试结果"`` with ``size=4`` →
        ``["端到端测", "到端测试", "端测试结", "测试结果"]``.

        Mirrors TS ``splitCjkChunks`` (lines 1304-1313). Deduplicates while
        preserving order.
        """
        chunks: list[str] = []
        for i in range(len(text) - size + 1):
            chunk = text[i : i + size]
            if chunk not in chunks:
                chunks.append(chunk)
        return chunks

    # ── CJK trigram FTS search ────────────────────────────────────────────────

    def _search_cjk_trigram(
        self,
        query: str,
        limit: int,
        conversation_id: int | None,
        conversation_ids: list[int] | None,
        since: datetime | None,
        before: datetime | None,
        sort: SearchSort | None,
    ) -> list[SummarySearchResult]:
        """CJK trigram FTS5 search against ``summaries_fts_cjk``.

        Mirrors TS ``searchCjkTrigram`` (lines 1315-1382).

        Each CJK segment of 3+ chars is split into overlapping 4-char chunks
        for trigram MATCH with OR semantics within the segment. Segment
        groups are combined with AND, and Latin tokens are applied as LIKE
        filters so mixed queries still require every part of the user's
        intent.
        """
        cjk_segments = [seg for seg in self._extract_cjk_segments(query) if len(seg) >= 3]
        if not cjk_segments:
            return []
        latin_tokens = self._extract_latin_tokens(query)

        # Build one OR group per CJK segment, then require every segment group
        # and every Latin token to match so mixed queries preserve full-intent
        # search.
        cjk_groups: list[str] = []
        for segment in cjk_segments:
            segment_terms = [segment] if len(segment) <= 4 else self._split_cjk_chunks(segment, 4)
            unique_terms: list[str] = []
            for term in segment_terms:
                if term not in unique_terms:
                    unique_terms.append(term)
            group_expr = " OR ".join(
                f'"{term.replace(chr(34), chr(34) + chr(34))}"' for term in unique_terms
            )
            cjk_groups.append(f"({group_expr})")

        where: list[str] = ["summaries_fts_cjk MATCH ?"]
        args: list[Any] = [" AND ".join(cjk_groups)]
        # v4.1 §10 invariant: exclude suppressed (CJK FTS path).
        where.append("s.suppressed_at IS NULL")
        for token in latin_tokens:
            where.append("LOWER(s.content) LIKE ? ESCAPE '\\'")
            args.append(f"%{self._escape_like_term(token)}%")
        append_conversation_scope_constraint(
            where=where,
            args=args,
            column_expr="s.conversation_id",
            conversation_id=conversation_id,
            conversation_ids=conversation_ids,
        )
        if since is not None:
            where.append(f"julianday({_SUMMARY_SEARCH_TIME_EXPR}) >= julianday(?)")
            args.append(since.isoformat())
        if before is not None:
            where.append(f"julianday({_SUMMARY_SEARCH_TIME_EXPR}) < julianday(?)")
            args.append(before.isoformat())
        args.append(limit)
        order_by = build_fts_order_by(sort, _SUMMARY_SEARCH_TIME_EXPR)

        sql = f"""
            SELECT
                f.summary_id,
                s.conversation_id,
                s.kind,
                snippet(summaries_fts_cjk, 1, '', '', '...', 32) AS snippet,
                rank,
                {_SUMMARY_SEARCH_TIME_EXPR} AS created_at
            FROM summaries_fts_cjk f
            JOIN summaries s ON s.summary_id = f.summary_id
            WHERE {" AND ".join(where)}
            ORDER BY {order_by}
            LIMIT ?
        """
        rows = self._conn.execute(sql, args).fetchall()
        return [_to_search_result(_row_to_dict(r, self._conn)) for r in rows]

    # ── CJK LIKE fallback ─────────────────────────────────────────────────────

    def _search_like_cjk(
        self,
        query: str,
        limit: int,
        conversation_id: int | None,
        conversation_ids: list[int] | None,
        since: datetime | None,
        before: datetime | None,
    ) -> list[SummarySearchResult]:
        """LIKE-based CJK fallback (when trigram unavailable or empty results).

        Mirrors TS ``searchLikeCjk`` (lines 1390-1479).

        Split each CJK segment into sliding-window 2-char terms so partial
        matches still work. Terms within a single segment are ORed together,
        but each segment and Latin token still has to match so mixed queries
        keep full-intent semantics.
        """
        cjk_segments = self._extract_cjk_segments(query)
        latin_tokens = self._extract_latin_tokens(query)

        if not cjk_segments and not latin_tokens:
            return []

        cjk_terms: list[str] = []
        cjk_clauses: list[str] = []
        cjk_args: list[str] = []
        for segment in cjk_segments:
            if len(segment) == 1:
                segment_terms = [segment]
            elif len(segment) == 2:
                segment_terms = [segment]
            else:
                segment_terms = self._split_cjk_chunks(segment, 2)
            unique_terms: list[str] = []
            for term in segment_terms:
                if term not in unique_terms:
                    unique_terms.append(term)
            cjk_terms.extend(unique_terms)
            cjk_clauses.append(
                "(" + " OR ".join("LOWER(content) LIKE ? ESCAPE '\\'" for _ in unique_terms) + ")"
            )
            cjk_args.extend(f"%{self._escape_like_term(term.lower())}%" for term in unique_terms)

        latin_clauses = ["LOWER(content) LIKE ? ESCAPE '\\'" for _ in latin_tokens]
        latin_args = [f"%{self._escape_like_term(token)}%" for token in latin_tokens]

        where: list[str] = [*cjk_clauses, *latin_clauses]
        # v4.1 §10 invariant + Group C adversarial Finding #1: searchLikeCjk
        # is the 5TH search code path; was missed in C.03's "4 paths" comment.
        # CJK queries fall through to this path when trigram returns empty
        # OR when CJK segments are <3 chars; without this filter, suppressed
        # rows leak through CJK searches.
        where.append("suppressed_at IS NULL")
        args: list[Any] = [*cjk_args, *latin_args]
        append_conversation_scope_constraint(
            where=where,
            args=args,
            column_expr="conversation_id",
            conversation_id=conversation_id,
            conversation_ids=conversation_ids,
        )
        if since is not None:
            where.append(f"julianday({_SUMMARY_SEARCH_TIME_EXPR_UNQUALIFIED}) >= julianday(?)")
            args.append(since.isoformat())
        if before is not None:
            where.append(f"julianday({_SUMMARY_SEARCH_TIME_EXPR_UNQUALIFIED}) < julianday(?)")
            args.append(before.isoformat())
        args.append(limit)

        sql = f"""
            SELECT summary_id, conversation_id, kind, depth, content, token_count, file_ids,
                   earliest_at, latest_at, descendant_count, descendant_token_count,
                   source_message_token_count, model,
                   {_SUMMARY_SEARCH_TIME_EXPR_UNQUALIFIED} AS created_at
            FROM summaries
            WHERE {" AND ".join(where)}
            ORDER BY {_SUMMARY_SEARCH_TIME_EXPR_UNQUALIFIED} DESC
            LIMIT ?
        """
        rows = self._conn.execute(sql, args).fetchall()

        if cjk_terms:
            snippet_terms_set: list[str] = []
            for term in [*cjk_terms, *latin_tokens]:
                if term not in snippet_terms_set:
                    snippet_terms_set.append(term)
            snippet_terms = snippet_terms_set
        else:
            snippet_terms = latin_tokens

        results: list[SummarySearchResult] = []
        for row in rows:
            row_dict = _row_to_dict(row, self._conn)
            results.append(
                SummarySearchResult(
                    summary_id=row_dict["summary_id"],
                    conversation_id=row_dict["conversation_id"],
                    kind=row_dict["kind"],
                    snippet=create_fallback_snippet(row_dict["content"], snippet_terms),
                    # Wave-7 Auditor #1 P1 fix: SQLite stores 'YYYY-MM-DD HH:MM:SS'
                    # (no Z), and `new Date(naive)` parses as LOCAL time. All other
                    # search paths use parseUtcTimestamp; this CJK fallback path was
                    # the outlier. Without this, CJK matches showed timestamps offset
                    # by the host's local timezone (8h for Asia/Shanghai, etc).
                    created_at=parse_utc_timestamp(row_dict["created_at"]),
                    rank=0.0,
                )
            )
        return results

    def _search_regex(
        self,
        pattern: str,
        limit: int,
        conversation_id: int | None,
        conversation_ids: list[int] | None,
        since: datetime | None,
        before: datetime | None,
    ) -> list[SummarySearchResult]:
        """Python-side regex search with ReDoS guard.

        Mirrors TS ``searchRegex`` (lines 1481-1558).

        Guards:

        * Reject patterns > 500 chars or with nested quantifiers.
        * Bound the SQL scan to 10,000 rows (Wave-8 Auditor #1 P1 fix).
        * Cap Python-side row scan at 10,000 entries.

        Returns whichever rows the pattern matches first (recency order),
        up to ``limit``.
        """
        if len(pattern) > _REGEX_PATTERN_MAX_LEN or _REGEX_REDOS_PATTERN.search(pattern):
            return []
        try:
            compiled = re.compile(pattern)
        except re.error:
            return []

        where: list[str] = ["suppressed_at IS NULL"]  # v4.1 §10
        args: list[Any] = []
        append_conversation_scope_constraint(
            where=where,
            args=args,
            column_expr="conversation_id",
            conversation_id=conversation_id,
            conversation_ids=conversation_ids,
        )
        if since is not None:
            where.append(f"julianday({_SUMMARY_SEARCH_TIME_EXPR_UNQUALIFIED}) >= julianday(?)")
            args.append(since.isoformat())
        if before is not None:
            where.append(f"julianday({_SUMMARY_SEARCH_TIME_EXPR_UNQUALIFIED}) < julianday(?)")
            args.append(before.isoformat())
        where_clause = f"WHERE {' AND '.join(where)}" if where else ""
        # Wave-8 Auditor #1 P1 fix: bound the SQL scan, don't materialize
        # entire summaries table in JS before applying the row-scan cap.
        # Bind a SQL LIMIT that's at least as large as the JS-side
        # MAX_ROW_SCAN (10K), ensuring SQLite stops short instead of
        # streaming N rows through the prepared statement.
        sql = f"""
            SELECT summary_id, conversation_id, kind, depth, content, token_count, file_ids,
                   earliest_at, latest_at, descendant_count, descendant_token_count,
                   source_message_token_count, model,
                   {_SUMMARY_SEARCH_TIME_EXPR_UNQUALIFIED} AS created_at
            FROM summaries
            {where_clause}
            ORDER BY {_SUMMARY_SEARCH_TIME_EXPR_UNQUALIFIED} DESC
            LIMIT ?
        """
        rows = self._conn.execute(sql, [*args, _REGEX_SQL_SCAN_BOUND]).fetchall()

        results: list[SummarySearchResult] = []
        scanned = 0
        for row in rows:
            if len(results) >= limit or scanned >= _REGEX_MAX_ROW_SCAN:
                break
            scanned += 1
            row_dict = _row_to_dict(row, self._conn)
            match = compiled.search(row_dict["content"])
            if match:
                results.append(
                    SummarySearchResult(
                        summary_id=row_dict["summary_id"],
                        conversation_id=row_dict["conversation_id"],
                        kind=row_dict["kind"],
                        snippet=match.group(0),
                        created_at=parse_utc_timestamp(row_dict["created_at"]),
                        rank=0.0,
                    )
                )
        return results

    # ── Large files ───────────────────────────────────────────────────────────

    def insert_large_file(self, input_: CreateLargeFileInput) -> LargeFileRecord:
        """Insert a large_files row + return the materialized record.

        Mirrors TS ``insertLargeFile`` (lines 1562-1586).
        """
        self._conn.execute(
            """
            INSERT INTO large_files (file_id, conversation_id, file_name, mime_type, byte_size, storage_uri, exploration_summary)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                input_.file_id,
                input_.conversation_id,
                input_.file_name,
                input_.mime_type,
                input_.byte_size,
                input_.storage_uri,
                input_.exploration_summary,
            ),
        )
        row = self._conn.execute(
            """
            SELECT file_id, conversation_id, file_name, mime_type, byte_size, storage_uri, exploration_summary, created_at
            FROM large_files WHERE file_id = ?
            """,
            (input_.file_id,),
        ).fetchone()
        return _to_large_file_record(_row_to_dict(row, self._conn))

    def get_large_file(self, file_id: str) -> LargeFileRecord | None:
        """Look up a large_files row by ``file_id``.

        Mirrors TS ``getLargeFile`` (lines 1588-1596).
        """
        row = self._conn.execute(
            """
            SELECT file_id, conversation_id, file_name, mime_type, byte_size, storage_uri, exploration_summary, created_at
            FROM large_files WHERE file_id = ?
            """,
            (file_id,),
        ).fetchone()
        if row is None:
            return None
        return _to_large_file_record(_row_to_dict(row, self._conn))

    def get_large_files_by_conversation(
        self,
        conversation_id: int,
    ) -> list[LargeFileRecord]:
        """Return all large_files for a conversation, in creation order.

        Mirrors TS ``getLargeFilesByConversation`` (lines 1598-1608).
        """
        rows = self._conn.execute(
            """
            SELECT file_id, conversation_id, file_name, mime_type, byte_size, storage_uri, exploration_summary, created_at
            FROM large_files
            WHERE conversation_id = ?
            ORDER BY created_at
            """,
            (conversation_id,),
        ).fetchall()
        return [_to_large_file_record(_row_to_dict(r, self._conn)) for r in rows]

    # ── Bootstrap state ───────────────────────────────────────────────────────

    def get_conversation_bootstrap_state(
        self,
        conversation_id: int,
    ) -> ConversationBootstrapStateRecord | None:
        """Look up the bootstrap-resume state for a conversation.

        Mirrors TS ``getConversationBootstrapState`` (lines 1612-1624).
        """
        row = self._conn.execute(
            """
            SELECT conversation_id, session_file_path, last_seen_size, last_seen_mtime_ms,
                   last_processed_offset, last_processed_entry_hash, updated_at
            FROM conversation_bootstrap_state
            WHERE conversation_id = ?
            """,
            (conversation_id,),
        ).fetchone()
        if row is None:
            return None
        return _to_conversation_bootstrap_state_record(_row_to_dict(row, self._conn))

    def upsert_conversation_bootstrap_state(
        self,
        input_: UpsertConversationBootstrapStateInput,
    ) -> ConversationBootstrapStateRecord:
        """Insert or update the bootstrap state for a conversation.

        Mirrors TS ``upsertConversationBootstrapState`` (lines 1626-1666).
        Integers are clamped to non-negative (matches TS ``Math.max(0,
        Math.floor(...))``).
        """
        self._conn.execute(
            """
            INSERT INTO conversation_bootstrap_state (
                conversation_id,
                session_file_path,
                last_seen_size,
                last_seen_mtime_ms,
                last_processed_offset,
                last_processed_entry_hash
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (conversation_id) DO UPDATE SET
                session_file_path = excluded.session_file_path,
                last_seen_size = excluded.last_seen_size,
                last_seen_mtime_ms = excluded.last_seen_mtime_ms,
                last_processed_offset = excluded.last_processed_offset,
                last_processed_entry_hash = excluded.last_processed_entry_hash,
                updated_at = datetime('now')
            """,
            (
                input_.conversation_id,
                input_.session_file_path,
                max(0, int(input_.last_seen_size)),
                max(0, int(input_.last_seen_mtime_ms)),
                max(0, int(input_.last_processed_offset)),
                input_.last_processed_entry_hash,
            ),
        )
        row = self._conn.execute(
            """
            SELECT conversation_id, session_file_path, last_seen_size, last_seen_mtime_ms,
                   last_processed_offset, last_processed_entry_hash, updated_at
            FROM conversation_bootstrap_state
            WHERE conversation_id = ?
            """,
            (input_.conversation_id,),
        ).fetchone()
        return _to_conversation_bootstrap_state_record(_row_to_dict(row, self._conn))


# ── Helpers ───────────────────────────────────────────────────────────────────


def _row_to_dict(
    row: sqlite3.Row | tuple[Any, ...] | None,
    conn: sqlite3.Connection,
) -> dict[str, Any]:
    """Convert a sqlite3 row to a dict, using the most recent statement description.

    When ``conn.row_factory`` is set to :class:`sqlite3.Row`, the row supports
    keyed lookup directly. Otherwise we fall back to a positional mapping via
    the last cursor's ``description``. The caller passes ``conn`` so we can
    introspect the cursor without depending on a global state.
    """
    if row is None:
        return {}
    if isinstance(row, sqlite3.Row):
        return {key: row[key] for key in row.keys()}
    # Fallback: the caller used a tuple-row connection; use the last
    # statement's description (sqlite3 sets this on the cursor, not the
    # connection — but we can recover it from a fresh cursor).
    # In practice the SummaryStore methods always go through `conn.execute(...)`
    # which returns a Cursor; we don't have that here. Tests must set
    # ``conn.row_factory = sqlite3.Row`` to use this store. The dict path is
    # the supported path; this fallback is kept for tuple-row backward
    # compat tests.
    raise TypeError(
        "SummaryStore requires sqlite3.Connection with row_factory=sqlite3.Row. "
        "Set ``conn.row_factory = sqlite3.Row`` before passing to SummaryStore."
    )


def _now_base36_ms() -> str:
    """Encode current epoch-ms as a lowercase base-36 string.

    Mirrors TS ``Date.now().toString(36)`` used to suffix
    ``lcm_extraction_queue.queue_id``.
    """
    ms = int(time.time() * 1000)
    if ms == 0:
        return "0"
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    chars: list[str] = []
    while ms > 0:
        chars.append(digits[ms % 36])
        ms //= 36
    return "".join(reversed(chars))
