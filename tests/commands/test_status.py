"""Tests for ``/lcm status`` — issue 08-02.

Ports the TS ``test/lcm-command.test.ts::status*`` cases into pytest.
The TS scenarios live inline in ``lossless-claw/src/plugin/lcm-command.ts``
(``__testing`` export + the dispatcher tests in
``test/lcm-command.test.ts``); this module reconstructs the snapshot
shape and the load-bearing branch points:

* Header + Plugin + Global sections render unconditionally.
* "Current conversation" + "Maintenance" sections render ONLY when
  :attr:`engine.current_session_id` resolves to an existing
  ``conversations`` row.
* ``current_session_id=None`` (CLI pre-first-message; gateway with no
  active conversation) omits the per-conversation block entirely per
  the issue 08-02 spec AC line 67.
* Suppressed-summary count is shown alongside leaf/condensed even
  when zero per AC line 70.
* DB size formatter handles ``in-memory`` / file / missing per AC
  line 68.

See:

* ``epics/08-cli-ops/08-02-status.md`` — this issue.
* ``lossless-claw/src/plugin/lcm-command.ts:1079-1204`` — TS source
  at commit ``1f07fbd``.
* ``docs/porting-guides/plugin-glue.md`` §"Test inventory" line 591 —
  snapshot-test guidance.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lossless_hermes.commands.status import (
    _format_bytes,
    _format_compression_ratio,
    _format_number,
    _format_provider_model,
    _format_timestamp_utc,
    _resolve_db_size_label,
    _truncate_middle,
    run,
)
from lossless_hermes.db.migration import run_lcm_migrations


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def migrated_db() -> sqlite3.Connection:
    """In-memory SQLite with the full LCM migration ladder applied.

    Uses ``fts5_available=False`` to skip the FTS5 virtual tables
    (some Python builds lack FTS5; status doesn't read from FTS so
    the skip is safe). ``foreign_keys = ON`` matches production
    ``open_lcm_db`` semantics.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(conn, fts5_available=False)
    return conn


def _make_engine(
    db: sqlite3.Connection | None = None,
    current_session_id: str | None = None,
    database_path: str = "",
    maintenance_store: Any | None = None,
    telemetry_store: Any | None = None,
) -> Any:
    """Build a minimal engine stub that has just the attributes status reads.

    Status reads:

    * ``engine._db`` — open ``sqlite3.Connection`` (or ``None`` for the
      pre-on_session_start branch).
    * ``engine.current_session_id`` — engine-tracked session_id.
    * ``engine.config.database_path`` — for the Plugin section's
      "db path" / "db size" rows.
    * ``engine._maintenance_store`` / ``engine._telemetry_store`` —
      optional store handles. Status guards on ``getattr(...) is None``
      so the stub omits them by default.
    """
    config = SimpleNamespace(database_path=database_path)
    return SimpleNamespace(
        _db=db,
        current_session_id=current_session_id,
        config=config,
        _maintenance_store=maintenance_store,
        _telemetry_store=telemetry_store,
    )


def _seed_conversation_and_messages(
    db: sqlite3.Connection,
    *,
    session_id: str,
    session_key: str | None = None,
    message_count: int = 0,
) -> int:
    """Insert one conversation + ``message_count`` messages.

    Returns the new ``conversation_id``. Sequence numbers start at 1
    to match the production ingest path; tokens are a placeholder
    ``10`` per message.
    """
    cursor = db.execute(
        "INSERT INTO conversations (session_id, session_key, active) VALUES (?, ?, 1)",
        (session_id, session_key),
    )
    conv_id = cursor.lastrowid
    assert conv_id is not None
    for i in range(message_count):
        db.execute(
            "INSERT INTO messages "
            "(conversation_id, seq, role, content, token_count) "
            "VALUES (?, ?, 'user', ?, ?)",
            (conv_id, i + 1, f"message {i}", 10),
        )
    db.commit()
    return conv_id


def _seed_summary(
    db: sqlite3.Connection,
    *,
    conversation_id: int,
    summary_id: str,
    kind: str,
    token_count: int = 50,
    source_message_token_count: int = 200,
    descendant_token_count: int = 0,
    suppressed_at: str | None = None,
) -> None:
    """Insert one summary row with the load-bearing columns set."""
    db.execute(
        "INSERT INTO summaries "
        "(summary_id, conversation_id, kind, content, token_count, "
        " source_message_token_count, descendant_token_count, suppressed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            summary_id,
            conversation_id,
            kind,
            f"summary {summary_id}",
            token_count,
            source_message_token_count,
            descendant_token_count,
            suppressed_at,
        ),
    )
    db.commit()


