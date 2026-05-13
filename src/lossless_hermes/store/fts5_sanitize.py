"""Sanitize a free-text query for use as an FTS5 ``MATCH`` expression.

Ports ``lossless-claw/src/store/fts5-sanitize.ts`` (LCM commit ``1f07fbd``,
50 LOC TS → ~70 LOC Python). FTS5 treats certain characters as operators —
``-`` (NOT), ``+`` (required), ``*`` (prefix), ``^`` (initial token), ``OR``,
``AND``, ``NOT``, ``:`` (column filter), ``"`` (phrase), ``(`` ``)`` (group),
``NEAR`` (proximity). A naive user query containing any of these either
errors (``no such column``) or returns unexpected results.

Strategy (mirrors TS): wrap each whitespace-delimited token in double quotes
so FTS5 treats it as a literal phrase. Tokens are joined with spaces
(implicit AND). User-quoted phrases (``"foo bar"``) are preserved as multi-
word phrase tokens.

See:

* ``/Volumes/LEXAR/Claude/lossless-claw/src/store/fts5-sanitize.ts`` — TS
  canonical (commit ``1f07fbd``).
* ``docs/porting-guides/storage.md`` §4.2 row "fts5-sanitize.ts".
"""

from __future__ import annotations

import re

__all__ = ["sanitize_fts5_query"]

_PHRASE_RE = re.compile(r'"([^"]+)"')
_WHITESPACE_RE = re.compile(r"\s+")


def sanitize_fts5_query(raw: str) -> str:
    """Wrap each token in double quotes so FTS5 treats it as a literal phrase.

    Examples:
        ``"sub-agent restrict"``  → ``'"sub-agent" "restrict"'``
        ``"lcm_expand OR crash"`` → ``'"lcm_expand" "OR" "crash"'``
        ``'hello "world"'``       → ``'"hello" "world"'``
        ``""``                    → ``'""'`` (empty query → empty quote pair)

    The empty-input fallback (``'""'``) matches the TS source — FTS5 accepts
    an empty phrase token and returns zero hits; the alternative (passing an
    empty string) raises ``no such column: ""``.

    Args:
        raw: User-typed query string. May contain FTS5 operators, quoted
            phrases, unicode, or be empty.

    Returns:
        A sanitized FTS5 ``MATCH``-compatible expression. Always a non-empty
        string.
    """
    parts: list[str] = []
    last_index = 0

    # Preserve user-quoted phrases: extract "..." groups first, then tokenize the rest.
    for match in _PHRASE_RE.finditer(raw):
        # Process unquoted text before this phrase
        before = raw[last_index : match.start()]
        for token in _WHITESPACE_RE.split(before):
            if token:
                # Strip any embedded quotes for safety before re-wrapping.
                parts.append(f'"{token.replace(chr(34), "")}"')
        # Preserve the phrase as-is (strip internal quotes for safety)
        phrase = match.group(1).replace('"', "").strip()
        if phrase:
            parts.append(f'"{phrase}"')
        last_index = match.end()

    # Process unquoted text after last phrase
    tail = raw[last_index:]
    for token in _WHITESPACE_RE.split(tail):
        if token:
            parts.append(f'"{token.replace(chr(34), "")}"')

    return " ".join(parts) if parts else '""'
