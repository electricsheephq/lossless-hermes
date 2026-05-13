"""Tests for ``/lcm health`` (issue 08-03).

Verifies the command-level dispatcher integration: the formatted
markdown output, header/section layout, graceful degradation when the
engine isn't yet open, and the ``parsed.engine`` contract.

The deep snapshot-shape tests live in :mod:`tests.operator.test_health`.
Tests here exercise the formatting helpers + the public ``run`` entry
point.

See:

* ``epics/08-cli-ops/08-03-health.md`` — this issue.
* ``lossless-claw/src/plugin/lcm-command.ts:1714-1724`` and ``:1627-1712``
  — TS source.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from lossless_hermes.commands.health import (
    _format_eval_section,
    _format_worker_line,
    format_v41_health_snapshot,
    run,
)
from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.operator.health import (
    ActiveEmbeddingProfile,
    EmbeddingsHealth,
    EvalHealth,
    MostRecentEvalRun,
    SuppressionHealth,
    SynthesisHealth,
    V41HealthSnapshot,
    WorkerStatus,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _new_db(*, seed_prompts: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(conn, fts5_available=False, seed_default_prompts=seed_prompts)
    return conn


@pytest.fixture
def migrated_db() -> Iterator[sqlite3.Connection]:
    conn = _new_db()
    try:
        yield conn
    finally:
        conn.close()


def _make_engine(
    db: sqlite3.Connection | None,
    database_path: str = "",
) -> Any:
    """Minimal engine stub. ``/lcm health`` reads ``engine._db`` only."""
    config = SimpleNamespace(database_path=database_path)
    return SimpleNamespace(_db=db, config=config, current_session_id=None)


# ---------------------------------------------------------------------------
# Snapshot factories — pure data, no DB
# ---------------------------------------------------------------------------


def _empty_snapshot() -> V41HealthSnapshot:
    return V41HealthSnapshot(
        embeddings=EmbeddingsHealth(
            active_profile=None,
            vec0_version=None,
            pending_backfill=0,
            embedded_count=0,
            over_cap_pending=0,
        ),
        workers=tuple(),
        synthesis=SynthesisHealth(
            active_prompt_count=0,
            distinct_memory_type_count=0,
            recent_synthesis_runs_7d=0,
            total_audit_rows=0,
            started_rows_older_than_1h=0,
            completed_or_failed_older_than_30d=0,
        ),
        eval=EvalHealth(
            query_set_count=0,
            most_recent_run=None,
            drift_index=None,
        ),
        suppression=SuppressionHealth(suppressed_leaves=0),
    )


# ---------------------------------------------------------------------------
# AC: dispatcher misconfig — no engine on parsed
# ---------------------------------------------------------------------------


def test_run_without_engine_returns_friendly_error() -> None:
    parsed = SimpleNamespace()
    out = run(parsed)
    assert "dispatcher misconfigured" in out
    assert "no engine reference" in out


# ---------------------------------------------------------------------------
# AC: pre-on_session_start branch — _db is None
# ---------------------------------------------------------------------------


def test_run_with_engine_no_db_returns_header_plus_hint() -> None:
    engine = _make_engine(db=None)
    parsed = SimpleNamespace(engine=engine)
    out = run(parsed)
    # Header renders.
    assert "Lossless Hermes v" in out
    # Section header for v4.1 Health is present.
    assert "### v4.1 Health" in out
    # Status footer reports the deferred-init state.
    assert "**Status**" in out
    assert "not yet opened" in out
    # No snapshot sections — DB isn't open.
    assert "**Embeddings**" not in out
    assert "**Workers**" not in out


# ---------------------------------------------------------------------------
# AC: full snapshot render — all five sections present
# ---------------------------------------------------------------------------


def test_run_with_open_db_renders_five_sections(
    migrated_db: sqlite3.Connection,
) -> None:
    """Bare fresh DB → all five sections render with sentinel values."""
    engine = _make_engine(db=migrated_db)
    parsed = SimpleNamespace(engine=engine)
    out = run(parsed)
    assert "Lossless Hermes v" in out
    assert "### v4.1 Health" in out
    assert "**Embeddings**" in out
    assert "**Workers**" in out
    assert "**Synthesis**" in out
    assert "**Eval**" in out
    assert "**Suppression**" in out


# ---------------------------------------------------------------------------
# AC: format_v41_health_snapshot line-for-line modulo whitespace
# ---------------------------------------------------------------------------


def test_format_snapshot_returns_five_sections_with_blank_separators() -> None:
    sections = format_v41_health_snapshot(_empty_snapshot())
    # Each non-empty section is a Markdown block (header + indented lines).
    section_titles = [s for s in sections if s and s.startswith("**")]
    assert len(section_titles) == 5
    # Sections separated by empty-string entries.
    empty_count = sum(1 for s in sections if s == "")
    assert empty_count == 4  # 5 sections → 4 separators between them


def test_format_embeddings_no_profile() -> None:
    out = "\n".join(format_v41_health_snapshot(_empty_snapshot()))
    assert "active model: NOT REGISTERED" in out
    assert "vec0 status: NOT LOADED" in out
    assert "pending backfill: 0 docs" in out
    assert "embedded count: 0" in out


def test_format_embeddings_with_profile() -> None:
    snap = _empty_snapshot()
    snap = V41HealthSnapshot(
        embeddings=EmbeddingsHealth(
            active_profile=ActiveEmbeddingProfile(
                model_name="voyage-3",
                dim=1024,
                registered_at="2025-05-13T12:00:00Z",
            ),
            vec0_version="v0.1.6",
            pending_backfill=42,
            embedded_count=1_247,
            over_cap_pending=0,
        ),
        workers=snap.workers,
        synthesis=snap.synthesis,
        eval=snap.eval,
        suppression=snap.suppression,
    )
    out = "\n".join(format_v41_health_snapshot(snap))
    assert "active model: voyage-3 (dim=1,024)" in out
    assert "vec0 status: v0.1.6" in out
    assert "pending backfill: 42 docs" in out
    assert "embedded count: 1,247" in out


def test_format_embeddings_over_cap_row_only_when_positive() -> None:
    """Per Wave-4 Auditor #15 P1: only render the over-cap row when > 0."""
    snap = _empty_snapshot()
    snap_zero = V41HealthSnapshot(
        embeddings=EmbeddingsHealth(
            active_profile=None,
            vec0_version=None,
            pending_backfill=0,
            embedded_count=0,
            over_cap_pending=0,
        ),
        workers=snap.workers,
        synthesis=snap.synthesis,
        eval=snap.eval,
        suppression=snap.suppression,
    )
    out_zero = "\n".join(format_v41_health_snapshot(snap_zero))
    assert "over-cap leaves" not in out_zero

    snap_positive = V41HealthSnapshot(
        embeddings=EmbeddingsHealth(
            active_profile=None,
            vec0_version=None,
            pending_backfill=0,
            embedded_count=0,
            over_cap_pending=3,
        ),
        workers=snap.workers,
        synthesis=snap.synthesis,
        eval=snap.eval,
        suppression=snap.suppression,
    )
    out_positive = "\n".join(format_v41_health_snapshot(snap_positive))
    assert "over-cap leaves" in out_positive
    assert "3 — re-summarize" in out_positive


