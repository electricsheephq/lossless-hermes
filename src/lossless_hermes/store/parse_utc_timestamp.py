"""Reinterpret SQLite ``datetime('now')`` strings as UTC.

Ports ``lossless-claw/src/store/parse-utc-timestamp.ts`` (LCM commit
``1f07fbd``, 26 LOC TS → ~30 LOC Python). SQLite stores ``datetime('now')``
results without a ``Z`` suffix (e.g. ``"2026-01-15 12:34:56"``), and a naive
``datetime.fromisoformat`` parse would treat the string as host-local time —
shifting timestamps by the host's UTC offset on every read. This module's
single job is to coerce SQLite naive UTC strings into Python aware
``datetime`` objects.

The TS reference uses the ``Date`` constructor; the Python port returns a
:class:`datetime.datetime` with ``tzinfo=timezone.utc`` set.

See:

* ``/Volumes/LEXAR/Claude/lossless-claw/src/store/parse-utc-timestamp.ts`` —
  TS canonical (commit ``1f07fbd``).
* https://github.com/Martian-Engineering/lossless-claw/issues/216 — original
  bug report (Asia/Shanghai users saw 8 h offset on all timestamps).
* ``docs/porting-guides/storage.md`` §4.2 row "parse-utc-timestamp.ts".
* ``epics/01-storage/01-09-summary-store.md`` — this module is one of the
  Phase-0 leaves that 01-09 ports inline.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

__all__ = ["parse_utc_timestamp", "parse_utc_timestamp_or_null"]

# Matches a trailing ``Z`` / ``+HH:MM`` / ``-HH:MM`` — i.e. an explicit
# timezone offset that means we should NOT default-apply ``Z``.
_TZ_SUFFIX_RE = re.compile(r"(?:[zZ]|[+-]\d{2}:\d{2})$")


def parse_utc_timestamp(value: str) -> datetime:
    """Parse a SQLite UTC timestamp string into an aware UTC :class:`datetime`.

    Handles the two SQLite output shapes:

    * ``"2026-01-15 12:34:56"`` (datetime('now') default) — no ``T``, no ``Z``.
    * ``"2026-01-15T12:34:56"`` (round-tripped through a ``Date.toISOString()``
      in the TS source then re-stored).

    If the input already carries a timezone (``Z`` or ``+HH:MM``), it is parsed
    as-is. Otherwise we append ``Z`` so the resulting ``datetime`` is aware.

    Args:
        value: SQLite-formatted timestamp string. A non-string input (None,
            int, float) yields ``datetime.fromisoformat("NaN")`` equivalent —
            but in practice the caller should branch on the column being null
            before calling this; see :func:`parse_utc_timestamp_or_null`.

    Returns:
        A timezone-aware :class:`datetime` in UTC.

    Raises:
        ValueError: When ``value`` cannot be parsed by
            :meth:`datetime.fromisoformat`. The TS source returns
            ``new Date(NaN)`` in that case; Python's stricter parser raises,
            which is the better failure mode (it surfaces the bug instead of
            silently producing an invalid Date).
    """
    if not isinstance(value, str):
        # TS returns `new Date(NaN)`; Python's stricter approach: raise.
        raise ValueError(f"parse_utc_timestamp: expected str, got {type(value).__name__}")

    s = value.strip()
    if _TZ_SUFFIX_RE.search(s):
        # Already has a tz suffix — parse directly.
        # Python's fromisoformat in 3.11+ handles 'Z' suffix natively.
        return datetime.fromisoformat(s.replace("Z", "+00:00"))

    # No tz suffix — normalize ``'YYYY-MM-DD HH:MM:SS'`` to ISO 8601 and add
    # ``+00:00`` so the resulting datetime is aware-UTC.
    normalized = s if "T" in s else s.replace(" ", "T")
    return datetime.fromisoformat(f"{normalized}+00:00")


def parse_utc_timestamp_or_null(value: str | None) -> datetime | None:
    """Parse a nullable SQLite UTC timestamp string.

    Returns ``None`` for null/empty inputs; otherwise delegates to
    :func:`parse_utc_timestamp`.

    Args:
        value: SQLite column value (``None`` for SQL ``NULL``, else string).

    Returns:
        Timezone-aware UTC :class:`datetime` or ``None``.
    """
    if value is None:
        return None
    return parse_utc_timestamp(value)
