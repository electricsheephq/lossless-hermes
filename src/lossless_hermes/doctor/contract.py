"""Canonical doctor contract surface (Pydantic models + constants).

Ports the type + constant exports of
``lossless-claw/src/plugin/lcm-doctor-shared.ts`` (LCM commit ``1f07fbd``
on branch ``pr-613``) into a Python module consumed by both
:mod:`lossless_hermes.doctor.apply` (issue 08-07) and
:mod:`lossless_hermes.doctor.cleaners` (issue 08-08).

Per ``docs/porting-guides/doctor-ops.md`` §"Doctor contract API
(canonical)" line 31:

    "No file named ``doctor-contract-api.d.ts`` exists in the
    lossless-claw tree on ``pr-613``. The 'formal contract' is the
    union of exported types and functions across the three plugin
    doctor modules."

The Python port consolidates the canonical types here so the apply and
cleaners modules cannot drift apart on the contract shape.

### Wire-protocol invariants (DO NOT CHANGE)

The six marker constants below MUST remain byte-equal to the TS
counterparts in ``lcm-doctor-shared.ts:9-16``. The doctor cleans up
summaries written by legacy + v4.1 LCM hosts; if a marker string is
edited here, those hosts' rows become invisible to detection and silently
ship as broken summaries forever.

### Snake-case mapping (TS → Python)

Per the issue spec (AC: "All four pydantic models match the TS shapes
1:1, snake_case fields"), TS camelCase field names map to Python
snake_case:

* TS ``conversationId`` → Python ``conversation_id``
* TS ``summaryId`` → Python ``summary_id``
* TS ``markerKind`` → Python ``marker_kind``
* TS ``tokenCount`` → Python ``token_count``
* TS ``createdAt`` → Python ``created_at``
* TS ``childCount`` → Python ``child_count``
* TS ``byConversation`` → Python ``by_conversation``

See:

* ``epics/08-cli-ops/08-06-doctor-shared.md`` — this issue spec.
* ``docs/porting-guides/doctor-ops.md`` §"Doctor contract API
  (canonical)" lines 30-100 — the verbatim contract spec.
* ``docs/adr/029-wave-fix-provenance.md`` — Wave-N comment protocol.
* ``lossless-claw/src/plugin/lcm-doctor-shared.ts:1-52`` — TS source
  pinned at commit ``1f07fbd`` on branch ``pr-613``.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Literal alias for the three cleaner ids — ports the TS ``DoctorCleanerId``
# union (``lcm-doctor-cleaners.ts:5-8``). Defined at module level (not nested)
# so both :mod:`lossless_hermes.doctor.cleaners` and any future CLI-render
# layer can annotate against it.
DoctorCleanerId = Literal["archived_subagents", "cron_sessions", "null_subagent_context"]
"""The three predefined doctor cleaner identifiers.

Ports TS ``DoctorCleanerId`` (``lcm-doctor-cleaners.ts:5-8``). The order
``archived_subagents`` → ``cron_sessions`` → ``null_subagent_context``
matches the ``CLEANER_DEFINITIONS`` array in the TS source and is the
order :func:`lossless_hermes.doctor.cleaners.get_doctor_cleaner_filters`
returns."""


__all__ = [
    "FALLBACK_SUMMARY_MARKER",
    "FALLBACK_SUMMARY_MARKER_V41_FULL",
    "FALLBACK_SUMMARY_MARKER_V41_TRUNC",
    "FALLBACK_SUMMARY_WINDOW",
    "TRUNCATED_SUMMARY_PREFIX",
    "TRUNCATED_SUMMARY_WINDOW",
    "DoctorCleanerApplyResult",
    "DoctorCleanerExample",
    "DoctorCleanerFilter",
    "DoctorCleanerFilterStat",
    "DoctorCleanerId",
    "DoctorCleanerScan",
    "DoctorConversationCounts",
    "DoctorMarkerKind",
    "DoctorSummaryCandidate",
    "DoctorSummaryStats",
    "DoctorTargetRecord",
]


# ---------------------------------------------------------------------------
# Marker constants (verbatim wire-protocol strings)
# ---------------------------------------------------------------------------

# LCM Wave-4 (2026-02-14): Auditor #18 P0 fix — the v4.1 fallback marker
# text was tightened. The legacy ``FALLBACK_SUMMARY_MARKER`` was a trailing
# suffix on TRUNCATED content only; under-cap fallback content shipped
# silently UNMARKED. The new v4.1 markers below are explicit prefixes that
# land on every fallback. Doctor still detects the legacy text on old DBs
# to support clean upgrade migration, plus the new prefix forms.
# Original: lossless-claw/src/plugin/lcm-doctor-shared.ts:3-9.

FALLBACK_SUMMARY_MARKER = "[LCM fallback summary; truncated for context management]"
"""Legacy (pre-Wave-4) trailing-suffix marker.

