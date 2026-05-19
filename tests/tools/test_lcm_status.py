"""Tests for the ``lcm_status`` model-callable diagnostic tool (ADR-035, #135).

Exercises :mod:`lossless_hermes.tools.status` — the read-only,
model-callable LCM health-snapshot tool added by
[ADR-035](../../docs/adr/035-lcm-status-doctor-model-tools.md). The tool
wraps :func:`lossless_hermes.commands.status.run` (the ``/lcm status``
command body) and caps the output to the tool-result budget.

Test inventory:

* The schema registers in the tool registry and is well-formed.
* The schema declares no parameters (ADR-035 §Consequences — empty-param).
* The handler dispatches via :data:`lossless_hermes.engine.TOOL_DISPATCH`.
* The handler returns a structured read-only payload (a ``report``
  field, ``ok: True``).
* The handler is NOT owner-gated — it runs without any permission /
  policy probe (read-only per ADR-013).
* The handler is NOT in :data:`TOKEN_GATE_TOOLS` — a self-diagnosis
  tool must stay callable when context is near-full.
* The output is capped: an oversized status body is truncated and the
  ``capped`` flag is set.
* ``ctx=None`` (engine still booting) → structured ``engine-unavailable``.
* The tool delegates to the command body (no new diagnostic logic) —
  the report content matches what ``commands.status.run`` produced.

See:

* ``docs/adr/035-lcm-status-doctor-model-tools.md`` — the decision.
* ``src/lossless_hermes/tools/status.py`` — the tool under test.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from typing import Any

import pytest

from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.engine import TOOL_DISPATCH
from lossless_hermes.plugin.needs_compact_gate import TOKEN_GATE_TOOLS
from lossless_hermes.tools import get_tool_schemas
from lossless_hermes.tools._diagnostics import DIAGNOSTIC_TOOL_OUTPUT_CHAR_CAP
from lossless_hermes.tools.status import (
    LCM_STATUS_SCHEMA,
    handle_lcm_status,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def migrated_db() -> sqlite3.Connection:
    """In-memory SQLite with the full LCM migration ladder applied."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(conn, fts5_available=False, seed_default_prompts=False)
    return conn


def _make_engine(
    db: sqlite3.Connection | None = None,
    current_session_id: str | None = None,
    database_path: str = "",
) -> Any:
    """Minimal engine stub exposing the attributes the status body reads.

    ``commands.status.run`` reads ``engine._db``,
    ``engine.current_session_id``, ``engine.config.database_path``, and
    the optional ``_maintenance_store`` / ``_telemetry_store`` (omitted
    here — the body guards on ``getattr(...) is None``).
    """
    return SimpleNamespace(
        _db=db,
        current_session_id=current_session_id,
        config=SimpleNamespace(database_path=database_path),
    )


# ---------------------------------------------------------------------------
# Schema registration + shape
# ---------------------------------------------------------------------------


def test_lcm_status_schema_is_registered() -> None:
    """``lcm_status`` appears in the tool-schema registry.

    Per ADR-035 §Consequences, the schema registers via the import-time
    ``TOOL_SCHEMAS.append(...)`` pattern.
    """
    names = {s["name"] for s in get_tool_schemas()}
    assert "lcm_status" in names


def test_lcm_status_schema_has_empty_parameters() -> None:
    """ADR-035 §Consequences: the schema takes NO parameters.

    A status snapshot operates on the engine's current conversation +
    DB — there is nothing for the model to supply.
    """
    params = LCM_STATUS_SCHEMA["parameters"]
    assert params["type"] == "object"
    assert params["properties"] == {}
    assert params["required"] == []


def test_lcm_status_schema_has_openai_keys() -> None:
    """The schema is a well-formed OpenAI function-call descriptor."""
    assert LCM_STATUS_SCHEMA["name"] == "lcm_status"
    assert isinstance(LCM_STATUS_SCHEMA["description"], str)
    assert LCM_STATUS_SCHEMA["description"].strip()
    assert "read-only" in LCM_STATUS_SCHEMA["description"].lower()


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def test_lcm_status_is_in_dispatch_table() -> None:
    """``lcm_status`` is registered in the engine dispatch table.

    ADR-035 mandates registering the tool in the dispatch table; this
    pins that the registration happened and points at the handler.
    """
    assert TOOL_DISPATCH.get("lcm_status") is handle_lcm_status


def test_lcm_status_dispatches_through_engine(migrated_db: sqlite3.Connection) -> None:
    """A full ``handle_tool_call("lcm_status", ...)`` round-trip works.

    Exercises the real engine dispatch path — the engine passes itself
    as ``ctx``, the handler delegates to the status body, and a JSON
    string comes back.
    """
    from lossless_hermes.engine import LCMEngine

    engine = LCMEngine()
    engine._db = migrated_db
    engine.current_session_id = None
    raw = engine.handle_tool_call("lcm_status", {})
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert "report" in payload


