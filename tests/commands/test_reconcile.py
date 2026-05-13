"""Tests for :mod:`lossless_hermes.commands.reconcile` (issue 08-05).

Exercises the slash-command handler that wraps
:func:`lossless_hermes.operator.reconcile.list_legacy_candidates` and
:func:`lossless_hermes.operator.reconcile.reconcile_session_keys`. The
operator-facing text output and the parse / dispatch layer are pinned
here; the underlying merge is covered by ``tests/operator/test_reconcile.py``.

Test inventory:

* List mode — empty DB renders "No ``legacy:conv_*`` session keys present".
* List mode — populated DB renders candidate lines with conv + leaf counts.
* List mode — combining ``--list-candidates`` and ``--apply`` rejects.
* Apply mode — missing ``--from`` / ``--to`` / ``--reason`` rejects.
* Apply mode — happy path renders ``completed`` block with counts.
* Apply mode — ``main_session_blocked`` from operator surfaces in output.
* Apply mode — unknown flag renders ``parse_error``.
* DB-unavailable engine renders ``unavailable`` block.

The dispatcher's router pre-parses ``--from``, ``--to``, ``--reason``,
``--allow-main-session`` into ``parsed.flags``; we replicate that here
in the ``_FakeParsed`` stub so the handler tests run independently of
the router. End-to-end coverage of the parse → dispatch → handle
pipeline lives in ``tests/commands/test_parse_lcm_command.py`` and
``tests/commands/test_dispatcher.py``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from lossless_hermes.commands.reconcile import run_apply, run_list
from lossless_hermes.db.migration import run_lcm_migrations


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@dataclass
class _FakeEngine:
    """Minimal engine stub exposing ``db_connection``.

    The handler's ``_resolve_db`` probes both ``db_connection`` and
    ``_db`` so this stub matches the canonical path.
    """

    db_connection: sqlite3.Connection | None


@dataclass
class _FakeParsed:
    """Minimal :class:`ParsedLcmCommand`-shaped stub for tests."""

    tokens: list[str]
    engine: _FakeEngine
    flags: dict[str, Any] = field(default_factory=dict)
    name: str = "reconcile-session-keys --apply"
    raw_args: str = ""


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    """In-memory DB with the migration ladder applied."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(conn, fts5_available=False, seed_default_prompts=False)
    try:
        yield conn
    finally:
        conn.close()


def _insert_conv(
    db: sqlite3.Connection,
    session_id: str,
    session_key: str,
    *,
    active: bool = True,
) -> int:
    cursor = db.execute(
        "INSERT INTO conversations (session_id, session_key, active) VALUES (?, ?, ?)",
        (session_id, session_key, 1 if active else 0),
    )
    return cursor.lastrowid  # type: ignore[return-value]


def _insert_leaf(
    db: sqlite3.Connection,
    summary_id: str,
    conversation_id: int,
    session_key: str,
) -> None:
    db.execute(
        "INSERT INTO summaries "
        "(summary_id, conversation_id, kind, content, token_count, session_key) "
        "VALUES (?, ?, 'leaf', 'x', 100, ?)",
        (summary_id, conversation_id, session_key),
    )


# ---------------------------------------------------------------------------
# List mode
# ---------------------------------------------------------------------------


def test_list_renders_empty_when_no_legacy_keys(db: sqlite3.Connection) -> None:
    """Empty DB renders "no candidates" outcome."""
    _insert_conv(db, "s1", "agent:main:main")
    parsed = _FakeParsed(
        tokens=["--list-candidates"],
        engine=_FakeEngine(db_connection=db),
        flags={"list_candidates": True},
    )
    output = run_list(parsed)
    assert "Lossless Claw Reconcile Session Keys" in output
    assert "matched session keys: 0" in output
    assert "No `legacy:conv_*` session keys present" in output


def test_list_renders_candidate_lines(db: sqlite3.Connection) -> None:
    """Populated DB renders one line per candidate with conv + leaf counts."""
    c1 = _insert_conv(db, "s1", "legacy:conv_5", active=False)
    c2 = _insert_conv(db, "s2", "legacy:conv_5", active=False)
    c3 = _insert_conv(db, "s3", "legacy:conv_8")
    _insert_leaf(db, "l1", c1, "legacy:conv_5")
    _insert_leaf(db, "l2", c2, "legacy:conv_5")
    _insert_leaf(db, "l3", c3, "legacy:conv_8")

    parsed = _FakeParsed(
        tokens=["--list-candidates"],
        engine=_FakeEngine(db_connection=db),
        flags={"list_candidates": True},
    )
    output = run_list(parsed)
    assert "matched session keys: 2" in output
    assert "`legacy:conv_5` · convs=2 · leaves=2" in output
    assert "`legacy:conv_8` · convs=1 · leaves=1" in output
    # "Next step" hint with the --apply template.
    assert "--apply --from k1,k2" in output


