"""Tests for the ``lcm_doctor`` model-callable diagnostic tool (ADR-035, #135).

Exercises :mod:`lossless_hermes.tools.doctor` — the read-only,
model-callable LCM integrity-scan tool added by
[ADR-035](../../docs/adr/035-lcm-status-doctor-model-tools.md). The tool
wraps :func:`lossless_hermes.doctor.shared.get_doctor_summary_stats`
(the existing, tested read-only scan; ``commands/doctor.py::run_scan``
is still a stub) and caps the output to the tool-result budget.

Test inventory:

* The schema registers in the tool registry and is well-formed.
* The schema declares no parameters (ADR-035 §Consequences — empty-param).
* The handler dispatches via :data:`lossless_hermes.engine.TOOL_DISPATCH`.
* On a clean DB the handler reports ``verdict: healthy`` and mutates
  nothing (read-only — the scan does not change row counts).
* On a DB with broken summaries the handler reports ``degraded`` and
  enumerates the affected summary IDs.
* The handler is NOT owner-gated (read-only per ADR-013) and NOT in
  :data:`TOKEN_GATE_TOOLS`.
* The finding list is capped at the ADR-035 ~20-finding cap with a
  ``"+N more"`` tail; the aggregate counts are always present.
* ``ctx=None`` / no DB → structured ``engine-unavailable`` /
  ``db-unavailable``.

See:

* ``docs/adr/035-lcm-status-doctor-model-tools.md`` — the decision.
* ``src/lossless_hermes/tools/doctor.py`` — the tool under test.
* ``src/lossless_hermes/doctor/shared.py`` — the wrapped read-only scan.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from typing import Any

import pytest

from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.doctor.contract import FALLBACK_SUMMARY_MARKER_V41_TRUNC
from lossless_hermes.engine import TOOL_DISPATCH
from lossless_hermes.plugin.needs_compact_gate import TOKEN_GATE_TOOLS
from lossless_hermes.tools import get_tool_schemas
from lossless_hermes.tools._diagnostics import (
    DIAGNOSTIC_DOCTOR_FINDING_CAP,
    DIAGNOSTIC_TOOL_OUTPUT_CHAR_CAP,
)
from lossless_hermes.tools.doctor import (
    LCM_DOCTOR_SCHEMA,
    handle_lcm_doctor,
)


# ---------------------------------------------------------------------------
# Fixtures + seed helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def migrated_db() -> sqlite3.Connection:
    """In-memory SQLite with the migration ladder + one conversation."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(conn, fts5_available=False, seed_default_prompts=False)
    conn.execute("INSERT INTO conversations (session_id, session_key) VALUES ('s1', 'sk1')")
    return conn


def _insert_broken_leaf(
    db: sqlite3.Connection,
    *,
    summary_id: str,
    conversation_id: int = 1,
) -> None:
    """Insert a leaf summary carrying a v4.1 fallback marker (a doctor finding).

    The fallback marker as a content PREFIX classifies as
    :attr:`~lossless_hermes.doctor.contract.DoctorMarkerKind.FALLBACK`
    — exactly the kind the doctor scan counts as broken.
    """
    db.execute(
        """
        INSERT INTO summaries
          (summary_id, conversation_id, kind, content, depth, token_count)
          VALUES (?, ?, 'leaf', ?, 0, 100)
        """,
        (
            summary_id,
            conversation_id,
            f"{FALLBACK_SUMMARY_MARKER_V41_TRUNC}\n\nbroken body for {summary_id}",
        ),
    )


def _make_engine(db: sqlite3.Connection | None = None) -> Any:
    """Minimal engine stub — the doctor tool only needs ``_db``."""
    return SimpleNamespace(_db=db)


# ---------------------------------------------------------------------------
# Schema registration + shape
# ---------------------------------------------------------------------------


def test_lcm_doctor_schema_is_registered() -> None:
    """``lcm_doctor`` appears in the tool-schema registry."""
    names = {s["name"] for s in get_tool_schemas()}
    assert "lcm_doctor" in names


def test_lcm_doctor_schema_has_empty_parameters() -> None:
    """ADR-035 §Consequences: the schema takes NO parameters.

    A whole-DB integrity scan needs no model-supplied input.
    """
    params = LCM_DOCTOR_SCHEMA["parameters"]
    assert params["type"] == "object"
    assert params["properties"] == {}
    assert params["required"] == []


