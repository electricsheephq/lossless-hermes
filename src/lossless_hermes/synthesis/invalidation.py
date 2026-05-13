"""Synthesis cache invalidation on leaf change — LCM v4.1 (issue 07-07).

When the operator purge path soft-suppresses a leaf
(``UPDATE summaries SET suppressed_at = datetime('now')``), any
synthesis-cache row that included that leaf in its source set MUST be
deleted explicitly. The ``lcm_cache_leaf_refs`` lookup table has
``ON DELETE CASCADE`` in both directions, but **the cascade only fires
on hard ``DELETE FROM summaries``** — soft suppression leaves the
``summaries`` row in place with ``suppressed_at`` set, so cascade does
not fire and the cache row survives, still serving its previously-
synthesised text (which may have baked the now-suppressed leaf's PII).

This module ports the two halves of the fix:

1. :func:`record_cache_leaf_refs` — best-effort populate of
   ``lcm_cache_leaf_refs`` immediately after a synthesis run, so the
   suppression-time DELETE has the inverse index to consult. Best-
   effort because the surrounding synthesis has already succeeded; a
   failure here is logged but does NOT raise (worst case the cache row
   survives a later suppression, the operator audit catches it, and we
   accept the rare leak rather than abort an otherwise-good synthesis).

2. :func:`invalidate_caches_for_suppressed_leaves` — single-statement
   DELETE that the purge path calls inside its own transaction, after
   the ``UPDATE summaries SET suppressed_at = ...`` write. Returns the
   row count for telemetry. The transaction is caller-owned (the purge
   path) — this function does NOT issue ``BEGIN`` / ``COMMIT``.

### Source pin

* TS canonical: ``lossless-claw/src/operator/purge.ts:346-352``
  (commit ``1f07fbd`` on branch ``pr-613``) for the suppression-time
  DELETE; ``lossless-claw/src/tools/lcm-synthesize-around-tool.ts:1396``
  for the post-synthesis populate.
* Migration table + index: see
  :mod:`lossless_hermes.db.migration` (``lcm_cache_leaf_refs``,
  ``lcm_cache_leaf_refs_by_leaf_idx``).
* Spec: ``epics/07-entity-synthesis/07-07-synthesis-invalidation.md``.

### Final.review.3 Loop 2 Leak 2.5 marker

The suppression-time DELETE carries a load-bearing inline comment
documenting why a Python loop of N DELETEs is wrong (would not be
atomic under the purge transaction) and why the FK cascade is not
sufficient on soft suppression.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable

__all__ = [
    "invalidate_caches_for_suppressed_leaves",
    "record_cache_leaf_refs",
]


_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Post-synthesis leaf-ref populate (best-effort)
# ---------------------------------------------------------------------------


def record_cache_leaf_refs(
    db: sqlite3.Connection,
    cache_id: str,
    leaf_ids: Iterable[str],
) -> None:
    """Populate ``lcm_cache_leaf_refs`` for a freshly-built cache row.

    Runs ``INSERT OR IGNORE`` once per leaf in ``leaf_ids``. The OR-IGNORE
    semantics make this idempotent — a retry of the surrounding
    synthesis (e.g. the loser-path SELECT-back deciding the existing
    row is stale and re-building) will not raise on duplicate
    ``(cache_id, leaf_summary_id)`` rows.

    **Best-effort.** A per-leaf INSERT failure (e.g. the leaf was
    hard-deleted between synthesis and ref-write, violating the
    ``REFERENCES summaries(summary_id)`` FK) is logged at warn level
    and skipped. The surrounding synthesis has already succeeded, and
    re-raising here would surface a misleading "synthesis failed" to
    the agent caller. Worst case: the cache row survives a later
    suppression of the missing leaf, the operator audit eventually
    catches it, and we accept the rare leak rather than abort a good
    synthesis.

    Transaction scope: the caller controls the transaction (if any).
    This function does NOT issue ``BEGIN`` / ``COMMIT``. Per-leaf
    failures do not roll back successful previous inserts in the
    iterable.

    Args:
        db: Open :class:`sqlite3.Connection`.
        cache_id: PK of the freshly-INSERTed ``lcm_synthesis_cache``
            row (caller has it from
            :func:`cache_key.insert_cache_row_single_flight`).
        leaf_ids: Iterable of ``summaries.summary_id`` values that fed
            this synthesis. Order is not significant for the lookup
            index (the PK is the unordered tuple).

    Returns:
        ``None``. Per-leaf failure counts are not surfaced — callers
        only need to know that the function does not raise. If
        per-leaf telemetry is needed in future, augment the signature
        to return ``inserted_count`` / ``failed_count``.

    Byte-for-byte parity with the TS source:
    ``lossless-claw/src/tools/lcm-synthesize-around-tool.ts:1395-1406``::

        try {
          const refStmt = db.prepare(
            `INSERT OR IGNORE INTO lcm_cache_leaf_refs (cache_id, leaf_summary_id) VALUES (?, ?)`,
          );
          for (const id of leafIds) {
            refStmt.run(cacheId, id);
          }
        } catch (refErr) {
          input.deps.log.warn(
            `[lcm] synthesize_around: cache_leaf_refs insert failed for ${cacheId}: ...`,
          );
        }

    The TS wraps the entire loop in one try/except; the Python port
    wraps each per-leaf INSERT individually so a single bad leaf does
    not orphan subsequent good leaves.
    """

    for leaf_id in leaf_ids:
        try:
            db.execute(
                "INSERT OR IGNORE INTO lcm_cache_leaf_refs"
                " (cache_id, leaf_summary_id) VALUES (?, ?)",
                (cache_id, leaf_id),
            )
        except sqlite3.DatabaseError as exc:
            # Best-effort: log + continue. Surrounding synthesis already
            # succeeded; do NOT re-raise.
            _LOG.warning(
                "lcm.synthesize_around: cache_leaf_refs insert failed for "
                "cache_id=%s leaf_id=%s: %s",
                cache_id,
                leaf_id,
                exc,
            )


# ---------------------------------------------------------------------------
# Suppression-time cache invalidation (single-statement DELETE)
# ---------------------------------------------------------------------------


def invalidate_caches_for_suppressed_leaves(
    db: sqlite3.Connection,
    leaf_ids: Iterable[str],
) -> int:
    """Delete cache rows that referenced any of ``leaf_ids``.

    Called from the operator purge path (``operator/purge.py``, ported
    in epic 06 / doctor-ops) immediately after the
    ``UPDATE summaries SET suppressed_at = datetime('now')`` write
    inside the same transaction. The DELETE is a single statement —
    NOT a Python loop of N DELETEs — so the SQLite planner uses
    ``lcm_cache_leaf_refs_by_leaf_idx`` to fan out from the suppressed
    leaves to the matching ``cache_id`` set, then issues one DELETE
    against ``lcm_synthesis_cache``.

    Transaction scope: caller-owned. The purge path opens its own
    ``BEGIN IMMEDIATE`` and commits after both the
    ``UPDATE suppressed_at`` and this DELETE succeed. This function
    must NOT issue ``BEGIN`` / ``COMMIT`` — otherwise a crash between
    the UPDATE and the DELETE would leave cache rows pointing at a
    suppressed leaf, and the next cache read would surface PII.

    Empty ``leaf_ids`` is a no-op that returns ``0`` without issuing
    SQL — avoids generating an empty ``IN ()`` clause that some SQLite
    parsers reject.

    Args:
        db: Open :class:`sqlite3.Connection`. Caller controls the
            surrounding transaction.
        leaf_ids: Iterable of ``summaries.summary_id`` values that
            were just suppressed. Order is not significant.

    Returns:
        Row count from ``cursor.rowcount`` — the number of
        ``lcm_synthesis_cache`` rows deleted. The caller emits the
        telemetry; this function does not log.

    Byte-for-byte parity with the TS source:
    ``lossless-claw/src/operator/purge.ts:346-352``::

        db.prepare(
          `DELETE FROM lcm_synthesis_cache
             WHERE cache_id IN (
               SELECT DISTINCT cache_id FROM lcm_cache_leaf_refs
                 WHERE leaf_summary_id IN (${placeholders})
             )`,
        ).run(...leafIds);
    """

    leaves_list = list(leaf_ids)
    if not leaves_list:
        # Empty input — no SQL issued, no rows deleted. Avoids the
        # ``IN ()`` empty-list parser hazard.
        return 0

    placeholders = ",".join("?" for _ in leaves_list)

    # LCM Final.review.3 Loop 2 Leak 2.5 (2026-04-08): explicit DELETE not
    # FK cascade — soft suppression leaves summaries row in place.
    # lcm_cache_leaf_refs has ON DELETE CASCADE on both
    # lcm_synthesis_cache.cache_id and summaries.summary_id, but the
    # cascade only fires on hard DELETE FROM summaries — not on the
    # soft-suppress UPDATE that sets suppressed_at. We MUST delete the
    # cache rows explicitly so a future cache read does NOT surface PII
    # baked into the synthesis before suppression. Single-statement
    # (cache_id subselect) is load-bearing: the purge path owns the
    # transaction, and a Python loop of N DELETEs would not be atomic
    # under that tx.
    # Original: lossless-claw/src/operator/purge.ts:346-352.
    cursor = db.execute(
        "DELETE FROM lcm_synthesis_cache"
        " WHERE cache_id IN ("
        "   SELECT DISTINCT cache_id FROM lcm_cache_leaf_refs"
        f"     WHERE leaf_summary_id IN ({placeholders})"
        " )",
        leaves_list,
    )
    return cursor.rowcount
