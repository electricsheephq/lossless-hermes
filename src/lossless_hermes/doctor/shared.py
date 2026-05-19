"""Doctor shared infrastructure — marker detection + target loading + stats.

Ports the three exported functions of
``lossless-claw/src/plugin/lcm-doctor-shared.ts`` (LCM commit ``1f07fbd``
on branch ``pr-613``):

* :func:`detect_doctor_marker` — classify a single summary's ``content``
  string into a :class:`DoctorMarkerKind` (or :data:`None`).
* :func:`load_doctor_targets` — SELECT marker-bearing summaries from the
  ``summaries`` table, with a 4-marker INSTR pre-filter for performance,
  then re-classify per row.
* :func:`get_doctor_summary_stats` — aggregate :func:`load_doctor_targets`
  output into the :class:`DoctorSummaryStats` shape consumed by the
  ``/lcm doctor`` scan command.

This module is consumed by both issue 08-07 (``doctor/apply.py``) and
issue 08-08 (``doctor/cleaners.py``). It is the load-bearing
detection-and-loading layer that the higher-level surfaces share.

### Determinism contract

Both query branches order results deterministically so the doctor scan +
apply pass produce identical row order across runs:

* **No conversation filter:** ``conversation_id ASC, depth ASC,
  created_at ASC, summary_id ASC`` (4-key sort; ``conversation_id``
  first because the result spans the DB).
* **Filtered to one conversation:** ``depth ASC, created_at ASC,
  summary_id ASC`` (3-key sort; the ``conversation_id`` is constant by
  the WHERE clause).

The ``summary_id`` tiebreaker is the final key on both branches —
``summary_id`` is the primary key of ``summaries`` so it is always
unique within the result set.

### Pre-filter INSTR clause

The SQL WHERE clause uses four INSTR predicates (one per marker string)
OR'd together. This is a pre-filter — fast, but loose: it matches any
row whose content contains one of the four strings ANYWHERE. The
authoritative classification re-runs :func:`detect_doctor_marker` per
row in Python, which applies the position constraints (start-of-string
for v4.1 prefixes; last-40-char window for ``TRUNCATED_SUMMARY_PREFIX``;
last-80-char window for the legacy trailing-suffix marker).

Rows that match INSTR but fail re-classification (the marker text
appears mid-content, not at a load-bearing position) are silently
dropped — they're false positives from the perspective of the doctor.

See:

* ``epics/08-cli-ops/08-06-doctor-shared.md`` — this issue.
* ``docs/porting-guides/doctor-ops.md`` §"Doctor marker detection"
  lines 193-201 — the canonical detection spec.
* ``docs/adr/029-wave-fix-provenance.md`` — Wave-N comment protocol.
* ``lossless-claw/src/plugin/lcm-doctor-shared.ts:1-270`` — TS source
  pinned at commit ``1f07fbd`` on branch ``pr-613``.
"""

from __future__ import annotations

import sqlite3
from typing import Literal, Optional, cast

from lossless_hermes.doctor.contract import (
    FALLBACK_SUMMARY_MARKER,
    FALLBACK_SUMMARY_MARKER_V41_FULL,
    FALLBACK_SUMMARY_MARKER_V41_TRUNC,
    FALLBACK_SUMMARY_WINDOW,
    TRUNCATED_SUMMARY_PREFIX,
    TRUNCATED_SUMMARY_WINDOW,
    DoctorConversationCounts,
    DoctorMarkerKind,
    DoctorSummaryCandidate,
    DoctorSummaryStats,
    DoctorTargetRecord,
)

__all__ = [
    "FIRST_MESSAGE_PREVIEW_LIMIT",
    "FIRST_MESSAGE_PREVIEW_TRIM",
    "detect_doctor_marker",
    "get_doctor_summary_stats",
    "load_doctor_targets",
    "normalize_first_message_preview",
]


# ---------------------------------------------------------------------------
# First-message preview normalization (shared by cleaners + reconcile-list)
# ---------------------------------------------------------------------------

FIRST_MESSAGE_PREVIEW_LIMIT = 256
"""Raw-content prefix length read from ``messages.content`` BEFORE
normalization. Ports the TS ``SCAN_FIRST_MESSAGE_PREVIEW_LIMIT`` constant
(``lcm-doctor-cleaners.ts:69``). The cleaner SQL applies ``substr(content,
1, 256)`` at query time; :func:`normalize_first_message_preview` then
collapses whitespace and trims to :data:`FIRST_MESSAGE_PREVIEW_TRIM`. The
256-char SQL prefix bounds how much content the window-function query
materializes per conversation."""