# ---------------------------------------------------------------------------
# AC: worker line formatter — matches TS formatWorkerLine output
# ---------------------------------------------------------------------------


def test_format_worker_line_idle() -> None:
    w = WorkerStatus(
        job_kind="embedding-backfill",
        active=False,
        worker_id=None,
        acquired_at=None,
        expires_at=None,
        expired=False,
    )
    assert _format_worker_line(w) == "embedding-backfill: (idle)"


def test_format_worker_line_active_not_expired() -> None:
    w = WorkerStatus(
        job_kind="extraction",
        active=True,
        worker_id="worker-pid-1234",
        acquired_at="2025-05-13 12:00:00",
        expires_at="2025-05-13 12:01:30",
        expired=False,
    )
    line = _format_worker_line(w)
    assert line == (
        "extraction: worker_id=worker-pid-1234 "
        "acquired_at=2025-05-13 12:00:00 expires_at=2025-05-13 12:01:30"
    )
    # EXPIRED marker absent.
    assert "EXPIRED" not in line


def test_format_worker_line_active_expired() -> None:
    w = WorkerStatus(
        job_kind="condensation",
        active=True,
        worker_id="worker-pid-1234",
        acquired_at="2025-05-13 11:00:00",
        expires_at="2025-05-13 11:01:30",
        expired=True,
    )
    line = _format_worker_line(w)
    # EXPIRED marker appears IMMEDIATELY after worker_id, no separator.
    assert "worker_id=worker-pid-1234 EXPIRED" in line