Detected via :data:`FALLBACK_SUMMARY_WINDOW`-char suffix scan. Practically
unreachable as a start-of-string prefix on real data (pre-Wave-4 emitted
it only as a trailing suffix on truncated content); defended for
defense-in-depth (a future code path that emits it as a prefix is
classified ``"old"`` so the issue is visible).
"""

FALLBACK_SUMMARY_MARKER_V41_TRUNC = (
    "[LCM fallback summary — model unavailable; raw source truncated for context management]"
)
"""v4.1 PREFIX marker: model unavailable AND raw source truncated."""

FALLBACK_SUMMARY_MARKER_V41_FULL = (
    "[LCM fallback summary — model unavailable; raw source preserved verbatim below]"
)
"""v4.1 PREFIX marker: model unavailable, raw source preserved verbatim."""

TRUNCATED_SUMMARY_PREFIX = "[Truncated from "
"""Trailing-suffix marker for "summary was emitted but content was
truncated for size" — distinct from the fallback markers, which mean
"summarizer fell back to raw content"."""

TRUNCATED_SUMMARY_WINDOW = 40
""":data:`TRUNCATED_SUMMARY_PREFIX` is only counted if it appears within
the last :data:`TRUNCATED_SUMMARY_WINDOW` chars of the content (a tight
suffix scan; the prefix is emitted at the very end of the summary)."""

FALLBACK_SUMMARY_WINDOW = 80
""":data:`FALLBACK_SUMMARY_MARKER` is only counted as legacy trailing
suffix if it appears within the last :data:`FALLBACK_SUMMARY_WINDOW`
chars of the content. Wider than the truncated window because the legacy
emitter sometimes appended additional context after the marker."""


# ---------------------------------------------------------------------------
# Pydantic models — TS interface parity, snake_case
# ---------------------------------------------------------------------------


class DoctorMarkerKind(str, Enum):
    """Detected marker classification for a broken summary.

    Ports TS ``DoctorMarkerKind`` (``lcm-doctor-shared.ts:18``):

    * ``OLD`` — content starts with legacy :data:`FALLBACK_SUMMARY_MARKER`
      as a PREFIX (defense-in-depth; practically unreachable on real
      data — pre-Wave-4 emitted the legacy marker only as a trailing
      suffix, which classifies as :attr:`FALLBACK`).
    * ``NEW`` — :data:`TRUNCATED_SUMMARY_PREFIX` appears within the last
      :data:`TRUNCATED_SUMMARY_WINDOW` chars (trailing-suffix marker;
      "summary was emitted but content was truncated for size").
    * ``FALLBACK`` — content starts with one of the v4.1 prefix markers
      (:data:`FALLBACK_SUMMARY_MARKER_V41_TRUNC` /
      :data:`FALLBACK_SUMMARY_MARKER_V41_FULL`), OR legacy
      :data:`FALLBACK_SUMMARY_MARKER` appears as a trailing suffix within
      the last :data:`FALLBACK_SUMMARY_WINDOW` chars (pre-Wave-4 data).
      Both classifications collapse to "fallback" because the repair
      semantics are identical (re-summarize the source).
    """

    OLD = "old"
    NEW = "new"
    FALLBACK = "fallback"


class DoctorSummaryCandidate(BaseModel):
    """A single summary marked by the doctor scan.

    Ports TS ``DoctorSummaryCandidate`` (``lcm-doctor-shared.ts:20-24``).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    conversation_id: int
    """Conversation owning the summary."""

    summary_id: str
    """Stable summary identifier (``summaries.summary_id``)."""

    marker_kind: DoctorMarkerKind
    """The marker classification returned by
    :func:`lossless_hermes.doctor.shared.detect_doctor_marker`."""