FIRST_MESSAGE_PREVIEW_TRIM = 120
"""Final display length of a normalized preview. Ports the ``120`` literal
in the TS ``truncatePreview`` helper (``lcm-doctor-cleaners.ts:118``).
Previews longer than this are cut to ``117`` chars + a ``"..."`` ellipsis
(``117 + 3 == 120`` — the ellipsis is inside the budget, not appended past
it)."""

_PREVIEW_ELLIPSIS = "..."
"""Three-character ellipsis appended when a normalized preview exceeds
:data:`FIRST_MESSAGE_PREVIEW_TRIM`. Spelled with literal dots (not the
U+2026 single-glyph ellipsis) to byte-match the TS source."""


def normalize_first_message_preview(value: str | None) -> Optional[str]:
    """Normalize a raw ``messages.content`` prefix into a display preview.

    Ports the TS ``truncatePreview`` helper (``lcm-doctor-cleaners.ts:110-119``).
    This is the single shared normalizer the issue 08-08 spec mandates —
    consumed by :func:`lossless_hermes.doctor.cleaners.scan_doctor_cleaners`
    (the ``null_subagent_context`` example previews) and intended for the
    08-05 reconcile-candidate listing (one helper, multiple consumers, per
    ``epics/08-cli-ops/08-08-doctor-cleaners.md`` AC "First-message preview
    normalization is shared with 08-05's reconcile-list").

    Algorithm (byte-for-byte port of the TS):

    1. ``None`` / empty input → ``None``.
    2. Collapse every run of Unicode whitespace to a single ASCII space
       (TS ``value.replace(/\\s+/g, " ")``), then strip leading/trailing
       whitespace (TS ``.trim()``).
    3. If the collapsed string is now empty → ``None`` (the content was
       all whitespace).
    4. If the collapsed string is at most :data:`FIRST_MESSAGE_PREVIEW_TRIM`
       characters → return it unchanged.
    5. Otherwise return the first ``FIRST_MESSAGE_PREVIEW_TRIM - 3`` (=117)
       characters plus a literal ``"..."`` — total length exactly
       :data:`FIRST_MESSAGE_PREVIEW_TRIM`.

    ### Whitespace-class parity note

    The TS regex ``\\s`` matches the JavaScript whitespace class. Python's
    :py:meth:`str.split` (no-arg) splits on the Python whitespace class.
    The two classes are not perfectly identical at the margins (e.g. the
    zero-width characters), but both cover the common cases — ASCII space,
    tab, newline, carriage return, form feed, vertical tab — that real
    message content carries. ``" ".join(value.split())`` is the canonical
    Python whitespace-collapse-and-trim idiom and is what the port uses.

    Args:
        value: A raw content string (typically already truncated to the
            256-char SQL prefix). May be :data:`None`.

    Returns:
        The normalized, length-bounded preview string, or :data:`None`
        when the input is empty / whitespace-only.
    """
    if not value:
        return None
    # Collapse all whitespace runs to a single space AND trim — the
    # no-arg ``str.split`` discards leading/trailing whitespace and
    # splits on any whitespace run, so ``" ".join(...)`` reconstructs
    # the TS ``replace(/\s+/g, " ").trim()`` result exactly.
    normalized = " ".join(value.split())
    if not normalized:
        return None
    if len(normalized) <= FIRST_MESSAGE_PREVIEW_TRIM:
        return normalized
    return normalized[: FIRST_MESSAGE_PREVIEW_TRIM - len(_PREVIEW_ELLIPSIS)] + _PREVIEW_ELLIPSIS


# ---------------------------------------------------------------------------
# Marker detection (pure function)
# ---------------------------------------------------------------------------