def test_list_rejects_combined_with_apply(db: sqlite3.Connection) -> None:
    """``--list-candidates`` + ``--apply`` rejects with ``list_and_apply``."""
    parsed = _FakeParsed(
        tokens=["--list-candidates", "--apply"],
        engine=_FakeEngine(db_connection=db),
        flags={"list_candidates": True, "apply": True},
    )
    output = run_list(parsed)
    assert "status: rejected" in output
    assert "kind: list_and_apply" in output


def test_list_handles_unavailable_engine() -> None:
    """No DB connection renders ``unavailable`` block (no AttributeError)."""
    parsed = _FakeParsed(
        tokens=["--list-candidates"],
        engine=_FakeEngine(db_connection=None),
        flags={"list_candidates": True},
    )
    output = run_list(parsed)
    assert "status: unavailable" in output


# ---------------------------------------------------------------------------
# Apply mode — validation branches
# ---------------------------------------------------------------------------


def test_apply_missing_from_renders_rejected(db: sqlite3.Connection) -> None:
    """No ``--from`` → ``missing_from`` rejection."""
    parsed = _FakeParsed(
        tokens=["--apply", "--to", "merged", "--reason", "test"],
        engine=_FakeEngine(db_connection=db),
        flags={"apply": True, "to": "merged", "reason": "test"},
    )
    output = run_apply(parsed)
    assert "status: rejected" in output
    assert "kind: missing_from" in output


def test_apply_missing_to_renders_rejected(db: sqlite3.Connection) -> None:
    """No ``--to`` → ``missing_to`` rejection."""
    parsed = _FakeParsed(
        tokens=["--apply", "--from", "legacy:conv_1", "--reason", "test"],
        engine=_FakeEngine(db_connection=db),
        flags={"apply": True, "from": ["legacy:conv_1"], "reason": "test"},
    )
    output = run_apply(parsed)
    assert "status: rejected" in output
    assert "kind: missing_to" in output


def test_apply_missing_reason_renders_rejected(db: sqlite3.Connection) -> None:
    """No ``--reason`` → ``missing_reason`` rejection."""
    parsed = _FakeParsed(
        tokens=["--apply", "--from", "legacy:conv_1", "--to", "merged"],
        engine=_FakeEngine(db_connection=db),
        flags={"apply": True, "from": ["legacy:conv_1"], "to": "merged"},
    )
    output = run_apply(parsed)
    assert "status: rejected" in output
    assert "kind: missing_reason" in output


def test_apply_unknown_flag_renders_parse_error(db: sqlite3.Connection) -> None:
    """Unknown flag → ``parse_error`` rejection."""
    parsed = _FakeParsed(
        tokens=[
            "--apply",
            "--from",
            "legacy:conv_1",
            "--to",
            "merged",
            "--reason",
            "test",
            "--bogus",
        ],
        engine=_FakeEngine(db_connection=db),
        flags={
            "apply": True,
            "from": ["legacy:conv_1"],
            "to": "merged",
            "reason": "test",
        },
    )
    output = run_apply(parsed)
    assert "status: rejected" in output
    assert "kind: parse_error" in output
    assert "--bogus" in output


def test_apply_bare_positional_arg_renders_parse_error(
    db: sqlite3.Connection,
) -> None:
    """Bare positional → ``parse_error`` rejection (TS parity).

    TS source rejects bare positional args with the generic
    "Unknown argument" message (lcm-command.ts:389-390).
    """
    parsed = _FakeParsed(
        tokens=[
            "--apply",
            "--from",
            "legacy:conv_1",
            "--to",
            "merged",
            "--reason",
            "test",
            "bogus_positional",
        ],
        engine=_FakeEngine(db_connection=db),
        flags={
            "apply": True,
            "from": ["legacy:conv_1"],
            "to": "merged",
            "reason": "test",
        },
    )
    output = run_apply(parsed)
    assert "kind: parse_error" in output
    assert "bogus_positional" in output


# ---------------------------------------------------------------------------
# Apply mode — happy path
# ---------------------------------------------------------------------------


