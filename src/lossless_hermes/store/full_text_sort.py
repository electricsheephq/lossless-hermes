"""Build the ``ORDER BY`` clause for FTS5-backed searches.

Ports ``lossless-claw/src/store/full-text-sort.ts`` (LCM commit ``1f07fbd``,
21 LOC TS → ~35 LOC Python). The ``rank`` column on FTS5-backed selects is
BM25's score (more negative ≡ more relevant); ``hybrid`` applies an age
penalty so older strong matches can still surface.

The three modes:

* ``"recency"`` (default) — newest first; pure chronological.
* ``"relevance"`` — best BM25 first; ties broken by recency.
* ``"hybrid"`` — BM25 scaled by ``1 / (1 + age_hours * decay)``; ties broken
  by recency.

See:

* ``/Volumes/LEXAR/Claude/lossless-claw/src/store/full-text-sort.ts`` — TS
  canonical (commit ``1f07fbd``).
* ``docs/porting-guides/storage.md`` §4.2 row "full-text-sort.ts".
"""

from __future__ import annotations

from typing import Literal

__all__ = ["AGE_DECAY_RATE", "SearchSort", "build_fts_order_by"]

SearchSort = Literal["recency", "relevance", "hybrid"]

# Decay rate applied to (julianday('now') - julianday(created_at)) in hours.
# Lower values → older results stay competitive; higher → recency penalty
# dominates. 0.001 means a 1000-hour-old (41-day) match takes a 2x penalty.
AGE_DECAY_RATE = 0.001


def build_fts_order_by(sort: SearchSort | None, created_at_expr: str) -> str:
    """Build the ``ORDER BY`` clause for an FTS5-backed query.

    Args:
        sort: Sort mode. ``None`` defaults to ``"recency"`` (matches TS
            default).
        created_at_expr: SQL expression that resolves to the row's
            ``created_at`` column (or COALESCE of latest_at + created_at for
            summary searches). Used for the recency tiebreaker.

    Returns:
        A SQL fragment suitable for direct interpolation after ``ORDER BY``.
        No semicolons or surrounding whitespace.
    """
    mode: SearchSort = sort if sort is not None else "recency"
    if mode == "relevance":
        return f"rank ASC, {created_at_expr} DESC"
    if mode == "hybrid":
        # rank / (1 + age_hours * decay_rate)
        return (
            f"(rank / (1 + ((julianday('now') - julianday({created_at_expr}))"
            f" * 24 * {AGE_DECAY_RATE}))) ASC, {created_at_expr} DESC"
        )
    return f"{created_at_expr} DESC"
