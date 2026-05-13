"""Per-conversation prompt-cache telemetry store.

Ports ``lossless-claw/src/store/compaction-telemetry-store.ts`` (commit
``1f07fbd``, 204 LOC) to Python. The store persists and queries
per-conversation prompt-cache telemetry used by cache-aware incremental
compaction (storage.md §4.3).

### Single row per conversation

``conversation_compaction_telemetry`` has ``conversation_id`` as
``PRIMARY KEY`` (with ``ON DELETE CASCADE``). The only mutation method
is :meth:`CompactionTelemetryStore.upsert_conversation_compaction_telemetry`
which is an ``INSERT ... ON CONFLICT(conversation_id) DO UPDATE`` — so
two concurrent ``upsert`` calls for the same conversation leave one row
with the second writer's values (last-write-wins semantics; the row is
not a queue).

### CHECK constraints

The migration (#01-04) declares two CHECK constraints on this table:

* ``cache_state IN ('hot', 'cold', 'unknown')``
* ``last_activity_band IN ('low', 'medium', 'high')``

Both are mirrored in Python as :data:`CacheState` / :data:`ActivityBand`
``Literal`` types. Passing a value outside the union to the upsert will
raise :class:`sqlite3.IntegrityError` at write time (the CHECK fires
inside SQLite), with the standard
``CHECK constraint failed: <table>`` message.

### Timestamp handling

SQLite stores timestamps via ``datetime('now')`` as
``"YYYY-MM-DD HH:MM:SS"`` with no timezone marker, which Python's
:meth:`datetime.fromisoformat` parses as naive (no tzinfo). Per
``lossless-claw/src/store/parse-utc-timestamp.ts`` and
``storage.md`` §4.3, these strings are **always** UTC — the store
attaches :class:`datetime.timezone.utc` after parsing so callers receive
timezone-aware datetimes. A small inline :func:`_parse_utc_timestamp`
helper does this (the full ``parse-utc-timestamp`` port lands in #01-11;
inlining it here avoids a cross-issue dependency).

See:

* ``lossless-claw/src/store/compaction-telemetry-store.ts`` (LCM commit
  ``1f07fbd``) — the canonical TS source.
* ``docs/porting-guides/storage.md`` §4.3 — store contract.
* ``epics/01-storage/01-10-telemetry-stores.md`` — this module's issue spec.
* :func:`lossless_hermes.db.migration.run_lcm_migrations` —
  creates the ``conversation_compaction_telemetry`` table.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Optional, TypeVar

from pydantic import BaseModel, ConfigDict

__all__ = [
    "ActivityBand",
    "CacheState",
    "CompactionTelemetryStore",
    "ConversationCompactionTelemetryRecord",
    "UpsertConversationCompactionTelemetryInput",
]

# ---------------------------------------------------------------------------
# Type aliases (mirror the TS unions)
# ---------------------------------------------------------------------------

CacheState = Literal["hot", "cold", "unknown"]
"""Prompt-cache state for a conversation.

Mirrors the CHECK constraint on ``conversation_compaction_telemetry.cache_state``
(``IN ('hot', 'cold', 'unknown')``). Passing any other string to the upsert
raises :class:`sqlite3.IntegrityError` at write time.
"""

ActivityBand = Literal["low", "medium", "high"]
"""Activity rate band for a conversation.

