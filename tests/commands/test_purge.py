"""Tests for :mod:`lossless_hermes.commands.purge` (issue 08-04).

Exercises the slash-command handler that wraps
:func:`lossless_hermes.operator.purge.run_purge`. The operator-facing
text output and the parse / dispatch layer are pinned here; the
underlying cascade is covered by ``tests/operator/test_purge.py``.

Test inventory:

* Missing-reason short-circuit renders ``missing_reason`` block.
* Empty-criteria dry-run renders ``no_criteria`` block (Wave-2 BUG-5).
* ``agent:main:main`` without flag in ``--apply`` mode renders
  ``main_session_blocked`` outcome.
* Dry-run preview echoes ``would-affect-leaves`` count.
* ``--apply`` happy path renders ``completed`` with affected count and
  purge_session_id.
* Unknown flag renders ``parse_error``.
* Bare positional arg renders ``parse_error`` with "Did you mean
  ``--session-key``?" hint (Wave-12 P2 regression).
* DB-unavailable engine renders ``unavailable`` block (no AttributeError).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from lossless_hermes.commands.purge import run as run_purge_command
from lossless_hermes.db.migration import run_lcm_migrations


@dataclass
class _FakeEngine:
    """Minimal engine stub exposing ``db_connection``.

    The real :class:`LCMEngine` exposes the connection via a property
    backed by an `_db`-like attribute; for the handler-level test the
    handler's ``_resolve_db`` probes both ``db_connection`` and ``_db``
    so this stub matches either path.
    """

    db_connection: sqlite3.Connection | None


@dataclass
class _FakeParsed:
    """Minimal :class:`ParsedLcmCommand`-shaped stub for tests."""

    tokens: list[str]
    engine: _FakeEngine
    name: str = "purge"
    raw_args: str = ""
    flags: dict[str, object] | None = None

    def __post_init__(self) -> None:
        if self.flags is None:
            self.flags = {}


@pytest.fixture
def db_with_seed() -> Iterator[sqlite3.Connection]:
    """In-memory DB with the migration ladder + a seeded leaf for apply tests."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(conn, fts5_available=False, seed_default_prompts=False)
    conn.execute("INSERT INTO conversations (session_id, session_key) VALUES ('s1', 'sk1')")
    conn.execute(
        """
        INSERT INTO summaries
          (summary_id, conversation_id, kind, content, token_count, session_key)
          VALUES ('leaf_a', 1, 'leaf', 'x', 100, 'sk1')
        """
    )
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Missing-reason branch
# ---------------------------------------------------------------------------


def test_missing_reason_renders_rejected_block(db_with_seed: sqlite3.Connection) -> None:
    """No ``--reason`` flag → ``missing_reason`` rejection text."""
    parsed = _FakeParsed(
        tokens=["--session-key", "sk1", "--apply"],
        engine=_FakeEngine(db_connection=db_with_seed),
    )
    output = run_purge_command(parsed)
    assert "missing_reason" in output
    assert "Pass `--reason" in output


# ---------------------------------------------------------------------------
# No-criteria branch (Wave-2 BUG-5)
# ---------------------------------------------------------------------------


def test_empty_criteria_dry_run_renders_no_criteria(
    db_with_seed: sqlite3.Connection,
) -> None:
    """Dry-run with only ``--reason`` set → ``no_criteria`` rejection.

    Wave-2 BUG-5 regression: previously empty-criteria dry-run returned
    a whole-DB count and scared operators.
    """
    parsed = _FakeParsed(
        tokens=["--reason", "noop"],
        engine=_FakeEngine(db_connection=db_with_seed),
    )
    output = run_purge_command(parsed)
    assert "no_criteria" in output
    assert "Pass at least one of" in output


# ---------------------------------------------------------------------------
# Preview (dry-run) happy path
# ---------------------------------------------------------------------------


def test_preview_renders_would_affect_count(db_with_seed: sqlite3.Connection) -> None:
    """Dry-run output contains the preview count and re-run hint."""
    parsed = _FakeParsed(
        tokens=["--reason", "dry-run", "--session-key", "sk1"],
        engine=_FakeEngine(db_connection=db_with_seed),
    )
    output = run_purge_command(parsed)
    assert "would-affect-leaves: 1" in output
    assert "Re-run" in output
    # Sanity: the DB was NOT modified (no rows suppressed).
    leaf_suppressed_at = db_with_seed.execute(
        "SELECT suppressed_at FROM summaries WHERE summary_id = 'leaf_a'"
    ).fetchone()[0]
    assert leaf_suppressed_at is None


