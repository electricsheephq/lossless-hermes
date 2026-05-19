"""Doctor cleaners — DB-wide bulk conversation deletion.

Ports ``lossless-claw/src/plugin/lcm-doctor-cleaners.ts`` (LCM commit
``1f07fbd`` on branch ``pr-613``, 641 LOC) to Python. This is the
DB-wide row-deletion path of ``/lcm doctor clean`` and ``/lcm doctor
clean apply`` — it bulk-deletes whole conversations matching predefined
predicates, then cascades the delete through every dependent table.

The module is the sibling of :mod:`lossless_hermes.doctor.apply` (issue
08-07, summary repair). The two are intentionally disjoint — cleaners
delete whole conversations by structural predicate; apply rewrites
broken summary *content* within one conversation. See
``docs/porting-guides/doctor-ops.md`` §"Cleaners — full inventory" for
the canonical spec, and ``epics/08-cli-ops/08-08-doctor-cleaners.md`` for
this issue.

### The three cleaners

There are EXACTLY three cleaner definitions (TS ``CLEANER_DEFINITIONS``,
``lcm-doctor-cleaners.ts:71-96``), in this fixed order:

1. ``archived_subagents`` — ``active = 0`` AND ``session_key LIKE
   'agent:main:subagent:%'``.
2. ``cron_sessions`` — ``session_key LIKE 'agent:main:cron:%'`` (no
   active filter — purges live and archived cron sessions alike).
3. ``null_subagent_context`` — ``session_key IS NULL`` AND ``active = 0``
   AND ``archived_at IS NOT NULL`` AND the conversation's earliest stored
   message begins with ``[Subagent Context]``. This is the only cleaner
   that needs a first-message join (``needs_first_message=True``).

### Scan vs. apply: same predicate SQL

:func:`scan_doctor_cleaners` (dry run) and :func:`apply_doctor_cleaners`
(destructive) build their matched-conversation sets from the SAME
``predicate_sql`` per cleaner, so the dry-run count is guaranteed to
equal the apply count (issue AC: "Scan + apply use the same predicate
SQL"). The only structural difference is that scan additionally computes
counts + top-3 examples for rendering.

### Apply-time guards (load-bearing — DO NOT reorder)

1. **Backup is mandatory and happens FIRST.**
   :func:`get_doctor_cleaner_apply_unavailable_reason` rejects in-memory
   DBs up front. On a file-backed DB, the backup is written via
   :func:`lossless_hermes.plugin.db_backup.write_lcm_database_backup`
   BEFORE the ``BEGIN IMMEDIATE`` — so a crash mid-cascade leaves a
   recoverable snapshot. The issue AC
   ``test_backup_before_begin_immediate`` asserts this ordering via the
   backup file's filesystem mtime.
2. **Temp-table staging.** Four (or five, when a ``needs_first_message``
   cleaner is selected) ``TEMP`` tables stage the candidate /
   conversation / summary / message id sets. They are ALWAYS dropped in
   a ``finally`` block — even when the cascade raises.
3. **FTS branches are best-effort.** Each of ``messages_fts`` /
   ``summaries_fts`` / ``summaries_fts_cjk`` is gated by :func:`_has_table`.
   A DB without one of those virtual tables (FTS5 unavailable) still
   applies cleanly.
4. **VACUUM only when it pays.** ``VACUUM`` + ``PRAGMA
   wal_checkpoint(TRUNCATE)`` fire only when ``vacuum=True`` AND at least
   one conversation was actually deleted — a no-op apply stays cheap.

### Why ``db.execute`` per statement, never ``executescript``

The stdlib :py:meth:`sqlite3.Connection.executescript` issues an
implicit ``COMMIT`` before executing — which would silently close the
``BEGIN IMMEDIATE`` transaction this module relies on. Every statement
here therefore runs through :py:meth:`sqlite3.Connection.execute` (one
statement per call). The TS source uses ``db.exec`` (node:sqlite, no
implicit commit) — the Python port deliberately diverges to preserve
transaction integrity.

### Connection mode

The connection is expected to be opened with ``isolation_level=None``
(autocommit / manual-transaction mode), matching every other LCM-host
connection (see :func:`lossless_hermes.db.connection.open_lcm_db`). This
module issues ``BEGIN IMMEDIATE`` / ``COMMIT`` / ``ROLLBACK`` explicitly;
the stdlib's implicit-transaction machinery must be OFF or it will fight
the explicit statements.

### Wave-N provenance

``grep -n "Wave-" src/plugin/lcm-doctor-cleaners.ts`` against commit
``1f07fbd`` returns NO matches — the TS source carries no Wave-N audit
comments. Per ADR-029 this module is therefore not tagged with any
``# LCM Wave-N`` markers.

See:

* ``epics/08-cli-ops/08-08-doctor-cleaners.md`` — this issue spec.
* ``docs/porting-guides/doctor-ops.md`` §"Cleaners — full inventory"
  lines 169-188 — the canonical cleaner inventory + apply-time guards.
* ``docs/adr/029-wave-fix-provenance.md`` — provenance policy (this
  module has no Wave-N markers; TS source has none).
* ``lossless-claw/src/plugin/lcm-doctor-cleaners.ts`` — TS source at
  commit ``1f07fbd`` (pr-613 head).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Union

from lossless_hermes.db.connection import get_file_backed_database_path
from lossless_hermes.doctor.contract import (
    DoctorCleanerApplyResult,
    DoctorCleanerExample,
    DoctorCleanerFilter,
    DoctorCleanerFilterStat,
    DoctorCleanerId,
    DoctorCleanerScan,
)
from lossless_hermes.doctor.shared import (
    FIRST_MESSAGE_PREVIEW_LIMIT,
    normalize_first_message_preview,
)
from lossless_hermes.plugin.db_backup import (
    LcmDatabaseBackupError,
    build_lcm_database_backup_path,
    write_lcm_database_backup,
)

__all__ = [
    "apply_doctor_cleaners",
    "get_doctor_cleaner_apply_unavailable_reason",
    "get_doctor_cleaner_filter_ids",
    "get_doctor_cleaner_filters",
    "scan_doctor_cleaners",
]


# ---------------------------------------------------------------------------
# Cleaner definitions — ports TS ``CLEANER_DEFINITIONS`` (lines 71-96)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CleanerDefinition:
    """Internal cleaner spec — id + label + description + predicate SQL.

    Ports the TS ``CleanerDefinition`` type (``lcm-doctor-cleaners.ts:46-53``).
    Not part of the public contract — :class:`DoctorCleanerFilter` /
    :class:`DoctorCleanerFilterStat` (in :mod:`lossless_hermes.doctor.contract`)
    are the public Pydantic shapes; this dataclass holds the SQL fragments
    that build the matched-conversation sets.

    Attributes:
        id: The cleaner identifier.
        label: Human-readable name (verbatim from the TS source).
        description: One-line description (verbatim from the TS source).
        candidate_predicate_sql: A SQL boolean expression over a
            ``conversations c`` alias that is the BROAD candidate filter —
            it never references the first-message join, so it can stage
            the candidate-conversation temp table cheaply. For two of the
            three cleaners this is identical to :attr:`predicate_sql`; for
            ``null_subagent_context`` it is the subset of the predicate
            that does not need the message join.
        predicate_sql: The FULL predicate. For ``null_subagent_context``
            this additionally references ``message_stats.first_message_preview``
            (the alias of the staged first-message temp table joined into
            the matched-conversation query).
        needs_first_message: ``True`` only for ``null_subagent_context`` —
            signals that a first-message temp table must be staged and
            LEFT JOINed when this cleaner is selected.
    """

    id: DoctorCleanerId
    label: str
    description: str
    candidate_predicate_sql: str
    predicate_sql: str
    needs_first_message: bool = False


# The three cleaner definitions, in the canonical order. Labels and
# descriptions are byte-verbatim from ``lcm-doctor-cleaners.ts:71-96``;
# the predicate SQL fragments are likewise transcribed 1:1 so the
# EXPLAIN-QUERY-PLAN snapshot test can confirm parity.
_CLEANER_DEFINITIONS: tuple[_CleanerDefinition, ...] = (
    _CleanerDefinition(
        id="archived_subagents",
        label="Archived subagents",
        description="Archived subagent conversations keyed as agent:main:subagent:*.",
        candidate_predicate_sql=("(c.active = 0 AND c.session_key LIKE 'agent:main:subagent:%')"),
        predicate_sql="(c.active = 0 AND c.session_key LIKE 'agent:main:subagent:%')",
    ),
    _CleanerDefinition(
        id="cron_sessions",
        label="Cron sessions",
        description="Background cron conversations keyed as agent:main:cron:*.",
        candidate_predicate_sql="(c.session_key LIKE 'agent:main:cron:%')",
        predicate_sql="(c.session_key LIKE 'agent:main:cron:%')",
    ),
    _CleanerDefinition(
        id="null_subagent_context",
        label="NULL-key subagent context",
        description=(
            "Archived conversations with NULL session_key whose first stored "
            "message begins with [Subagent Context]."
        ),
        candidate_predicate_sql=(
            "(c.session_key IS NULL AND c.active = 0 AND c.archived_at IS NOT NULL)"
        ),
        predicate_sql=(
            "(c.session_key IS NULL AND c.active = 0 AND c.archived_at IS NOT NULL "
            "AND message_stats.first_message_preview LIKE '[Subagent Context]%')"
        ),
        needs_first_message=True,
    ),
)


# Canonical id order — derived from the definitions so the two never drift.
_DOCTOR_CLEANER_IDS: tuple[DoctorCleanerId, ...] = tuple(
    definition.id for definition in _CLEANER_DEFINITIONS
)


def _get_cleaner_definitions(
    filter_ids: Optional[Sequence[DoctorCleanerId]] = None,
) -> list[_CleanerDefinition]:
    """Resolve the selected cleaner definitions, preserving canonical order.

    Ports the TS ``getCleanerDefinitions`` (``lcm-doctor-cleaners.ts:102-108``).

    Args:
        filter_ids: Optional subset of cleaner ids to select. ``None`` or
            an empty sequence selects ALL three cleaners (TS:
            ``!filterIds || filterIds.length === 0``). Unknown ids in the
            sequence are silently ignored (TS uses ``Set`` membership).

    Returns:
        The matching :class:`_CleanerDefinition` objects in the canonical
        ``_CLEANER_DEFINITIONS`` order — NOT in the caller's argument
        order (mirrors the TS ``.filter`` over the definitions array).
    """
    if not filter_ids:
        return list(_CLEANER_DEFINITIONS)
    requested = set(filter_ids)
    return [definition for definition in _CLEANER_DEFINITIONS if definition.id in requested]


# ---------------------------------------------------------------------------
# SQL builders — ports the TS query-string assembly helpers
# ---------------------------------------------------------------------------


def _build_matched_conversations_sql(
    definitions: Sequence[_CleanerDefinition],
    *,
    include_filter_id: bool,
    message_stats_table_name: Optional[str] = None,
) -> str:
    """Build the ``UNION ALL`` query of (filter_id?, conversation_id) rows.

    Ports the TS ``buildMatchedConversationsSql`` (``lcm-doctor-cleaners.ts:121-147``).
    Each cleaner contributes one ``SELECT ... FROM conversations c WHERE
    <predicate>`` arm; the arms are joined with ``UNION ALL`` (NOT
    ``UNION`` — a conversation matching two cleaners SHOULD appear once
    per matching filter so the per-filter counts are correct).

    Args:
        definitions: The selected cleaner definitions.
        include_filter_id: When ``True``, each arm selects a literal
            ``'<id>' AS filter_id`` column (used by the scan path to
            attribute rows to cleaners). When ``False``, only
            ``conversation_id`` is selected (used by the apply path,
            which deduplicates into a single conversation-id set).
        message_stats_table_name: When a ``needs_first_message`` cleaner
            is present, the fully-qualified name of the staged
            first-message temp table (e.g.
            ``"temp.doctor_cleaner_first_messages"``). The arm for that
            cleaner LEFT JOINs this table under the alias ``message_stats``
            so its ``predicate_sql`` can reference
            ``message_stats.first_message_preview``.

    Returns:
        A SQL ``SELECT`` string. For an empty ``definitions`` sequence,
        a degenerate ``... WHERE 0`` query (selects no rows) so callers
        can always ``INSERT ... <this>`` without a special case.
    """
    if not definitions:
        # Degenerate empty query — never matches a row. Mirrors the TS
        # ``WHERE 0`` short-circuit.
        return (
            "SELECT NULL AS filter_id, NULL AS conversation_id WHERE 0"
            if include_filter_id
            else "SELECT NULL AS conversation_id WHERE 0"
        )

    arms: list[str] = []
    for definition in definitions:
        select_sql = (
            f"SELECT '{definition.id}' AS filter_id, c.conversation_id"
            if include_filter_id
            else "SELECT c.conversation_id"
        )
        join_sql = (
            f"LEFT JOIN {message_stats_table_name} message_stats "
            "ON message_stats.conversation_id = c.conversation_id"
            if definition.needs_first_message and message_stats_table_name
            else ""
        )
        arms.append(
            f"{select_sql}\n"
            f"              FROM conversations c\n"
            f"              {join_sql}\n"
            f"              WHERE {definition.predicate_sql}"
        )
    return "\nUNION ALL\n".join(arms)


def _build_candidate_conversations_sql(definitions: Sequence[_CleanerDefinition]) -> str:
    """Build the ``UNION`` query of candidate ``conversation_id`` rows.

    Ports the TS ``buildCandidateConversationsSql`` (``lcm-doctor-cleaners.ts:149-160``).
    Uses each cleaner's ``candidate_predicate_sql`` (the broad filter that
    never touches the first-message join) and joins the arms with
    ``UNION`` (deduplicating) — the candidate temp table is a plain id
    set, so duplicate rows would just be redundant.

    Args:
        definitions: The selected cleaner definitions.

    Returns:
        A SQL ``SELECT`` string; a degenerate ``... WHERE 0`` for an
        empty ``definitions`` sequence.
    """
    if not definitions:
        return "SELECT NULL AS conversation_id WHERE 0"
    arms = [
        f"SELECT c.conversation_id\n"
        f"              FROM conversations c\n"
        f"              WHERE {definition.candidate_predicate_sql}"
        for definition in definitions
    ]
    return "\nUNION\n".join(arms)


# ---------------------------------------------------------------------------
# Scan-time temp tables
# ---------------------------------------------------------------------------


def _drop_temp_cleaner_scan_tables(db: sqlite3.Connection) -> None:
    """Drop the three scan-time temp tables (idempotent).

    Ports the TS ``dropTempCleanerScanTables`` (``lcm-doctor-cleaners.ts:162-166``).
    ``DROP TABLE IF EXISTS`` so it is a clean no-op when the tables were
    never created. Called both before staging (defensive — clears a
    prior aborted scan) and in the scan's ``finally`` block.
    """
    db.execute("DROP TABLE IF EXISTS temp.doctor_cleaner_scan_matches")
    db.execute("DROP TABLE IF EXISTS temp.doctor_cleaner_scan_message_stats")
    db.execute("DROP TABLE IF EXISTS temp.doctor_cleaner_candidate_conversations")


def _stage_cleaner_scan_tables(
    db: sqlite3.Connection,
    definitions: Sequence[_CleanerDefinition],
) -> None:
    """Stage the three scan-time temp tables for the selected cleaners.

    Ports the TS ``stageCleanerScanTables`` (``lcm-doctor-cleaners.ts:168-249``).
    Builds, in order:

    1. ``doctor_cleaner_candidate_conversations`` — the deduplicated
       candidate conversation-id set (broad ``candidate_predicate_sql``).
    2. ``doctor_cleaner_scan_message_stats`` — per candidate conversation,
       its message count and (only when a ``needs_first_message`` cleaner
       is selected) the 256-char prefix of its earliest message. When no
       cleaner needs the first message, ``first_message_preview`` is left
       ``NULL`` and only the count is computed.
    3. ``doctor_cleaner_scan_matches`` — the ``(filter_id,
       conversation_id)`` attribution set, built from the FULL
       ``predicate_sql`` joined against the message-stats table.

    The earliest-message resolution uses
    ``ROW_NUMBER() OVER (PARTITION BY conversation_id ORDER BY seq ASC,
    created_at ASC, message_id ASC)`` — the ``seq`` column is the primary
    ordering key (it is the canonical message sequence); ``created_at``
    and ``message_id`` are deterministic tie-breakers for the rare case
    of equal ``seq`` values.

    Args:
        db: Open connection. Caller is responsible for the temp tables'
            lifecycle (this function's caller drops them in ``finally``).
        definitions: The selected cleaner definitions. An empty sequence
            stages nothing (the caller short-circuits before reaching
            here, but the guard is kept for parity with the TS source).
    """
    _drop_temp_cleaner_scan_tables(db)
    if not definitions:
        return

    # ---- (1) candidate conversation id set ----
    db.execute(
        """
        CREATE TEMP TABLE doctor_cleaner_candidate_conversations (
          conversation_id INTEGER PRIMARY KEY
        ) WITHOUT ROWID
        """
    )
    db.execute(
        "INSERT INTO temp.doctor_cleaner_candidate_conversations (conversation_id)\n"
        + _build_candidate_conversations_sql(definitions)
    )

    # ---- (2) per-conversation message stats ----
    db.execute(
        """
        CREATE TEMP TABLE doctor_cleaner_scan_message_stats (
          conversation_id INTEGER PRIMARY KEY,
          first_message_preview TEXT,
          message_count INTEGER NOT NULL
        )
        """
    )
    if any(definition.needs_first_message for definition in definitions):
        # Window-function path: rank messages within each candidate
        # conversation, pick row_num == 1 as the earliest message, and
        # carry the per-conversation COUNT(*) alongside.
        db.execute(
            f"""
            WITH ranked_messages AS (
              SELECT
                m.conversation_id,
                m.content,
                ROW_NUMBER() OVER (
                  PARTITION BY m.conversation_id
                  ORDER BY m.seq ASC, m.created_at ASC, m.message_id ASC
                ) AS row_num,
                COUNT(*) OVER (PARTITION BY m.conversation_id) AS message_count
              FROM messages m
              JOIN temp.doctor_cleaner_candidate_conversations candidates
                ON candidates.conversation_id = m.conversation_id
            )
            INSERT INTO temp.doctor_cleaner_scan_message_stats (
              conversation_id,
              first_message_preview,
              message_count
            )
            SELECT
              conversation_id,
              MAX(
                CASE WHEN row_num = 1
                  THEN substr(content, 1, {FIRST_MESSAGE_PREVIEW_LIMIT})
                END
              ) AS first_message_preview,
              MAX(message_count) AS message_count
            FROM ranked_messages
            GROUP BY conversation_id
            """
        )
    else:
        # No cleaner needs the first message — only the count is needed.
        db.execute(
            """
            INSERT INTO temp.doctor_cleaner_scan_message_stats (
              conversation_id,
              first_message_preview,
              message_count
            )
            SELECT
              m.conversation_id,
              NULL AS first_message_preview,
              COUNT(*) AS message_count
            FROM messages m
            JOIN temp.doctor_cleaner_candidate_conversations candidates
              ON candidates.conversation_id = m.conversation_id
            GROUP BY m.conversation_id
            """
        )

    # ---- (3) (filter_id, conversation_id) attribution set ----
    db.execute(
        """
        CREATE TEMP TABLE doctor_cleaner_scan_matches (
          filter_id TEXT NOT NULL,
          conversation_id INTEGER NOT NULL,
          PRIMARY KEY (filter_id, conversation_id)
        ) WITHOUT ROWID
        """
    )
    matched_conversations_sql = _build_matched_conversations_sql(
        definitions,
        include_filter_id=True,
        message_stats_table_name="temp.doctor_cleaner_scan_message_stats",
    )
    db.execute(
        "INSERT INTO temp.doctor_cleaner_scan_matches (filter_id, conversation_id)\n"
        + matched_conversations_sql
    )


# ---------------------------------------------------------------------------
# Public: cleaner metadata listing
# ---------------------------------------------------------------------------


def get_doctor_cleaner_filters() -> list[DoctorCleanerFilter]:
    """Return the three cleaner definitions as metadata (no DB read).

    Ports the TS ``getDoctorCleanerFilters`` (``lcm-doctor-cleaners.ts:251-257``).
    Pure — does not touch a database. Returns the ``id`` / ``label`` /
    ``description`` triplet for each cleaner, in the canonical order
    (``archived_subagents``, ``cron_sessions``, ``null_subagent_context``).

    Returns:
        A fresh list of three :class:`DoctorCleanerFilter` objects.
    """
    return [
        DoctorCleanerFilter(
            id=definition.id,
            label=definition.label,
            description=definition.description,
        )
        for definition in _CLEANER_DEFINITIONS
    ]


def get_doctor_cleaner_filter_ids() -> list[DoctorCleanerId]:
    """Return the three cleaner ids in canonical order.

    Ports the TS ``getDoctorCleanerFilterIds`` (``lcm-doctor-cleaners.ts:259-261``).
    Returns a fresh list (the TS spreads ``[...DOCTOR_CLEANER_IDS]`` so
    callers cannot mutate the module-level array).

    Returns:
        ``["archived_subagents", "cron_sessions", "null_subagent_context"]``.
    """
    return list(_DOCTOR_CLEANER_IDS)


# ---------------------------------------------------------------------------
# Public: scan (dry run)
# ---------------------------------------------------------------------------


def scan_doctor_cleaners(
    db: sqlite3.Connection,
    filter_ids: Optional[Sequence[DoctorCleanerId]] = None,
) -> DoctorCleanerScan:
    """Scan the DB for conversations the selected cleaners would delete.

    Ports the TS ``scanDoctorCleaners`` (``lcm-doctor-cleaners.ts:263-380``).
    Read-only — stages three ``TEMP`` tables, runs two aggregate queries
    (counts + examples), and ALWAYS drops the temp tables in a ``finally``
    block. The dry-run conversation/message counts are guaranteed to
    equal an :func:`apply_doctor_cleaners` run with the same ``filter_ids``
    (both build their matched sets from the same ``predicate_sql``).

    Args:
        db: Open connection. The schema must have ``conversations`` and
            ``messages``; both exist on any LCM-host DB by definition.
        filter_ids: Optional subset of cleaner ids. ``None`` / empty
            selects all three cleaners.

    Returns:
        A :class:`DoctorCleanerScan` with one
        :class:`DoctorCleanerFilterStat` per selected cleaner (counts +
        up to three example conversations sorted ``message_count DESC,
        created_at DESC, conversation_id DESC``), plus the
        deduplicated DB-wide ``total_distinct_*`` counts. An empty scan
        (no filters resolved) returns all-zero counts and an empty
        ``filters`` list.
    """
    definitions = _get_cleaner_definitions(filter_ids)
    if not definitions:
        return DoctorCleanerScan(
            filters=[],
            total_distinct_conversations=0,
            total_distinct_messages=0,
        )

    try:
        _stage_cleaner_scan_tables(db, definitions)

        # ---- counts: per-filter + DB-wide distinct totals ----
        # The outer SELECT repeats the two distinct-total scalar
        # sub-selects on every filter_counts row; they are constant, so
        # ``counts[0]`` carries the canonical totals (mirrors the TS
        # ``const totals = counts[0]``).
        count_rows = db.execute(
            """
            WITH filter_counts AS (
              SELECT
                matches.filter_id,
                COUNT(*) AS conversation_count,
                COALESCE(SUM(COALESCE(stats.message_count, 0)), 0) AS message_count
              FROM temp.doctor_cleaner_scan_matches matches
              LEFT JOIN temp.doctor_cleaner_scan_message_stats stats
                ON stats.conversation_id = matches.conversation_id
              GROUP BY matches.filter_id
            ),
            distinct_conversations AS (
              SELECT DISTINCT conversation_id
              FROM temp.doctor_cleaner_scan_matches
            )
            SELECT
              fc.filter_id,
              fc.conversation_count,
              fc.message_count,
              COALESCE((SELECT COUNT(*) FROM distinct_conversations), 0)
                AS total_conversation_count,
              COALESCE((
                SELECT SUM(COALESCE(stats.message_count, 0))
                FROM distinct_conversations dc
                LEFT JOIN temp.doctor_cleaner_scan_message_stats stats
                  ON stats.conversation_id = dc.conversation_id
              ), 0) AS total_message_count
            FROM filter_counts fc
            """
        ).fetchall()

        # ---- examples: top-3 conversations per filter ----
        example_rows = db.execute(
            """
            WITH ranked_examples AS (
              SELECT
                matches.filter_id,
                c.conversation_id,
                c.session_key,
                COALESCE(stats.message_count, 0) AS message_count,
                stats.first_message_preview,
                ROW_NUMBER() OVER (
                  PARTITION BY matches.filter_id
                  ORDER BY COALESCE(stats.message_count, 0) DESC,
                           c.created_at DESC,
                           c.conversation_id DESC
                ) AS example_rank
              FROM temp.doctor_cleaner_scan_matches matches
              JOIN conversations c ON c.conversation_id = matches.conversation_id
              LEFT JOIN temp.doctor_cleaner_scan_message_stats stats
                ON stats.conversation_id = matches.conversation_id
            )
            SELECT
              filter_id,
              conversation_id,
              session_key,
              message_count,
              first_message_preview
            FROM ranked_examples
            WHERE example_rank <= 3
            ORDER BY filter_id, example_rank
            """
        ).fetchall()

        # ---- assemble per-filter stats ----
        # count_rows columns: 0 filter_id, 1 conversation_count,
        # 2 message_count, 3 total_conversation_count, 4 total_message_count.
        counts_by_id: dict[str, sqlite3.Row | tuple] = {str(row[0]): row for row in count_rows}
        # example_rows columns: 0 filter_id, 1 conversation_id,
        # 2 session_key, 3 message_count, 4 first_message_preview.
        examples_by_id: dict[str, list[DoctorCleanerExample]] = {}
        for row in example_rows:
            examples_by_id.setdefault(str(row[0]), []).append(
                DoctorCleanerExample(
                    conversation_id=int(row[1]),
                    session_key=(None if row[2] is None else str(row[2])),
                    message_count=int(row[3] or 0),
                    first_message_preview=normalize_first_message_preview(row[4]),
                )
            )

        filters: list[DoctorCleanerFilterStat] = []
        for definition in definitions:
            count_row = counts_by_id.get(definition.id)
            filters.append(
                DoctorCleanerFilterStat(
                    id=definition.id,
                    label=definition.label,
                    description=definition.description,
                    conversation_count=int(count_row[1] or 0) if count_row else 0,
                    message_count=int(count_row[2] or 0) if count_row else 0,
                    examples=examples_by_id.get(definition.id, []),
                )
            )

        # The distinct totals are constant across all count rows; the
        # first row carries them. No rows at all → all-zero scan.
        totals = count_rows[0] if count_rows else None
        return DoctorCleanerScan(
            filters=filters,
            total_distinct_conversations=int(totals[3] or 0) if totals else 0,
            total_distinct_messages=int(totals[4] or 0) if totals else 0,
        )
    finally:
        _drop_temp_cleaner_scan_tables(db)


# ---------------------------------------------------------------------------
# Apply-time temp tables
# ---------------------------------------------------------------------------


def _has_table(db: sqlite3.Connection, table_name: str) -> bool:
    """Return ``True`` if a user table or virtual table ``table_name`` exists.

    Ports the TS ``hasTable`` (``lcm-doctor-cleaners.ts:382-387``). Used to
    gate the three FTS cascade branches (``messages_fts`` / ``summaries_fts``
    / ``summaries_fts_cjk``) — those are FTS5 virtual tables that only
    exist when FTS5 is available at migration time, so the cascade MUST
    tolerate their absence.

    ``sqlite_master`` lists FTS5 virtual tables with ``type = 'table'``
    (the shadow tables get ``'table'`` too, but the virtual table itself
    is what we query by name), so the single ``type = 'table'`` filter
    matches both plain and virtual tables — same as the TS source.

    Args:
        db: Open connection.
        table_name: The table name to probe.

    Returns:
        ``True`` when a row with that name and ``type = 'table'`` exists.
    """
    row = db.execute(
        "SELECT 1 AS found FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def _drop_temp_cleaner_tables(db: sqlite3.Connection) -> None:
    """Drop the four apply-time temp tables (idempotent).

    Ports the TS ``dropTempCleanerTables`` (``lcm-doctor-cleaners.ts:389-394``).
    ``DROP TABLE IF EXISTS`` so it is a clean no-op when a table was never
    created. Called defensively before staging AND in the apply's
    ``finally`` block (the issue AC ``test_temp_tables_dropped_on_raise``
    asserts the ``finally`` path runs even when the cascade raises).

    The ``doctor_cleaner_first_messages`` table is only created for a
    ``needs_first_message`` cleaner selection, but it is unconditionally
    listed here — ``IF EXISTS`` makes the extra drop harmless.
    """
    db.execute("DROP TABLE IF EXISTS temp.doctor_cleaner_first_messages")
    db.execute("DROP TABLE IF EXISTS temp.doctor_cleaner_message_ids")
    db.execute("DROP TABLE IF EXISTS temp.doctor_cleaner_summary_ids")
    db.execute("DROP TABLE IF EXISTS temp.doctor_cleaner_conversation_ids")


def _stage_temp_cleaner_first_messages(db: sqlite3.Connection) -> None:
    """Stage the apply-time first-message temp table for ALL conversations.

    Ports the TS ``stageTempCleanerFirstMessages`` (``lcm-doctor-cleaners.ts:396-424``).
    Unlike the scan-time message-stats table (which is restricted to
    candidate conversations), the apply-time first-message table is
    computed across EVERY conversation — the matched-conversation query
    LEFT JOINs it, and SQLite's planner restricts the scan anyway. The TS
    source does the same (no candidate-conversation pre-filter on the
    apply-time staging).

    The earliest message per conversation is resolved with the same
    ``ROW_NUMBER() OVER (PARTITION BY conversation_id ORDER BY seq ASC,
    created_at ASC, message_id ASC)`` window as the scan path; the
    content is truncated to the 256-char prefix.

    Only called when a ``needs_first_message`` cleaner is selected.
    """
    db.execute(
        """
        CREATE TEMP TABLE doctor_cleaner_first_messages (
          conversation_id INTEGER PRIMARY KEY,
          first_message_preview TEXT
        )
        """
    )
    db.execute(
        f"""
        WITH ranked_messages AS (
          SELECT
            m.conversation_id,
            substr(m.content, 1, {FIRST_MESSAGE_PREVIEW_LIMIT}) AS content,
            ROW_NUMBER() OVER (
              PARTITION BY m.conversation_id
              ORDER BY m.seq ASC, m.created_at ASC, m.message_id ASC
            ) AS row_num
          FROM messages m
        )
        INSERT INTO temp.doctor_cleaner_first_messages (
          conversation_id,
          first_message_preview
        )
        SELECT
          conversation_id,
          MAX(CASE WHEN row_num = 1 THEN content END) AS first_message_preview
        FROM ranked_messages
        GROUP BY conversation_id
        """
    )


def _stage_cleaner_conversation_ids(
    db: sqlite3.Connection,
    definitions: Sequence[_CleanerDefinition],
) -> None:
    """Stage the four apply-time temp tables (conversation/summary/message ids).

    Ports the TS ``stageCleanerConversationIds`` (``lcm-doctor-cleaners.ts:426-473``).
    Builds, in order:

    1. ``doctor_cleaner_conversation_ids`` — the deduplicated set of
       conversation ids the selected cleaners match (full
       ``predicate_sql``, ``SELECT DISTINCT`` over the ``UNION ALL``).
    2. ``doctor_cleaner_summary_ids`` — every ``summaries.summary_id``
       owned by a matched conversation.
    3. ``doctor_cleaner_message_ids`` — every ``messages.message_id``
       owned by a matched conversation.

    The ``doctor_cleaner_first_messages`` table is staged first when a
    ``needs_first_message`` cleaner is selected (the matched-conversation
    query LEFT JOINs it).

    Args:
        db: Open connection inside a ``BEGIN IMMEDIATE`` transaction.
        definitions: The selected cleaner definitions. An empty sequence
            creates the (empty) temp tables and returns — the caller
            short-circuits before reaching here on an empty selection,
            but the guard is kept for parity with the TS source.
    """
    _drop_temp_cleaner_tables(db)
    db.execute(
        "CREATE TEMP TABLE doctor_cleaner_conversation_ids (conversation_id INTEGER PRIMARY KEY)"
    )
    db.execute("CREATE TEMP TABLE doctor_cleaner_summary_ids (summary_id TEXT PRIMARY KEY)")
    db.execute("CREATE TEMP TABLE doctor_cleaner_message_ids (message_id INTEGER PRIMARY KEY)")

    if not definitions:
        return

    needs_first_message = any(definition.needs_first_message for definition in definitions)
    if needs_first_message:
        _stage_temp_cleaner_first_messages(db)
    matched_conversations_sql = _build_matched_conversations_sql(
        definitions,
        include_filter_id=False,
        message_stats_table_name=(
            "temp.doctor_cleaner_first_messages" if needs_first_message else None
        ),
    )
    db.execute(
        "INSERT INTO temp.doctor_cleaner_conversation_ids (conversation_id)\n"
        "SELECT DISTINCT conversation_id\n"
        "FROM (\n"
        f"  {matched_conversations_sql}\n"
        ")"
    )

    db.execute(
        """
        INSERT INTO temp.doctor_cleaner_summary_ids (summary_id)
        SELECT s.summary_id
        FROM summaries s
        JOIN temp.doctor_cleaner_conversation_ids ids
          ON ids.conversation_id = s.conversation_id
        """
    )

    db.execute(
        """
        INSERT INTO temp.doctor_cleaner_message_ids (message_id)
        SELECT m.message_id
        FROM messages m
        JOIN temp.doctor_cleaner_conversation_ids ids
          ON ids.conversation_id = m.conversation_id
        """
    )


def _read_temp_cleaner_delete_counts(db: sqlite3.Connection) -> tuple[int, int]:
    """Read the staged conversation + message counts from the temp tables.

    Ports the TS ``readTempCleanerDeleteCounts`` (``lcm-doctor-cleaners.ts:475-490``).
    Read BEFORE the cascade DELETEs — the ``messages`` rows are removed by
    the ``ON DELETE CASCADE`` on ``messages.conversation_id`` when the
    final ``conversations`` DELETE fires, so the count must be taken from
    the staged ``doctor_cleaner_message_ids`` table while it still
    reflects the pre-delete state.

    Returns:
        A ``(conversation_count, message_count)`` tuple. Both ``0`` when
        no conversation matched.
    """
    row = db.execute(
        """
        SELECT
          COALESCE((SELECT COUNT(*) FROM temp.doctor_cleaner_conversation_ids), 0)
            AS conversation_count,
          COALESCE((SELECT COUNT(*) FROM temp.doctor_cleaner_message_ids), 0)
            AS message_count
        """
    ).fetchone()
    if row is None:
        return (0, 0)
    return (int(row[0] or 0), int(row[1] or 0))


def _delete_temp_cleaner_candidates(db: sqlite3.Connection) -> int:
    """Run the full cascade DELETE against the staged temp tables.

    Ports the TS ``deleteTempCleanerCandidates`` (``lcm-doctor-cleaners.ts:492-555``).
    Deletes, in this fixed order (each step keyed by a staged id set):

    1. ``summary_messages`` by ``summary_id`` IN the summary id set.
    2. ``summary_messages`` by ``message_id`` IN the message id set.
    3. ``summary_parents`` by ``summary_id`` IN the summary id set.
    4. ``summary_parents`` by ``parent_summary_id`` IN the summary id set
       (catches condensed→leaf edges where the *parent* is being deleted).
    5. ``context_items`` by ``message_id`` IN the message id set.
    6. ``context_items`` by ``summary_id`` IN the summary id set.
    7. ``context_items`` by ``conversation_id`` IN the conversation id set
       (catches any context row whose typed parent id was not caught
       above).
    8. ``messages_fts`` by ``rowid`` IN the message id set — best-effort,
       gated by :func:`_has_table`.
    9. ``summaries_fts`` by ``summary_id`` IN the summary id set —
       best-effort, gated by :func:`_has_table`.
    10. ``summaries_fts_cjk`` by ``summary_id`` IN the summary id set —
        best-effort, gated by :func:`_has_table`.
    11. ``conversations`` — the final DELETE. ``messages``, plus any
        remaining ``ON DELETE CASCADE`` dependents, cascade from here.

    The explicit steps 1-10 exist because ``summary_messages`` /
    ``summary_parents`` / ``context_items`` carry ``ON DELETE RESTRICT``
    on their message / summary foreign keys (see ``db/migration.py``):
    the cascade from ``conversations`` would be BLOCKED by those RESTRICT
    constraints unless the dependent rows are cleared first. The FTS
    virtual tables have no foreign keys at all — they are mirrors, so
    they must be cleared by hand.

    Args:
        db: Open connection inside a ``BEGIN IMMEDIATE`` transaction with
            the four temp tables already staged.

    Returns:
        The number of ``conversations`` rows deleted by the final
        DELETE (``cursor.rowcount``).
    """
    has_messages_fts = _has_table(db, "messages_fts")
    has_summaries_fts = _has_table(db, "summaries_fts")
    has_summaries_fts_cjk = _has_table(db, "summaries_fts_cjk")

    # ---- (1-2) summary_messages ----
    db.execute(
        "DELETE FROM summary_messages "
        "WHERE summary_id IN (SELECT summary_id FROM temp.doctor_cleaner_summary_ids)"
    )
    db.execute(
        "DELETE FROM summary_messages "
        "WHERE message_id IN (SELECT message_id FROM temp.doctor_cleaner_message_ids)"
    )

    # ---- (3-4) summary_parents ----
    db.execute(
        "DELETE FROM summary_parents "
        "WHERE summary_id IN (SELECT summary_id FROM temp.doctor_cleaner_summary_ids)"
    )
    db.execute(
        "DELETE FROM summary_parents "
        "WHERE parent_summary_id IN (SELECT summary_id FROM temp.doctor_cleaner_summary_ids)"
    )

    # ---- (5-7) context_items (all three ref types) ----
    db.execute(
        "DELETE FROM context_items "
        "WHERE message_id IN (SELECT message_id FROM temp.doctor_cleaner_message_ids)"
    )
    db.execute(
        "DELETE FROM context_items "
        "WHERE summary_id IN (SELECT summary_id FROM temp.doctor_cleaner_summary_ids)"
    )
    db.execute(
        "DELETE FROM context_items "
        "WHERE conversation_id IN "
        "(SELECT conversation_id FROM temp.doctor_cleaner_conversation_ids)"
    )

    # ---- (8-10) FTS mirrors — best-effort, gated by _has_table ----
    if has_messages_fts:
        db.execute(
            "DELETE FROM messages_fts "
            "WHERE rowid IN (SELECT message_id FROM temp.doctor_cleaner_message_ids)"
        )
    if has_summaries_fts:
        db.execute(
            "DELETE FROM summaries_fts "
            "WHERE summary_id IN (SELECT summary_id FROM temp.doctor_cleaner_summary_ids)"
        )
    if has_summaries_fts_cjk:
        db.execute(
            "DELETE FROM summaries_fts_cjk "
            "WHERE summary_id IN (SELECT summary_id FROM temp.doctor_cleaner_summary_ids)"
        )

    # ---- (11) conversations — final DELETE; messages cascade from here ----
    cursor = db.execute(
        "DELETE FROM conversations "
        "WHERE conversation_id IN "
        "(SELECT conversation_id FROM temp.doctor_cleaner_conversation_ids)"
    )
    return int(cursor.rowcount or 0)


# ---------------------------------------------------------------------------
# Public: apply unavailability check + apply
# ---------------------------------------------------------------------------


def get_doctor_cleaner_apply_unavailable_reason(
    database_path: Union[str, Path],
) -> Optional[str]:
    """Return why a cleaner apply is unavailable, or ``None`` when it is OK.

    Ports the TS ``getDoctorCleanerApplyUnavailableReason``
    (``lcm-doctor-cleaners.ts:557-561``). The cleaner apply is destructive,
    so it MUST be able to write a backup first — and a backup requires a
    file-backed source DB (``VACUUM INTO`` cannot copy from ``:memory:``).

    Args:
        database_path: The filesystem path the connection was opened
            against. An in-memory marker (``:memory:`` / ``file::memory:...``)
            makes the apply unavailable.

    Returns:
        ``None`` when the DB is file-backed (apply may proceed). The
        canonical "Cleaner apply requires a file-backed SQLite database
        so Lossless Claw can create a backup first." reason string when
        the DB is in-memory.
    """
    if get_file_backed_database_path(database_path) is not None:
        return None
    return (
        "Cleaner apply requires a file-backed SQLite database so "
        "Lossless Claw can create a backup first."
    )


def apply_doctor_cleaners(
    db: sqlite3.Connection,
    *,
    database_path: Union[str, Path],
    filter_ids: Optional[Sequence[DoctorCleanerId]] = None,
    vacuum: bool = False,
) -> DoctorCleanerApplyResult:
    """Apply the selected cleaners — bulk-delete matched conversations.

    Ports the TS ``applyDoctorCleaners`` (``lcm-doctor-cleaners.ts:567-641``).
    The destructive sibling of :func:`scan_doctor_cleaners`. Performs, in
    this strict order:

    1. Resolve the selected cleaners. An empty selection returns
       ``kind="unavailable"`` (``"No valid doctor cleaner filters were
       selected."``) — NO mutation, NO backup.
    2. :func:`get_doctor_cleaner_apply_unavailable_reason` — an in-memory
       DB returns ``kind="unavailable"`` with the file-backed-required
       reason. NO mutation.
    3. Build the backup destination path. If it cannot be built, return
       ``kind="unavailable"``.
    4. **Write the backup** via
       :func:`lossless_hermes.plugin.db_backup.write_lcm_database_backup`
       — BEFORE ``BEGIN IMMEDIATE`` so a crash mid-cascade is recoverable.
       A :class:`LcmDatabaseBackupError` here is converted to
       ``kind="unavailable"``.
    5. ``BEGIN IMMEDIATE`` → stage the four temp tables → read the
       pre-delete counts → run the cascade DELETE (only when at least one
       conversation matched) → ``COMMIT``. On any exception: ``ROLLBACK``
       and re-raise. The temp tables are dropped in a ``finally`` block
       regardless.
    6. After the transaction commits: when ``vacuum=True`` AND at least
       one conversation was deleted, run ``VACUUM`` followed by ``PRAGMA
       wal_checkpoint(TRUNCATE)``. A no-op apply skips this.

    Args:
        db: Open connection. Expected to be in autocommit /
            manual-transaction mode (``isolation_level=None``); this
            function issues ``BEGIN IMMEDIATE`` / ``COMMIT`` / ``ROLLBACK``
            explicitly.
        database_path: The filesystem path the connection was opened
            against. Used both to reject in-memory DBs and to anchor the
            backup file next to the source DB. MUST match the path the
            ``db`` connection was opened with.
        filter_ids: Optional subset of cleaner ids. ``None`` / empty
            selects all three cleaners.
        vacuum: When ``True``, run ``VACUUM`` + ``wal_checkpoint`` after a
            non-empty apply.

    Returns:
        A :class:`DoctorCleanerApplyResult`. ``kind="applied"`` carries
        the deleted-conversation / deleted-message counts, the
        ``vacuumed`` flag, the applied filter ids, and the backup path.
        ``kind="unavailable"`` carries only a ``reason``.

    Raises:
        Exception: Any error raised by the cascade DELETE inside the
            transaction is re-raised AFTER a ``ROLLBACK`` (the temp
            tables are still dropped in the ``finally`` block). The
            backup file written in step 4 is left in place — it is the
            recovery artifact.
    """
    definitions = _get_cleaner_definitions(filter_ids)
    if not definitions:
        return DoctorCleanerApplyResult(
            kind="unavailable",
            reason="No valid doctor cleaner filters were selected.",
        )

    unavailable_reason = get_doctor_cleaner_apply_unavailable_reason(database_path)
    if unavailable_reason is not None:
        return DoctorCleanerApplyResult(kind="unavailable", reason=unavailable_reason)

    # Build the backup destination path. build_lcm_database_backup_path
    # raises LcmDatabaseBackupError for an in-memory DB — but step 2
    # already rejected those, so this path is reached only for a
    # file-backed DB. The except is defensive (parity with the TS
    # ``if (!backupPath)`` re-check).
    try:
        backup_path = build_lcm_database_backup_path(database_path, label="doctor-cleaners")
    except LcmDatabaseBackupError as exc:
        return DoctorCleanerApplyResult(
            kind="unavailable",
            reason=(
                get_doctor_cleaner_apply_unavailable_reason(database_path)
                or f"Cleaner apply could not determine a backup path: {exc}"
            ),
        )

    # ---- (4) write the backup BEFORE the destructive transaction ----
    # This MUST precede BEGIN IMMEDIATE — a crash mid-cascade then leaves
    # a clean, restorable snapshot. The issue AC
    # ``test_backup_before_begin_immediate`` asserts the ordering via the
    # backup file's mtime. A backup failure aborts the apply entirely.
    try:
        written_backup_path = write_lcm_database_backup(
            db,
            label="doctor-cleaners",
            db_path=database_path,
        )
    except LcmDatabaseBackupError as exc:
        return DoctorCleanerApplyResult(
            kind="unavailable",
            reason=f"Cleaner apply could not write a backup: {exc}",
        )
    # build_lcm_database_backup_path and write_lcm_database_backup both
    # mint their own random suffix, so the planned path and the written
    # path differ in the suffix only. The WRITTEN path is the artifact
    # that actually exists on disk — report that one.
    backup_path = written_backup_path

    deleted_conversations = 0
    deleted_messages = 0
    vacuumed = False
    transaction_active = False

    try:
        db.execute("BEGIN IMMEDIATE")
        transaction_active = True
        _stage_cleaner_conversation_ids(db, definitions)
        conversation_count, deleted_messages = _read_temp_cleaner_delete_counts(db)
        if conversation_count > 0:
            deleted_conversations = _delete_temp_cleaner_candidates(db)
        db.execute("COMMIT")
        transaction_active = False
    except Exception:
        if transaction_active:
            db.execute("ROLLBACK")
        raise
    finally:
        _drop_temp_cleaner_tables(db)

    # ---- (6) VACUUM only when requested AND something was deleted ----
    if vacuum and deleted_conversations > 0:
        db.execute("VACUUM")
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        vacuumed = True

    return DoctorCleanerApplyResult(
        kind="applied",
        filter_ids=[definition.id for definition in definitions],
        deleted_conversations=deleted_conversations,
        deleted_messages=deleted_messages,
        vacuumed=vacuumed,
        backup_path=str(backup_path),
    )
