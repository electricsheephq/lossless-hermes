"""Per-conversation proactive-compaction maintenance store.

Ports ``lossless-claw/src/store/compaction-maintenance-store.ts`` (commit
``1f07fbd``, 219 LOC) to Python. The store persists and queries
per-conversation proactive-compaction *debt* — compaction the engine
deferred because of cache-aware gating, prompt-mutating risk, or other
runtime constraints (storage.md §4.4).

### Coalesced single-row state machine — NOT a queue

``conversation_compaction_maintenance`` has ``conversation_id`` as
``PRIMARY KEY``. There is exactly one debt row per conversation; two
``request_proactive_compaction_debt`` calls for the same conversation
collapse into one row with the second writer's metadata. The TS source
spells this out in its class docstring (line 79-83):

    "The row is intentionally coalesced: there is one maintenance record
    per conversation, not a queue of pending jobs."

### State-machine flow

The store exposes four mutation methods that drive a 3-state machine
per conversation:

* :meth:`request_proactive_compaction_debt` — set ``pending=1``, record
  ``reason`` + ``token_budget`` + ``current_token_count`` + bump
  ``requested_at``. The conversation now has outstanding debt.
* :meth:`mark_proactive_compaction_running` — atomic compare-and-set
  claiming the debt for processing. Sets ``running=1`` and bumps
  ``last_started_at`` IFF the row is ``running=0`` AND ``pending=1``.
  Returns ``True`` if the claim succeeded, ``False`` if another worker
  beat us to it (or the debt was never requested). This is the
  single-flight gate per storage.md §4.4.
* :meth:`mark_proactive_compaction_finished` — clear ``running``.
  If ``failure_summary`` is ``None`` (success): also clear ``pending``
  and ``last_failure_summary``. If ``failure_summary`` is a string
  (failure): keep ``pending=1`` so the next sweep retries, and store
  the failure summary for observability. Always bumps
  ``last_finished_at``.

### CHECK / FK constraints

The migration (#01-04) declares ``conversation_id`` as
``REFERENCES conversations(conversation_id) ON DELETE CASCADE``. There
are no CHECK constraints on this table — the column types (``INTEGER`` for
booleans, ``TEXT`` for ISO timestamps) carry the contract.

### Timestamp handling

See the docstring in :mod:`lossless_hermes.store.compaction_telemetry` —
the same UTC-aware-on-read / ISO-8601-on-write semantics apply here.
The :func:`_parse_utc_timestamp` and :func:`_to_iso_or_none` helpers
are duplicated locally to keep the 01-10 PR self-contained; the
:mod:`lossless_hermes.store.parse_utc_timestamp` module (#01-11) will
unify both stores onto a single helper.

See:

* ``lossless-claw/src/store/compaction-maintenance-store.ts`` (LCM
  commit ``1f07fbd``) — the canonical TS source.
* ``docs/porting-guides/storage.md`` §4.4 — store contract + coalesced
  single-row design.
* ``epics/01-storage/01-10-telemetry-stores.md`` — this module's issue spec.
* :func:`lossless_hermes.db.migration.run_lcm_migrations` —
  creates the ``conversation_compaction_maintenance`` table.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Optional, TypeVar

from pydantic import BaseModel, ConfigDict

__all__ = [
    "CompactionMaintenanceStore",
    "ConversationCompactionMaintenanceRecord",
]


T = TypeVar("T")


# ---------------------------------------------------------------------------
# Records (Pydantic model — ADR-024 §"Open questions" #1 default)
# ---------------------------------------------------------------------------


class ConversationCompactionMaintenanceRecord(BaseModel):
    """A persisted proactive-compaction maintenance snapshot for one conversation.

    Field names mirror the TS ``ConversationCompactionMaintenanceRecord``
    type in camelCase form (Python: snake_case). All ``datetime`` fields
    are UTC-aware (parsed via :func:`_parse_utc_timestamp`); ``updated_at``
    falls back to Unix epoch (``datetime(1970, 1, 1, tzinfo=UTC)``) when the
    underlying column is unexpectedly ``NULL`` — mirrors the TS
    ``new Date(0)`` fallback on line 47.

    SQLite ``INTEGER 0/1`` booleans are surfaced as Python ``bool``
    (TS lines 38 + 41: ``row.pending === 1`` / ``row.running === 1``).
    """

    # Frozen: the record is a snapshot; mutations go through one of the
    # state-transition methods on the store, which re-reads and re-writes.
    model_config = ConfigDict(frozen=True)

    conversation_id: int
    pending: bool
    requested_at: datetime | None = None
    reason: str | None = None
    running: bool
    last_started_at: datetime | None = None
    last_finished_at: datetime | None = None
    last_failure_summary: str | None = None
    token_budget: int | None = None
    current_token_count: int | None = None
    updated_at: datetime


# ---------------------------------------------------------------------------
# Internal helpers (duplicated from compaction_telemetry; unified in #01-11)
# ---------------------------------------------------------------------------


def _parse_utc_timestamp(value: str | None) -> datetime | None:
    """Parse a SQLite UTC timestamp string into a UTC-aware ``datetime``.

    See :func:`lossless_hermes.store.compaction_telemetry._parse_utc_timestamp`
    for the full contract. Duplicated here verbatim to keep the 01-10 PR
    self-contained; #01-11 unifies both stores onto a single helper.
    """
    if value is None:
        return None
    s = value.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if "T" not in s and " " in s:
        s = s.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _to_iso_or_none(value: datetime | None) -> str | None:
    """Format a datetime as ISO-8601 UTC, or ``None``.

    See :func:`lossless_hermes.store.compaction_telemetry._to_iso_or_none`
    for the full contract.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat()


