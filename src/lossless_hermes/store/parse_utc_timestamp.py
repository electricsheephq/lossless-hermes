"""Reinterpret SQLite ``datetime('now')`` strings as UTC.

Port of ``lossless-claw/src/store/parse-utc-timestamp.ts`` (LCM commit ``1f07fbd``).

SQLite's ``datetime('now')`` writes timestamps in the form
``YYYY-MM-DD HH:MM:SS`` (space-separated, no trailing ``Z``). The TS code
ran into a subtle bug where Node's ``new Date(value)`` parses such strings
as **local time** rather than UTC — see
https://github.com/Martian-Engineering/lossless-claw/issues/216.

The Python equivalent — :func:`datetime.fromisoformat` — has the same
issue: strings without a trailing ``Z`` or ``+HH:MM`` offset are treated
as naive (no tzinfo). The TS fix detects naive strings and forces UTC by
appending ``Z`` and re-parsing; our port does the same by attaching
:class:`datetime.timezone.utc` after :func:`datetime.fromisoformat`
succeeds on the normalized form.

The function ALWAYS returns an aware :class:`datetime.datetime` in UTC.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

__all__ = ["parse_utc_timestamp", "parse_utc_timestamp_or_null"]

# Detects an explicit timezone tail (``Z`` or ``+HH:MM`` / ``-HH:MM``).
_TZ_TAIL_RE = re.compile(r"(?:[zZ]|[+-]\d{2}:\d{2})$")


def parse_utc_timestamp(value: str) -> datetime:
    """Parse a SQLite UTC timestamp string into an aware :class:`datetime`.

    Mirrors ``parseUtcTimestamp`` in
    ``lossless-claw/src/store/parse-utc-timestamp.ts``: if the input
    carries an explicit ``Z`` / ``+HH:MM`` offset it's parsed directly;
    otherwise the string is treated as a SQLite-emitted local-form
    timestamp and rewritten with a ``T`` separator (if needed) and a
    trailing ``Z`` so :func:`datetime.fromisoformat` produces an aware UTC
    datetime.

    If ``value`` is not a string this returns the Python equivalent of
    JS's ``new Date(NaN)`` — :func:`datetime.fromtimestamp` cannot
    express ``NaN``; we raise :class:`TypeError` instead because callers
    rely on the function returning a valid object.

    Args:
        value: A SQLite timestamp string. Accepted forms:

            * ``"2026-05-13 10:22:42"`` (SQLite ``datetime('now')`` form)
            * ``"2026-05-13T10:22:42"`` (ISO 8601 with ``T`` separator)
            * ``"2026-05-13T10:22:42Z"`` (explicit UTC marker)
            * ``"2026-05-13T10:22:42+00:00"`` (explicit offset)

    Returns:
        An aware :class:`datetime.datetime` in UTC.

    Raises:
        TypeError: ``value`` is not a string.
        ValueError: ``value`` does not match an ISO-8601-ish form that
            Python's :func:`datetime.fromisoformat` accepts.
    """
    if not isinstance(value, str):
        raise TypeError(f"parse_utc_timestamp: expected str, got {type(value).__name__}")
    s = value.strip()
    if _TZ_TAIL_RE.search(s):
        # Replace trailing "Z" with "+00:00" for compatibility with all
        # Python 3.x fromisoformat implementations (3.11+ accepts both).
        if s.endswith(("z", "Z")):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)

    normalized = s if "T" in s else s.replace(" ", "T", 1)
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_utc_timestamp_or_null(value: str | None) -> datetime | None:
    """Parse a nullable SQLite UTC timestamp string.

    Mirrors ``parseUtcTimestampOrNull`` in TS. Returns ``None`` when the
    input is ``None``; otherwise delegates to :func:`parse_utc_timestamp`.
    """
    if value is None:
        return None
    return parse_utc_timestamp(value)
