"""LCM integrity checks — orphan detection + dangling FK references.

Ports ``lossless-claw/src/integrity.ts`` (commit ``1f07fbd``, ~600 LOC) to
Python. The TS module exposes an ``IntegrityChecker`` class with 8 checks
plus a ``repairPlan`` helper plus a ``collectMetrics`` observability hook.

### What the checks find

The 8 checks (per doctor-ops.md §"Integrity checks") cover the structural
invariants of the LCM tables:

| Check | What it verifies | Status on failure |
|---|---|---|
| ``conversation_exists`` | ``conversation_id`` row is in ``conversations`` | ``fail`` |
| ``context_items_contiguous`` | ``context_items.ordinal`` is ``[0..N-1]`` (no gaps) | ``fail`` |
| ``context_items_valid_refs`` | every ``message_id`` / ``summary_id`` resolves | ``fail`` |
| ``summaries_have_lineage`` | leaves have ≥1 summary_messages row; condensed have ≥1 summary_parents row | ``fail`` |
| ``no_orphan_summaries`` | every summary appears in context_items or as a parent | **``warn``** (only check that warns) |
| ``context_token_consistency`` | item-level token sum equals the aggregate query | ``fail`` |
| ``message_seq_contiguous`` | ``messages.seq`` is ``[0..N-1]`` | ``fail`` |
| ``no_duplicate_context_refs`` | no ``message_id`` / ``summary_id`` appears twice | ``fail`` |

All 8 checks run on every scan — a failure does not short-circuit the
remaining checks. The report is always complete.

### Read-only contract

Per the issue spec: integrity is **pure read-only**. The checks do not
mutate state. The :func:`build_repair_plan` helper returns human-readable
suggestion strings — the caller (the doctor in Epic 08) decides whether
to apply any of them.

### Python port differences from TS

The TS source takes ``conversationStore`` + ``summaryStore`` instances and
calls their methods. At this point in the port (issue #01-13) the stores
have not landed (#01-08 / #01-09 come later in the chain — internal
ordering inside the issue says transaction_mutex first, then prune, then
integrity per storage.md §9 phase 6).

To unblock #01-13 without taking a dependency on the stores, this module
talks **directly to the connection** via SQL — the TS store methods are
all thin SQL wrappers, so re-deriving the queries here is a stable
contract. When the stores land (#01-08 / #01-09), a follow-up can
refactor :class:`IntegrityChecker` to consume them; the public surface
stays the same.

### Public surface

| Symbol | TS analogue |
|---|---|
| :class:`IntegrityCheck` | ``IntegrityCheck`` |
| :class:`IntegrityReport` | ``IntegrityReport`` |
| :class:`LcmMetrics` | ``LcmMetrics`` |
| :class:`IntegrityChecker` | ``IntegrityChecker`` |
| :func:`check_integrity` | (convenience entry — runs all 8 checks) |
| :func:`build_repair_plan` | ``repairPlan`` |
| :func:`collect_metrics` | ``collectMetrics`` |

The convenience :func:`check_integrity(conn, conversation_id)` matches
the task brief's simpler shape. It instantiates an
:class:`IntegrityChecker`, calls :meth:`scan`, and returns the
:class:`IntegrityReport`.

See:

* ``docs/porting-guides/doctor-ops.md`` §"Integrity checks" — the
  canonical check list + statuses.
* ``lossless-claw/src/integrity.ts`` — verbatim TS source.
* ``epics/01-storage/01-13-integrity-prune.md`` — issue spec.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

__all__ = [
    "IntegrityCheck",
    "IntegrityChecker",
    "IntegrityReport",
    "LcmMetrics",
    "build_repair_plan",
    "check_integrity",
    "collect_metrics",
]


_log = logging.getLogger("lossless_hermes.integrity")


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


CheckStatus = Literal["pass", "fail", "warn"]


@dataclass(frozen=True, slots=True)
class IntegrityCheck:
    """Single integrity-check result.

    Mirrors TS ``IntegrityCheck``: a ``name`` identifying the check, a
    ``status`` of ``"pass"`` / ``"fail"`` / ``"warn"``, a human-readable
    ``message``, and optional structured ``details`` for repair planning.
    """

    name: str
    status: CheckStatus
    message: str
    details: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class IntegrityReport:
    """Aggregate report from a single :meth:`IntegrityChecker.scan`.

    Mirrors TS ``IntegrityReport``: the scanned ``conversation_id``, the
    full list of checks, derived pass/fail/warn counts, and a UTC
    timestamp.
    """

    conversation_id: int
    checks: tuple[IntegrityCheck, ...]
    pass_count: int
    fail_count: int
    warn_count: int
    scanned_at: datetime


@dataclass(frozen=True, slots=True)
class LcmMetrics:
    """Observability snapshot from :func:`collect_metrics`.

    Mirrors TS ``LcmMetrics``: per-conversation context token sum,
    message/summary/context counts, plus leaf/condensed/large-file
    cardinality.
    """

    conversation_id: int
    context_tokens: int
    message_count: int
    summary_count: int
    context_item_count: int
    leaf_summary_count: int
    condensed_summary_count: int
    large_file_count: int
    collected_at: datetime


# ---------------------------------------------------------------------------
# IntegrityChecker
# ---------------------------------------------------------------------------


class IntegrityChecker:
    """8-check integrity scanner for a single conversation.

    Per the issue spec: pure read-only. No transactions, no mutations.
    Each check runs to completion even if another fails — the report is
    always 8 entries long.

    Per ADR-017 the implementation is synchronous (the TS source is
    decoratively async; the underlying SQLite calls are blocking).
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def scan(self, conversation_id: int) -> IntegrityReport:
        """Run all 8 checks and return an :class:`IntegrityReport`.

        Mirrors TS ``IntegrityChecker.scan``. Order of checks is
        canonical (matches the TS source) so test fixtures can index
        into the result by position if needed.
        """
        checks: list[IntegrityCheck] = [
            self._check_conversation_exists(conversation_id),
            self._check_context_items_contiguous(conversation_id),
            self._check_context_items_valid_refs(conversation_id),
            self._check_summaries_have_lineage(conversation_id),
            self._check_no_orphan_summaries(conversation_id),
            self._check_context_token_consistency(conversation_id),
            self._check_message_seq_contiguous(conversation_id),
            self._check_no_duplicate_context_refs(conversation_id),
        ]
        pass_count = sum(1 for c in checks if c.status == "pass")
        fail_count = sum(1 for c in checks if c.status == "fail")
        warn_count = sum(1 for c in checks if c.status == "warn")
        return IntegrityReport(
            conversation_id=conversation_id,
            checks=tuple(checks),
            pass_count=pass_count,
            fail_count=fail_count,
            warn_count=warn_count,
            scanned_at=datetime.now(timezone.utc),
        )

    # ── Individual checks (private; one method per check) ────────────────

    def _check_conversation_exists(self, conversation_id: int) -> IntegrityCheck:
        """The ``conversation_id`` row exists in ``conversations``."""
        row = self._conn.execute(
            "SELECT 1 FROM conversations WHERE conversation_id = ? LIMIT 1",
            (conversation_id,),
        ).fetchone()
        if row is not None:
            return IntegrityCheck(
                name="conversation_exists",
                status="pass",
                message=f"Conversation {conversation_id} exists",
            )
        return IntegrityCheck(
            name="conversation_exists",
            status="fail",
            message=f"Conversation {conversation_id} not found",
        )

    def _check_context_items_contiguous(self, conversation_id: int) -> IntegrityCheck:
        """``context_items.ordinal`` is ``[0..N-1]`` (no gaps, sorted)."""
        rows = self._conn.execute(
            "SELECT ordinal FROM context_items WHERE conversation_id = ? ORDER BY ordinal",
            (conversation_id,),
        ).fetchall()
        if not rows:
            return IntegrityCheck(
                name="context_items_contiguous",
                status="pass",
                message="No context items to check",
            )
        gaps: list[dict[str, int]] = []
        for i, row in enumerate(rows):
            if row[0] != i:
                gaps.append({"expected": i, "actual": row[0]})
        if not gaps:
            return IntegrityCheck(
                name="context_items_contiguous",
                status="pass",
                message=f"All {len(rows)} context items have contiguous ordinals",
            )
        return IntegrityCheck(
            name="context_items_contiguous",
            status="fail",
            message=f"Found {len(gaps)} ordinal gap(s) in context items",
            details={"gaps": gaps},
        )

    def _check_context_items_valid_refs(self, conversation_id: int) -> IntegrityCheck:
        """Every ``context_items.message_id`` / ``summary_id`` resolves."""
        rows = self._conn.execute(
            "SELECT ordinal, item_type, message_id, summary_id "
            "FROM context_items WHERE conversation_id = ? ORDER BY ordinal",
            (conversation_id,),
        ).fetchall()
        dangling: list[dict[str, Any]] = []
        for ordinal, item_type, message_id, summary_id in rows:
            if item_type == "message" and message_id is not None:
                hit = self._conn.execute(
                    "SELECT 1 FROM messages WHERE message_id = ? LIMIT 1",
                    (message_id,),
                ).fetchone()
                if hit is None:
                    dangling.append({
                        "ordinal": ordinal,
                        "itemType": "message",
                        "refId": message_id,
                    })
            elif item_type == "summary" and summary_id is not None:
                hit = self._conn.execute(
                    "SELECT 1 FROM summaries WHERE summary_id = ? LIMIT 1",
                    (summary_id,),
                ).fetchone()
                if hit is None:
                    dangling.append({
                        "ordinal": ordinal,
                        "itemType": "summary",
                        "refId": summary_id,
                    })
        if not dangling:
            return IntegrityCheck(
                name="context_items_valid_refs",
                status="pass",
                message="All context item references are valid",
            )
        return IntegrityCheck(
            name="context_items_valid_refs",
            status="fail",
            message=f"Found {len(dangling)} dangling reference(s) in context items",
            details={"danglingRefs": dangling},
        )

    def _check_summaries_have_lineage(self, conversation_id: int) -> IntegrityCheck:
        """Leaves have ≥1 summary_messages row; condensed have ≥1 parents row."""
        summary_rows = self._conn.execute(
            "SELECT summary_id, kind FROM summaries WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchall()
        missing: list[dict[str, str]] = []
        for summary_id, kind in summary_rows:
            if kind == "leaf":
                row = self._conn.execute(
                    "SELECT 1 FROM summary_messages WHERE summary_id = ? LIMIT 1",
                    (summary_id,),
                ).fetchone()
                if row is None:
                    missing.append({
                        "summaryId": summary_id,
                        "kind": "leaf",
                        "issue": "no linked messages in summary_messages",
                    })
            elif kind == "condensed":
                row = self._conn.execute(
                    "SELECT 1 FROM summary_parents WHERE summary_id = ? LIMIT 1",
                    (summary_id,),
                ).fetchone()
                if row is None:
                    missing.append({
                        "summaryId": summary_id,
                        "kind": "condensed",
                        "issue": "no linked parents in summary_parents",
                    })
        if not missing:
            return IntegrityCheck(
                name="summaries_have_lineage",
                status="pass",
                message=f"All {len(summary_rows)} summaries have proper lineage",
            )
        return IntegrityCheck(
            name="summaries_have_lineage",
            status="fail",
            message=f"Found {len(missing)} summary/summaries missing lineage",
            details={"missingLineage": missing},
        )

    def _check_no_orphan_summaries(self, conversation_id: int) -> IntegrityCheck:
        """Every summary appears either in ``context_items`` or as a parent.

        **The only check that warns rather than fails** — an orphaned
        summary is harmless on the read path (it just sits unreferenced)
        but consumes disk + indicates a cleaner could remove it.
        """
        summary_ids = [
            r[0]
            for r in self._conn.execute(
                "SELECT summary_id FROM summaries WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchall()
        ]
        if not summary_ids:
            return IntegrityCheck(
                name="no_orphan_summaries",
                status="pass",
                message="No orphaned summaries found",
            )

        context_summary_ids: set[str] = {
            r[0]
            for r in self._conn.execute(
                "SELECT summary_id FROM context_items "
                "WHERE conversation_id = ? AND item_type = 'summary' "
                "AND summary_id IS NOT NULL",
                (conversation_id,),
            ).fetchall()
        }
        # A summary is a parent if it appears as parent_summary_id in
        # summary_parents (i.e. some condensed summary has it as a child).
        parent_summary_ids: set[str] = {
            r[0]
            for r in self._conn.execute(
                "SELECT DISTINCT parent_summary_id FROM summary_parents "
                "WHERE parent_summary_id IN ("
                "  SELECT summary_id FROM summaries WHERE conversation_id = ?"
                ")",
                (conversation_id,),
            ).fetchall()
        }
        orphans = [
            sid
            for sid in summary_ids
            if sid not in context_summary_ids and sid not in parent_summary_ids
        ]
        if not orphans:
            return IntegrityCheck(
                name="no_orphan_summaries",
                status="pass",
                message="No orphaned summaries found",
            )
        return IntegrityCheck(
            name="no_orphan_summaries",
            status="warn",
            message=(f"Found {len(orphans)} orphaned summary/summaries disconnected from the DAG"),
            details={"orphanedSummaryIds": orphans},
        )

    def _check_context_token_consistency(self, conversation_id: int) -> IntegrityCheck:
        """Item-level token sum equals the aggregate query result.

        TS source iterates context_items, looks up each message/summary
        by id, and sums ``token_count``; we issue equivalent SQL.
        """
        manual_sum_row = self._conn.execute(
            """
            SELECT COALESCE((
              SELECT SUM(m.token_count)
                FROM context_items ci
                JOIN messages m ON m.message_id = ci.message_id
               WHERE ci.conversation_id = ?
                 AND ci.item_type = 'message'
            ), 0) + COALESCE((
              SELECT SUM(s.token_count)
                FROM context_items ci
                JOIN summaries s ON s.summary_id = ci.summary_id
               WHERE ci.conversation_id = ?
                 AND ci.item_type = 'summary'
            ), 0) AS manual_sum
            """,
            (conversation_id, conversation_id),
        ).fetchone()
        manual_sum = int(manual_sum_row[0] or 0) if manual_sum_row else 0

        # The "aggregate" query mirrors what ``SummaryStore.getContextTokenCount``
        # uses upstream: same form, joined differently. We keep it the same here
        # (the *value* must equal the item-level sum). A mismatch indicates
        # either a stale denormalized counter (not present today, but the test
        # could simulate one) or a row-level corruption.
        aggregate_row = self._conn.execute(
            """
            SELECT COALESCE(SUM(CASE
              WHEN ci.item_type = 'message' THEN (SELECT token_count FROM messages WHERE message_id = ci.message_id)
              WHEN ci.item_type = 'summary' THEN (SELECT token_count FROM summaries WHERE summary_id = ci.summary_id)
              ELSE 0
            END), 0) AS aggregate_total
              FROM context_items ci
             WHERE ci.conversation_id = ?
            """,
            (conversation_id,),
        ).fetchone()
        aggregate_total = int(aggregate_row[0] or 0) if aggregate_row else 0

        if manual_sum == aggregate_total:
            return IntegrityCheck(
                name="context_token_consistency",
                status="pass",
                message=f"Context token count is consistent ({aggregate_total} tokens)",
            )
        return IntegrityCheck(
            name="context_token_consistency",
            status="fail",
            message=(
                f"Token count mismatch: item-level sum = {manual_sum}, "
                f"aggregate query = {aggregate_total}"
            ),
            details={
                "manualSum": manual_sum,
                "aggregateTotal": aggregate_total,
                "difference": manual_sum - aggregate_total,
            },
        )

    def _check_message_seq_contiguous(self, conversation_id: int) -> IntegrityCheck:
        """``messages.seq`` is ``[0..N-1]`` with no gaps."""
        rows = self._conn.execute(
            "SELECT seq FROM messages WHERE conversation_id = ? ORDER BY seq",
            (conversation_id,),
        ).fetchall()
        if not rows:
            return IntegrityCheck(
                name="message_seq_contiguous",
                status="pass",
                message="No messages to check",
            )
        gaps: list[dict[str, int]] = []
        for i, row in enumerate(rows):
            if row[0] != i:
                gaps.append({"expected": i, "actual": row[0]})
        if not gaps:
            return IntegrityCheck(
                name="message_seq_contiguous",
                status="pass",
                message=f"All {len(rows)} messages have contiguous seq values",
            )
        return IntegrityCheck(
            name="message_seq_contiguous",
            status="fail",
            message=f"Found {len(gaps)} seq gap(s) in messages",
            details={"gaps": gaps},
        )

    def _check_no_duplicate_context_refs(self, conversation_id: int) -> IntegrityCheck:
        """No ``message_id`` / ``summary_id`` appears twice in ``context_items``."""
        rows = self._conn.execute(
            "SELECT ordinal, item_type, message_id, summary_id "
            "FROM context_items WHERE conversation_id = ? ORDER BY ordinal",
            (conversation_id,),
        ).fetchall()
        seen_messages: dict[int, list[int]] = {}
        seen_summaries: dict[str, list[int]] = {}
        for ordinal, item_type, message_id, summary_id in rows:
            if item_type == "message" and message_id is not None:
                seen_messages.setdefault(message_id, []).append(ordinal)
            elif item_type == "summary" and summary_id is not None:
                seen_summaries.setdefault(summary_id, []).append(ordinal)

        duplicates: list[dict[str, Any]] = []
        for message_id, ordinals in seen_messages.items():
            if len(ordinals) > 1:
                duplicates.append({
                    "refType": "message",
                    "refId": message_id,
                    "ordinals": ordinals,
                })
        for summary_id, ordinals in seen_summaries.items():
            if len(ordinals) > 1:
                duplicates.append({
                    "refType": "summary",
                    "refId": summary_id,
                    "ordinals": ordinals,
                })

        if not duplicates:
            return IntegrityCheck(
                name="no_duplicate_context_refs",
                status="pass",
                message="No duplicate references in context items",
            )
        return IntegrityCheck(
            name="no_duplicate_context_refs",
            status="fail",
            message=f"Found {len(duplicates)} duplicate reference(s) in context items",
            details={"duplicates": duplicates},
        )


# ---------------------------------------------------------------------------
# Convenience entry — :func:`check_integrity`
# ---------------------------------------------------------------------------


def check_integrity(conn: sqlite3.Connection, conversation_id: int) -> IntegrityReport:
    """Run all 8 integrity checks against ``conversation_id``.

    Convenience wrapper matching the task brief signature
    (``check_integrity(conn) -> IntegrityReport`` — extended here to take
    a conversation id, mirroring TS ``IntegrityChecker.scan``).

    Returns the full :class:`IntegrityReport`.
    """
    return IntegrityChecker(conn).scan(conversation_id)


# ---------------------------------------------------------------------------
# Repair plan
# ---------------------------------------------------------------------------


def build_repair_plan(report: IntegrityReport) -> list[str]:
    """Render human-readable repair suggestions for every fail/warn check.

    Mirrors TS ``repairPlan``. **Planning only** — does NOT perform any
    repairs. The caller (the doctor in Epic 08) decides which suggestions
    to act on.

    Returns:
        A list of suggestion strings, one per failing/warning check (or
        per dangling/duplicate/missing entry when the check carries
        per-row details).
    """
    suggestions: list[str] = []
    for check in report.checks:
        if check.status == "pass":
            continue

        if check.name == "conversation_exists":
            suggestions.append(
                f"Create or restore conversation {report.conversation_id} "
                "in the conversations table"
            )
        elif check.name == "context_items_contiguous":
            suggestions.append("Resequence context items to fix ordinal gaps")
        elif check.name == "context_items_valid_refs":
            details = check.details or {}
            dangling = details.get("danglingRefs", [])
            if dangling:
                for ref in dangling:
                    suggestions.append(
                        f"Remove context item at ordinal {ref['ordinal']} "
                        f"referencing missing {ref['itemType']} {ref['refId']}"
                    )
            else:
                suggestions.append("Remove context items with dangling references")
        elif check.name == "summaries_have_lineage":
            details = check.details or {}
            missing = details.get("missingLineage", [])
            if missing:
                for entry in missing:
                    if entry["kind"] == "leaf":
                        suggestions.append(
                            f"Add missing lineage for leaf summary "
                            f"{entry['summaryId']} "
                            "(link to source messages via summary_messages)"
                        )
                    else:
                        suggestions.append(
                            f"Add missing lineage for condensed summary "
                            f"{entry['summaryId']} "
                            "(link to parent summaries via summary_parents)"
                        )
            else:
                suggestions.append("Add missing lineage links for summaries")
        elif check.name == "no_orphan_summaries":
            details = check.details or {}
            orphans = details.get("orphanedSummaryIds", [])
            if orphans:
                for sid in orphans:
                    suggestions.append(f"Remove orphaned summary {sid} from summaries table")
            else:
                suggestions.append("Remove orphaned summaries disconnected from the DAG")
        elif check.name == "context_token_consistency":
            suggestions.append(
                "Recompute context token count to reconcile mismatch between "
                "item-level sum and aggregate query"
            )
        elif check.name == "message_seq_contiguous":
            suggestions.append(
                "Resequence message seq values to eliminate gaps (renumber starting from 0)"
            )
        elif check.name == "no_duplicate_context_refs":
            details = check.details or {}
            duplicates = details.get("duplicates", [])
            if duplicates:
                for dup in duplicates:
                    ordinals = dup["ordinals"]
                    keep = ordinals[0]
                    remove = ", ".join(str(o) for o in ordinals[1:])
                    suggestions.append(
                        f"Deduplicate {dup['refType']} {dup['refId']}: "
                        f"keep ordinal {keep}, remove ordinals {remove}"
                    )
            else:
                suggestions.append(
                    "Remove duplicate message_id or summary_id references from context items"
                )
        else:  # pragma: no cover - defensive default; TS source has the same shape
            suggestions.append(f"Address failing check: {check.name} -- {check.message}")
    return suggestions


# ---------------------------------------------------------------------------
# Observability — :func:`collect_metrics`
# ---------------------------------------------------------------------------


def collect_metrics(conn: sqlite3.Connection, conversation_id: int) -> LcmMetrics:
    """Snapshot observability metrics for ``conversation_id``.

    Mirrors TS ``collectMetrics``. Pulls one row per metric via aggregate
    SQL — cheaper than instantiating the store helpers and calling each
    in turn.
    """
    context_tokens_row = conn.execute(
        """
        SELECT COALESCE(SUM(CASE
          WHEN ci.item_type = 'message' THEN (SELECT token_count FROM messages WHERE message_id = ci.message_id)
          WHEN ci.item_type = 'summary' THEN (SELECT token_count FROM summaries WHERE summary_id = ci.summary_id)
          ELSE 0
        END), 0) AS total
          FROM context_items ci
         WHERE ci.conversation_id = ?
        """,
        (conversation_id,),
    ).fetchone()
    context_tokens = int(context_tokens_row[0] or 0) if context_tokens_row else 0

    message_count = (
        conn.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()[0]
        or 0
    )

    summaries_row = conn.execute(
        """
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN kind = 'leaf' THEN 1 ELSE 0 END) AS leaves,
          SUM(CASE WHEN kind = 'condensed' THEN 1 ELSE 0 END) AS condensed
        FROM summaries WHERE conversation_id = ?
        """,
        (conversation_id,),
    ).fetchone()
    summary_count = int(summaries_row[0] or 0) if summaries_row else 0
    leaf_summary_count = int(summaries_row[1] or 0) if summaries_row else 0
    condensed_summary_count = int(summaries_row[2] or 0) if summaries_row else 0

    context_item_count = (
        conn.execute(
            "SELECT COUNT(*) FROM context_items WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()[0]
        or 0
    )

    large_file_count = (
        conn.execute(
            "SELECT COUNT(*) FROM large_files WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()[0]
        or 0
    )

    return LcmMetrics(
        conversation_id=conversation_id,
        context_tokens=context_tokens,
        message_count=int(message_count),
        summary_count=summary_count,
        context_item_count=int(context_item_count),
        leaf_summary_count=leaf_summary_count,
        condensed_summary_count=condensed_summary_count,
        large_file_count=int(large_file_count),
        collected_at=datetime.now(timezone.utc),
    )
