"""Tests for :mod:`lossless_hermes.tools._period_parser` — period shortcut parser.

Mirrors ``lossless-claw/test/v41-period-timezone.test.ts`` 1:1. Covers:

* Wave-10 reviewer P1: local-timezone day boundaries (today / yesterday /
  this-week / last-week / this-month / last-month) honour the operator's
  local clock rather than UTC.
* Wave-11 reviewer P1: half-hour offsets (Asia/Kolkata UTC+5:30,
  Asia/Kathmandu UTC+5:45) work without special-casing; DST transition
  days (spring-forward 23h, fall-back 25h) compute the correct duration.
* Wave-12 P2: table-driven timezone × period × edge-case matrix.
* Wave-7 Auditor #6 P1: ``last-Nd`` regex rejects undocumented variants.
* Wave-12 P2: round-trip invariants (yesterday.before == today.since;
  today duration ∈ [22h, 26h] for any timezone).
* Helpful errors for unrecognized period strings.

Source pin: ``lossless-claw`` at commit ``1f07fbd`` on branch ``pr-613``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from lossless_hermes.tools._period_parser import (
    PeriodParseError,
    get_local_day_duration_ms,
    get_local_day_start_utc,
    parse_period_shortcut,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _date_utc_ms(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> int:
    """Compute a UTC ms-since-epoch (Python equivalent of ``Date.UTC(...)``)."""
    return int(datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp() * 1000)


def _iso(dt: datetime) -> str:
    """Format a UTC datetime as the TS ``.toISOString()`` shape."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# ===========================================================================
# Wave-10 reviewer P1: local-timezone day boundaries
# ===========================================================================


# Anchor: 2026-05-07T02:00:00 in Bangkok = 2026-05-06T19:00:00 UTC.
_BKK_NOW_MS = _date_utc_ms(2026, 5, 6, 19, 0)
# 2026-05-07T23:00:00 in LA (UTC-7 PDT) = 2026-05-08T06:00:00 UTC.
_LA_NOW_MS = _date_utc_ms(2026, 5, 8, 6, 0)


class TestLocalTimezoneDayBoundaries:
    """Wave-10 P1: day-boundary periods honour operator's local timezone."""

    def test_bangkok_yesterday_is_local_yesterday(self) -> None:
        """Bangkok 'yesterday' returns local-yesterday (2026-05-06), NOT UTC-yesterday."""
        r = parse_period_shortcut("yesterday", now_ms=_BKK_NOW_MS, timezone_name="Asia/Bangkok")
        # Bangkok 2026-05-06 00:00 BKK = 2026-05-05T17:00 UTC.
        # Bangkok 2026-05-07 00:00 BKK = 2026-05-06T17:00 UTC.
        assert _iso(r.since) == "2026-05-05T17:00:00.000Z"
        assert _iso(r.before) == "2026-05-06T17:00:00.000Z"
        assert r.label == "yesterday"

    def test_bangkok_today_is_local_today(self) -> None:
        r = parse_period_shortcut("today", now_ms=_BKK_NOW_MS, timezone_name="Asia/Bangkok")
        assert _iso(r.since) == "2026-05-06T17:00:00.000Z"
        assert _iso(r.before) == "2026-05-07T17:00:00.000Z"

    def test_la_yesterday_at_late_evening_is_la_yesterday(self) -> None:
        """LA at 23:00 PDT: yesterday is LA-yesterday, not UTC-yesterday."""
        r = parse_period_shortcut(
            "yesterday", now_ms=_LA_NOW_MS, timezone_name="America/Los_Angeles"
        )
        # LA 2026-05-06 00:00 PDT = 2026-05-06T07:00 UTC.
        # LA 2026-05-07 00:00 PDT = 2026-05-07T07:00 UTC.
        assert _iso(r.since) == "2026-05-06T07:00:00.000Z"
        assert _iso(r.before) == "2026-05-07T07:00:00.000Z"
        assert r.label == "yesterday"

    def test_utc_yesterday_returns_utc_yesterday(self) -> None:
        """UTC control case: timezone='UTC' gives UTC-anchored boundaries."""
        r = parse_period_shortcut("yesterday", now_ms=_BKK_NOW_MS, timezone_name="UTC")
        assert _iso(r.since) == "2026-05-05T00:00:00.000Z"
        assert _iso(r.before) == "2026-05-06T00:00:00.000Z"

    def test_last_7_days_is_timezone_independent(self) -> None:
        """``last-7-days`` is now-anchored; timezone is irrelevant."""
        r_utc = parse_period_shortcut("last-7-days", now_ms=_BKK_NOW_MS, timezone_name="UTC")
        r_bkk = parse_period_shortcut(
            "last-7-days", now_ms=_BKK_NOW_MS, timezone_name="Asia/Bangkok"
        )
        assert _iso(r_utc.since) == _iso(r_bkk.since)
        assert _iso(r_utc.before) == _iso(r_bkk.before)

    def test_last_12h_is_timezone_independent(self) -> None:
        """``last-12h`` is now-anchored; timezone is irrelevant."""
        r = parse_period_shortcut("last-12h", now_ms=_BKK_NOW_MS, timezone_name="Asia/Bangkok")
        assert _iso(r.before) == "2026-05-06T19:00:00.000Z"
        assert _iso(r.since) == "2026-05-06T07:00:00.000Z"

    def test_this_month_uses_local_month_boundaries(self) -> None:
        """Bangkok 'this-month' returns LOCAL month boundaries, not UTC."""
        # Bangkok 2026-05-01 00:01 BKK = 2026-04-30T17:01 UTC.
        just_after_month_start_bkk = _date_utc_ms(2026, 4, 30, 17, 1)
        r = parse_period_shortcut(
            "this-month",
            now_ms=just_after_month_start_bkk,
            timezone_name="Asia/Bangkok",
        )
        # Bangkok May 2026 starts at Bangkok 2026-05-01 00:00 = 2026-04-30T17:00 UTC.
        assert _iso(r.since) == "2026-04-30T17:00:00.000Z"
        # Bangkok June 2026 starts at Bangkok 2026-06-01 00:00 = 2026-05-31T17:00 UTC.
        assert _iso(r.before) == "2026-05-31T17:00:00.000Z"

    def test_invalid_timezone_falls_back_to_utc(self) -> None:
        """An invalid IANA timezone string falls back to UTC silently."""
        r = parse_period_shortcut("yesterday", now_ms=_BKK_NOW_MS, timezone_name="Not/A/Timezone")
        # Should fall back to UTC behavior.
        assert _iso(r.since) == "2026-05-05T00:00:00.000Z"
        assert _iso(r.before) == "2026-05-06T00:00:00.000Z"