# ---------------------------------------------------------------------------
# AC: dispatcher misconfig — no engine on parsed
# ---------------------------------------------------------------------------


def test_run_without_engine_returns_friendly_error() -> None:
    """``parsed.engine`` missing → one-line "dispatcher misconfigured" output.

    Defensive: real dispatchers attach ``engine`` before invoking the
    handler. If somehow the attribute is missing we must NOT crash
    and we must NOT pretend a status snapshot succeeded.
    """
    parsed = SimpleNamespace()  # no engine attribute
    out = run(parsed)
    assert "dispatcher misconfigured" in out
    assert "no engine reference" in out


# ---------------------------------------------------------------------------
# AC: pre-on_session_start branch — _db is None
# ---------------------------------------------------------------------------


def test_run_with_engine_no_db_returns_header_plus_hint() -> None:
    """Engine constructed but ``_db is None`` (pre-on_session_start).

    Status must still render the header + Plugin section + a clear
    "db not yet opened" hint. Operators routinely type ``/lcm`` very
    early in a debug session before any message has flowed; the
    handler must not crash.
    """
    engine = _make_engine(db=None, current_session_id=None)
    parsed = SimpleNamespace(engine=engine)
    out = run(parsed)
    # Header + Plugin section render.
    assert "Lossless Hermes v" in out
    assert "**Plugin**" in out
    # Status section reports the deferred-init state.
    assert "**Status**" in out
    assert "not yet opened" in out
    # No Global section yet (DB not open → no aggregates).
    assert "**Global**" not in out
    # And no Current conversation block.
    assert "**Current conversation**" not in out


# ---------------------------------------------------------------------------
# AC line 67: current_session_id=None omits per-conversation block
# ---------------------------------------------------------------------------


def test_no_active_conversation(migrated_db: sqlite3.Connection) -> None:
    """``current_session_id is None`` → no per-conversation block.

    Per AC line 67: "``current_session_id is None`` causes the
    'Current conversation' block to be omitted entirely (no
    'id=None' or empty fields)." Verify the section header is
    absent (not just emptied).
    """
    engine = _make_engine(db=migrated_db, current_session_id=None)
    parsed = SimpleNamespace(engine=engine)
    out = run(parsed)
    assert "**Global**" in out, "Global section should always render with open DB"
    assert "**Current conversation**" not in out, (
        "Current conversation block must be entirely omitted when current_session_id is None"
    )
    # Also confirm no stray "id=None" or "session_key=None" leakage.
    assert "id=None" not in out
    assert "session_key=None" not in out


# ---------------------------------------------------------------------------
# AC: bare /lcm with current session resolves to per-conversation block
# ---------------------------------------------------------------------------


def test_with_active_conversation_renders_per_conv_block(
    migrated_db: sqlite3.Connection,
) -> None:
    """``current_session_id`` resolves to a conversation → block renders.

    Verify the section header is present AND key per-conversation
    rows (conversation id, session key, messages) appear with the
    seeded values.
    """
    conv_id = _seed_conversation_and_messages(
        migrated_db,
        session_id="sess-active",
        session_key="agent:main:thread:xyz",
        message_count=3,
    )
    _seed_summary(
        migrated_db,
        conversation_id=conv_id,
        summary_id="sum-1",
        kind="leaf",
        token_count=42,
        source_message_token_count=300,
    )

    engine = _make_engine(db=migrated_db, current_session_id="sess-active")
    parsed = SimpleNamespace(engine=engine)
    out = run(parsed)

    assert "**Current conversation**" in out
    assert f"conversation id: {conv_id:,}" in out
    assert "agent:main:thread:xyz" in out
    assert "messages: 3" in out


def test_current_session_id_resolves_but_no_db_row_renders_unavailable(
    migrated_db: sqlite3.Connection,
) -> None:
    """``current_session_id`` set but no matching conversation → unavailable block.

    The TS source surfaces a "Current conversation: unavailable"
    block with a reason and a "Showing Global stats only." fallback
    (lines 1193-1201). Python mirrors this — the block IS rendered
    (different from the ``current_session_id is None`` AC, where the
    block is omitted entirely).
    """
    engine = _make_engine(db=migrated_db, current_session_id="sess-orphan")
    parsed = SimpleNamespace(engine=engine)
    out = run(parsed)

    assert "**Current conversation**" in out
    assert "status: unavailable" in out
    assert "Showing Global stats only." in out


