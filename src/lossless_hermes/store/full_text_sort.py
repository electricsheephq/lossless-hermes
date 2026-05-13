"""Build ORDER BY clauses for FTS5-backed searches.

Port of ``lossless-claw/src/store/full-text-sort.ts`` (LCM commit
``1f07fbd``, 21 LOC TS → Python).

FTS5's ``rank`` column is its BM25 score where **lower (more negative) is
better**. The three sort modes:

* ``"recency"`` (default) — pure ``created_at DESC``. Used when the caller
  wants the newest matches regardless of relevance.
* ``"relevance"`` — pure BM25 (``rank ASC, created_at DESC`` as
  tiebreaker). Used when the caller wants the strongest matches.
* ``"hybrid"`` — relevance with a mild age penalty. Older strong matches
  can still surface, but recent matches get a small boost. The
  ``AGE_DECAY_RATE`` of 0.001 / hour gives a ~24% boost to documents one
  day old.
"""

from __future__ import annotations

from typing import Literal

__all__ = ["AGE_DECAY_RATE", "SearchSort", "build_fts_order_by"]

# Sort modes accepted by the search dispatcher.
SearchSort = Literal["recency", "relevance", "hybrid"]

# Hours-decay rate used by the "hybrid" sort mode. Lifted verbatim from
# ``full-text-sort.ts:3``; tune in both runtimes if changed.
AGE_DECAY_RATE = 0.001


def build_fts_order_by(sort: SearchSort | None, created_at_expr: str) -> str:
    """Return the ORDER BY clause for an FTS5-backed search.

    Mirrors ``buildFtsOrderBy`` in TS. When ``sort`` is ``None`` the
    default ``"recency"`` mode is used (matches the TS ``?? "recency"``).

    Args:
        sort: One of ``"recency"`` / ``"relevance"`` / ``"hybrid"``.
            ``None`` is treated as ``"recency"``.
        created_at_expr: SQL expression for the ``created_at`` column on
            the table being searched, e.g. ``"m.created_at"`` (with a
            table alias when the search joins ``messages_fts`` against
            ``messages``).

    Returns:
        An ORDER BY fragment **without** the leading ``ORDER BY``
        keyword — caller embeds in their SQL after their own
        ``ORDER BY``.
    """
    mode = sort or "recency"
    if mode == "relevance":
        return f"rank ASC, {created_at_expr} DESC"
    if mode == "hybrid":
        return (
            f"(rank / (1 + ((julianday('now') - julianday({created_at_expr}))"
            f" * 24 * {AGE_DECAY_RATE}))) ASC, {created_at_expr} DESC"
        )
    return f"{created_at_expr} DESC"