Mirrors the CHECK constraint on
``conversation_compaction_telemetry.last_activity_band``
(``IN ('low', 'medium', 'high')``). Passing any other string to the upsert
raises :class:`sqlite3.IntegrityError` at write time.
"""


T = TypeVar("T")


# ---------------------------------------------------------------------------
# Records (Pydantic models — ADR-024 §"Open questions" #1 default)
# ---------------------------------------------------------------------------


class ConversationCompactionTelemetryRecord(BaseModel):
    """A persisted prompt-cache telemetry snapshot for one conversation.

    Field names mirror the TS ``ConversationCompactionTelemetryRecord``
    type in camelCase form (Python: snake_case). All ``datetime`` fields
    are **UTC-aware** (parsed via :func:`_parse_utc_timestamp`); the only
    exception is ``updated_at`` which defaults to ``datetime(1970, 1, 1, tzinfo=UTC)``
    (the Unix epoch) when the column is somehow ``NULL`` — mirrors the TS
    ``new Date(0)`` fallback on line 91 of the source.

    See the table definition in
    :func:`lossless_hermes.db.migration._SQL_TABLE_CONVERSATION_COMPACTION_TELEMETRY`.
    """

    # Frozen + immutable: the record is a snapshot; producers re-upsert
    # the next snapshot rather than mutating the existing instance.
    model_config = ConfigDict(frozen=True)

    conversation_id: int
    last_observed_cache_read: int | None = None
    last_observed_cache_write: int | None = None
    last_observed_prompt_token_count: int | None = None
    last_observed_cache_hit_at: datetime | None = None
    last_observed_cache_break_at: datetime | None = None
    cache_state: CacheState
    consecutive_cold_observations: int = 0
    retention: str | None = None
    last_leaf_compaction_at: datetime | None = None
    turns_since_leaf_compaction: int = 0
    tokens_accumulated_since_leaf_compaction: int = 0
    last_activity_band: ActivityBand = "low"
    last_api_call_at: datetime | None = None
    last_cache_touch_at: datetime | None = None
    provider: str | None = None
    model: str | None = None
    updated_at: datetime


class UpsertConversationCompactionTelemetryInput(BaseModel):
    """Input for :meth:`CompactionTelemetryStore.upsert_conversation_compaction_telemetry`.

    Only ``conversation_id`` and ``cache_state`` are required; every other
    field has a sensible default mirroring the TS upsert (lines 184-202 of
    the source). Defaults applied here are the **same** defaults the
    SQLite table would apply for omitted columns — keeping defaults
    centralized in one place (Python) instead of duplicating in both the
    Python record and the SQL DEFAULT clauses.
    """

    # The upsert accepts user input — allow flexible construction but
    # validate field types/constraints at instantiation time.
    model_config = ConfigDict(strict=False)

    conversation_id: int
    last_observed_cache_read: int | None = None
    last_observed_cache_write: int | None = None
    last_observed_prompt_token_count: int | None = None
    last_observed_cache_hit_at: datetime | None = None
    last_observed_cache_break_at: datetime | None = None
    cache_state: CacheState
    consecutive_cold_observations: int = 0
    retention: str | None = None
    last_leaf_compaction_at: datetime | None = None
    turns_since_leaf_compaction: int = 0
    tokens_accumulated_since_leaf_compaction: int = 0
    last_activity_band: ActivityBand = "low"
    last_api_call_at: datetime | None = None
    last_cache_touch_at: datetime | None = None
    provider: str | None = None
    model: str | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_utc_timestamp(value: str | None) -> datetime | None:
    """Parse a SQLite UTC timestamp string into a UTC-aware ``datetime``.

    SQLite stores timestamps via ``datetime('now')`` as
    ``"YYYY-MM-DD HH:MM:SS"`` — no timezone marker, but the value IS
    UTC per the SQLite docs. ``datetime.fromisoformat`` parses this as
    naive (no tzinfo); we attach :data:`datetime.timezone.utc` so
    callers receive aware datetimes.

    Mirrors ``lossless-claw/src/store/parse-utc-timestamp.ts``
    ``parseUtcTimestampOrNull``. The full helper lands in #01-11; inlined
    here to avoid a cross-issue dependency for the 01-10 PR.

    Args:
        value: A SQLite timestamp string or ``None``.

    Returns:
        A UTC-aware :class:`datetime` instance, or ``None`` for ``None``
        input. Strings already carrying a ``Z`` suffix or ``±HH:MM``
        offset are parsed directly (timezone preserved).
    """
    if value is None:
        return None
    s = value.strip()
    if not s:
        return None
    # If the value already has a Z or numeric offset, fromisoformat
    # (Python 3.11+) handles both: trailing 'Z' was added in 3.11.
    if s.endswith("Z"):
        # Python 3.11 fromisoformat accepts 'Z'; for forward compat we
        # normalize to +00:00 explicitly.
        s = s[:-1] + "+00:00"
    # SQLite default format uses space separator; ISO requires 'T'.
    if "T" not in s and " " in s:
        s = s.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Mirrors the TS ``new Date(value)`` behavior — invalid strings
        # become an "invalid date" sentinel. In Python the closest analogue
        # is to return None (callers already handle None for missing rows).
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _to_iso_or_none(value: datetime | None) -> str | None:
    """Format a datetime as ISO-8601 UTC, or ``None``.

    Mirrors the TS ``date?.toISOString() ?? null`` pattern used throughout
    ``compaction-telemetry-store.ts``. Naive datetimes are interpreted as
    UTC (the producer would be misusing the API otherwise — every
    persisted timestamp in this store is UTC per ``storage.md`` §4.3).

    Output format: ``"YYYY-MM-DDTHH:MM:SS[.ffffff]+00:00"`` (Python's
    :meth:`datetime.isoformat` default). SQLite's ``datetime()`` builtin
    accepts this form on read for any subsequent comparisons.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        # Defensive: treat naive as UTC rather than letting astimezone
        # raise. Documented in the docstring above.
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat()