def detect_doctor_marker(content: str) -> Optional[DoctorMarkerKind]:
    """Classify a summary ``content`` string by its doctor marker.

    Ports TS ``detectDoctorMarker`` (``lcm-doctor-shared.ts:89-115``).
    Returns:

    * :attr:`DoctorMarkerKind.FALLBACK` if ``content`` starts with
      :data:`FALLBACK_SUMMARY_MARKER_V41_TRUNC` or
      :data:`FALLBACK_SUMMARY_MARKER_V41_FULL` (v4.1 prefix form), OR
      if legacy :data:`FALLBACK_SUMMARY_MARKER` appears as a trailing
      suffix within the last :data:`FALLBACK_SUMMARY_WINDOW` chars
      (pre-Wave-4 data path).
    * :attr:`DoctorMarkerKind.OLD` if ``content`` starts with the
      legacy :data:`FALLBACK_SUMMARY_MARKER` (defense-in-depth;
      practically unreachable on real data — pre-Wave-4 emitted the
      legacy marker only as a trailing suffix, classified
      :attr:`FALLBACK` above).
    * :attr:`DoctorMarkerKind.NEW` if :data:`TRUNCATED_SUMMARY_PREFIX`
      appears within the last :data:`TRUNCATED_SUMMARY_WINDOW` chars
      (trailing-suffix marker meaning "summary was emitted but content
      was truncated for size").
    * :data:`None` otherwise (clean content).

    ### Wave-5 P3 clarification (preserved from TS)

    The "old" branch was historically dead code for legitimate data; the
    v4.1 prefix markers MAY start-of-string but their check fires
    FIRST (the v4.1 markers are checked before the legacy prefix). The
    ordering is for clarity, not for correctness — the v4.1 marker
    strings differ from the legacy marker at byte position 36 (``;
    truncated`` vs `` — model``) so there is no actual collision.

    Args:
        content: Summary content. May be empty (returns :data:`None`).

    Returns:
        :class:`DoctorMarkerKind` if a marker is detected at a
        load-bearing position; :data:`None` if the content is clean OR
        if the marker text appears only mid-content (a false positive
        from the SQL INSTR pre-filter).
    """
    # v4.1 fallback markers: always at start (prefix form). Check FIRST
    # because they're the dominant case on post-Wave-4 DBs and the
    # legacy marker check below uses ``startswith`` on the same prefix
    # of bytes — these would also match it. Mirrors TS ordering.
    if content.startswith(FALLBACK_SUMMARY_MARKER_V41_TRUNC) or content.startswith(
        FALLBACK_SUMMARY_MARKER_V41_FULL
    ):
        return DoctorMarkerKind.FALLBACK

    # Legacy marker as a PREFIX — defense-in-depth. Real data never
    # has this shape; if seen, doctor classifies "old" so the operator
    # can investigate how a legacy-shaped row ended up in a new-format
    # DB.
    if content.startswith(FALLBACK_SUMMARY_MARKER):
        return DoctorMarkerKind.OLD

    # TRUNCATED_SUMMARY_PREFIX as trailing suffix (last 40 chars).
    # ``str.find`` returns -1 on no-match (matches the TS ``indexOf``).
    truncated_index = content.find(TRUNCATED_SUMMARY_PREFIX)
    if truncated_index >= 0 and len(content) - truncated_index < TRUNCATED_SUMMARY_WINDOW:
        return DoctorMarkerKind.NEW

    # Legacy FALLBACK_SUMMARY_MARKER as trailing suffix (last 80 chars).
    # Pre-Wave-4 emitter appended the legacy marker at the end of
    # truncated content; the wider 80-char window absorbs the optional
    # trailing context the emitter sometimes added.
    fallback_index = content.find(FALLBACK_SUMMARY_MARKER)
    if fallback_index >= 0 and len(content) - fallback_index < FALLBACK_SUMMARY_WINDOW:
        return DoctorMarkerKind.FALLBACK

    return None


# ---------------------------------------------------------------------------
# Target loading (DB query + per-row reclassification)
# ---------------------------------------------------------------------------


# The two query branches share these SELECT/JOIN clauses; only the WHERE
# and ORDER BY tail differs. Authored as inline strings (not joined from
# fragments) so the executor + reviewer can read them top-to-bottom.

_SELECT_TARGETS_ALL = """
    SELECT
       s.conversation_id,
       s.summary_id,
       s.kind,
       COALESCE(s.depth, 0) AS depth,
       COALESCE(s.token_count, 0) AS token_count,
       COALESCE(s.content, '') AS content,
       COALESCE(s.created_at, '') AS created_at,
       COALESCE(spc.child_count, 0) AS child_count
     FROM summaries s
     LEFT JOIN (
       SELECT summary_id, COUNT(*) AS child_count
       FROM summary_parents
       GROUP BY summary_id
     ) spc ON spc.summary_id = s.summary_id
     WHERE INSTR(COALESCE(s.content, ''), ?) > 0
        OR INSTR(COALESCE(s.content, ''), ?) > 0
        OR INSTR(COALESCE(s.content, ''), ?) > 0
        OR INSTR(COALESCE(s.content, ''), ?) > 0
     ORDER BY s.conversation_id ASC,
              COALESCE(s.depth, 0) ASC,
              s.created_at ASC,
              s.summary_id ASC
"""
"""DB-wide query (no conversation filter).

The 4-key ORDER BY mirrors ``lcm-doctor-shared.ts:149`` — the unfiltered
branch leads with ``conversation_id`` so callers iterating the result
naturally group by conversation, then by depth (so leaves can be
processed before their condensed parents in :func:`apply_scoped_doctor_repair`
in issue 08-07), then by ``created_at`` and ``summary_id`` for
determinism."""