def _row_to_record(
    row: tuple[Any, ...],
) -> ConversationCompactionMaintenanceRecord:
    """Convert a SELECT row tuple into a typed maintenance record.

    Mirrors ``toMaintenanceRecord`` (TS lines 33-49). 11 columns in order:
    (conversation_id, pending, requested_at, reason, running,
     last_started_at, last_finished_at, last_failure_summary,
     token_budget, current_token_count, updated_at).
    """
    (
        conversation_id,
        pending,
        requested_at,
        reason,
        running,
        last_started_at,
        last_finished_at,
        last_failure_summary,
        token_budget,
        current_token_count,
        updated_at,
    ) = row

    parsed_updated_at = _parse_utc_timestamp(updated_at)
    if parsed_updated_at is None:
        # TS line 47: `parseUtcTimestampOrNull(row.updated_at) ?? new Date(0)`.
        parsed_updated_at = datetime(1970, 1, 1, tzinfo=timezone.utc)

    return ConversationCompactionMaintenanceRecord(
        conversation_id=conversation_id,
        pending=pending == 1,
        requested_at=_parse_utc_timestamp(requested_at),
        reason=reason,
        running=running == 1,
        last_started_at=_parse_utc_timestamp(last_started_at),
        last_finished_at=_parse_utc_timestamp(last_finished_at),
        last_failure_summary=last_failure_summary,
        token_budget=token_budget,
        current_token_count=current_token_count,
        updated_at=parsed_updated_at,
    )


# ---------------------------------------------------------------------------
# SQL constants
# ---------------------------------------------------------------------------

_SELECT_BY_CONV_SQL = """
    SELECT
       conversation_id,
       pending,
       requested_at,
       reason,
       running,
       last_started_at,
       last_finished_at,
       last_failure_summary,
       token_budget,
       current_token_count,
       updated_at
     FROM conversation_compaction_maintenance
     WHERE conversation_id = ?
"""

# Request-debt UPSERT: sets pending=1, running=0 (entering the queue
# fresh-clear of any previous run), refreshes requested_at + reason, and
# updates token_budget / current_token_count if provided. Does NOT touch
# last_started_at / last_finished_at / last_failure_summary on conflict —
# those carry forward from the prior cycle for observability (matches
# TS lines 169-180 which merge the patch into the existing record).
#
# The TS path is a get-then-save with a Python-side merge. Doing it as a
# single SQL UPSERT (with ``COALESCE(?, <col>)`` for the optional
# token_budget / current_token_count fields and explicit overwrite for
# the always-set fields) achieves the same end state with fewer
# round-trips, and remains atomic under WAL.
_REQUEST_DEBT_SQL = """
    INSERT INTO conversation_compaction_maintenance (
       conversation_id,
       pending,
       requested_at,
       reason,
       running,
       last_started_at,
       last_finished_at,
       last_failure_summary,
       token_budget,
       current_token_count,
       updated_at
     ) VALUES (?, 1, ?, ?, 0, NULL, NULL, NULL, ?, ?, datetime('now'))
     ON CONFLICT(conversation_id) DO UPDATE SET
       pending = 1,
       requested_at = excluded.requested_at,
       reason = excluded.reason,
       running = 0,
       token_budget = COALESCE(excluded.token_budget, token_budget),
       current_token_count = COALESCE(excluded.current_token_count, current_token_count),
       updated_at = datetime('now')
"""