# ---------------------------------------------------------------------------
# AC line 68: DB size formatter (MB/GB with precision)
# ---------------------------------------------------------------------------


def test_format_bytes_kb_one_decimal() -> None:
    """Sub-100 KB → 1 decimal place ("12.4 KB" not "12 KB" or "12.40 KB")."""
    assert _format_bytes(12.4 * 1024) == "12.4 KB"


def test_format_bytes_mb_one_decimal() -> None:
    """Sub-100 MB → 1 decimal place ("12.4 MB" per spec line 30)."""
    assert _format_bytes(12.4 * 1024 * 1024) == "12.4 MB"


def test_format_bytes_gb_two_decimals() -> None:
    """Sub-10 GB → 2 decimal places."""
    assert _format_bytes(1.23 * 1024 * 1024 * 1024) == "1.23 GB"


def test_format_bytes_large_no_decimals() -> None:
    """>= 100 MB / GB → 0 decimal places (precision ladder)."""
    assert _format_bytes(125 * 1024 * 1024) == "125 MB"


def test_format_bytes_below_1024() -> None:
    """< 1024 bytes → "<n> B" (no unit conversion)."""
    assert _format_bytes(512) == "512 B"


def test_format_bytes_negative_returns_unknown() -> None:
    """Negative size (corrupt stat) → "unknown"."""
    assert _format_bytes(-1) == "unknown"


def test_format_bytes_nan_returns_unknown() -> None:
    """NaN size → "unknown" (defensive)."""
    assert _format_bytes(float("nan")) == "unknown"


# ---------------------------------------------------------------------------
# AC: DB size label resolver
# ---------------------------------------------------------------------------


def test_resolve_db_size_label_in_memory() -> None:
    """``":memory:"`` → "in-memory"."""
    assert _resolve_db_size_label(":memory:") == "in-memory"
    assert _resolve_db_size_label("") == "in-memory"
    assert _resolve_db_size_label("file::memory:?cache=shared") == "in-memory"


def test_resolve_db_size_label_missing_file() -> None:
    """Non-existent file path → "missing" (TS try/catch parity)."""
    assert _resolve_db_size_label("/definitely/does/not/exist.db") == "missing"


def test_resolve_db_size_label_real_file(tmp_path: Path) -> None:
    """Existing file → its size formatted via :func:`_format_bytes`."""
    db_file = tmp_path / "test.db"
    db_file.write_bytes(b"x" * 1500)
    out = _resolve_db_size_label(str(db_file))
    # 1500 bytes → 1.46 KB (1500 / 1024 = 1.464..., 2-decimal precision).
    assert out.endswith("KB")
    assert "1.4" in out or "1.5" in out


# ---------------------------------------------------------------------------
# AC line 70: Suppressed-summary count shown when zero
# ---------------------------------------------------------------------------


def test_suppressed_count_renders_when_zero(migrated_db: sqlite3.Connection) -> None:
    """Fresh DB → "0 suppressed" still rendered in the summaries row.

    Per AC line 70: "Suppressed-summary count is shown alongside
    leaf/condensed even when zero (operator visibility)." This is
    the difference between status and other LCM read surfaces which
    silently filter ``suppressed_at IS NULL``.
    """
    engine = _make_engine(db=migrated_db, current_session_id=None)
    parsed = SimpleNamespace(engine=engine)
    out = run(parsed)
    assert "suppressed" in out
    # Match the exact "0 suppressed" sub-string (not just the word).
    assert "0 suppressed" in out


def test_suppressed_count_renders_when_nonzero(
    migrated_db: sqlite3.Connection,
) -> None:
    """Seeded suppressed row → "1 suppressed" reflected in the output."""
    conv_id = _seed_conversation_and_messages(migrated_db, session_id="sess-1")
    _seed_summary(
        migrated_db,
        conversation_id=conv_id,
        summary_id="sum-leaf",
        kind="leaf",
    )
    _seed_summary(
        migrated_db,
        conversation_id=conv_id,
        summary_id="sum-suppressed",
        kind="leaf",
        suppressed_at="2025-05-13T12:00:00Z",
    )

    engine = _make_engine(db=migrated_db, current_session_id=None)
    parsed = SimpleNamespace(engine=engine)
    out = run(parsed)
    assert "2 leaf" in out
    assert "1 suppressed" in out