class DoctorConversationCounts(BaseModel):
    """Per-conversation marker counts aggregated by doctor.

    Ports TS ``DoctorConversationCounts`` (``lcm-doctor-shared.ts:26-31``).
    """

    model_config = ConfigDict(extra="forbid")
    # Not frozen: :func:`lossless_hermes.doctor.shared.get_doctor_summary_stats`
    # mutates this in-place as it iterates targets (mirrors the TS
    # ``current.total += 1`` pattern at ``lcm-doctor-shared.ts:237``).

    total: int = Field(default=0, ge=0)
    """Total candidates for this conversation (sum of the next three)."""

    old: int = Field(default=0, ge=0)
    """Candidates classified :attr:`DoctorMarkerKind.OLD`."""

    truncated: int = Field(default=0, ge=0)
    """Candidates classified :attr:`DoctorMarkerKind.NEW` (truncated-suffix)."""

    fallback: int = Field(default=0, ge=0)
    """Candidates classified :attr:`DoctorMarkerKind.FALLBACK`."""


class DoctorSummaryStats(BaseModel):
    """Aggregate stats over all marker-bearing summaries.

    Ports TS ``DoctorSummaryStats`` (``lcm-doctor-shared.ts:33-40``). The
    TS ``byConversation: Map<number, DoctorConversationCounts>`` becomes
    a Python ``dict[int, DoctorConversationCounts]`` (Python's dicts
    preserve insertion order, matching the iteration semantics callers
    expect from the TS ``Map``).
    """

    model_config = ConfigDict(extra="forbid")

    candidates: list[DoctorSummaryCandidate] = Field(default_factory=list)
    """Per-summary classification rows, in iteration order from
    :func:`lossless_hermes.doctor.shared.load_doctor_targets` (deterministic:
    ``conversation_id ASC, depth ASC, created_at ASC, summary_id ASC`` when
    no ``conversation_id`` filter; ``depth ASC, created_at ASC, summary_id
    ASC`` when filtered to one conversation)."""

    total: int = Field(default=0, ge=0)
    """Total candidate count (equals ``len(candidates)``)."""

    old: int = Field(default=0, ge=0)
    """DB-wide :attr:`DoctorMarkerKind.OLD` count."""

    truncated: int = Field(default=0, ge=0)
    """DB-wide :attr:`DoctorMarkerKind.NEW` (truncated-suffix) count."""

    fallback: int = Field(default=0, ge=0)
    """DB-wide :attr:`DoctorMarkerKind.FALLBACK` count."""

    by_conversation: dict[int, DoctorConversationCounts] = Field(default_factory=dict)
    """Per-conversation breakdown. Only conversations with at least one
    candidate are present (matches the TS ``Map`` population semantics)."""