# ===========================================================================
# Wave-11 reviewer P1: fractional-offset + DST robustness
# ===========================================================================


class TestFractionalOffsetAndDst:
    """Wave-11 P1: half-hour offsets and DST transition days."""

    def test_kolkata_yesterday_half_hour_offset(self) -> None:
        """Asia/Kolkata (UTC+5:30) yesterday returns local-yesterday boundaries."""
        # 2026-05-07 02:00 IST = 2026-05-06 20:30 UTC.
        r = parse_period_shortcut(
            "yesterday",
            now_ms=_date_utc_ms(2026, 5, 6, 20, 30),
            timezone_name="Asia/Kolkata",
        )
        # Kolkata 2026-05-06 00:00 IST = 2026-05-05 18:30 UTC.
        # Kolkata 2026-05-07 00:00 IST = 2026-05-06 18:30 UTC.
        assert _iso(r.since) == "2026-05-05T18:30:00.000Z"
        assert _iso(r.before) == "2026-05-06T18:30:00.000Z"

    def test_kathmandu_today_quarter_hour_offset(self) -> None:
        """Asia/Kathmandu (UTC+5:45) handles 15-minute offsets."""
        # 2026-05-07 06:00 NPT = 2026-05-07 00:15 UTC.
        r = parse_period_shortcut(
            "today",
            now_ms=_date_utc_ms(2026, 5, 7, 0, 15),
            timezone_name="Asia/Kathmandu",
        )
        # Kathmandu 2026-05-07 00:00 NPT = 2026-05-06 18:15 UTC.
        assert _iso(r.since) == "2026-05-06T18:15:00.000Z"
        # "today" duration in Kathmandu (no DST) is exactly 24h.
        assert _iso(r.before) == "2026-05-07T18:15:00.000Z"

    def test_la_spring_forward_today_is_23h(self) -> None:
        """LA spring-forward day: today's duration is 23h."""
        # US DST 2026 starts March 8 02:00 PST → 03:00 PDT.
        # 2026-03-08 12:00 LA local = 2026-03-08 19:00 UTC (post-spring).
        r = parse_period_shortcut(
            "today",
            now_ms=_date_utc_ms(2026, 3, 8, 19, 0),
            timezone_name="America/Los_Angeles",
        )
        # LA 2026-03-08 00:00 PST = 2026-03-08 08:00 UTC.
        assert _iso(r.since) == "2026-03-08T08:00:00.000Z"
        # LA 2026-03-09 00:00 PDT = 2026-03-09 07:00 UTC.
        assert _iso(r.before) == "2026-03-09T07:00:00.000Z"
        # Duration = 23h.
        dur_ms = int((r.before.timestamp() - r.since.timestamp()) * 1000)
        assert dur_ms == 23 * 60 * 60 * 1000


