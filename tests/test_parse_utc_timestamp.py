"""Tests for :mod:`lossless_hermes.store.parse_utc_timestamp`.

Ports the cases from ``lossless-claw/test/parse-utc-timestamp.test.ts``
(LCM commit ``1f07fbd``). The TS suite uses ``toISOString()`` comparisons;
we use :meth:`datetime.datetime.isoformat` and explicit-UTC checks.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from lossless_hermes.store.parse_utc_timestamp import (
    parse_utc_timestamp,
    parse_utc_timestamp_or_null,
)


def test_treats_bare_sqlite_timestamps_as_utc() -> None:
    """A bare SQLite ``datetime('now')`` form is treated as UTC."""
    parsed = parse_utc_timestamp("2026-03-30 23:11:15")
    assert parsed == datetime(2026, 3, 30, 23, 11, 15, tzinfo=timezone.utc)


def test_preserves_explicit_utc_suffix() -> None:
    """``...Z`` suffix is treated as UTC."""
    parsed = parse_utc_timestamp("2026-03-30T23:11:15Z")
    assert parsed == datetime(2026, 3, 30, 23, 11, 15, tzinfo=timezone.utc)


def test_preserves_explicit_timezone_offset() -> None:
    """``...+02:00`` offset is preserved (and converts to equivalent UTC)."""
    parsed = parse_utc_timestamp("2026-03-30T23:11:15+02:00")
    # The result is an aware datetime at the explicit offset.
    assert parsed.tzinfo is not None
    # Equivalent UTC: 21:11:15.
    assert parsed.astimezone(timezone.utc) == datetime(2026, 3, 30, 21, 11, 15, tzinfo=timezone.utc)


def test_non_string_raises_type_error() -> None:
    """A non-string input raises :class:`TypeError`.

    TS returned ``new Date(NaN)``; the Python port raises instead because
    callers rely on the function returning a valid datetime.
    """
    with pytest.raises(TypeError):
        parse_utc_timestamp(123)  # type: ignore[arg-type]


def test_parse_utc_timestamp_or_null_for_none() -> None:
    """``None`` input returns ``None``."""
    assert parse_utc_timestamp_or_null(None) is None


def test_parse_utc_timestamp_or_null_for_value() -> None:
    """A non-None input is delegated to :func:`parse_utc_timestamp`."""
    parsed = parse_utc_timestamp_or_null("2026-03-30 23:11:15")
    assert parsed == datetime(2026, 3, 30, 23, 11, 15, tzinfo=timezone.utc)
