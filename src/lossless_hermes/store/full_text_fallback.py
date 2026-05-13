"""LIKE-based search plan + snippet builder for the FTS5-unavailable / CJK paths.

Ports ``lossless-claw/src/store/full-text-fallback.ts`` (LCM commit
``1f07fbd``, 84 LOC TS → ~120 LOC Python). Used when:

* FTS5 is not compiled into the host Python build, and
* CJK queries route through the LIKE path even on FTS5-available hosts (FTS5
  unicode61 cannot segment CJK ideographs).

Surface (mirrors TS):

* :func:`contains_cjk` — detect CJK (Unified, Compat, Kana, Hangul).
* :func:`build_like_search_plan` — convert a free-text query into a list of
  normalized terms + ``WHERE`` fragments + bound args.
* :func:`create_fallback_snippet` — center a short window of content around
  the first matching term.

See:

* ``/Volumes/LEXAR/Claude/lossless-claw/src/store/full-text-fallback.ts`` — TS
  canonical (commit ``1f07fbd``).
* ``docs/porting-guides/storage.md`` §4.2 row "full-text-fallback.ts".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = [
    "LikeSearchPlan",
    "build_like_search_plan",
    "contains_cjk",
    "create_fallback_snippet",
]

# Raw token extractor: prefers double-quoted phrases; falls back to whitespace
# tokens. Matches TS ``RAW_TERM_RE = /"([^"]+)"|(\S+)/g``.
_RAW_TERM_RE = re.compile(r'"([^"]+)"|(\S+)')

# CJK detection: covers CJK Unified, Compat, Kana, Hangul. Translated 1:1 from
# TS ``CJK_RE = /[⺀-鿿㐀-䶿豈-﫿가-힯
# ぀-ゟ゠-ヿ]/``. We use explicit ``\u`` escapes here to keep
# the file round-trippable through every editor / encoding pipeline.
_CJK_RE = re.compile("[⺀-鿿㐀-䶿豈-﫿가-힯぀-ゟ゠-ヿ]")

# Edge punctuation stripped during normalization. Same set as TS, including
# the closing-bracket family.
_EDGE_PUNCT_RE = re.compile(
    r"^[`'\"()\[\]{}<>.,:;!?*_+=|\\/\-]+|[`'\"()\[\]{}<>.,:;!?*_+=|\\/\-]+$"
)


@dataclass
class LikeSearchPlan:
    """A LIKE-backed search plan with three parallel arrays.

    Attributes:
        terms: Normalized search tokens. Snippet builder uses these to find
            the earliest match in the content.
        where: Parallel SQL ``WHERE`` fragments (each ``LOWER(<col>) LIKE ?
            ESCAPE '\\'``). Same length as ``args``.
        args: Parallel bound-parameter values (each ``%<escaped>%``). Same
            length as ``where``.
    """

    terms: list[str] = field(default_factory=list)
    where: list[str] = field(default_factory=list)
    args: list[str] = field(default_factory=list)


def contains_cjk(text: str) -> bool:
    """Detect whether ``text`` contains CJK characters.

    Covers CJK Unified Ideographs (``\\u2E80-\\u9FFF``), CJK Extension A
    (``\\u3400-\\u4DBF``), CJK Compatibility Ideographs (``\\uF900-\\uFAFF``),
    Hangul Syllables (``\\uAC00-\\uD7AF``), Hiragana (``\\u3040-\\u309F``), and
    Katakana (``\\u30A0-\\u30FF``).

    Args:
        text: Any string. Empty string returns ``False``.

    Returns:
        ``True`` if any CJK character is present; ``False`` otherwise.
    """
    return bool(_CJK_RE.search(text))


def _normalize_fallback_term(raw: str) -> str:
    """Strip edge punctuation, trim, lowercase."""
    if not isinstance(raw, str):
        return ""
    return _EDGE_PUNCT_RE.sub("", raw.strip()).lower()


def _escape_like(term: str) -> str:
    """Escape ``\\``, ``%``, ``_`` for SQLite ``LIKE ... ESCAPE '\\'``.

    Args:
        term: A search token.

    Returns:
        The token with backslashes, percents, and underscores prefixed by
        ``\\``.
    """
    out_chars: list[str] = []
    for ch in term:
        if ch in ("\\", "%", "_"):
            out_chars.append("\\")
        out_chars.append(ch)
    return "".join(out_chars)


def build_like_search_plan(column: str, query: str) -> LikeSearchPlan:
    """Convert a free-text query into a conservative LIKE search plan.

    The fallback keeps phrase tokens when the query uses double quotes, and
    otherwise searches for all normalized tokens as case-insensitive
    substrings. All terms must match (implicit AND).

    Args:
        column: Column expression (without alias) to search against — e.g.
            ``"content"``. Caller is responsible for any required aliasing.
        query: The user query. Quoted phrases (e.g. ``"foo bar"``) become
            single phrase tokens; everything else splits on whitespace.

    Returns:
        A :class:`LikeSearchPlan` with empty fields when the query has no
        usable terms (caller should short-circuit to an empty result).
    """
    terms: list[str] = []
    for match in _RAW_TERM_RE.finditer(query):
        raw = match.group(1) if match.group(1) is not None else match.group(2)
        if raw is None:
            continue
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


def create_fallback_snippet(content: str, terms: list[str]) -> str:
    """Build a short snippet centered on the earliest term hit.

    When no term matches, returns the first 80 characters of ``content``
    (with ellipsis when truncated). When a term matches, returns a window
    that spans 24 chars before to ``max(term_len, 1) + 40`` chars after the
    earliest hit, prefixed/suffixed with ``"..."`` if cut off at either end.

    Args:
        content: Raw content text.
        terms: Normalized search terms (lowercase). Used as substring needles
            against a lower-cased haystack for case-insensitive matching.

    Returns:
        A snippet string. Always non-empty when ``content`` is non-empty
        (matches TS — even an empty terms list falls through to the
        head-of-content path).
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
        return head if len(head) <= 80 else head[:77].rstrip() + "..."

    start = max(0, match_index - 24)
    end = min(len(content), match_index + max(match_length, 1) + 40)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(content) else ""
    return f"{prefix}{content[start:end].strip()}{suffix}"