# ---------------------------------------------------------------------------
# Read-only output
# ---------------------------------------------------------------------------


def test_handler_returns_read_only_report(migrated_db: sqlite3.Connection) -> None:
    """The handler returns a structured ``ok: True`` payload with a report."""
    engine = _make_engine(db=migrated_db, current_session_id=None)
    payload = json.loads(handle_lcm_status({}, ctx=engine))
    assert payload["ok"] is True
    assert isinstance(payload["report"], str)
    assert payload["report"].strip()
    # The report is the rendered status text — it carries the header.
    assert "Lossless Hermes" in payload["report"]


def test_handler_delegates_to_command_body(migrated_db: sqlite3.Connection) -> None:
    """The tool adds no diagnostic logic — its report IS the command body's.

    ADR-035 invariant: the handler delegates to ``commands.status.run``.
    With a small DB the status output is well under the cap, so the
    tool's ``report`` is byte-identical to what the command body emits.
    """
    from lossless_hermes.commands.status import run as run_status

    engine = _make_engine(db=migrated_db, current_session_id=None)
    direct = run_status(SimpleNamespace(engine=engine))
    payload = json.loads(handle_lcm_status({}, ctx=engine))
    assert payload["capped"] is False
    assert payload["report"] == direct


def test_handler_ignores_extra_args(migrated_db: sqlite3.Connection) -> None:
    """The empty-param tool ignores any args a sloppy provider sends."""
    engine = _make_engine(db=migrated_db, current_session_id=None)
    payload = json.loads(handle_lcm_status({"unexpected": "value"}, ctx=engine))
    assert payload["ok"] is True


# ---------------------------------------------------------------------------
# Not owner-gated, not token-gated
# ---------------------------------------------------------------------------


def test_lcm_status_is_not_owner_gated(migrated_db: sqlite3.Connection) -> None:
    """The handler runs with no policy / permission probe (read-only).

    ADR-013's owner gate fronts *destructive* ``/lcm`` subcommands. A
    status snapshot mutates nothing — the handler has no gate. The
    engine stub here exposes no ``allow_admin_from`` / policy surface at
    all; a successful call proves no gate was consulted.
    """
    engine = _make_engine(db=migrated_db, current_session_id=None)
    payload = json.loads(handle_lcm_status({}, ctx=engine))
    assert payload["ok"] is True


def test_lcm_status_is_not_token_gated() -> None:
    """``lcm_status`` is NOT in TOKEN_GATE_TOOLS.

    A self-diagnosis tool must stay callable when context is near-full
    — that is exactly when the model needs to observe LCM's state.
    """
    assert "lcm_status" not in TOKEN_GATE_TOOLS


# ---------------------------------------------------------------------------
# Output cap (ADR-035 mandatory caveat)
# ---------------------------------------------------------------------------


def test_handler_caps_oversized_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """An oversized status body is capped to the tool-result budget.

    ADR-035 §Consequences makes capping mandatory. We stub the command
    body to return a huge string and assert the handler caps it: the
    ``report`` is ``<= DIAGNOSTIC_TOOL_OUTPUT_CHAR_CAP`` and ``capped``
    is ``True``.
    """
    huge = "status line\n" * 5_000  # well over the cap
    monkeypatch.setattr(
        "lossless_hermes.tools.status._run_status",
        lambda parsed: huge,
    )
    engine = _make_engine()
    payload = json.loads(handle_lcm_status({}, ctx=engine))
    assert payload["ok"] is True
    assert payload["capped"] is True
    assert len(payload["report"]) <= DIAGNOSTIC_TOOL_OUTPUT_CHAR_CAP
    assert "/lcm status" in payload["report"]


def test_handler_does_not_cap_small_output(migrated_db: sqlite3.Connection) -> None:
    """A normal-sized status report is not flagged as capped."""
    engine = _make_engine(db=migrated_db, current_session_id=None)
    payload = json.loads(handle_lcm_status({}, ctx=engine))
    assert payload["capped"] is False


# ---------------------------------------------------------------------------
# Engine-unavailable + exception arms
# ---------------------------------------------------------------------------


def test_handler_handles_missing_engine() -> None:
    """``ctx=None`` (plugin still booting) → structured engine-unavailable."""
    payload = json.loads(handle_lcm_status({}, ctx=None))
    assert payload["ok"] is False
    assert payload["reason"] == "engine-unavailable"


def test_handler_never_raises_on_command_body_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the delegated body raises, the handler returns a structured failure.

    ``commands.status.run`` is written to catch its own errors, but the
    handler must never propagate a stack trace to the dispatcher.
    """

    def _boom(parsed: Any) -> str:
        raise RuntimeError("simulated status failure")

    monkeypatch.setattr("lossless_hermes.tools.status._run_status", _boom)
    payload = json.loads(handle_lcm_status({}, ctx=_make_engine()))
    assert payload["ok"] is False
    assert payload["reason"] == "exception"
    assert "simulated status failure" in payload["note"]