class DoctorTargetRecord(BaseModel):
    """A single row from :func:`lossless_hermes.doctor.shared.load_doctor_targets`.

    Ports TS ``DoctorTargetRecord`` (``lcm-doctor-shared.ts:42-52``). The
    TS ``kind: string`` is constrained to ``"leaf" | "condensed"`` via
    the ``summaries.kind`` CHECK constraint in the schema, so the Python
    port narrows the type with a :class:`typing.Literal` for static
    typing benefit. Anything else would be a schema violation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    conversation_id: int
    """Conversation owning the summary (``summaries.conversation_id``)."""

    summary_id: str
    """Stable summary identifier (``summaries.summary_id``)."""

    kind: Literal["leaf", "condensed"]
    """``summaries.kind`` — narrowed to the two values the schema
    permits via the ``CHECK (kind IN ('leaf', 'condensed'))`` constraint
    (see ``src/lossless_hermes/db/migration.py:208``). The TS type is the
    permissive ``string``; we tighten to a :class:`typing.Literal` so
    static type checkers can verify exhaustive matches in callers."""

    depth: int = Field(ge=0)
    """``summaries.depth`` (``COALESCE(s.depth, 0)``)."""

    token_count: int = Field(ge=0)
    """``summaries.token_count`` (``COALESCE(s.token_count, 0)``)."""

    content: str
    """``summaries.content`` (``COALESCE(s.content, '')`` from the SELECT,
    so always a string — never NULL)."""

    created_at: str
    """``summaries.created_at`` ISO-8601 string
    (``COALESCE(s.created_at, '')`` from the SELECT)."""

    child_count: int = Field(ge=0)
    """Count of rows in ``summary_parents`` where
    ``summary_parents.summary_id = summaries.summary_id`` — the number
    of direct children this summary has. ``0`` for leaves and for
    childless condensed summaries (which are rare; usually only present
    during compaction)."""

    marker_kind: DoctorMarkerKind
    """The marker classification from
    :func:`lossless_hermes.doctor.shared.detect_doctor_marker` applied to
    :attr:`content`. Re-classified per-row in Python — the SQL INSTR
    pre-filter is permissive (matches the four marker strings anywhere
    in content), so a row may match INSTR but not actually have a marker
    in a load-bearing position. Re-classification yields the precise kind
    or filters the row out entirely (returns :data:`None`)."""


# ---------------------------------------------------------------------------
# Cleaner contract surface — ports ``lcm-doctor-cleaners.ts`` exported types
# ---------------------------------------------------------------------------
#
# These models back the DB-wide row-deletion path of ``/lcm doctor clean``
# and ``/lcm doctor clean apply``. Consumed by
# :mod:`lossless_hermes.doctor.cleaners` (issue 08-08). See
# ``epics/08-cli-ops/08-08-doctor-cleaners.md`` for the full spec.


class DoctorCleanerFilter(BaseModel):
    """Static metadata for one cleaner (id + label + description).

    Ports the ``Pick<DoctorCleanerFilterStat, "id" | "label" |
    "description">`` shape returned by TS ``getDoctorCleanerFilters``
    (``lcm-doctor-cleaners.ts:251-257``). The scan-time counts +
    examples live on the richer :class:`DoctorCleanerFilterStat`; this
    bare model is the "what cleaners exist" listing with no DB read.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: DoctorCleanerId
    """The cleaner identifier (one of the three :data:`DoctorCleanerId`
    literals)."""

    label: str
    """Human-readable cleaner name (verbatim from the TS
    ``CleanerDefinition.label``)."""

    description: str
    """One-line description of what the cleaner detects (verbatim from
    the TS ``CleanerDefinition.description``)."""


class DoctorCleanerExample(BaseModel):
    """A single example conversation surfaced by a cleaner scan.

    Ports TS ``DoctorCleanerExample`` (``lcm-doctor-cleaners.ts:10-15``).
    The scan returns up to three of these per cleaner, sorted
    ``message_count DESC, created_at DESC, conversation_id DESC``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    conversation_id: int
    """``conversations.conversation_id`` of the example conversation."""

    session_key: str | None
    """``conversations.session_key`` — ``None`` for the
    ``null_subagent_context`` cleaner (which matches NULL-key rows by
    definition)."""

    message_count: int = Field(ge=0)
    """Number of ``messages`` rows in the example conversation."""

    first_message_preview: str | None
    """Normalized preview of the conversation's earliest message
    (whitespace collapsed, trimmed to 120 chars with a ``"..."``
    ellipsis if longer). ``None`` when the conversation has no messages
    or the earliest message is whitespace-only. See
    :func:`lossless_hermes.doctor.shared.normalize_first_message_preview`."""


class DoctorCleanerFilterStat(BaseModel):
    """Per-cleaner scan result: metadata + counts + top-3 examples.

    Ports TS ``DoctorCleanerFilterStat`` (``lcm-doctor-cleaners.ts:17-24``).
    Returned (one per selected cleaner) inside :class:`DoctorCleanerScan`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: DoctorCleanerId
    """The cleaner identifier."""

    label: str
    """Human-readable cleaner name."""

    description: str
    """One-line description of what the cleaner detects."""

    conversation_count: int = Field(ge=0)
    """How many conversations this cleaner would delete."""

    message_count: int = Field(ge=0)
    """How many ``messages`` rows belong to those conversations (the
    sum of per-conversation message counts)."""

    examples: list[DoctorCleanerExample] = Field(default_factory=list)
    """Up to three example conversations, sorted ``message_count DESC,
    created_at DESC, conversation_id DESC`` (deterministic)."""


