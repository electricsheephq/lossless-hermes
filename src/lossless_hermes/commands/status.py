"""``/lcm status`` — full LCM health snapshot.

Ports the TS ``buildStatusText`` from
``lossless-claw/src/plugin/lcm-command.ts`` (case ``"status"`` in
``parseLcmCommand``, ``buildStatusText`` at line 1079). The TS handler
renders markdown with four to five sections — header, plugin config,
global counts, current conversation, and (when a conversation is
active) the cache-aware maintenance snapshot.

### Why this module exists

Issue 08-01 shipped a minimal Epic-02 status body (lines like
``last_prompt_tokens``, ``threshold_tokens``, ...) — enough to verify
the router worked end-to-end. Issue 08-02 replaces that with the full
OpenClaw output per ``docs/porting-guides/plugin-glue.md`` line 425.
The minimal Epic-02 fields are no longer surfaced: they were chosen
when the engine had no DB yet (Wave 2 was incomplete); now the DB is
the source of truth and the global counts come from it.

### Hermes-handler vs TS-plugin signature

TS ``buildStatusText`` takes ``{ ctx, db, config }``. The ``ctx``
exposes ``sessionId`` / ``sessionKey`` — used to find "the current
conversation". Hermes's ``register_command`` hook receives only
``raw_args: str``; there is no plugin context. Per
``docs/porting-guides/plugin-glue.md`` line 650, the Python port
substitutes ``engine.current_session_id`` (set by
:meth:`_LifecycleMixin.on_session_start`) for ``ctx.sessionId``.

When the engine has not yet seen an ``on_session_start`` call (CLI
pre-first-message; gateway with no active conversation) the field is
``None`` and the per-conversation block is omitted entirely — per the
issue 08-02 spec acceptance criterion "``current_session_id is None``
causes the 'Current conversation' block to be omitted entirely".

### What's deliberately NOT ported here

* The TS ``getDoctorSummaryStats`` integration (TS lines 1118-1124,
  1156-1159) — Doctor stats land in Epic 08-05 / 08-06, not 08-02.
  Status renders a ``doctor: pending Epic 08-05/06`` placeholder so
  operators see the surface exists.
* The TS ``DoctorSummaryStats.byConversation`` map — same epic.
* The full timezone-aware formatter from
  ``compaction.ts::formatTimestamp`` — Python ports a minimal UTC
  formatter inline since the TS uses Intl.DateTimeFormat which
  requires platform-specific timezone data. The minimal port matches
  the TS fallback path (UTC always; see TS lines 142-149).

See:

* ``epics/08-cli-ops/08-02-status.md`` — this issue.
* ``docs/porting-guides/plugin-glue.md`` line 425 — output format
  contract.
* ``lossless-claw/src/plugin/lcm-command.ts:1079-1204`` — TS source
  pinned at commit ``1f07fbd``.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional, Tuple

logger = logging.getLogger("lossless_hermes.commands.status")


# ---------------------------------------------------------------------------
# Formatting helpers — narrow ports of the TS helpers in lcm-command.ts
# ---------------------------------------------------------------------------


def _format_number(value: int) -> str:
    """Thousands-separated integer (TS parity: ``Intl.NumberFormat("en-US")``).

    The TS source uses ``new Intl.NumberFormat("en-US").format(value)``
    (line 161). Python's ``"{:,}".format(...)`` gives the same output
    for non-negative integers — which is what every call site here
    passes.
    """
    return f"{value:,}"


def _format_bytes(size: float) -> str:
    """Human-readable byte size (TS parity: ``formatBytes`` line 164).

    Mirrors the TS algorithm exactly:

    * ``NaN`` / negative → ``"unknown"``.
    * ``< 1024`` → ``"<n> B"`` (no decimals).
    * ``>= 1024`` → divide by 1024 until ``< 1024`` (or TB is reached).
      Precision: ``>= 100`` → 0 decimals; ``>= 10`` → 1 decimal; else 2.

    The precision ladder matters because the TS snapshot test asserts
    "12.4 MB" not "12 MB" or "12.40 MB" — see the spec output sample
    line 30.
    """
    if size != size or size < 0:  # NaN check via inequality + neg check
        return "unknown"
    if size < 1024:
        return f"{int(size)} B"
    units = ["KB", "MB", "GB", "TB"]
    value = size / 1024.0
    unit_index = 0
    while value >= 1024 and unit_index < len(units) - 1:
        value /= 1024.0
        unit_index += 1
    if value >= 100:
        precision = 0
    elif value >= 10:
        precision = 1
    else:
        precision = 2
    return f"{value:.{precision}f} {units[unit_index]}"


def _format_boolean(value: bool) -> str:
    """``"yes"`` / ``"no"`` (TS parity: ``formatBoolean`` line 156)."""
    return "yes" if value else "no"


def _format_command(command: str) -> str:
    """Wrap in backticks (TS parity: ``formatCommand`` line 182)."""
    return f"`{command}`"


def _truncate_middle(value: str, max_chars: int) -> str:
    """Trim middle with ellipsis (TS parity: ``truncateMiddle`` line 219).

    ``"sk_very_long_value_here_xyz"`` with ``max_chars=10`` becomes
    ``"sk_ve…_xyz"``. The TS uses a single-char Unicode ellipsis
    (``…``); the Python port uses the same char so the snapshot test
    matches byte-for-byte.
    """
    if len(value) <= max_chars:
        return value
    if max_chars <= 3:
        return value[:max_chars]
    head = (max_chars - 1 + 1) // 2  # ceil((max_chars - 1) / 2)
    tail = (max_chars - 1) // 2  # floor((max_chars - 1) / 2)
    return f"{value[:head]}…{value[len(value) - tail :]}"


def _format_compression_ratio(context_tokens: int, compressed_tokens: int) -> str:
    """``"1:<n>"`` ratio (TS parity: ``formatCompressionRatio`` line 206).

    Returns ``"n/a"`` when either input is non-positive or non-finite.
    Otherwise rounds ``compressed / context`` to an integer ratio
    (clamped to a minimum of 1 — same as TS ``Math.max(1, ...)``).
    """
    if context_tokens <= 0 or compressed_tokens <= 0:
        return "n/a"
    ratio = max(1, round(compressed_tokens / context_tokens))
    return f"1:{_format_number(ratio)}"


def _format_timestamp_utc(value: datetime) -> str:
    """UTC-only timestamp formatter (TS-fallback parity: lines 142-149).

    The TS ``formatTimestamp`` (``compaction.ts:125``) prefers
    ``Intl.DateTimeFormat`` with the configured timezone, falling back
    to UTC on any error (lines 142-149: ``"YYYY-MM-DD HH:MM UTC"``).
    Python's stdlib has no IANA-zone-aware formatter without external
    deps (``zoneinfo`` is stdlib but requires platform-specific
    ``tzdata`` packages on Windows; the eval suite runs cross-platform).
    Issue 08-02 ports the UTC-fallback path only — sufficient for
    status output, which is operator-facing not user-facing. If a
    later spec wants the configured-tz path, it adds a ``zoneinfo``
    dep + the corresponding helper.

    Input value MUST be timezone-aware (UTC); the helper converts to
    UTC before formatting so callers passing local-timezone datetimes
    still get the correct wall-clock string. ``naive`` datetimes are
    assumed UTC (no implicit local-zone interpretation).
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%d %H:%M UTC")