# ===========================================================================
# Wave-12: table-driven timezone matrix
# ===========================================================================


_TIMEZONE_MATRIX: list[dict[str, object]] = [
    # Integer offsets (positive)
    {
        "tz": "Asia/Bangkok",
        "description": "+7 fixed (no DST)",
        "now_utc_ms": _date_utc_ms(2026, 5, 6, 19, 0),
        "local_date": "2026-05-07",
        "expected_yesterday_since_utc": "2026-05-05T17:00:00.000Z",
        "expected_yesterday_before_utc": "2026-05-06T17:00:00.000Z",
    },
    {
        "tz": "Asia/Tokyo",
        "description": "+9 fixed",
        "now_utc_ms": _date_utc_ms(2026, 5, 6, 17, 0),
        "local_date": "2026-05-07",
        "expected_yesterday_since_utc": "2026-05-05T15:00:00.000Z",
        "expected_yesterday_before_utc": "2026-05-06T15:00:00.000Z",
    },
    {
        "tz": "Pacific/Auckland",
        "description": "+12 at May NZST",
        "now_utc_ms": _date_utc_ms(2026, 5, 6, 14, 0),
        "local_date": "2026-05-07",
        "expected_yesterday_since_utc": "2026-05-05T12:00:00.000Z",
        "expected_yesterday_before_utc": "2026-05-06T12:00:00.000Z",
    },
    # Integer offsets (negative)
    {
        "tz": "America/Los_Angeles",
        "description": "-7 (PDT)",
        "now_utc_ms": _date_utc_ms(2026, 5, 7, 9, 0),
        "local_date": "2026-05-07",
        "expected_yesterday_since_utc": "2026-05-06T07:00:00.000Z",
        "expected_yesterday_before_utc": "2026-05-07T07:00:00.000Z",
    },
    {
        "tz": "America/New_York",
        "description": "-4 (EDT)",
        "now_utc_ms": _date_utc_ms(2026, 5, 7, 6, 0),
        "local_date": "2026-05-07",
        "expected_yesterday_since_utc": "2026-05-06T04:00:00.000Z",
        "expected_yesterday_before_utc": "2026-05-07T04:00:00.000Z",
    },
    # Half-hour offset
    {
        "tz": "Asia/Kolkata",
        "description": "+5:30 (no DST)",
        "now_utc_ms": _date_utc_ms(2026, 5, 6, 20, 30),
        "local_date": "2026-05-07",
        "expected_yesterday_since_utc": "2026-05-05T18:30:00.000Z",
        "expected_yesterday_before_utc": "2026-05-06T18:30:00.000Z",
    },
    # Quarter-hour offset
    {
        "tz": "Asia/Kathmandu",
        "description": "+5:45 (no DST)",
        "now_utc_ms": _date_utc_ms(2026, 5, 6, 20, 15),
        "local_date": "2026-05-07",
        "expected_yesterday_since_utc": "2026-05-05T18:15:00.000Z",
        "expected_yesterday_before_utc": "2026-05-06T18:15:00.000Z",
    },
    # UTC control
    {
        "tz": "UTC",
        "description": "+0 (control case)",
        "now_utc_ms": _date_utc_ms(2026, 5, 7, 2, 0),
        "local_date": "2026-05-07",
        "expected_yesterday_since_utc": "2026-05-06T00:00:00.000Z",
        "expected_yesterday_before_utc": "2026-05-07T00:00:00.000Z",
    },
]


