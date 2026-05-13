"""LIKE search plan + CJK detection + snippet builder.

Port of ``lossless-claw/src/store/full-text-fallback.ts`` (LCM commit
``1f07fbd``, 84 LOC TS → Python).

Used when:

1. FTS5 is unavailable on the SQLite build (e.g. custom-compiled with
   ``--disable-fts5``).
2. FTS5 unicode61 tokenizer cannot handle CJK text correctly — the LIKE
   path uses raw substring matching which handles CJK by default.

Three exports:

* :func:`contains_cjk` — detect CJK characters in a query string. Used by
  ConversationStore.search_messages to route through LIKE when the query
  contains any CJK character.
* :func:`build_like_search_plan` — convert a free-text query into a list
  of normalized terms + parameterized ``WHERE LOWER(<column>) LIKE ?``
  clauses + escaped LIKE pattern arguments.
* :func:`create_fallback_snippet` — build a compact ``...content...``
  snippet centered on the earliest matching term, for the result row's
  ``snippet`` column.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

__all__ = [
    "LikeSearchPlan",
    "build_like_search_plan",
    "contains_cjk",
    "create_fallback_snippet",
]

# Match each non-quoted whitespace-delimited token OR each "..."-wrapped
# phrase. Group 1 = phrase contents (no quotes); group 2 = bare token.
_RAW_TERM_RE = re.compile(r'"([^"]+)"|(\S+)')

# CJK Unicode block ranges (CJK Unified, CJK Extension A, CJK Compat,
# Hangul, Hiragana, Katakana) — matches the TS regex character class
# verbatim.
_CJK_RE = re.compile(r"[⺀-鿿㐀-䶿豈-﫿가-힯぀-ゟ゠-ヿ]")

# Punctuation to trim from term edges (leading or trailing).
_EDGE_PUNCT_RE = re.compile(r"^[`'\"()\[\]{}<>.,:;!?*_+=|\\/-]+|[`'\"()\[\]{}<>.,:;!?*_+=|\\/-]+$")


@dataclass(frozen=True)
class LikeSearchPlan:
    """A normalized LIKE-fallback search plan.

    Attributes:
        terms: Normalized lowercase tokens (edge punctuation stripped,
            de-duplicated, in first-seen order).
        where: Parameterized ``LOWER(<column>) LIKE ? ESCAPE '\\'``
            clauses, one per term.
        args: Escaped LIKE pattern arguments (``%term%``), one per term.
    """

    terms: List[str]
    where: List[str]
    args: List[str]


def contains_cjk(text: str) -> bool:
    """Return ``True`` when ``text`` contains any CJK character.

    Used by :meth:`ConversationStore.search_messages` to route through
    the LIKE fallback when the query contains CJK — FTS5 unicode61
    tokenizer cannot index/match CJK reliably (the tokenizer splits on
    Unicode word boundaries that don't align with CJK character
    boundaries).
    """
    return bool(_CJK_RE.search(text))


def _normalize_fallback_term(raw: str) -> str:
    """Trim whitespace + edge punctuation; lowercase.

    Mirrors ``normalizeFallbackTerm`` in TS. Non-string inputs return
    the empty string (TS used a ``typeof !== "string"`` guard).
    """
    if not isinstance(raw, str):
        return ""
    return _EDGE_PUNCT_RE.sub("", raw.strip()).lower()


def _escape_like(term: str) -> str:
    """Escape ``\\``, ``%``, and ``_`` for use with ``LIKE ? ESCAPE '\\'``."""
    return re.sub(r"([\\%_])", r"\\\1", term)


def build_like_search_plan(column: str, query: str) -> LikeSearchPlan:
    """Convert a free-text query into a LIKE-fallback search plan.

    Mirrors ``buildLikeSearchPlan`` in TS. Each whitespace-delimited
    token (or double-quoted phrase) becomes one normalized term; terms
    are de-duplicated in first-seen order.

    Args:
        column: SQL expression for the searchable text column, e.g.
            ``"content"``.
        query: Free-text user query.

    Returns:
        A :class:`LikeSearchPlan` with one entry per unique term.
    """
    terms: List[str] = []
    for match in _RAW_TERM_RE.finditer(query):
        raw = match.group(1) or match.group(2) or ""
        normalized = _normalize_fallback_term(raw)
        if normalized and normalized not in terms:
            terms.append(normalized)

    if not terms:
        fallback = _normalize_fallback_term(query)
        if fallback:
            terms.append(fallback)

    return LikeSearchPlan(
        terms=terms,
        where=[f"LOWER({column}) LIKE ? ESCAPE '\\'" for _ in terms],
        args=[f"%{_escape_like(term)}%" for term in terms],
    )


def create_fallback_snippet(content: str, terms: List[str]) -> str:
    """Build a compact ``...content...`` snippet centered on the earliest match.

    Mirrors ``createFallbackSnippet`` in TS. Strategy:

    1. Find the earliest match for any of the terms in
       ``content.lower()``.
    2. Return ``content[max(0, match - 24) : min(len, match + max(termlen, 1) + 40)]``
       with leading/trailing ``...`` markers when the window doesn't reach
       the edges.
    3. If no term matches, return ``content.strip()`` truncated to 80
       characters with a trailing ``...`` if longer.

    Args:
        content: The full message/summary text.
        terms: Normalized terms from :class:`LikeSearchPlan`.

    Returns:
        A snippet string (UTF-8 safe — no surrogate-pair concerns since
        Python ``str`` indexes code points, not UTF-16 units).
    """
    haystack = content.lower()
    match_index = -1
    match_length = 0

    for term in terms:
        idx = haystack.find(term)
        if idx != -1 and (match_index == -1 or idx < match_index):
            match_index = idx
            match_length = len(term)

    if match_index == -1:
        head = content.strip()
        return head if len(head) <= 80 else f"{head[:77].rstrip()}..."

    start = max(0, match_index - 24)
    end = min(len(content), match_index + max(match_length, 1) + 40)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(content) else ""
    return f"{prefix}{content[start:end].strip()}{suffix}"