_SELECT_TARGETS_FILTERED = """
    SELECT
       s.conversation_id,
       s.summary_id,
       s.kind,
       COALESCE(s.depth, 0) AS depth,
       COALESCE(s.token_count, 0) AS token_count,
       COALESCE(s.content, '') AS content,
       COALESCE(s.created_at, '') AS created_at,
       COALESCE(spc.child_count, 0) AS child_count
     FROM summaries s
     LEFT JOIN (
       SELECT summary_id, COUNT(*) AS child_count
       FROM summary_parents
       GROUP BY summary_id
     ) spc ON spc.summary_id = s.summary_id
     WHERE s.conversation_id = ?
       AND (
         INSTR(COALESCE(s.content, ''), ?) > 0
         OR INSTR(COALESCE(s.content, ''), ?) > 0
         OR INSTR(COALESCE(s.content, ''), ?) > 0
         OR INSTR(COALESCE(s.content, ''), ?) > 0
       )
     ORDER BY COALESCE(s.depth, 0) ASC,
              s.created_at ASC,
              s.summary_id ASC
"""
"""Single-conversation query.

Mirrors ``lcm-doctor-shared.ts:151-175``. The ORDER BY drops
``conversation_id`` (constant by the WHERE clause) and keeps the same
3-key tiebreaker chain for determinism."""


def load_doctor_targets(
    db: sqlite3.Connection,
    conversation_id: Optional[int] = None,
) -> list[DoctorTargetRecord]:
    """Load broken summary rows the doctor should consider repairing.

    Ports TS ``loadDoctorTargets`` (``lcm-doctor-shared.ts:120-214``).
    Selects from ``summaries`` with a 4-marker INSTR pre-filter, then
    re-runs :func:`detect_doctor_marker` per row to classify (the SQL
    pre-filter is permissive; the Python re-classification is precise).

    ### Wave-4 Auditor #18 P0 (preserved from TS)

    The INSTR pre-filter includes BOTH the legacy marker and the two new
    v4.1 prefix markers. Without this, freshly-summarized v4.1 DBs would
    fall through (the legacy marker doesn't appear in v4.1 content).
    Detection still uses :func:`detect_doctor_marker` per-row for the
    final classification.

    Args:
        db: Open :class:`sqlite3.Connection`. The schema must have the
            ``summaries`` + ``summary_parents`` tables created by the
            standard migration (any LCM-host DB has them by definition).
        conversation_id: Optional filter. If :data:`None` (the default),
            returns rows across the entire DB. If set, restricts to
            that conversation.

    Returns:
        List of :class:`DoctorTargetRecord` in deterministic order (see
        module docstring §"Determinism contract" — 4-key sort
        DB-wide, 3-key sort filtered). Rows that match INSTR but fail
        re-classification (i.e. :func:`detect_doctor_marker` returns
        :data:`None`) are silently dropped.
    """
    if conversation_id is None:
        cursor = db.execute(
            _SELECT_TARGETS_ALL,
            # LCM Wave-4 (2026-02-14): Auditor #18 P0 fix — include the new
            # v4.1 fallback markers in the INSTR pre-filter so doctor still
            # finds rows with the new prefix form on a freshly-summarized
            # DB. Order of bind parameters mirrors the WHERE-clause
            # placeholders top-to-bottom; the OR semantics make order
            # irrelevant for matching, but we keep it lexically
            # legacy → v4.1-trunc → v4.1-full → truncated-prefix for
            # readability against ``lcm-doctor-shared.ts:178-183``.
            # Original: lossless-claw/src/plugin/lcm-doctor-shared.ts:124-127.
            (
                FALLBACK_SUMMARY_MARKER,
                FALLBACK_SUMMARY_MARKER_V41_TRUNC,
                FALLBACK_SUMMARY_MARKER_V41_FULL,
                TRUNCATED_SUMMARY_PREFIX,
            ),
        )
    else:
        cursor = db.execute(
            _SELECT_TARGETS_FILTERED,
            (
                conversation_id,
                FALLBACK_SUMMARY_MARKER,
                FALLBACK_SUMMARY_MARKER_V41_TRUNC,
                FALLBACK_SUMMARY_MARKER_V41_FULL,
                TRUNCATED_SUMMARY_PREFIX,
            ),
        )

    targets: list[DoctorTargetRecord] = []
    for row in cursor.fetchall():
        # Row indices match the SELECT order above.
        # 0: conversation_id, 1: summary_id, 2: kind, 3: depth,
        # 4: token_count, 5: content, 6: created_at, 7: child_count.
        content = row[5] if row[5] is not None else ""
        marker_kind = detect_doctor_marker(content)
        if marker_kind is None:
            # INSTR matched but the marker isn't in a load-bearing
            # position (mid-content) — false positive from the SQL
            # pre-filter; drop it.
            continue

        # The ``summaries.kind`` CHECK constraint guarantees this is
        # ``"leaf"`` or ``"condensed"``; narrow to the Literal for
        # static type checkers.
        kind_raw = str(row[2])
        if kind_raw not in ("leaf", "condensed"):
            # Defensive — should never fire because the schema enforces
            # the CHECK constraint. Skip the row rather than raise so a
            # corrupt DB still yields a usable doctor scan.
            continue
        kind: Literal["leaf", "condensed"] = cast('Literal["leaf", "condensed"]', kind_raw)

        targets.append(
            DoctorTargetRecord(
                conversation_id=int(row[0]),
                summary_id=str(row[1]),
                kind=kind,
                # COALESCE in SQL returns 0 on NULL; the additional
                # max(0, ...) clamp mirrors the TS ``Math.max(0,
                # Math.floor(...))`` (defends against malformed data
                # somehow landing a negative or fractional value).
                depth=max(0, int(row[3] or 0)),
                token_count=max(0, int(row[4] or 0)),
                content=content,
                created_at=str(row[6]) if row[6] is not None else "",
                child_count=max(0, int(row[7] or 0)),
                marker_kind=marker_kind,
            )
        )
    return targets


