"""Timezone-aware period-shortcut parser for ``lcm_synthesize_around``.

Port of the ``parsePeriodShortcut`` function exposed from
``lossless-claw/src/tools/lcm-synthesize-around-tool.ts`` (LCM commit
``1f07fbd`` on branch ``pr-613``, lines 174-440). The parser is a
self-contained string → ``(since, before, label)`` function: NO database
access, NO I/O — just date math against the stdlib :mod:`zoneinfo`
provider.

The same module also exposes :func:`get_local_day_start_utc` and
:func:`get_local_day_duration_ms` so test fixtures can drill into the
half-hour-offset / DST-transition edge cases the TS source covers via
``test/v41-period-timezone.test.ts``.

Why a separate module
---------------------

The TS source carries the parser inline at the top of
``lcm-synthesize-around-tool.ts``; the Python port splits it out so:

1. The unit tests in ``tests/tools/test_period_parser.py`` can import
   the parser without pulling in the full synthesis pipeline (DB,
   dispatch, summarizer chain).
2. The main tool module stays focused on the orchestration logic; the
   timezone math (half-hour offsets, DST 23h/25h days) is a finished
   primitive, not load-bearing prose.

The split mirrors the TS surface where ``parsePeriodShortcut`` is
``export``-ed specifically for the timezone test fixture.

Accepted period shortcuts (case-insensitive after trim)
-------------------------------------------------------

* ``today`` — start of the operator's local day → start of next local
  day (local-day duration; 23h on spring-forward, 25h on fall-back).
* ``yesterday`` — start of previous local day → start of current local
  day.
* ``this-week`` — Monday 00:00 local of the current ISO week → Monday
  00:00 of the next ISO week (snapped via ``get_local_day_start_utc``).
* ``last-week`` — Monday 00:00 local of the previous ISO week →
  Monday 00:00 of the current week.
* ``this-month`` — first day of the current calendar month at 00:00
  local → first day of next month at 00:00 local.
* ``last-month`` — first day of the previous calendar month → first
  day of current month.
* ``last-Nh`` — ``now - N*3600s`` → ``now`` (UTC-anchored, max 90 days).
* ``last-Nd`` / ``last-N-days`` — ``now - N*86400s`` → ``now``
  (UTC-anchored, max 366 days). Both forms are accepted; the regex was
  tightened per Wave-7 Auditor #6 P1 to reject undocumented variants
  like ``last-3day`` / ``last-3-d``.

Returns ``(since, before, label)`` on success; raises
:exc:`PeriodParseError` with a helpful message on unrecognized input.

The ``last-Nh`` / ``last-Nd`` patterns are intentionally NOT timezone-
aware — they are "now minus N hours/days," not calendar-day-anchored.
Day-boundary periods (today / yesterday / this-week / last-week /
this-month / last-month) ARE timezone-aware, computed against the
operator's local day boundaries (configured via ``lcm.timezone`` on the
engine — typically ``UTC`` unless the operator opted in).

LCM Wave-N provenance
---------------------

Per [ADR-029](../../docs/adr/029-wave-fix-provenance.md), the load-
bearing scar tissue this module carries:

* **Wave-10 reviewer P1** (TS lines 160-172, ported to
  :func:`get_local_day_start_utc`): operator-facing periods like
  "yesterday" / "this-week" must use LOCAL day boundaries, not UTC. A
  Bangkok operator (UTC+7) at 02:00 local asking "yesterday" wants
  local-yesterday (~17h different from UTC-yesterday).
* **Wave-11 reviewer P1** (TS lines 174-263, ported to
  :func:`get_local_day_start_utc` + :func:`get_local_day_duration_ms`):
  half-hour offsets (Asia/Kolkata UTC+5:30, Asia/Kathmandu UTC+5:45)
  and DST transition days (23h spring-forward, 25h fall-back). The
  Wave-10 hour-only sample dropped minute offsets and assumed every
  local day is 24h — false on DST days.
* **Wave-12 reviewer P2** (TS lines 317-321, ported to the weekly
  branches in :func:`parse_period_shortcut`): the week containing a
  DST transition is 167h or 169h, not 168h. We overshoot by ±12h then
  snap via :func:`get_local_day_start_utc`.
* **Wave-7 Auditor #6 P1** (TS lines 423-426, ported to the
  ``last-Nd`` regex): tightened to only accept the documented
  ``last-Nd`` / ``last-N-days`` forms; previously also accepted
  undocumented variants like ``last-3day``, ``last-3-d``, ``last-3-day``.

Source pin
----------

* TS canonical: ``lossless-claw/src/tools/lcm-synthesize-around-tool.ts``
  (commit ``1f07fbd`` on branch ``pr-613``, lines 174-440).
* Test fixture: ``lossless-claw/test/v41-period-timezone.test.ts``.
* Spec: ``epics/06-tools/06-13-lcm-synthesize-around.md``.
* ADR-029: ``docs/adr/029-wave-fix-provenance.md``.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "PeriodParseError",
    "PeriodParseResult",
    "get_local_day_duration_ms",
    "get_local_day_start_utc",
    "parse_period_shortcut",
]


# ---------------------------------------------------------------------------
# Public dataclasses / errors
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PeriodParseResult:
    """One successful parse — half-open ``[since, before)`` plus a label.

    Mirrors the TS shape ``{ since: Date; before: Date; label: string }``
    at TS line 282. Frozen so callers can pass the result through
    without worrying about downstream mutation.

    Attributes:
        since: Inclusive lower UTC bound (``datetime`` with
            ``tzinfo=UTC``).
        before: Exclusive upper UTC bound.
        label: Human-readable label for telemetry / cache row metadata.
            One of: ``"today"``, ``"yesterday"``, ``"this-week"``,
            ``"last-week"``, ``"this-month"``, ``"last-month"``, or
            the parametric ``"last-Nh"`` / ``"last-Nd"`` form (with N
            substituted by the actual count after clamping).
    """

    since: datetime
    before: datetime
    label: str


class PeriodParseError(ValueError):
    """Raised by :func:`parse_period_shortcut` for unrecognized input.

    The error message lists the accepted forms verbatim — callers
    should surface it to the agent unchanged (the listing is the
    actionable hint).
    """


# ---------------------------------------------------------------------------
# Timezone math helpers
# ---------------------------------------------------------------------------


_HOUR_MS: Final[int] = 60 * 60 * 1000
_DAY_MS: Final[int] = 24 * _HOUR_MS
_HALF_DAY_MS: Final[int] = 12 * _HOUR_MS


def _safe_zone_info(tz_name: str) -> ZoneInfo:
    """Return :class:`ZoneInfo` for ``tz_name``, falling back to UTC.

    Matches the TS source's silent-fallback behaviour: when
    ``Intl.DateTimeFormat`` rejects an invalid timezone string, the
    TS code returns ``new Date(Date.UTC(...))`` rather than throwing.
    Python's :class:`ZoneInfo` raises :exc:`ZoneInfoNotFoundError`; we
    catch it and substitute UTC so the parser stays robust against
    misconfigured engines.
    """
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def get_local_day_start_utc(at: datetime, tz_name: str) -> datetime:
    """UTC instant corresponding to the START of the local day containing ``at``.

    Why: operator-facing periods like "yesterday" / "this-week" must use
    LOCAL day boundaries, not UTC. A Bangkok operator (UTC+7) at 02:00
    local time asking "yesterday" wants local-yesterday (~17h different
    from UTC-yesterday).

    Implementation (Wave-11 reviewer P1 — robust against half-hour
    offsets like Asia/Kolkata UTC+5:30 and DST transition days):

    1. Format ``at`` in target tz to get the local Y-M-D.
    2. Compute the naive UTC midnight for that y/m/d as a starting
       probe.
    3. Iteratively converge: re-format the probe in the target tz,
       compute the delta between the rendered local time and target
       midnight (in minute precision so half/quarter-hour offsets are
       captured), subtract that delta from the probe. Repeat up to 3
       iterations (typically converges in 1-2).

    The iteration handles BOTH fractional offsets (Kolkata +5:30,
    Kathmandu +5:45) AND DST-transition days where the rendered probe
    might land in the wrong day (spring-forward / fall-back).

    Args:
        at: An instant in time. The result is the local-day-start of
            the day that CONTAINS this instant in ``tz_name``.
        tz_name: An IANA timezone identifier (e.g. ``"Asia/Kolkata"``,
            ``"America/Los_Angeles"``, ``"UTC"``). Invalid timezones
            fall back to UTC silently (TS parity).

    Returns:
        A UTC :class:`datetime` (``tzinfo=UTC``) marking the start of
        the local day.

    Source pin: TS ``getLocalDayStartUtc`` at
    ``lossless-claw/src/tools/lcm-synthesize-around-tool.ts:174-246``.
    """
    tz = _safe_zone_info(tz_name)

    # Step 1: get y/m/d in the target tz.
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    local_at = at.astimezone(tz)
    y, m, d = local_at.year, local_at.month, local_at.day

    # Step 2: initial probe — naive UTC midnight for that local y/m/d.
    target_midnight_local_ms = int(datetime(y, m, d, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    probe = datetime(y, m, d, 0, 0, tzinfo=timezone.utc)

    # Step 3: iterate to converge. Three iterations is the TS upper
    # bound; typically converges in 1-2. The third is a safety check.
    # We compare in MINUTE precision so half/quarter-hour offsets
    # converge without special-casing.
    for _ in range(3):
        rendered_local = probe.astimezone(tz)
        # Treat rendered local clock time as if it were UTC, in ms, then
        # delta against target midnight (also treated as UTC).
        rendered_as_local_ms = int(
            datetime(
                rendered_local.year,
                rendered_local.month,
                rendered_local.day,
                rendered_local.hour,
                rendered_local.minute,
                tzinfo=timezone.utc,
            ).timestamp()
            * 1000
        )
        delta_ms = rendered_as_local_ms - target_midnight_local_ms
        if delta_ms == 0:
            return probe
        probe = probe - timedelta(milliseconds=delta_ms)

    return probe


def get_local_day_duration_ms(local_start_utc: datetime, tz_name: str) -> int:
    """Compute the duration (in ms) of the LOCAL day starting at ``local_start_utc``.

    On DST spring-forward days this is 23h; on fall-back days it's 25h.
    Used by period parsing to compute "yesterday" =
    ``[local_start - yesterday_duration, local_start)`` correctly across
    DST.

    Implementation: probe the next-day boundary by overshooting ~36h
    forward and snapping via :func:`get_local_day_start_utc`, then
    subtract. The ±12h buffer absorbs DST shifts.

    Sanity bounds: ms must be in ``[22h, 26h]`` (covers spring-forward
    23h + fall-back 25h with margin). If the computed value falls
    outside, fall back to a flat 24h.

    Args:
        local_start_utc: The UTC instant marking the START of a local
            day (typically from :func:`get_local_day_start_utc`).
        tz_name: An IANA timezone identifier.

    Returns:
        Duration in milliseconds — typically ``24 * 3600 * 1000``, but
        ``23 * 3600 * 1000`` on spring-forward days and
        ``25 * 3600 * 1000`` on fall-back days.

    Source pin: TS ``getLocalDayDurationMs`` at
    ``lossless-claw/src/tools/lcm-synthesize-around-tool.ts:248-263``.
    """
    next_start = get_local_day_start_utc(
        local_start_utc + timedelta(hours=36),
        tz_name,
    )
    ms = int((next_start.timestamp() - local_start_utc.timestamp()) * 1000)
    # Sanity bounds: ms should be in [22h, 26h]. If not, fall back to 24h.
    if ms < 22 * _HOUR_MS or ms > 26 * _HOUR_MS:
        return _DAY_MS
    return ms


# ---------------------------------------------------------------------------
# parse_period_shortcut
# ---------------------------------------------------------------------------


# Wave-7 Auditor #6 P1 fix (2026-02-14): tighten regex to only accept
# documented forms: ``last-Nh`` (e.g. ``last-12h``) OR ``last-Nd`` /
# ``last-N-days`` (e.g. ``last-3d`` / ``last-7-days``). Previously also
# accepted undocumented variants like ``last-3day``, ``last-3-d``,
# ``last-3-day`` which silently worked but weren't in docs.
# Original: lossless-claw/src/tools/lcm-synthesize-around-tool.ts:414-435.
_LAST_NH_RE: Final[re.Pattern[str]] = re.compile(r"^last-(\d+)h$")
_LAST_ND_RE: Final[re.Pattern[str]] = re.compile(r"^last-(\d+)d$|^last-(\d+)-days$")


# Per TS line 333: ISO-week mapping with Monday=1. The TS source uses
# ``Intl.DateTimeFormat("en-US", { weekday: "short" })``; the Python
# port uses :meth:`datetime.isoweekday` which is already 1=Monday..7=Sunday.


def _compute_iso_dow(local_midnight: datetime, tz_name: str) -> int:
    """Return the ISO weekday (1=Mon..7=Sun) of ``local_midnight`` in ``tz_name``.

    Mirrors the TS ``computeDow`` inline at line 324-334. The TS source
    formats ``local_midnight`` via ``Intl.DateTimeFormat`` + a name
    lookup; the Python port goes via :class:`ZoneInfo` + the stdlib
    :meth:`datetime.isoweekday`.
    """
    tz = _safe_zone_info(tz_name)
    if local_midnight.tzinfo is None:
        local_midnight = local_midnight.replace(tzinfo=timezone.utc)
    return local_midnight.astimezone(tz).isoweekday()


def parse_period_shortcut(
    raw: str,
    *,
    now_ms: Optional[int] = None,
    timezone_name: str = "UTC",
) -> PeriodParseResult:
    """Parse a period shortcut into a half-open ``[since, before)`` range.

    Reviewer P1 fix (Wave-10): parses operator-facing period strings
    like ``today`` / ``yesterday`` / ``this-week`` / ``last-7-days``
    into the matching UTC range, honouring the operator's local
    timezone for day-boundary periods.

    Args:
        raw: The period string (case-insensitive after trim).
            Accepted forms:

            * ``today``, ``yesterday``
            * ``this-week``, ``last-week``
            * ``this-month``, ``last-month``
            * ``last-Nh`` (1 ≤ N ≤ 24*90, clamped)
            * ``last-Nd`` or ``last-N-days`` (1 ≤ N ≤ 366, clamped)

        now_ms: Optional ``time.time() * 1000`` override. Used by
            tests to pin "now" so the parse is deterministic.
        timezone_name: An IANA timezone identifier. Day-boundary
            periods are computed in this timezone. ``last-Nh`` and
            ``last-Nd`` are timezone-independent.

    Returns:
        A :class:`PeriodParseResult` with UTC bounds and a label.

    Raises:
        PeriodParseError: For unrecognized period strings. The error
            message lists the accepted forms.

    Source pin: TS ``parsePeriodShortcut`` at
    ``lossless-claw/src/tools/lcm-synthesize-around-tool.ts:279-440``.
    """
    period = raw.strip().lower()
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    now_dt = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)

    # Local-day midnight in the operator's timezone (Wave-10 P1).
    local_midnight = get_local_day_start_utc(now_dt, timezone_name)

    # ----- today / yesterday (Wave-11 P1: actual local-day durations) -----

    if period == "today":
        # Wave-11 reviewer P1 fix: use actual local-day duration
        # (23h on spring-forward, 25h on fall-back) rather than fixed 24h.
        # Original: lossless-claw/src/tools/lcm-synthesize-around-tool.ts:295-302.
        today_duration_ms = get_local_day_duration_ms(local_midnight, timezone_name)
        return PeriodParseResult(
            since=local_midnight,
            before=local_midnight + timedelta(milliseconds=today_duration_ms),
            label="today",
        )

    if period == "yesterday":
        # Yesterday's local-day-start = snap on (today's local-day-start
        # minus 12h). The -12h buffer lands us in yesterday's local day
        # regardless of DST shift; get_local_day_start_utc snaps to the
        # correct midnight.
        yesterday_start = get_local_day_start_utc(
            local_midnight - timedelta(hours=12),
            timezone_name,
        )
        return PeriodParseResult(
            since=yesterday_start,
            before=local_midnight,
            label="yesterday",
        )

    # ----- this-week / last-week (Wave-12 P2: ±12h overshoot + snap) -----

    if period == "this-week":
        # ISO week: Monday = day 1. Land at this-Monday-noon (overshoot
        # by 12h) then snap to local-midnight to absorb DST shift.
        # Original: lossless-claw/src/tools/lcm-synthesize-around-tool.ts:335-349.
        dow = _compute_iso_dow(local_midnight, timezone_name)
        offset_to_monday_ms = (dow - 1) * _DAY_MS - _HALF_DAY_MS
        monday = get_local_day_start_utc(
            local_midnight - timedelta(milliseconds=offset_to_monday_ms),
            timezone_name,
        )
        next_monday = get_local_day_start_utc(
            monday + timedelta(milliseconds=7 * _DAY_MS + _HALF_DAY_MS),
            timezone_name,
        )
        return PeriodParseResult(since=monday, before=next_monday, label="this-week")

    if period == "last-week":
        # Original: lossless-claw/src/tools/lcm-synthesize-around-tool.ts:351-364.
        dow = _compute_iso_dow(local_midnight, timezone_name)
        offset_to_monday_ms = (dow - 1) * _DAY_MS - _HALF_DAY_MS
        this_monday = get_local_day_start_utc(
            local_midnight - timedelta(milliseconds=offset_to_monday_ms),
            timezone_name,
        )
        # Last Monday: 7 local-days before this-Monday. Overshoot
        # forward by 12h after subtracting 7d so we land in the target
        # local-day.
        last_monday = get_local_day_start_utc(
            this_monday - timedelta(milliseconds=7 * _DAY_MS - _HALF_DAY_MS),
            timezone_name,
        )
        return PeriodParseResult(since=last_monday, before=this_monday, label="last-week")

    # ----- this-month / last-month (TS lines 366-410) -----

    tz = _safe_zone_info(timezone_name)
    local_now = now_dt.astimezone(tz)
    y, m = local_now.year, local_now.month

    if period == "this-month":
        # Local first-of-month at midnight, in UTC instants. Probe the
        # 1st at local noon (offsetting by +12h inside the day handles
        # DST transitions and fractional offsets uniformly via the
        # iterative snap).
        month_start = get_local_day_start_utc(
            datetime(y, m, 1, 12, tzinfo=timezone.utc), timezone_name
        )
        next_y, next_m = (y + 1, 1) if m == 12 else (y, m + 1)
        next_month_start = get_local_day_start_utc(
            datetime(next_y, next_m, 1, 12, tzinfo=timezone.utc), timezone_name
        )
        return PeriodParseResult(
            since=month_start,
            before=next_month_start,
            label="this-month",
        )

    if period == "last-month":
        last_y, last_m = (y - 1, 12) if m == 1 else (y, m - 1)
        last_month_start = get_local_day_start_utc(
            datetime(last_y, last_m, 1, 12, tzinfo=timezone.utc), timezone_name
        )
        this_month_start = get_local_day_start_utc(
            datetime(y, m, 1, 12, tzinfo=timezone.utc), timezone_name
        )
        return PeriodParseResult(
            since=last_month_start,
            before=this_month_start,
            label="last-month",
        )

    # ----- last-Nh — UTC-anchored, max 24*90 = 2160h ----------------------

    h_match = _LAST_NH_RE.match(period)
    if h_match is not None:
        # Clamp 1..2160 (24*90 days) per TS line 416.
        hours = min(24 * 90, max(1, int(h_match.group(1))))
        return PeriodParseResult(
            since=now_dt - timedelta(hours=hours),
            before=now_dt,
            label=f"last-{hours}h",
        )

    # ----- last-Nd / last-N-days — UTC-anchored, max 366 days -------------

    d_match = _LAST_ND_RE.match(period)
    if d_match is not None:
        # Wave-7 Auditor #6 P1 (2026-02-14): regex accepts only
        # ``last-Nd`` OR ``last-N-days`` — undocumented variants
        # rejected.
        captured = d_match.group(1) if d_match.group(1) is not None else d_match.group(2)
        days = min(366, max(1, int(captured)))
        return PeriodParseResult(
            since=now_dt - timedelta(days=days),
            before=now_dt,
            label=f"last-{days}d",
        )

    # ----- Unrecognized → raise -------------------------------------------

    raise PeriodParseError(
        f"Unrecognized period shortcut: '{raw}'. "
        "Accepted: today | yesterday | this-week | last-week | this-month | "
        "last-month | last-Nh (e.g. last-12h) | last-Nd (e.g. last-3d) | "
        "last-7-days | last-30-days."
    )
