"""Sanitize user-provided queries for use with FTS5 MATCH.

Port of ``lossless-claw/src/store/fts5-sanitize.ts`` (LCM commit
``1f07fbd``, 50 LOC TS → Python).

FTS5 treats certain characters as operators:

* ``-`` (NOT), ``+`` (required), ``*`` (prefix), ``^`` (initial token)
* ``OR``, ``AND``, ``NOT`` (boolean operators)
* ``:`` (column filter — e.g. ``agent:foo`` means "search column agent")
* ``"`` (phrase query), ``(`` ``)`` (grouping)
* ``NEAR`` (proximity)

If a query contains any of these characters, the naive MATCH path
either errors ("no such column") or returns unexpected results.

Strategy: wrap each whitespace-delimited token in double quotes so FTS5
treats it as a literal phrase token. Internal double quotes are stripped.
Empty tokens are dropped. Tokens are joined with spaces (implicit AND).

Examples:
    ``"sub-agent restrict"``  → ``'"sub-agent" "restrict"'``
    ``"lcm_expand OR crash"`` → ``'"lcm_expand" "OR" "crash"'``
    ``'hello "world"'``       → ``'"hello" "world"'``
"""

from __future__ import annotations

import re
from typing import List

__all__ = ["sanitize_fts5_query"]

# Match a double-quoted phrase: '"..."'
_PHRASE_RE = re.compile(r'"([^"]+)"')

# Match runs of whitespace (used for tokenizing the non-quoted gaps).
_WHITESPACE_RE = re.compile(r"\s+")


def sanitize_fts5_query(raw: str) -> str:
    """Wrap each token in double quotes so FTS5 operators don't fire.

    Preserves user-quoted phrases: extracts ``"..."`` groups first, then
    tokenizes the rest. Internal double quotes in any token are stripped.

    Returns ``'""'`` (a literal empty phrase) when the input produces no
    tokens — keeps the MATCH expression valid rather than letting an
    empty MATCH raise.

    Args:
        raw: Free-text user query.

    Returns:
        A space-separated string of double-quoted tokens suitable for
        embedding in an FTS5 ``MATCH ?`` parameter.
    """
    parts: List[str] = []
    last_index = 0

    for match in _PHRASE_RE.finditer(raw):
        # Process unquoted text before this phrase.
        before = raw[last_index : match.start()]
        for token in _WHITESPACE_RE.split(before):
            if not token:
                continue
            cleaned = token.replace('"', "")
            parts.append(f'"{cleaned}"')
        # Preserve the phrase as-is (strip internal quotes for safety).
        phrase = match.group(1).replace('"', "").strip()
        if phrase:
            parts.append(f'"{phrase}"')
        last_index = match.end()

    # Process unquoted text after the last phrase.
    for token in _WHITESPACE_RE.split(raw[last_index:]):
        if not token:
            continue
        cleaned = token.replace('"', "")
        parts.append(f'"{cleaned}"')

    return " ".join(parts) if parts else '""'