def test_lcm_doctor_schema_has_openai_keys() -> None:
    """The schema is a well-formed OpenAI function-call descriptor."""
    assert LCM_DOCTOR_SCHEMA["name"] == "lcm_doctor"
    assert isinstance(LCM_DOCTOR_SCHEMA["description"], str)
    desc = LCM_DOCTOR_SCHEMA["description"].lower()
    assert "read-only" in desc
    # The description must make clear the tool never repairs (the write
    # path stays slash-only) — ADR-035 §"Open questions" row 3.
    assert "scan only" in desc or "does not repair" in desc


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def test_lcm_doctor_is_in_dispatch_table() -> None:
    """``lcm_doctor`` is registered in the engine dispatch table."""
    assert TOOL_DISPATCH.get("lcm_doctor") is handle_lcm_doctor


def test_lcm_doctor_dispatches_through_engine(migrated_db: sqlite3.Connection) -> None:
    """A full ``handle_tool_call("lcm_doctor", ...)`` round-trip works."""
    from lossless_hermes.engine import LCMEngine

    engine = LCMEngine()
    engine._db = migrated_db
    raw = engine.handle_tool_call("lcm_doctor", {})
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert "report" in payload


# ---------------------------------------------------------------------------
# Read-only scan output — clean + degraded
# ---------------------------------------------------------------------------


def test_clean_db_reports_healthy(migrated_db: sqlite3.Connection) -> None:
    """A DB with no broken summaries scans as ``healthy``."""
    payload = json.loads(handle_lcm_doctor({}, ctx=_make_engine(migrated_db)))
    assert payload["ok"] is True
    assert payload["verdict"] == "healthy"
    assert payload["total"] == 0
    assert "healthy" in payload["report"].lower()


def test_broken_summaries_report_degraded(migrated_db: sqlite3.Connection) -> None:
    """A DB with broken summaries scans as ``degraded`` and lists the IDs."""
    _insert_broken_leaf(migrated_db, summary_id="sum_broken1")
    _insert_broken_leaf(migrated_db, summary_id="sum_broken2")
    payload = json.loads(handle_lcm_doctor({}, ctx=_make_engine(migrated_db)))
    assert payload["ok"] is True
    assert payload["verdict"] == "degraded"
    assert payload["total"] == 2
    assert "sum_broken1" in payload["report"]
    assert "sum_broken2" in payload["report"]
    assert "fallback" in payload["report"].lower()


def test_scan_is_read_only(migrated_db: sqlite3.Connection) -> None:
    """The scan mutates nothing — row counts are unchanged after the call.

    ADR-035: ``lcm_doctor`` is a read-only *scan*. Running it must not
    repair, suppress, or delete any summary.
    """
    _insert_broken_leaf(migrated_db, summary_id="sum_x")

    def _counts() -> tuple[int, int]:
        summaries = migrated_db.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
        convs = migrated_db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        return summaries, convs

    before = _counts()
    handle_lcm_doctor({}, ctx=_make_engine(migrated_db))
    handle_lcm_doctor({}, ctx=_make_engine(migrated_db))  # idempotent — twice
    assert _counts() == before
    # The broken summary's content is untouched (not repaired).
    content = migrated_db.execute(
        "SELECT content FROM summaries WHERE summary_id = 'sum_x'"
    ).fetchone()[0]
    assert FALLBACK_SUMMARY_MARKER_V41_TRUNC in content


def test_handler_ignores_extra_args(migrated_db: sqlite3.Connection) -> None:
    """The empty-param tool ignores any args a sloppy provider sends."""
    payload = json.loads(handle_lcm_doctor({"unexpected": 1}, ctx=_make_engine(migrated_db)))
    assert payload["ok"] is True


# ---------------------------------------------------------------------------
# Not owner-gated, not token-gated
# ---------------------------------------------------------------------------


def test_lcm_doctor_is_not_owner_gated(migrated_db: sqlite3.Connection) -> None:
    """The handler runs with no policy / permission probe (read-only).

    The destructive arm — ``/lcm doctor apply`` — is owner-gated and
    stays slash-only. The *scan* tool has no gate; the engine stub here
    exposes no policy surface, and a successful call proves none was
    consulted.
    """
    payload = json.loads(handle_lcm_doctor({}, ctx=_make_engine(migrated_db)))
    assert payload["ok"] is True