# Row tuple shape returned by SELECT — 18 columns in order:
# (conversation_id, last_observed_cache_read, last_observed_cache_write,
#  last_observed_prompt_token_count, last_observed_cache_hit_at,
#  last_observed_cache_break_at, cache_state, consecutive_cold_observations,
#  retention, last_leaf_compaction_at, turns_since_leaf_compaction,
#  tokens_accumulated_since_leaf_compaction, last_activity_band,
#  last_api_call_at, last_cache_touch_at, provider, model, updated_at)


def _row_to_record(
    row: tuple[Any, ...],
) -> ConversationCompactionTelemetryRecord:
    """Convert a SELECT row tuple into a typed record.

    Mirrors ``toConversationCompactionTelemetryRecord`` (TS lines 70-93).
    Applies the same NULL-coalesce defaults the TS uses:
    ``consecutive_cold_observations ?? 0``,
    ``turns_since_leaf_compaction ?? 0``,
    ``tokens_accumulated_since_leaf_compaction ?? 0``,
    ``last_activity_band ?? "low"``,
    ``updated_at ?? Unix epoch``.
    """
    # Unpack defensively (tuple-of-18 is the contract; mismatch = bug).
    (
        conversation_id,
        last_observed_cache_read,
        last_observed_cache_write,
        last_observed_prompt_token_count,
        last_observed_cache_hit_at,
        last_observed_cache_break_at,
        cache_state,
        consecutive_cold_observations,
        retention,
        last_leaf_compaction_at,
        turns_since_leaf_compaction,
        tokens_accumulated_since_leaf_compaction,
        last_activity_band,
        last_api_call_at,
        last_cache_touch_at,
        provider,
        model,
        updated_at,
    ) = row

    parsed_updated_at = _parse_utc_timestamp(updated_at)
    if parsed_updated_at is None:
        # TS line 91: `parseUtcTimestampOrNull(row.updated_at) ?? new Date(0)`.
        # Python equivalent: Unix epoch as a UTC-aware datetime.
        parsed_updated_at = datetime(1970, 1, 1, tzinfo=timezone.utc)

    return ConversationCompactionTelemetryRecord(
        conversation_id=conversation_id,
        last_observed_cache_read=last_observed_cache_read,
        last_observed_cache_write=last_observed_cache_write,
        last_observed_prompt_token_count=last_observed_prompt_token_count,
        last_observed_cache_hit_at=_parse_utc_timestamp(last_observed_cache_hit_at),
        last_observed_cache_break_at=_parse_utc_timestamp(last_observed_cache_break_at),
        cache_state=cache_state,
        # Default ?? 0 mirrors TS line 81.
        consecutive_cold_observations=(
            consecutive_cold_observations if consecutive_cold_observations is not None else 0
        ),
        retention=retention,
        last_leaf_compaction_at=_parse_utc_timestamp(last_leaf_compaction_at),
        turns_since_leaf_compaction=(
            turns_since_leaf_compaction if turns_since_leaf_compaction is not None else 0
        ),
        tokens_accumulated_since_leaf_compaction=(
            tokens_accumulated_since_leaf_compaction
            if tokens_accumulated_since_leaf_compaction is not None
            else 0
        ),
        # Default 'low' mirrors TS line 86.
        last_activity_band=(last_activity_band if last_activity_band is not None else "low"),
        last_api_call_at=_parse_utc_timestamp(last_api_call_at),
        last_cache_touch_at=_parse_utc_timestamp(last_cache_touch_at),
        provider=provider,
        model=model,
        updated_at=parsed_updated_at,
    )