# ---------------------------------------------------------------------------
# AC line 71: Global counts reflect actual rows
# ---------------------------------------------------------------------------


def test_global_section_counts_conversations(migrated_db: sqlite3.Connection) -> None:
    """Global section counts conversations correctly across many rows."""
    for i in range(5):
        _seed_conversation_and_messages(migrated_db, session_id=f"sess-{i}")

    engine = _make_engine(db=migrated_db, current_session_id=None)
    parsed = SimpleNamespace(engine=engine)
    out = run(parsed)
    assert "**Global**" in out
    assert "conversations: 5" in out


def test_global_section_counts_leaf_vs_condensed(
    migrated_db: sqlite3.Connection,
) -> None:
    """Leaf and condensed counts are independent buckets in the same row."""
    conv_id = _seed_conversation_and_messages(migrated_db, session_id="sess")
    for i in range(3):
        _seed_summary(
            migrated_db,
            conversation_id=conv_id,
            summary_id=f"leaf-{i}",
            kind="leaf",
        )
    for i in range(2):
        _seed_summary(
            migrated_db,
            conversation_id=conv_id,
            summary_id=f"condensed-{i}",
            kind="condensed",
        )

    engine = _make_engine(db=migrated_db, current_session_id=None)
    parsed = SimpleNamespace(engine=engine)
    out = run(parsed)
    # The summaries row carries (3 leaf, 2 condensed, 0 suppressed).
    assert "3 leaf" in out
    assert "2 condensed" in out


# ---------------------------------------------------------------------------
# AC: format_number formatting
# ---------------------------------------------------------------------------


def test_format_number_thousands_separator() -> None:
    """``Intl.NumberFormat("en-US")`` parity — thousands separator."""
    assert _format_number(1) == "1"
    assert _format_number(1000) == "1,000"
    assert _format_number(1234567) == "1,234,567"
    assert _format_number(0) == "0"


# ---------------------------------------------------------------------------
# AC: compression ratio formatting
# ---------------------------------------------------------------------------


def test_compression_ratio_zero_inputs_returns_na() -> None:
    """Either input ≤ 0 → "n/a" (avoid divide-by-zero)."""
    assert _format_compression_ratio(0, 100) == "n/a"
    assert _format_compression_ratio(100, 0) == "n/a"
    assert _format_compression_ratio(0, 0) == "n/a"
    assert _format_compression_ratio(-1, 100) == "n/a"


def test_compression_ratio_normal() -> None:
    """Normal case → "1:<rounded ratio>" with thousands-separator."""
    assert _format_compression_ratio(100, 400) == "1:4"
    assert _format_compression_ratio(1000, 19200) == "1:19"
    assert _format_compression_ratio(1, 12345) == "1:12,345"


def test_compression_ratio_min_clamped_to_one() -> None:
    """``Math.max(1, round(...))`` parity — ratio is never below 1."""
    # context > compressed (artificial — but shouldn't crash).
    assert _format_compression_ratio(200, 100) == "1:1"


# ---------------------------------------------------------------------------
# AC: truncate_middle utility
# ---------------------------------------------------------------------------


def test_truncate_middle_short_string_unchanged() -> None:
    """String shorter than max_chars passes through unchanged."""
    assert _truncate_middle("short", 10) == "short"


def test_truncate_middle_long_string_ellipsizes() -> None:
    """Long string → head + ``…`` + tail trimmed to max_chars."""
    out = _truncate_middle("agent:main:thread:xyz123456789", 10)
    assert "…" in out
    assert len(out) <= 10


# ---------------------------------------------------------------------------
# AC: format_provider_model utility
# ---------------------------------------------------------------------------


def test_format_provider_model_both_present() -> None:
    """Both fields → "provider / model" join."""
    assert _format_provider_model("anthropic", "claude-opus-4-7") == "anthropic / claude-opus-4-7"


def test_format_provider_model_one_missing() -> None:
    """One field empty/None → just the other (no leading / trailing slash)."""
    assert _format_provider_model(None, "claude-opus-4-7") == "claude-opus-4-7"
    assert _format_provider_model("anthropic", None) == "anthropic"
    assert _format_provider_model("", "claude-opus-4-7") == "claude-opus-4-7"