def _resolve_package_version() -> str:
    """Return the installed package version (or ``"dev"`` if unknown).

    Production-installed callers see the wheel's
    ``importlib.metadata.version("lossless-hermes")``. Editable installs
    + test runs see the same string. The fallback to ``"dev"`` covers
    the edge case where the distribution metadata isn't registered
    (e.g. a partially-installed editable env that lost its ``.egg-info``).
    """
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("lossless-hermes")
        except PackageNotFoundError:
            return "dev"
    except ImportError:  # pragma: no cover — importlib.metadata is stdlib >=3.8
        return "dev"


def _build_header_lines() -> list[str]:
    """Render the two-line header (TS parity: ``buildHeaderLines`` line 186).

    The TS source pulls ``packageJson.version``; we use the Python
    package version via :func:`_resolve_package_version` so dev envs
    don't crash on missing distribution metadata.
    """
    version = _resolve_package_version()
    return [
        f"**Lossless Hermes v{version}**",
        f"Help: {_format_command('/lcm help')}",
    ]


def _build_section(title: str, lines: list[str]) -> str:
    """Render a section with indented stat lines (TS parity: line 193).

    Two-space indent on each value line (TS uses ``"  "`` — same).
    """
    body = "\n".join(f"  {line}" for line in lines)
    return f"**{title}**\n{body}"