def test_apply_happy_path_renders_completed(db: sqlite3.Connection) -> None:
    """Apply with valid args renders ``completed`` block with counts."""
    c1 = _insert_conv(db, "s1", "legacy:conv_1")
    _insert_leaf(db, "leaf_a", c1, "legacy:conv_1")

    parsed = _FakeParsed(
        tokens=[
            "--apply",
            "--from",
            "legacy:conv_1",
            "--to",
            "merged",
            "--reason",
            "test",
        ],
        engine=_FakeEngine(db_connection=db),
        flags={
            "apply": True,
            "from": ["legacy:conv_1"],
            "to": "merged",
            "reason": "test",
        },
    )
    output = run_apply(parsed)
    assert "status: completed" in output
    assert "conversations moved: 1" in output
    assert "summaries moved: 1" in output
    assert "audit entries: 1" in output
    assert "Moved 1 conversations" in output

    # Sanity: the DB was modified.
    conv_session_key = db.execute(
        "SELECT session_key FROM conversations WHERE conversation_id = ?",
        (c1,),
    ).fetchone()[0]
    assert conv_session_key == "merged"


def test_apply_main_session_blocked_renders_failed(
    db: sqlite3.Connection,
) -> None:
    """Apply into ``agent:main:main`` without flag → ``main_session_blocked``."""
    _insert_conv(db, "s1", "legacy:conv_1")
    parsed = _FakeParsed(
        tokens=[
            "--apply",
            "--from",
            "legacy:conv_1",
            "--to",
            "agent:main:main",
            "--reason",
            "test",
        ],
        engine=_FakeEngine(db_connection=db),
        flags={
            "apply": True,
            "from": ["legacy:conv_1"],
            "to": "agent:main:main",
            "reason": "test",
        },
    )
    output = run_apply(parsed)
    assert "status: failed" in output
    assert "kind: main_session_blocked" in output


def test_apply_with_allow_main_session_succeeds(db: sqlite3.Connection) -> None:
    """``--allow-main-session`` permits merge into ``agent:main:main``."""
    c1 = _insert_conv(db, "s1", "legacy:conv_1")
    _insert_leaf(db, "leaf_a", c1, "legacy:conv_1")
    parsed = _FakeParsed(
        tokens=[
            "--apply",
            "--from",
            "legacy:conv_1",
            "--to",
            "agent:main:main",
            "--reason",
            "explicit main merge",
            "--allow-main-session",
        ],
        engine=_FakeEngine(db_connection=db),
        flags={
            "apply": True,
            "from": ["legacy:conv_1"],
            "to": "agent:main:main",
            "reason": "explicit main merge",
            "allow_main_session": True,
        },
    )
    output = run_apply(parsed)
    assert "status: completed" in output
    assert "allow main session: yes" in output


def test_apply_active_conflict_renders_failed(db: sqlite3.Connection) -> None:
    """Two active convs across source + target → ``active_conflict``."""
    _insert_conv(db, "s1", "legacy:conv_a")  # active=1
    _insert_conv(db, "s2", "legacy:conv_b")  # active=1
    parsed = _FakeParsed(
        tokens=[
            "--apply",
            "--from",
            "legacy:conv_a,legacy:conv_b",
            "--to",
            "merged",
            "--reason",
            "test",
        ],
        engine=_FakeEngine(db_connection=db),
        flags={
            "apply": True,
            "from": ["legacy:conv_a", "legacy:conv_b"],
            "to": "merged",
            "reason": "test",
        },
    )
    output = run_apply(parsed)
    assert "status: failed" in output
    assert "kind: active_conflict" in output


def test_apply_handles_unavailable_engine() -> None:
    """No DB connection renders ``unavailable`` (no AttributeError)."""
    parsed = _FakeParsed(
        tokens=[
            "--apply",
            "--from",
            "legacy:conv_1",
            "--to",
            "merged",
            "--reason",
            "test",
        ],
        engine=_FakeEngine(db_connection=None),
        flags={
            "apply": True,
            "from": ["legacy:conv_1"],
            "to": "merged",
            "reason": "test",
        },
    )
    output = run_apply(parsed)
    assert "status: unavailable" in output


def test_apply_plan_section_echoes_args(db: sqlite3.Connection) -> None:
    """The "Plan" section echoes parsed args so operators can spot typos."""
    c1 = _insert_conv(db, "s1", "legacy:conv_1")
    _insert_leaf(db, "leaf_a", c1, "legacy:conv_1")
    parsed = _FakeParsed(
        tokens=[
            "--apply",
            "--from",
            "legacy:conv_1,legacy:conv_2",
            "--to",
            "merged",
            "--reason",
            "echo test",
        ],
        engine=_FakeEngine(db_connection=db),
        flags={
            "apply": True,
            "from": ["legacy:conv_1", "legacy:conv_2"],
            "to": "merged",
            "reason": "echo test",
        },
    )
    output = run_apply(parsed)
    assert "Plan:" in output
    assert "`legacy:conv_1`" in output
    assert "`legacy:conv_2`" in output
    assert "reason: echo test" in output
    assert "allow main session: no" in output