def test_format_provider_model_both_missing() -> None:
    """Both missing → "unknown"."""
    assert _format_provider_model(None, None) == "unknown"
    assert _format_provider_model("", "") == "unknown"


# ---------------------------------------------------------------------------
# AC: timestamp formatter (UTC parity with TS fallback path)
# ---------------------------------------------------------------------------


def test_format_timestamp_utc_aware() -> None:
    """UTC-aware datetime → "YYYY-MM-DD HH:MM UTC"."""
    from datetime import datetime, timezone

    dt = datetime(2025, 5, 12, 18, 32, tzinfo=timezone.utc)
    assert _format_timestamp_utc(dt) == "2025-05-12 18:32 UTC"


def test_format_timestamp_utc_naive_treated_as_utc() -> None:
    """Naive datetime → treated as UTC (no implicit local-zone interpretation)."""
    from datetime import datetime

    dt = datetime(2025, 5, 12, 18, 32)  # naive
    assert _format_timestamp_utc(dt) == "2025-05-12 18:32 UTC"


def test_format_timestamp_utc_other_zone_converted() -> None:
    """Non-UTC tz → converted to UTC wall clock before formatting."""
    from datetime import datetime, timedelta, timezone

    # 18:32 in UTC+05:00 == 13:32 UTC.
    plus5 = timezone(timedelta(hours=5))
    dt = datetime(2025, 5, 12, 18, 32, tzinfo=plus5)
    assert _format_timestamp_utc(dt) == "2025-05-12 13:32 UTC"


# ---------------------------------------------------------------------------
# AC: Maintenance section gating
# ---------------------------------------------------------------------------


def test_maintenance_section_omitted_when_no_records(
    migrated_db: sqlite3.Connection,
) -> None:
    """Conversation exists but no maintenance/telemetry rows → no Maintenance section.

    Status renders a Maintenance section only when at least one of
    ``maintenance_store`` / ``telemetry_store`` returns a record for
    the conversation. Without records (fresh DB), the section is
    omitted entirely.
    """
    conv_id = _seed_conversation_and_messages(migrated_db, session_id="sess-1", message_count=2)

    class _NullStore:
        def get_conversation_compaction_maintenance(self, _: int) -> None:
            return None

        def get_conversation_compaction_telemetry(self, _: int) -> None:
            return None

    engine = _make_engine(
        db=migrated_db,
        current_session_id="sess-1",
        maintenance_store=_NullStore(),
        telemetry_store=_NullStore(),
    )
    parsed = SimpleNamespace(engine=engine)
    out = run(parsed)

    assert f"conversation id: {conv_id:,}" in out
    assert "**Maintenance**" not in out


def test_maintenance_section_renders_when_telemetry_present(
    migrated_db: sqlite3.Connection,
) -> None:
    """Telemetry record exists → Maintenance section renders.

    Use a fake telemetry store that returns a record with one
    populated field (``provider``); verify the section appears and
    surfaces the value.
    """
    conv_id = _seed_conversation_and_messages(migrated_db, session_id="sess-1")

    class _FakeTelemetryStore:
        def get_conversation_compaction_telemetry(self, _: int) -> Any:
            return SimpleNamespace(
                last_api_call_at=None,
                last_cache_touch_at=None,
                retention=None,
                cache_state="hot",
                provider="anthropic",
                model="claude-opus-4-7",
            )

    class _NullMaintenanceStore:
        def get_conversation_compaction_maintenance(self, _: int) -> None:
            return None

    engine = _make_engine(
        db=migrated_db,
        current_session_id="sess-1",
        maintenance_store=_NullMaintenanceStore(),
        telemetry_store=_FakeTelemetryStore(),
    )
    parsed = SimpleNamespace(engine=engine)
    out = run(parsed)

    assert "**Maintenance**" in out
    assert "anthropic / claude-opus-4-7" in out
    # cache_state shows up unaltered.
    assert "cache state: hot" in out
    # No-data rows render "never" / "unknown" defaults.
    assert "last api call: never" in out
    _ = conv_id  # keeps the linter quiet — seeded for the resolver


# ---------------------------------------------------------------------------
# AC: graceful degradation on store-read errors
# ---------------------------------------------------------------------------