def _build_stat_line(label: str, value: str) -> str:
    """``"label: value"`` (TS parity: ``buildStatLine`` line 197)."""
    return f"{label}: {value}"


# ---------------------------------------------------------------------------
# DB helpers — narrow ports of the TS helpers
# ---------------------------------------------------------------------------


def _resolve_db_size_label(db_path: str) -> str:
    """Format the DB size for the Plugin section (TS parity: line 981).

    Three cases per the TS source:

    * Empty / ``":memory:"`` / ``file::memory:`` prefix → ``"in-memory"``.
    * File exists → :func:`_format_bytes` of the file size.
    * File doesn't exist → ``"missing"`` (matches the TS try/catch).
    """
    if not isinstance(db_path, str):
        return "unknown"
    trimmed = db_path.strip()
    if not trimmed or trimmed == ":memory:" or trimmed.startswith("file::memory:"):
        return "in-memory"
    try:
        return _format_bytes(os.path.getsize(trimmed))
    except OSError:
        return "missing"


def _get_lcm_status_stats(db: sqlite3.Connection) -> dict[str, int]:
    """Global summary stats (TS parity: ``getLcmStatusStats`` line 705).

    Single SQL query joining over ``conversations`` + ``summaries`` to
    aggregate the seven numbers the Global section needs:

    * ``conversation_count`` — total conversation rows.
    * ``summary_count`` — total summaries.
    * ``stored_summary_tokens`` — ``SUM(token_count)`` over all rows.
    * ``summarized_source_tokens`` — ``SUM(source_message_token_count)``
      filtered to ``kind='leaf'`` (per the TS source: only leaf summaries
      have a meaningful source-token count).
    * ``leaf_summary_count`` — count of ``kind='leaf'`` rows.
    * ``condensed_summary_count`` — count of ``kind='condensed'`` rows.
    * ``suppressed_summary_count`` — count of rows with
      ``suppressed_at IS NOT NULL``. Per the issue 08-02 spec line 54
      and ``docs/porting-guides/doctor-ops.md`` §"Read paths that
      filter ``suppressed_at IS NULL``" — status is one of the few read
      surfaces that COUNTS suppressed rows for operator visibility
      (most reads silently filter them out). Shown alongside
      leaf/condensed even when zero per the AC.

    All ``COALESCE(..., 0)`` per TS source so an empty DB returns
    seven zeros, not a ``NULL`` dict.
    """
    row = db.execute(
        """
        SELECT
            COALESCE((SELECT COUNT(*) FROM conversations), 0) AS conversation_count,
            COALESCE(COUNT(*), 0) AS summary_count,
            COALESCE(SUM(token_count), 0) AS stored_summary_tokens,
            COALESCE(SUM(CASE WHEN kind = 'leaf' THEN source_message_token_count ELSE 0 END), 0)
                AS summarized_source_tokens,
            COALESCE(SUM(CASE WHEN kind = 'leaf' THEN 1 ELSE 0 END), 0)
                AS leaf_summary_count,
            COALESCE(SUM(CASE WHEN kind = 'condensed' THEN 1 ELSE 0 END), 0)
                AS condensed_summary_count,
            COALESCE(SUM(CASE WHEN suppressed_at IS NOT NULL THEN 1 ELSE 0 END), 0)
                AS suppressed_summary_count
        FROM summaries
        """
    ).fetchone()
    if row is None:
        return {
            "conversation_count": 0,
            "summary_count": 0,
            "stored_summary_tokens": 0,
            "summarized_source_tokens": 0,
            "leaf_summary_count": 0,
            "condensed_summary_count": 0,
            "suppressed_summary_count": 0,
        }
    return {
        "conversation_count": row[0] or 0,
        "summary_count": row[1] or 0,
        "stored_summary_tokens": row[2] or 0,
        "summarized_source_tokens": row[3] or 0,
        "leaf_summary_count": row[4] or 0,
        "condensed_summary_count": row[5] or 0,
        "suppressed_summary_count": row[6] or 0,
    }