# ---------------------------------------------------------------------------
# SQL constants
# ---------------------------------------------------------------------------

_SELECT_BY_CONV_SQL = """
    SELECT
       conversation_id,
       last_observed_cache_read,
       last_observed_cache_write,
       last_observed_prompt_token_count,
       last_observed_cache_hit_at,
       last_observed_cache_break_at,
       cache_state,
       consecutive_cold_observations,
       retention,
       last_leaf_compaction_at,
       turns_since_leaf_compaction,
       tokens_accumulated_since_leaf_compaction,
       last_activity_band,
       last_api_call_at,
       last_cache_touch_at,
       provider,
       model,
       updated_at
     FROM conversation_compaction_telemetry
     WHERE conversation_id = ?
"""

# UPSERT mirrors TS lines 145-182. Note the trailing ``updated_at`` is
# ALWAYS overwritten with ``datetime('now')`` on conflict — the upsert is
# a "refresh full row + bump updated_at" operation, not a "merge changed
# fields" operation. This matches the TS semantics exactly: every column
# is taken from ``excluded.*``.
_UPSERT_SQL = """
    INSERT INTO conversation_compaction_telemetry (
       conversation_id,
       last_observed_cache_read,
       last_observed_cache_write,
       last_observed_prompt_token_count,
       last_observed_cache_hit_at,
       last_observed_cache_break_at,
       cache_state,
       consecutive_cold_observations,
       retention,
       last_leaf_compaction_at,
       turns_since_leaf_compaction,
       tokens_accumulated_since_leaf_compaction,
       last_activity_band,
       last_api_call_at,
       last_cache_touch_at,
       provider,
       model,
       updated_at
     ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
     ON CONFLICT(conversation_id) DO UPDATE SET
       last_observed_cache_read = excluded.last_observed_cache_read,
       last_observed_cache_write = excluded.last_observed_cache_write,
       last_observed_prompt_token_count = excluded.last_observed_prompt_token_count,
       last_observed_cache_hit_at = excluded.last_observed_cache_hit_at,
       last_observed_cache_break_at = excluded.last_observed_cache_break_at,
       cache_state = excluded.cache_state,
       consecutive_cold_observations = excluded.consecutive_cold_observations,
       retention = excluded.retention,
       last_leaf_compaction_at = excluded.last_leaf_compaction_at,
       turns_since_leaf_compaction = excluded.turns_since_leaf_compaction,
       tokens_accumulated_since_leaf_compaction = excluded.tokens_accumulated_since_leaf_compaction,
       last_activity_band = excluded.last_activity_band,
       last_api_call_at = excluded.last_api_call_at,
       last_cache_touch_at = excluded.last_cache_touch_at,
       provider = excluded.provider,
       model = excluded.model,
       updated_at = datetime('now')
"""


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class CompactionTelemetryStore:
    """Persist and query per-conversation prompt-cache telemetry.

    Ports ``CompactionTelemetryStore`` from
    ``lossless-claw/src/store/compaction-telemetry-store.ts`` (LCM commit
    ``1f07fbd``). The store wraps the ``conversation_compaction_telemetry``
    table (created in #01-04) with a CRUD-only surface — there is no
    queue semantic, no event stream; producers compute a fresh snapshot
    per turn and upsert it.

    See:

    * :class:`ConversationCompactionTelemetryRecord` — the read shape.
    * :class:`UpsertConversationCompactionTelemetryInput` — the write shape.
    * ``docs/porting-guides/storage.md`` §4.3 — the contract.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        """Bind the store to a configured LCM database connection.

        Args:
            conn: A :class:`sqlite3.Connection` opened via
                :func:`lossless_hermes.db.connection.open_lcm_db` (so
                PRAGMAs and foreign-key enforcement are already applied).
        """
        self._conn = conn

    def with_transaction(self, fn: Callable[[], T]) -> T:
        """Execute ``fn`` inside a single SQLite transaction.

        Mirrors the TS ``withTransaction`` (lines 102-105 of the source).
        The TS implementation delegates to the per-DB ``transaction-mutex``
        helper; the Python equivalent lands in #01-13. For the v0 of this
        store we use the simpler ``with self._conn:`` context-manager
        form, which BEGINs a deferred transaction, COMMITs on success,
        and ROLLBACKs on exception.

        When #01-13 lands, this method will be rewritten to delegate to
        the transaction-mutex helper for re-entrant savepoint semantics
        (matching the TS contract). Until then, nested calls would
        raise ``sqlite3.OperationalError`` — callers should not nest.

        Args:
            fn: A zero-arg callable performing one or more writes.

        Returns:
            Whatever ``fn`` returns.

        Raises:
            Any exception raised by ``fn`` — the transaction is rolled
            back before the exception propagates.
        """
        with self._conn:
            return fn()

    def get_conversation_compaction_telemetry(
        self, conversation_id: int
    ) -> Optional[ConversationCompactionTelemetryRecord]:
        """Load the latest persisted telemetry for a conversation.

        Mirrors ``getConversationCompactionTelemetry`` (TS lines 108-137).

        Args:
            conversation_id: The conversation row PK.

        Returns:
            The telemetry record for that conversation, or ``None`` if no
            row exists yet (the conversation has not been observed by the
            cache-aware compaction path).
        """
        cur = self._conn.execute(_SELECT_BY_CONV_SQL, (conversation_id,))
        row = cur.fetchone()
        if row is None:
            return None
        return _row_to_record(tuple(row))

    def upsert_conversation_compaction_telemetry(
        self, input: UpsertConversationCompactionTelemetryInput
    ) -> None:
        """Insert or refresh the cache telemetry snapshot.

        Mirrors ``upsertConversationCompactionTelemetry`` (TS lines 140-203).
        On conflict (existing row for ``conversation_id``), every column is
        overwritten from the input and ``updated_at`` is bumped to
        ``datetime('now')``. The operation is idempotent: calling twice
        with the same input is equivalent to calling once (modulo
        ``updated_at`` advancing).

        Args:
            input: The snapshot to persist. ``conversation_id`` and
                ``cache_state`` are required; all other fields have
                sensible defaults.

        Raises:
            sqlite3.IntegrityError: ``cache_state`` is outside
                ``{'hot', 'cold', 'unknown'}`` or ``last_activity_band``
                is outside ``{'low', 'medium', 'high'}`` — the SQLite
                CHECK constraints in the table fire.
            sqlite3.IntegrityError: ``conversation_id`` references a
                non-existent conversation row — the FK constraint fires.
        """
        self._conn.execute(
            _UPSERT_SQL,
            (
                input.conversation_id,
                input.last_observed_cache_read,
                input.last_observed_cache_write,
                input.last_observed_prompt_token_count,
                _to_iso_or_none(input.last_observed_cache_hit_at),
                _to_iso_or_none(input.last_observed_cache_break_at),
                input.cache_state,
                input.consecutive_cold_observations,
                input.retention,
                _to_iso_or_none(input.last_leaf_compaction_at),
                input.turns_since_leaf_compaction,
                input.tokens_accumulated_since_leaf_compaction,
                input.last_activity_band,
                _to_iso_or_none(input.last_api_call_at),
                _to_iso_or_none(input.last_cache_touch_at),
                input.provider,
                input.model,
            ),
        )