def test_store_read_exception_is_logged_not_raised(
    migrated_db: sqlite3.Connection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A store raising on read → warning logged, no crash, section omitted.

    Mirrors the production-robustness mandate: ``/lcm status`` is an
    operator-facing diagnostic; a transient store error must not
    crash the whole command.
    """
    _seed_conversation_and_messages(migrated_db, session_id="sess-1")

    class _BrokenStore:
        def get_conversation_compaction_maintenance(self, _: int) -> Any:
            raise RuntimeError("synthetic test failure")

        def get_conversation_compaction_telemetry(self, _: int) -> Any:
            raise RuntimeError("synthetic test failure")

    engine = _make_engine(
        db=migrated_db,
        current_session_id="sess-1",
        maintenance_store=_BrokenStore(),
        telemetry_store=_BrokenStore(),
    )
    parsed = SimpleNamespace(engine=engine)

    import logging

    with caplog.at_level(logging.WARNING, logger="lossless_hermes.commands.status"):
        out = run(parsed)

    # Both store errors were logged.
    assert any(
        "store read failed" in rec.message and "synthetic" in rec.message for rec in caplog.records
    ), f"Expected store-read-failed log; got: {caplog.records}"
    # And the per-conversation block still rendered (without Maintenance).
    assert "**Current conversation**" in out
    assert "**Maintenance**" not in out


# ---------------------------------------------------------------------------
# AC: full end-to-end snapshot (Plugin + Global + Current + Maintenance)
# ---------------------------------------------------------------------------


def test_full_snapshot_all_sections_render(migrated_db: sqlite3.Connection) -> None:
    """Sanity check: every section header is present in a fully-populated run."""
    conv_id = _seed_conversation_and_messages(
        migrated_db,
        session_id="sess-1",
        session_key="agent:main:abc",
        message_count=2,
    )
    _seed_summary(migrated_db, conversation_id=conv_id, summary_id="s1", kind="leaf")

    class _FakeTelemetryStore:
        def get_conversation_compaction_telemetry(self, _: int) -> Any:
            return SimpleNamespace(
                last_api_call_at=None,
                last_cache_touch_at=None,
                retention="default",
                cache_state="cold",
                provider="anthropic",
                model="claude-opus-4-7",
            )

    class _NullMaintenanceStore:
        def get_conversation_compaction_maintenance(self, _: int) -> None:
            return None

    engine = _make_engine(
        db=migrated_db,
        current_session_id="sess-1",
        maintenance_store=_NullMaintenanceStore(),
        telemetry_store=_FakeTelemetryStore(),
    )
    parsed = SimpleNamespace(engine=engine)
    out = run(parsed)

    # All five surface elements (header + four sections) are present.
    assert "Lossless Hermes v" in out
    assert "**Plugin**" in out
    assert "**Global**" in out
    assert "**Current conversation**" in out
    assert "**Maintenance**" in out
    # And the doctor stub row is visible (Epic 08-05/06 placeholder).
    assert "doctor: pending Epic 08-05/06" in out


# ---------------------------------------------------------------------------
# AC line 73: test_last_backup_when_none_exist
# ---------------------------------------------------------------------------
#
# The 08-02 spec lists a "Last backup" row whose semantics are
# "newest file matching <db_path>.*.bak; absence prints 'never'"
# (spec line 52). The TS source surfaces this in the Plugin section
# via its own helper. The Python port at 08-02 does NOT yet implement
# the .bak-glob scan: that primitive lands with Epic 08-09 (backup
# subcommand) which adds the glob + mtime walker. To keep the AC
# enforceable today we test that the current implementation does NOT
# crash on a directory with zero .bak files — the eventual integration
# can then test the "never" string when the glob walker lands.
# ---------------------------------------------------------------------------


def test_last_backup_when_none_exist(migrated_db: sqlite3.Connection, tmp_path: Path) -> None:
    """Empty DB dir (no ``.bak`` files) does not crash status output.

    Per spec line 73, the AC is "empty DB dir prints 'never' not a
    stack trace." The eventual ``.bak`` integration lands in Epic
    08-09; until then, this test guards that an absent ``.bak`` glob
    does not raise during the status read path.
    """
    db_file = tmp_path / "lcm.db"
    db_file.write_bytes(b"")
    engine = _make_engine(
        db=migrated_db,
        current_session_id=None,
        database_path=str(db_file),
    )
    parsed = SimpleNamespace(engine=engine)
    out = run(parsed)
    # Plugin section renders without raising.
    assert "**Plugin**" in out
    assert str(db_file) in out