def test_preview_warns_about_main_session_without_flag(
    db_with_seed: sqlite3.Connection,
) -> None:
    """Dry-run with ``agent:main:main`` and no flag surfaces a warning.

    Per TS ``buildPurgeText`` lines 2301-2309: preview surfaces a
    warning that apply will be blocked.
    """
    parsed = _FakeParsed(
        tokens=["--reason", "warn", "--session-key", "agent:main:main"],
        engine=_FakeEngine(db_connection=db_with_seed),
    )
    output = run_purge_command(parsed)
    assert "warning" in output.lower()
    assert "agent:main:main" in output


# ---------------------------------------------------------------------------
# Apply happy path
# ---------------------------------------------------------------------------


def test_apply_renders_completed_with_purge_session_id(
    db_with_seed: sqlite3.Connection,
) -> None:
    """``--apply`` runs the cascade and renders ``completed``."""
    parsed = _FakeParsed(
        tokens=["--reason", "go", "--summary-ids", "leaf_a", "--apply"],
        engine=_FakeEngine(db_connection=db_with_seed),
    )
    output = run_purge_command(parsed)
    assert "completed" in output
    assert "affected leaves:  1" in output
    assert "purge_" in output  # purge_session_id token
    # And the DB IS modified:
    leaf_suppressed_at = db_with_seed.execute(
        "SELECT suppressed_at FROM summaries WHERE summary_id = 'leaf_a'"
    ).fetchone()[0]
    assert leaf_suppressed_at is not None


def test_apply_main_session_blocked_without_flag(
    db_with_seed: sqlite3.Connection,
) -> None:
    """``--apply --session-key agent:main:main`` without ``--allow-main-session``
    renders ``main_session_blocked``."""
    parsed = _FakeParsed(
        tokens=[
            "--reason",
            "test",
            "--session-key",
            "agent:main:main",
            "--apply",
        ],
        engine=_FakeEngine(db_connection=db_with_seed),
    )
    output = run_purge_command(parsed)
    assert "main_session_blocked" in output


# ---------------------------------------------------------------------------
# Parse-error branches
# ---------------------------------------------------------------------------


def test_unknown_flag_renders_parse_error(db_with_seed: sqlite3.Connection) -> None:
    """``--bogus`` flag → ``parse_error`` rejection."""
    parsed = _FakeParsed(
        tokens=["--reason", "r", "--session-key", "sk1", "--bogus"],
        engine=_FakeEngine(db_connection=db_with_seed),
    )
    output = run_purge_command(parsed)
    assert "parse_error" in output
    assert "--bogus" in output


def test_bare_positional_renders_parse_error_with_hint(
    db_with_seed: sqlite3.Connection,
) -> None:
    """Wave-12 P2 regression: ``purge sk1`` (bare positional) hints
    ``--session-key``."""
    parsed = _FakeParsed(
        tokens=["sk1", "--reason", "r"],
        engine=_FakeEngine(db_connection=db_with_seed),
    )
    output = run_purge_command(parsed)
    assert "parse_error" in output
    assert "Did you mean `--session-key sk1`" in output


def test_bad_iso_timestamp_renders_parse_error(db_with_seed: sqlite3.Connection) -> None:
    """Invalid ``--since`` value → ``parse_error``."""
    parsed = _FakeParsed(
        tokens=["--reason", "r", "--since", "notadate"],
        engine=_FakeEngine(db_connection=db_with_seed),
    )
    output = run_purge_command(parsed)
    assert "parse_error" in output


def test_negative_min_token_count_renders_parse_error(
    db_with_seed: sqlite3.Connection,
) -> None:
    """Negative ``--min-token-count`` → ``parse_error``."""
    parsed = _FakeParsed(
        tokens=["--reason", "r", "--min-token-count", "-5"],
        engine=_FakeEngine(db_connection=db_with_seed),
    )
    output = run_purge_command(parsed)
    assert "parse_error" in output


# ---------------------------------------------------------------------------
# Engine without DB
# ---------------------------------------------------------------------------


def test_engine_without_db_renders_unavailable() -> None:
    """No engine connection → ``unavailable`` text, no exception."""
    parsed = _FakeParsed(
        tokens=["--reason", "r", "--session-key", "sk1"],
        engine=_FakeEngine(db_connection=None),
    )
    output = run_purge_command(parsed)
    assert "unavailable" in output