def _get_conversation_status_stats(
    db: sqlite3.Connection, conversation_id: int
) -> Optional[dict[str, Any]]:
    """Per-conversation status (TS parity: ``getConversationStatusStats`` line 738).

    One row per ``conversation_id``; ``None`` if the conversation
    doesn't exist (defensive — the caller already resolved an id from
    ``conversations``, so this should almost never miss, but a race
    with archival could).

    Returns 11 fields: identity (id, session_id, session_key) + counts
    (message_count, summary_count, leaf_summary_count,
    condensed_summary_count) + token aggregates (stored_summary_tokens,
    summarized_source_tokens, context_token_count, compressed_token_count).
    The context/compressed pair feeds ``_format_compression_ratio``.
    """
    row = db.execute(
        """
        SELECT
            c.conversation_id,
            c.session_id,
            c.session_key,
            COALESCE((SELECT COUNT(*) FROM messages WHERE conversation_id = c.conversation_id), 0)
                AS message_count,
            COALESCE((SELECT COUNT(*) FROM summaries WHERE conversation_id = c.conversation_id), 0)
                AS summary_count,
            COALESCE((SELECT SUM(token_count) FROM summaries
                      WHERE conversation_id = c.conversation_id), 0)
                AS stored_summary_tokens,
            COALESCE((SELECT SUM(CASE WHEN kind = 'leaf'
                                      THEN source_message_token_count ELSE 0 END)
                      FROM summaries WHERE conversation_id = c.conversation_id), 0)
                AS summarized_source_tokens,
            COALESCE((
                SELECT SUM(token_count)
                FROM (
                    SELECT m.token_count AS token_count
                    FROM context_items ci
                    JOIN messages m ON m.message_id = ci.message_id
                    WHERE ci.conversation_id = c.conversation_id
                      AND ci.item_type = 'message'
                    UNION ALL
                    SELECT s.token_count AS token_count
                    FROM context_items ci
                    JOIN summaries s ON s.summary_id = ci.summary_id
                    WHERE ci.conversation_id = c.conversation_id
                      AND ci.item_type = 'summary'
                ) context_token_rows
            ), 0) AS context_token_count,
            COALESCE((
                SELECT SUM(COALESCE(s.source_message_token_count, 0)
                          + COALESCE(s.descendant_token_count, 0))
                FROM context_items ci
                JOIN summaries s ON s.summary_id = ci.summary_id
                WHERE ci.conversation_id = c.conversation_id
                  AND ci.item_type = 'summary'
            ), 0) AS compressed_token_count,
            COALESCE((SELECT SUM(CASE WHEN kind = 'leaf' THEN 1 ELSE 0 END)
                      FROM summaries WHERE conversation_id = c.conversation_id), 0)
                AS leaf_summary_count,
            COALESCE((SELECT SUM(CASE WHEN kind = 'condensed' THEN 1 ELSE 0 END)
                      FROM summaries WHERE conversation_id = c.conversation_id), 0)
                AS condensed_summary_count
        FROM conversations c
        WHERE c.conversation_id = ?
        """,
        (conversation_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "conversation_id": row[0],
        "session_id": row[1],
        "session_key": row[2],
        "message_count": row[3] or 0,
        "summary_count": row[4] or 0,
        "stored_summary_tokens": row[5] or 0,
        "summarized_source_tokens": row[6] or 0,
        "context_token_count": row[7] or 0,
        "compressed_token_count": row[8] or 0,
        "leaf_summary_count": row[9] or 0,
        "condensed_summary_count": row[10] or 0,
    }


def _resolve_conversation_id_for_session(db: sqlite3.Connection, session_id: str) -> Optional[int]:
    """Find the conversation_id for ``session_id`` (active preferred).

    TS uses ``getConversationStatusBySessionId`` (line 841) — the
    equivalent shape: ``ORDER BY active DESC, created_at DESC LIMIT 1``
    so an active row wins over an archived row of the same session.
    """
    row = db.execute(
        """
        SELECT conversation_id
        FROM conversations
        WHERE session_id = ?
        ORDER BY active DESC, created_at DESC
        LIMIT 1
        """,
        (session_id,),
    ).fetchone()
    if row is None:
        return None
    return int(row[0])


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


def _build_plugin_section(engine: Any) -> str:
    """Render the ``🧩 Plugin`` section.

    Three rows: ``db path``, ``db size`` (formatted), ``current
    session id`` (the engine-tracked field that replaces TS
    ``ctx.sessionId``). The TS source also surfaces ``enabled`` and
    ``selected`` slot info; those rely on Hermes ``config.plugins``
    structure which the Python port doesn't replicate (Hermes-side
    config-loading isn't a Python concern). The two omitted fields
    are documented in the module docstring's "deliberately NOT
    ported" list.
    """
    db_path = getattr(engine.config, "database_path", "") or ""
    db_size = _resolve_db_size_label(db_path)
    current_session = getattr(engine, "current_session_id", None)
    return _build_section(
        "Plugin",
        [
            _build_stat_line("db path", db_path or "<unset>"),
            _build_stat_line("db size", db_size),
            _build_stat_line("current session", current_session if current_session else "<none>"),
        ],
    )


def _build_global_section(stats: dict[str, int]) -> str:
    """Render the ``🌐 Global`` section — TS parity line 1105.

    Per AC line 70 ("Suppressed-summary count is shown alongside
    leaf/condensed even when zero"), the summaries row carries three
    fields — leaf, condensed, AND suppressed — even on a fresh DB
    where suppressed is zero. Most LCM read surfaces filter
    ``suppressed_at IS NULL``; status is one of the few that exposes
    the suppressed bucket for operator visibility.
    """
    return _build_section(
        "Global",
        [
            _build_stat_line("conversations", _format_number(stats["conversation_count"])),
            _build_stat_line(
                "summaries",
                f"{_format_number(stats['summary_count'])} "
                f"({_format_number(stats['leaf_summary_count'])} leaf, "
                f"{_format_number(stats['condensed_summary_count'])} condensed, "
                f"{_format_number(stats['suppressed_summary_count'])} suppressed)",
            ),
            _build_stat_line(
                "stored summary tokens", _format_number(stats["stored_summary_tokens"])
            ),
            _build_stat_line(
                "summarized source tokens",
                _format_number(stats["summarized_source_tokens"]),
            ),
        ],
    )


def _build_current_conversation_sections(
    db: sqlite3.Connection,
    engine: Any,
    current_session_id: str,
) -> Tuple[list[str], bool]:
    """Render the ``📍 Current conversation`` + ``🛠️ Maintenance`` sections.

    Returns ``(rendered_section_strings, resolved)``. The flag tells the
    caller whether a conversation was actually found — when ``False``
    the rendered list contains the single "unavailable" section that
    falls back to global stats only (TS parity lines 1193-1201).
    """
    conversation_id = _resolve_conversation_id_for_session(db, current_session_id)
    if conversation_id is None:
        return (
            [
                _build_section(
                    "Current conversation",
                    [
                        _build_stat_line("status", "unavailable"),
                        _build_stat_line(
                            "reason",
                            f"No LCM conversation stored yet for session "
                            f"{_format_command(current_session_id)}.",
                        ),
                        _build_stat_line("fallback", "Showing Global stats only."),
                    ],
                )
            ],
            False,
        )

    stats = _get_conversation_status_stats(db, conversation_id)
    if stats is None:
        # Defensive — race between resolution and stats query (archival).
        return (
            [
                _build_section(
                    "Current conversation",
                    [
                        _build_stat_line("status", "unavailable"),
                        _build_stat_line(
                            "reason",
                            "Conversation row vanished between resolution and stats query.",
                        ),
                        _build_stat_line("fallback", "Showing Global stats only."),
                    ],
                )
            ],
            False,
        )

    session_key_display = (
        _format_command(_truncate_middle(stats["session_key"], 44))
        if stats["session_key"]
        else "missing"
    )

    current_lines = [
        _build_stat_line("conversation id", _format_number(stats["conversation_id"])),
        _build_stat_line("session key", session_key_display),
        _build_stat_line("messages", _format_number(stats["message_count"])),
        _build_stat_line(
            "summaries",
            f"{_format_number(stats['summary_count'])} "
            f"({_format_number(stats['leaf_summary_count'])} leaf, "
            f"{_format_number(stats['condensed_summary_count'])} condensed)",
        ),
        _build_stat_line("stored summary tokens", _format_number(stats["stored_summary_tokens"])),
        _build_stat_line(
            "summarized source tokens", _format_number(stats["summarized_source_tokens"])
        ),
        _build_stat_line("tokens in context", _format_number(stats["context_token_count"])),
        _build_stat_line(
            "compression ratio",
            _format_compression_ratio(
                stats["context_token_count"], stats["compressed_token_count"]
            ),
        ),
        # Doctor stats land in Epic 08-05/06; keep the row so the surface
        # is operator-visible and the eventual port is a one-line change.
        _build_stat_line("doctor", "pending Epic 08-05/06"),
    ]
    rendered = [_build_section("Current conversation", current_lines)]

    # Maintenance section — only renders when telemetry/maintenance
    # stores are available AND have rows for this conversation. Both
    # stores are optional under the read-only contract; gating prevents
    # spurious "unknown / never / none" rows on a fresh DB.
    maintenance_store = getattr(engine, "_maintenance_store", None)
    telemetry_store = getattr(engine, "_telemetry_store", None)

    maintenance_record = None
    telemetry_record = None
    if maintenance_store is not None:
        try:
            maintenance_record = maintenance_store.get_conversation_compaction_maintenance(
                conversation_id
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning("[lcm] maintenance store read failed: %s", exc)
    if telemetry_store is not None:
        try:
            telemetry_record = telemetry_store.get_conversation_compaction_telemetry(
                conversation_id
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning("[lcm] telemetry store read failed: %s", exc)

    def _fmt_ts(value: Optional[datetime]) -> str:
        return _format_timestamp_utc(value) if value is not None else "never"

    if maintenance_record is None and telemetry_record is None:
        return rendered, True

    state = "idle"
    if maintenance_record is not None:
        if maintenance_record.pending:
            state = "pending"
        elif maintenance_record.running:
            state = "running"

    maintenance_lines = [
        _build_stat_line("state", state),
        _build_stat_line(
            "requested at",
            _fmt_ts(maintenance_record.requested_at if maintenance_record else None),
        ),
        _build_stat_line(
            "reason",
            (
                maintenance_record.reason
                if maintenance_record and maintenance_record.reason
                else "none"
            ),
        ),
        _build_stat_line(
            "last started",
            _fmt_ts(maintenance_record.last_started_at if maintenance_record else None),
        ),
        _build_stat_line(
            "last finished",
            _fmt_ts(maintenance_record.last_finished_at if maintenance_record else None),
        ),
        _build_stat_line(
            "last failure",
            (
                maintenance_record.last_failure_summary
                if maintenance_record and maintenance_record.last_failure_summary
                else "none"
            ),
        ),
        _build_stat_line(
            "requested token budget",
            (
                _format_number(maintenance_record.token_budget)
                if maintenance_record and maintenance_record.token_budget is not None
                else "unknown"
            ),
        ),
        _build_stat_line(
            "observed token count",
            (
                _format_number(maintenance_record.current_token_count)
                if maintenance_record and maintenance_record.current_token_count is not None
                else "unknown"
            ),
        ),
        _build_stat_line(
            "last api call",
            _fmt_ts(telemetry_record.last_api_call_at if telemetry_record else None),
        ),
        _build_stat_line(
            "last cache touch",
            _fmt_ts(telemetry_record.last_cache_touch_at if telemetry_record else None),
        ),
        _build_stat_line(
            "cache retention",
            (
                telemetry_record.retention
                if telemetry_record and telemetry_record.retention
                else "unknown"
            ),
        ),
        _build_stat_line(
            "cache state",
            (
                telemetry_record.cache_state
                if telemetry_record and telemetry_record.cache_state
                else "unknown"
            ),
        ),
        _build_stat_line(
            "provider/model",
            _format_provider_model(
                telemetry_record.provider if telemetry_record else None,
                telemetry_record.model if telemetry_record else None,
            ),
        ),
    ]
    rendered.append(_build_section("Maintenance", maintenance_lines))
    return rendered, True


def _format_provider_model(provider: Optional[str], model: Optional[str]) -> str:
    """``"provider / model"`` or ``"unknown"`` (TS parity line 1190).

    The TS source: ``[provider, model].filter(Boolean).join(" / ") || "unknown"``.
    Both empty/None → ``"unknown"``; one empty → just the other.
    """
    parts = [p for p in (provider, model) if p]
    return " / ".join(parts) if parts else "unknown"


# ---------------------------------------------------------------------------
# Public entry point — the dispatcher routes ``/lcm status`` here
# ---------------------------------------------------------------------------


def run(parsed: Any) -> str:
    """Render ``/lcm status``.

    Reads from :attr:`engine._db` (open ``sqlite3.Connection``) plus
    the engine state set by :meth:`_LifecycleMixin.on_session_start`.
    When the engine is uninitialized (``_db is None``, e.g. the
    dispatcher is invoked before any Hermes session start), the body
    returns a graceful "engine not yet initialized" message rather
    than crashing — operators may type ``/lcm`` very early in a
    debug session.

    Per the issue 08-02 spec acceptance criteria:

    * "``current_session_id is None`` causes the 'Current conversation'
      block to be omitted entirely" — implemented in
      :func:`_build_current_conversation_sections`'s caller branch.
    * "DB size formatted as MB/GB with one decimal place" — handled
      by :func:`_format_bytes`'s precision ladder.
    * "Suppressed-summary count is shown alongside leaf/condensed even
      when zero" — currently only leaf/condensed are surfaced; the
      suppressed count requires a column-aware port of the doctor's
      suppressed-summary filter that lands in Epic 08-05/06. The
      stub doctor row in the Current conversation block flags this.

    Args:
        parsed: The :class:`ParsedLcmCommand`. Reads
            ``parsed.engine`` — set by the dispatcher before invoking.

    Returns:
        Multi-line markdown string ready for chat rendering. On any
        unexpected error (DB read failure, missing engine attribute),
        the body logs the exception and returns a one-line "status
        failed: <reason>" string — never raises.
    """
    engine = getattr(parsed, "engine", None)
    if engine is None:
        logger.warning("[lcm] /lcm status invoked with no engine on parsed")
        return "/lcm status: dispatcher misconfigured (no engine reference)."

    db = getattr(engine, "_db", None)
    lines = list(_build_header_lines())
    lines.append("")
    lines.append(_build_plugin_section(engine))
    lines.append("")

    if db is None:
        # Engine constructed but on_session_start has not yet run (CLI
        # pre-first-message; gateway with no active session). Return
        # the plugin section + a clarifying "DB not yet open" footer.
        lines.append(
            _build_section(
                "Status",
                [
                    _build_stat_line("db", "not yet opened"),
                    _build_stat_line(
                        "hint",
                        "Send at least one message to trigger on_session_start.",
                    ),
                ],
            )
        )
        return "\n".join(lines)

    # Global stats are always available once the DB is open.
    try:
        stats = _get_lcm_status_stats(db)
    except sqlite3.Error as exc:
        logger.exception("[lcm] /lcm status: global stats query failed")
        return f"/lcm status failed: DB error reading global stats — {exc!s}"
    lines.append(_build_global_section(stats))

    # Per-conversation block only renders when current_session_id is
    # set (per spec AC: None → omit entirely). The TS source returned
    # an "unavailable" placeholder block even in this case (line 1193);
    # the Python port follows the explicit AC requirement to "omit
    # the block entirely".
    current_session_id = getattr(engine, "current_session_id", None)
    if current_session_id:
        try:
            current_sections, _resolved = _build_current_conversation_sections(
                db, engine, current_session_id
            )
        except sqlite3.Error as exc:
            logger.exception("[lcm] /lcm status: current conversation query failed")
            return f"/lcm status failed: DB error reading current conversation — {exc!s}"
        for section in current_sections:
            lines.append("")
            lines.append(section)

    return "\n".join(lines)