# ---------------------------------------------------------------------------
# Stats aggregation (consumed by /lcm doctor scan rendering)
# ---------------------------------------------------------------------------


def get_doctor_summary_stats(
    db: sqlite3.Connection,
    conversation_id: Optional[int] = None,
) -> DoctorSummaryStats:
    """Aggregate per-conversation + DB-wide counts from
    :func:`load_doctor_targets`.

    Ports TS ``getDoctorSummaryStats`` (``lcm-doctor-shared.ts:219-269``).
    Iterates the targets once, building both the flat
    :class:`DoctorSummaryCandidate` list and the per-conversation
    :class:`DoctorConversationCounts` map. Total counts at the top level
    equal the sum of per-conversation breakdowns.

    Args:
        db: Open :class:`sqlite3.Connection`. Same contract as
            :func:`load_doctor_targets`.
        conversation_id: Optional filter forwarded to
            :func:`load_doctor_targets`. When set, the returned
            :attr:`DoctorSummaryStats.by_conversation` map has at most
            one key.

    Returns:
        :class:`DoctorSummaryStats` with the flat candidate list, total
        counts by marker kind, and per-conversation breakdown. Empty
        stats (all counts 0, empty list + map) on a clean DB — never
        :data:`None`.
    """
    targets = load_doctor_targets(db, conversation_id)
    candidates: list[DoctorSummaryCandidate] = []
    by_conversation: dict[int, DoctorConversationCounts] = {}
    old = 0
    truncated = 0
    fallback = 0

    for target in targets:
        current = by_conversation.get(target.conversation_id)
        if current is None:
            current = DoctorConversationCounts()
            by_conversation[target.conversation_id] = current
        current.total += 1

        # match-case mirrors the TS ``switch`` (``lcm-doctor-shared.ts:239-252``).
        match target.marker_kind:
            case DoctorMarkerKind.OLD:
                old += 1
                current.old += 1
            case DoctorMarkerKind.NEW:
                truncated += 1
                current.truncated += 1
            case DoctorMarkerKind.FALLBACK:
                fallback += 1
                current.fallback += 1

        candidates.append(
            DoctorSummaryCandidate(
                conversation_id=target.conversation_id,
                summary_id=target.summary_id,
                marker_kind=target.marker_kind,
            )
        )

    return DoctorSummaryStats(
        candidates=candidates,
        total=len(candidates),
        old=old,
        truncated=truncated,
        fallback=fallback,
        by_conversation=by_conversation,
    )