class TestTimezoneMatrix:
    """Wave-12: table-driven matrix of timezone × period × edge cases."""

    @pytest.mark.parametrize("case", _TIMEZONE_MATRIX, ids=lambda c: f"{c['tz']}")
    def test_yesterday_boundaries(self, case: dict[str, object]) -> None:
        """Each timezone's 'yesterday' produces correct UTC bounds."""
        r = parse_period_shortcut(
            "yesterday",
            now_ms=case["now_utc_ms"],  # type: ignore[arg-type]
            timezone_name=case["tz"],  # type: ignore[arg-type]
        )
        assert _iso(r.since) == case["expected_yesterday_since_utc"]
        assert _iso(r.before) == case["expected_yesterday_before_utc"]

    def test_yesterday_before_equals_today_since_invariant(self) -> None:
        """Round-trip invariant: every entry's yesterday.before == today.since."""
        for case in _TIMEZONE_MATRIX:
            now_ms = case["now_utc_ms"]
            tz = case["tz"]
            yesterday = parse_period_shortcut(
                "yesterday",
                now_ms=now_ms,
                timezone_name=tz,  # type: ignore[arg-type]
            )
            today = parse_period_shortcut(
                "today",
                now_ms=now_ms,
                timezone_name=tz,  # type: ignore[arg-type]
            )
            assert _iso(yesterday.before) == _iso(today.since), f"mismatch in {tz}"

    def test_today_duration_within_dst_bounds(self) -> None:
        """Today's duration must be in [22h, 26h] for any timezone (catches DST off-by-error)."""
        for case in _TIMEZONE_MATRIX:
            r = parse_period_shortcut(
                "today",
                now_ms=case["now_utc_ms"],  # type: ignore[arg-type]
                timezone_name=case["tz"],  # type: ignore[arg-type]
            )
            duration_hrs = (r.before.timestamp() - r.since.timestamp()) / 3600
            assert 22 <= duration_hrs <= 26, (
                f"{case['tz']} duration {duration_hrs}h outside [22, 26]"
            )


# ===========================================================================
# last-Nh / last-Nd parametric forms — clamping + accepted shapes
# ===========================================================================


class TestParametricForms:
    """``last-Nh`` and ``last-Nd`` parametric forms."""

    def test_last_24h_24_hours_back(self) -> None:
        r = parse_period_shortcut(
            "last-24h", now_ms=_date_utc_ms(2026, 5, 7, 12, 0), timezone_name="UTC"
        )
        assert _iso(r.before) == "2026-05-07T12:00:00.000Z"
        assert _iso(r.since) == "2026-05-06T12:00:00.000Z"
        assert r.label == "last-24h"

    def test_last_3d_3_days_back(self) -> None:
        r = parse_period_shortcut(
            "last-3d", now_ms=_date_utc_ms(2026, 5, 7, 12, 0), timezone_name="UTC"
        )
        assert _iso(r.before) == "2026-05-07T12:00:00.000Z"
        assert _iso(r.since) == "2026-05-04T12:00:00.000Z"
        assert r.label == "last-3d"

    def test_last_7_days_long_form(self) -> None:
        r = parse_period_shortcut(
            "last-7-days", now_ms=_date_utc_ms(2026, 5, 7, 12, 0), timezone_name="UTC"
        )
        # 7 days = 168 hours.
        dur_hrs = (r.before.timestamp() - r.since.timestamp()) / 3600
        assert dur_hrs == 168
        assert r.label == "last-7d"

    def test_last_30_days_long_form(self) -> None:
        r = parse_period_shortcut(
            "last-30-days", now_ms=_date_utc_ms(2026, 5, 7, 12, 0), timezone_name="UTC"
        )
        assert r.label == "last-30d"

    def test_last_Nh_clamp_lower(self) -> None:
        """``last-0h`` clamps to ``last-1h``."""
        r = parse_period_shortcut(
            "last-0h", now_ms=_date_utc_ms(2026, 5, 7, 12, 0), timezone_name="UTC"
        )
        # 1 hour minimum.
        dur_hrs = (r.before.timestamp() - r.since.timestamp()) / 3600
        assert dur_hrs == 1
        assert r.label == "last-1h"

    def test_last_Nh_clamp_upper(self) -> None:
        """``last-99999h`` clamps to 24*90 = 2160h."""
        r = parse_period_shortcut(
            "last-99999h", now_ms=_date_utc_ms(2026, 5, 7, 12, 0), timezone_name="UTC"
        )
        dur_hrs = (r.before.timestamp() - r.since.timestamp()) / 3600
        assert dur_hrs == 24 * 90
        assert r.label == "last-2160h"

    def test_last_Nd_clamp_upper(self) -> None:
        """``last-999d`` clamps to 366d (leap-year max)."""
        r = parse_period_shortcut(
            "last-999d", now_ms=_date_utc_ms(2026, 5, 7, 12, 0), timezone_name="UTC"
        )
        dur_days = (r.before.timestamp() - r.since.timestamp()) / 86400
        assert dur_days == 366
        assert r.label == "last-366d"

    def test_case_insensitive(self) -> None:
        """Period strings are case-insensitive after trim."""
        r1 = parse_period_shortcut(
            "YESTERDAY", now_ms=_date_utc_ms(2026, 5, 7, 12, 0), timezone_name="UTC"
        )
        r2 = parse_period_shortcut(
            "yesterday", now_ms=_date_utc_ms(2026, 5, 7, 12, 0), timezone_name="UTC"
        )
        assert _iso(r1.since) == _iso(r2.since)