class DoctorCleanerScan(BaseModel):
    """Aggregate result of a doctor-cleaner scan (dry run).

    Ports TS ``DoctorCleanerScan`` (``lcm-doctor-cleaners.ts:26-30``).
    The ``total_distinct_*`` fields deduplicate conversations that match
    more than one cleaner (so the totals are never an over-count).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    filters: list[DoctorCleanerFilterStat] = Field(default_factory=list)
    """Per-cleaner stats, in the order the cleaners were requested (or
    the canonical three-cleaner order when no filter is passed)."""

    total_distinct_conversations: int = Field(default=0, ge=0)
    """Count of DISTINCT conversations matched across all selected
    cleaners. Lower than ``sum(f.conversation_count)`` when a
    conversation matches two cleaners."""

    total_distinct_messages: int = Field(default=0, ge=0)
    """Count of ``messages`` rows belonging to the distinct matched
    conversations."""


class DoctorCleanerApplyResult(BaseModel):
    """Result of :func:`lossless_hermes.doctor.cleaners.apply_doctor_cleaners`.

    Ports the TS ``DoctorCleanerApplyResult`` discriminated union
    (``lcm-doctor-cleaners.ts:32-44``). TS models the two arms as a true
    union; the Python port collapses them into one model discriminated
    by :attr:`kind` (matching the spec's ``class
    DoctorCleanerApplyResult(BaseModel)`` shape with a
    ``Literal["applied", "unavailable"]`` discriminator).

    * ``kind="applied"`` — the cleaners ran. :attr:`filter_ids`,
      :attr:`deleted_conversations`, :attr:`deleted_messages`,
      :attr:`vacuumed`, :attr:`backup_path` are meaningful;
      :attr:`reason` is :data:`None`.
    * ``kind="unavailable"`` — the cleaners did NOT run (in-memory DB,
      no valid filters, or backup path could not be built).
      :attr:`reason` carries the human-readable explanation; the count
      fields stay at their ``0`` / ``False`` / ``""`` defaults.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["applied", "unavailable"]
    """Discriminator. ``"applied"`` when the cleaner cascade ran;
    ``"unavailable"`` when it was refused before any mutation."""

    filter_ids: list[DoctorCleanerId] = Field(default_factory=list)
    """The cleaner ids that were applied, in canonical order. Empty on
    the ``"unavailable"`` arm."""

    deleted_conversations: int = Field(default=0, ge=0)
    """How many ``conversations`` rows were deleted (``0`` on a no-op
    apply or the ``"unavailable"`` arm)."""

    deleted_messages: int = Field(default=0, ge=0)
    """How many ``messages`` rows belonged to the deleted conversations
    (counted from the staged temp table BEFORE the cascade DELETEs; the
    ``messages`` rows themselves are removed by the ``ON DELETE
    CASCADE`` on ``messages.conversation_id``)."""

    vacuumed: bool = False
    """``True`` only when ``vacuum=True`` was requested AND
    :attr:`deleted_conversations` ``> 0`` (a no-op apply skips VACUUM so
    it stays cheap)."""

    backup_path: str = ""
    """Absolute path of the backup file written BEFORE the destructive
    transaction. Empty string on the ``"unavailable"`` arm."""

    reason: str | None = None
    """Set ONLY on the ``"unavailable"`` arm — the human-readable reason
    the cleaners were refused. :data:`None` on the ``"applied"`` arm."""