def test_format_worker_line_missing_fields_render_unknown() -> None:
    """Active row with missing ids → ``"unknown"`` placeholder.

    Defensive: ``lock_info`` should always populate these, but if the
    upstream sends ``None`` we should still produce a parseable line.
    """
    w = WorkerStatus(
        job_kind="eval",
        active=True,
        worker_id=None,
        acquired_at=None,
        expires_at=None,
        expired=False,
    )
    line = _format_worker_line(w)
    assert "worker_id=unknown" in line
    assert "acquired_at=unknown" in line
    assert "expires_at=unknown" in line


# ---------------------------------------------------------------------------
# AC: eval section formatting
# ---------------------------------------------------------------------------


def test_format_eval_no_baseline() -> None:
    snap = _empty_snapshot()
    out = _format_eval_section(snap.eval)
    assert "query sets registered: 0" in out
    assert "most-recent run: (none)" in out
    assert "drift index: (no baseline)" in out


def test_format_eval_with_recent_run() -> None:
    eval_health = EvalHealth(
        query_set_count=2,
        most_recent_run=MostRecentEvalRun(
            run_id="run-abc",
            query_set_id="qs-1",
            mode="hybrid",
            recall_score=0.84321,
        ),
        drift_index=-0.0234,
    )
    out = _format_eval_section(eval_health)
    assert "query sets registered: 2" in out
    # recall is .3f → 0.843.
    assert "qs-1 mode=hybrid recall=0.843 (run_id=run-abc)" in out
    # Negative drift renders WITHOUT a "+" prefix; .4f → -0.0234.
    assert "drift index: -0.0234" in out


def test_format_eval_positive_drift_gets_plus_prefix() -> None:
    eval_health = EvalHealth(
        query_set_count=0,
        most_recent_run=None,
        drift_index=0.0123,
    )
    out = _format_eval_section(eval_health)
    assert "drift index: +0.0123" in out


def test_format_eval_zero_drift_gets_plus_prefix() -> None:
    """``drift >= 0`` → ``"+"`` per TS source: ``drift >= 0 ? "+" : ""``."""
    eval_health = EvalHealth(
        query_set_count=0,
        most_recent_run=None,
        drift_index=0.0,
    )
    out = _format_eval_section(eval_health)
    assert "drift index: +0.0000" in out


# ---------------------------------------------------------------------------
# AC: end-to-end render shape
# ---------------------------------------------------------------------------


def test_end_to_end_run_output_shape(migrated_db: sqlite3.Connection) -> None:
    """Smoke: ``run`` output is a non-empty multi-line markdown string."""
    engine = _make_engine(db=migrated_db)
    parsed = SimpleNamespace(engine=engine)
    out = run(parsed)
    lines = out.split("\n")
    # Header (2 lines) + blank + section header + blank + 5 sections + 4 blanks.
    # Don't assert exact line count (formatting may evolve), but check minimums.
    assert len(lines) > 10
    # First two lines are the header.
    assert lines[0].startswith("**Lossless Hermes v")
    # Third line is blank.
    assert lines[2] == ""
    # Fourth line is the section header.
    assert lines[3] == "### v4.1 Health"