# ===========================================================================
# Wave-7 P1: regex tightening — undocumented variants are rejected
# ===========================================================================


class TestUndocumentedVariantsRejected:
    """Wave-7 P1: regex tightened to only accept documented forms."""

    @pytest.mark.parametrize(
        "bad_form",
        [
            "last-3day",
            "last-3-d",
            "last-3-day",
            "last-3days",
            "lasth-3",
        ],
    )
    def test_undocumented_variant_raises(self, bad_form: str) -> None:
        """Wave-7 P1 fix: undocumented variants are rejected."""
        with pytest.raises(PeriodParseError) as excinfo:
            parse_period_shortcut(bad_form, now_ms=_date_utc_ms(2026, 5, 7), timezone_name="UTC")
        # The error message lists the accepted forms.
        assert "Accepted:" in str(excinfo.value)
        assert "today" in str(excinfo.value)


# ===========================================================================
# Error message shape
# ===========================================================================


class TestErrors:
    """Error message shapes for unrecognized input."""

    def test_unknown_period_raises(self) -> None:
        with pytest.raises(PeriodParseError) as excinfo:
            parse_period_shortcut(
                "next-tuesday", now_ms=_date_utc_ms(2026, 5, 7), timezone_name="UTC"
            )
        msg = str(excinfo.value)
        assert "Unrecognized period shortcut" in msg
        assert "next-tuesday" in msg
        # Lists accepted forms.
        assert "today" in msg
        assert "yesterday" in msg
        assert "this-week" in msg
        assert "last-Nh" in msg

    def test_empty_string_raises(self) -> None:
        with pytest.raises(PeriodParseError):
            parse_period_shortcut("", now_ms=_date_utc_ms(2026, 5, 7), timezone_name="UTC")

    def test_whitespace_only_raises(self) -> None:
        with pytest.raises(PeriodParseError):
            parse_period_shortcut("   \t", now_ms=_date_utc_ms(2026, 5, 7), timezone_name="UTC")


# ===========================================================================
# Direct tests for get_local_day_start_utc / get_local_day_duration_ms
# ===========================================================================


class TestDayStartHelpers:
    """Standalone tests for the day-start / duration helpers."""

    def test_day_start_utc_anchors_to_local_midnight(self) -> None:
        """Bangkok 02:00 local → local midnight at 17:00 UTC the prior day."""
        at = datetime(2026, 5, 6, 19, 0, tzinfo=timezone.utc)  # = 2026-05-07 02:00 BKK
        result = get_local_day_start_utc(at, "Asia/Bangkok")
        # Bangkok 2026-05-07 00:00 = 2026-05-06 17:00 UTC.
        assert _iso(result) == "2026-05-06T17:00:00.000Z"

    def test_day_start_handles_half_hour_offset(self) -> None:
        """Asia/Kolkata (UTC+5:30) day start preserves minute offset."""
        at = datetime(2026, 5, 6, 20, 30, tzinfo=timezone.utc)  # = 2026-05-07 02:00 IST
        result = get_local_day_start_utc(at, "Asia/Kolkata")
        # Kolkata 2026-05-07 00:00 IST = 2026-05-06 18:30 UTC.
        assert _iso(result) == "2026-05-06T18:30:00.000Z"

    def test_day_duration_24h_no_dst(self) -> None:
        """Non-DST day duration is exactly 24h."""
        start = datetime(2026, 5, 1, 17, 0, tzinfo=timezone.utc)  # = 2026-05-02 00:00 BKK
        dur = get_local_day_duration_ms(start, "Asia/Bangkok")
        assert dur == 24 * 60 * 60 * 1000

    def test_day_duration_23h_spring_forward(self) -> None:
        """Spring-forward day in LA is 23h."""
        # 2026-03-08 00:00 PST = 2026-03-08 08:00 UTC.
        start = datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc)
        dur = get_local_day_duration_ms(start, "America/Los_Angeles")
        assert dur == 23 * 60 * 60 * 1000