# Atomic compare-and-set claim. Returns affected-row count via
# ``Cursor.rowcount``: 1 on successful claim, 0 if the WHERE didn't match
# (already running OR no pending debt). Per the issue spec §"Acceptance
# criteria" rows 3-4 and storage.md §4.4 single-flight semantics.
_CLAIM_RUNNING_SQL = """
    UPDATE conversation_compaction_maintenance
     SET running = 1,
         last_started_at = datetime('now'),
         updated_at = datetime('now')
     WHERE conversation_id = ?
       AND running = 0
       AND pending = 1
"""

# Success path: clear pending + running, clear last_failure_summary,
# bump last_finished_at.
_FINISH_SUCCESS_SQL = """
    UPDATE conversation_compaction_maintenance
     SET pending = 0,
         running = 0,
         last_finished_at = datetime('now'),
         last_failure_summary = NULL,
         updated_at = datetime('now')
     WHERE conversation_id = ?
"""

# Failure path: clear running only (keep pending=1 for retry), record
# failure summary, bump last_finished_at.
_FINISH_FAILURE_SQL = """
    UPDATE conversation_compaction_maintenance
     SET running = 0,
         last_finished_at = datetime('now'),
         last_failure_summary = ?,
         updated_at = datetime('now')
     WHERE conversation_id = ?
"""


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class CompactionMaintenanceStore:
    """Persist and query per-conversation proactive-compaction debt.

    Ports ``CompactionMaintenanceStore`` from
    ``lossless-claw/src/store/compaction-maintenance-store.ts`` (LCM commit
    ``1f07fbd``). The store wraps the ``conversation_compaction_maintenance``
    table (created in #01-04) with a single-row-per-conversation,
    coalesced state machine — see the module docstring for the full
    state-machine flow.

    See:

    * :class:`ConversationCompactionMaintenanceRecord` — the read shape.
    * ``docs/porting-guides/storage.md`` §4.4 — the contract.
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

        Mirrors the TS ``withTransaction`` (lines 88-90 of the source).
        See :meth:`lossless_hermes.store.compaction_telemetry.CompactionTelemetryStore.with_transaction`
        for the full contract and the #01-13 follow-up note.

        Args:
            fn: A zero-arg callable performing one or more writes.

        Returns:
            Whatever ``fn`` returns.
        """
        with self._conn:
            return fn()

    def get_conversation_compaction_maintenance(
        self, conversation_id: int
    ) -> Optional[ConversationCompactionMaintenanceRecord]:
        """Load the latest persisted maintenance state for a conversation.

        Mirrors ``getConversationCompactionMaintenance`` (TS lines 93-115).

        Args:
            conversation_id: The conversation row PK.

        Returns:
            The maintenance record for that conversation, or ``None`` if
            no row exists yet (no proactive-compaction debt has ever
            been recorded for the conversation).
        """
        cur = self._conn.execute(_SELECT_BY_CONV_SQL, (conversation_id,))
        row = cur.fetchone()
        if row is None:
            return None
        return _row_to_record(tuple(row))

    def request_proactive_compaction_debt(
        self,
        *,
        conversation_id: int,
        reason: str,
        token_budget: int | None = None,
        current_token_count: int | None = None,
        requested_at: datetime | None = None,
    ) -> None:
        """Record (or refresh) deferred proactive-compaction debt.

        Mirrors ``requestProactiveCompactionDebt`` (TS lines 161-180) plus
        the issue spec §"CompactionMaintenanceStore" mutation signature.

        Sets ``pending=1`` and stores ``reason``, ``requested_at`` (default
        ``now``), ``token_budget``, ``current_token_count``. Two
        consecutive calls for the same conversation collapse into one
        row with the second writer's metadata — per storage.md §4.4
        last sentence, the row is coalesced (no queue).

        On an existing row, ``token_budget`` / ``current_token_count``
        are only overwritten when the caller passes a non-``None`` value
        (matches the TS ``input.tokenBudget ?? existing?.tokenBudget``
        pattern on lines 176-177).

        Args:
            conversation_id: The conversation row PK.
            reason: Free-text describing why the debt was incurred
                (e.g. ``"threshold"``, ``"cache-aware-defer"``,
                ``"prompt-mutation-guard"``).
            token_budget: The current token budget for the conversation,
                if known. ``None`` preserves the existing column value
                on update.
            current_token_count: The current token count for the
                conversation, if known. ``None`` preserves the existing
                column value on update.
            requested_at: Override for the ``requested_at`` column.
                Defaults to ``datetime.now(timezone.utc)``.

        Raises:
            sqlite3.IntegrityError: ``conversation_id`` references a
                non-existent conversation row — the FK constraint fires.
        """
        if requested_at is None:
            requested_at = datetime.now(timezone.utc)
        self._conn.execute(
            _REQUEST_DEBT_SQL,
            (
                conversation_id,
                _to_iso_or_none(requested_at),
                reason,
                token_budget,
                current_token_count,
            ),
        )

    def mark_proactive_compaction_running(self, conversation_id: int) -> bool:
        """Atomically claim a pending debt for processing.

        Per the issue spec §"CompactionMaintenanceStore" mutation signature
        (and storage.md §4.4 single-flight requirement): this is an atomic
        compare-and-set that only flips ``running`` from 0 to 1 when there
        is outstanding pending debt. Two concurrent workers cannot both
        claim the same row — SQLite's WAL serializes the UPDATEs, and
        the second one's WHERE clause won't match.

        Note: this is a deliberate behavioral upgrade vs the TS source
        (which does a non-atomic get-then-save merge on lines 187-194).
        The issue spec §"Acceptance criteria" rows 3-4 explicitly require
        the atomic compare-and-set with bool return.

        Args:
            conversation_id: The conversation row PK.

        Returns:
            ``True`` if the row was claimed (``running`` flipped from 0 to
            1 and ``last_started_at`` advanced). ``False`` if the row was
            already running, had no pending debt, or did not exist.
        """
        cur = self._conn.execute(_CLAIM_RUNNING_SQL, (conversation_id,))
        return cur.rowcount == 1

    def mark_proactive_compaction_finished(
        self,
        conversation_id: int,
        *,
        failure_summary: str | None = None,
    ) -> None:
        """Release the running claim, recording success or failure.

        Mirrors ``markProactiveCompactionFinished`` (TS lines 197-218) with
        the issue spec §"CompactionMaintenanceStore" simplified signature.

        Two paths:

        * **Success** (``failure_summary is None``): clear ``pending`` and
          ``running``, clear any prior ``last_failure_summary``, bump
          ``last_finished_at``. The conversation has no outstanding
          debt and the next ``request_proactive_compaction_debt`` will
          start a fresh cycle.
        * **Failure** (``failure_summary`` is a string): clear only
          ``running``, record the failure summary, bump
          ``last_finished_at``. ``pending`` stays at 1 so the next
          sweep retries — failures don't drain the debt.

        Note: this signature differs slightly from the TS source, which
        also accepted ``keepPending: boolean | undefined`` to manually
        force-keep pending on success. The issue spec drops that
        affordance — ``failure_summary != None`` is the canonical
        keep-pending signal. The TS ``finishedAt`` override is also
        dropped; we always use ``datetime('now')`` for write-time
        clock consistency.

        Args:
            conversation_id: The conversation row PK.
            failure_summary: If a string, the run failed and this is
                recorded in ``last_failure_summary``; ``pending`` stays
                1 for retry. If ``None``, the run succeeded; ``pending``
                clears to 0 and any prior failure summary clears.

        Note:
            If no row exists for ``conversation_id``, this is a no-op
            (the UPDATE's WHERE clause doesn't match). Mirrors the TS
            ``get → merge → save`` flow where save with a missing prior
            record creates the row; here we choose the safer no-op
            semantic — callers should always call
            :meth:`request_proactive_compaction_debt` first to seed the
            row, and :meth:`mark_proactive_compaction_running` to claim
            it, before this method runs.
        """
        if failure_summary is None:
            self._conn.execute(_FINISH_SUCCESS_SQL, (conversation_id,))
        else:
            self._conn.execute(_FINISH_FAILURE_SQL, (failure_summary, conversation_id))