def test_lcm_doctor_is_not_token_gated() -> None:
    """``lcm_doctor`` is NOT in TOKEN_GATE_TOOLS — a diagnostic must stay
    callable when context is near-full."""
    assert "lcm_doctor" not in TOKEN_GATE_TOOLS


# ---------------------------------------------------------------------------
# Output cap (ADR-035 mandatory caveat)
# ---------------------------------------------------------------------------


def test_finding_list_is_capped_with_more_tail(migrated_db: sqlite3.Connection) -> None:
    """More than ~20 findings → the list caps and a ``"+N more"`` tail appears.

    ADR-035 §"Open questions" row 1 sets a ~20-finding cap. We seed
    well past it and assert (a) only the cap's worth of summary IDs are
    enumerated, (b) the ``"+N more"`` tail is present, (c) the DB-wide
    total still reflects ALL findings.
    """
    n = DIAGNOSTIC_DOCTOR_FINDING_CAP + 12
    for i in range(n):
        _insert_broken_leaf(migrated_db, summary_id=f"sum_b{i:03d}")
    payload = json.loads(handle_lcm_doctor({}, ctx=_make_engine(migrated_db)))
    assert payload["total"] == n  # aggregate count is complete
    report = payload["report"]
    # The "+N more" tail names the overflow count.
    assert f"+{n - DIAGNOSTIC_DOCTOR_FINDING_CAP} more" in report
    assert "/lcm doctor" in report
    # Only DIAGNOSTIC_DOCTOR_FINDING_CAP summary-id lines are enumerated
    # (count the "  sum_b" indented finding lines).
    enumerated = sum(1 for line in report.splitlines() if line.startswith("  sum_b"))
    assert enumerated == DIAGNOSTIC_DOCTOR_FINDING_CAP


def test_report_stays_within_char_budget(migrated_db: sqlite3.Connection) -> None:
    """Even with many findings the rendered report respects the char cap."""
    for i in range(DIAGNOSTIC_DOCTOR_FINDING_CAP + 50):
        _insert_broken_leaf(migrated_db, summary_id=f"sum_huge{i:04d}")
    payload = json.loads(handle_lcm_doctor({}, ctx=_make_engine(migrated_db)))
    assert len(payload["report"]) <= DIAGNOSTIC_TOOL_OUTPUT_CHAR_CAP


def test_aggregate_counts_always_present(migrated_db: sqlite3.Connection) -> None:
    """The DB-wide + per-conversation counts are in the report regardless of cap.

    Only the per-summary finding *list* is capped; the aggregate counts
    are tiny and the highest-signal part of the scan.
    """
    for i in range(5):
        _insert_broken_leaf(migrated_db, summary_id=f"sum_c{i}")
    payload = json.loads(handle_lcm_doctor({}, ctx=_make_engine(migrated_db)))
    report = payload["report"]
    assert "5 broken summary" in report
    assert "By conversation:" in report
    assert "conversation 1:" in report


# ---------------------------------------------------------------------------
# Engine / DB unavailable + exception arms
# ---------------------------------------------------------------------------


def test_handler_handles_missing_engine() -> None:
    """``ctx=None`` (plugin still booting) → structured engine-unavailable."""
    payload = json.loads(handle_lcm_doctor({}, ctx=None))
    assert payload["ok"] is False
    assert payload["reason"] == "engine-unavailable"


def test_handler_handles_missing_db() -> None:
    """An engine with no DB open yet → structured db-unavailable."""
    payload = json.loads(handle_lcm_doctor({}, ctx=_make_engine(db=None)))
    assert payload["ok"] is False
    assert payload["reason"] == "db-unavailable"


def test_handler_never_raises_on_scan_error(
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: sqlite3.Connection,
) -> None:
    """If the scan raises, the handler returns a structured failure.

    The handler must never propagate a stack trace to the dispatcher.
    """

    def _boom(db: Any, conversation_id: Any = None) -> Any:
        raise RuntimeError("simulated scan failure")

    monkeypatch.setattr(
        "lossless_hermes.tools.doctor.get_doctor_summary_stats",
        _boom,
    )
    payload = json.loads(handle_lcm_doctor({}, ctx=_make_engine(migrated_db)))
    assert payload["ok"] is False
    assert payload["reason"] == "exception"
    assert "simulated scan failure" in payload["note"]
