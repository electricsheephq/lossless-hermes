"""Tests for :mod:`lossless_hermes.commands.doctor` ``run_apply`` (issue 08-07).

Exercises the ``/lcm doctor apply`` slash-command handler — the thin
wrapper that resolves db + config + current conversation off
``parsed.engine``, delegates to
:func:`lossless_hermes.doctor.apply.apply_scoped_doctor_repair`, and
renders the :class:`~lossless_hermes.doctor.contract.DoctorApplyResult`
as operator-facing text. Ports the TS ``buildDoctorApplyText`` renderer
(``lcm-command.ts:2474-2597``).

The repair algorithm itself is covered by ``tests/doctor/test_apply.py``;
this file pins the handler's text output + the engine-resolution edge
cases (no DB, no active conversation, summarizer unavailable, the
happy-path repair render).

Test inventory:

* No DB connection → ``unavailable`` block.
* No active conversation → conversation-scoped ``unavailable`` block.
* Summarizer unresolvable (no ``deps``/``summarize`` on the engine) →
  the ``Apply`` section renders ``status: unavailable``.
* Happy path — an injected ``summarize`` callable repairs a broken
  leaf; the output carries the detected/repaired counts + the
  ``Repaired summaries`` section.
* Skipped targets render in the ``Deferred`` section.
* A clean conversation (no broken summaries) renders ``clean; no writes
  ran``.

See:

* ``epics/08-cli-ops/08-07-doctor-apply.md`` — this issue.
* ``lossless-claw/src/plugin/lcm-command.ts:2474-2597`` — TS renderer.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from lossless_hermes.commands.doctor import run_apply
from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.doctor.contract import FALLBACK_SUMMARY_MARKER_V41_TRUNC
from lossless_hermes.summarize import LcmSummarizeOptions

# ---------------------------------------------------------------------------
# Engine + parsed stubs
# ---------------------------------------------------------------------------


@dataclass
class _FakeEngine:
    """Minimal engine stub exposing the attributes ``run_apply`` reads.

    ``run_apply`` probes ``_db`` (for the connection),
    ``current_session_id`` (to resolve the conversation), ``config``,
    and the optional ``deps`` / ``summarize`` / ``runtime_config``
    summarizer-seam attributes.
    """

    _db: Optional[sqlite3.Connection] = None
    current_session_id: Optional[str] = None
    config: Any = field(default_factory=lambda: SimpleNamespace(timezone="UTC"))
    deps: Any = None
    summarize: Any = None
    runtime_config: Any = None


@dataclass
class _FakeParsed:
    """Minimal :class:`ParsedLcmCommand`-shaped stub."""

    engine: _FakeEngine
    tokens: list[str] = field(default_factory=list)
    name: str = "doctor apply"
    raw_args: str = ""


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    """In-memory DB with the migration ladder + a seeded conversation."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(conn, fts5_available=True, seed_default_prompts=False)
    conn.execute(
        "INSERT INTO conversations (session_id, session_key) VALUES ('sess-1', 'agent:main:main')"
    )
    try:
        yield conn
    finally:
        conn.close()


def _broken(text: str = "stale") -> str:
    """A summary body :func:`detect_doctor_marker` flags FALLBACK."""
    return f"{FALLBACK_SUMMARY_MARKER_V41_TRUNC}\n\n{text}"


def _seed_broken_leaf(
    db: sqlite3.Connection,
    *,
    summary_id: str,
    conversation_id: int = 1,
    ordinal: int = 0,
) -> None:
    """Seed one broken leaf summary + its source message + context row."""
    cursor = db.execute(
        """
        INSERT INTO messages (conversation_id, seq, role, content, token_count, created_at)
          VALUES (?, ?, 'user', ?, ?, '2026-04-22 10:00:00')
        """,
        (conversation_id, ordinal, f"raw message {summary_id}", 20),
    )
    message_id = int(cursor.lastrowid or 0)
    db.execute(
        """
        INSERT INTO summaries (summary_id, conversation_id, kind, content, depth, token_count)
          VALUES (?, ?, 'leaf', ?, 0, 100)
        """,
        (summary_id, conversation_id, _broken(summary_id)),
    )
    db.execute(
        "INSERT INTO summary_messages (summary_id, message_id, ordinal) VALUES (?, ?, 0)",
        (summary_id, message_id),
    )
    db.execute(
        """
        INSERT INTO context_items (conversation_id, ordinal, item_type, summary_id)
          VALUES (?, ?, 'summary', ?)
        """,
        (conversation_id, ordinal, summary_id),
    )


class _RecordingSummarizer:
    """A summarizer double for the handler happy-path tests."""

    def __init__(self, *, output: str = "clean rewrite") -> None:
        self._output = output
        self.calls: list[tuple[str, bool, LcmSummarizeOptions]] = []

    def __call__(self, text: str, aggressive: bool, options: LcmSummarizeOptions) -> str:
        self.calls.append((text, aggressive, options))
        return self._output


# ---------------------------------------------------------------------------
# Engine-resolution edge cases
# ---------------------------------------------------------------------------


def test_no_db_connection_renders_unavailable() -> None:
    """An engine with no DB connection renders the ``unavailable`` block."""
    parsed = _FakeParsed(engine=_FakeEngine(_db=None))
    out = run_apply(parsed)

    assert "Lossless Hermes Doctor Apply" in out
    assert "status: unavailable" in out
    assert "engine DB connection not available" in out


def test_no_active_conversation_renders_unavailable(db: sqlite3.Connection) -> None:
    """No ``current_session_id`` → conversation-scoped ``unavailable`` block."""
    parsed = _FakeParsed(engine=_FakeEngine(_db=db, current_session_id=None))
    out = run_apply(parsed)

    assert "Lossless Hermes Doctor Apply" in out
    assert "status: unavailable" in out
    assert "conversation-scoped" in out


def test_unknown_session_renders_unavailable(db: sqlite3.Connection) -> None:
    """A ``current_session_id`` with no conversation row → ``unavailable``."""
    parsed = _FakeParsed(engine=_FakeEngine(_db=db, current_session_id="no-such-session"))
    out = run_apply(parsed)

    assert "status: unavailable" in out
    assert "no-such-session" in out


# ---------------------------------------------------------------------------
# Summarizer unavailable.
# ---------------------------------------------------------------------------


def test_summarizer_unavailable_renders_apply_unavailable(db: sqlite3.Connection) -> None:
    """No ``deps``/``summarize`` on the engine → ``Apply`` status unavailable.

    With a broken summary present but no summarizer resolvable,
    :func:`apply_scoped_doctor_repair` returns its ``"unavailable"`` arm
    and the handler renders that in the ``Apply`` section.
    """
    _seed_broken_leaf(db, summary_id="leaf_a")
    parsed = _FakeParsed(
        engine=_FakeEngine(_db=db, current_session_id="sess-1", deps=None, summarize=None)
    )
    out = run_apply(parsed)

    assert "Lossless Hermes Doctor Apply" in out
    assert "conversation id: 1" in out
    assert "mode: in-place summary rewrite" in out
    assert "status: unavailable" in out


# ---------------------------------------------------------------------------
# Happy path — repair runs.
# ---------------------------------------------------------------------------


def test_happy_path_repairs_and_renders_counts(db: sqlite3.Connection) -> None:
    """An injected summarizer repairs a broken leaf; counts render.

    The engine carries a ``summarize`` callable, so the handler forwards
    it to :func:`apply_scoped_doctor_repair`. The output must show the
    detected/repaired counts and list the repaired summary id.
    """
    _seed_broken_leaf(db, summary_id="leaf_repair")
    summarizer = _RecordingSummarizer(output="freshly rewritten clean summary")
    parsed = _FakeParsed(
        engine=_FakeEngine(_db=db, current_session_id="sess-1", summarize=summarizer)
    )
    out = run_apply(parsed)

    assert "Lossless Hermes Doctor Apply" in out
    assert "conversation id: 1" in out
    assert "session key: `agent:main:main`" in out
    assert "scope: this conversation only" in out
    # The detected stat reflects the pre-repair scan.
    assert "detected summaries: 1" in out
    assert "fallback-marker summaries: 1" in out
    assert "repaired summaries: 1" in out
    assert "unchanged summaries: 0" in out
    assert "skipped summaries: 0" in out
    assert "repaired 1 summary(s) in place" in out
    # The Repaired summaries section lists the id.
    assert "**Repaired summaries**" in out
    assert "leaf_repair" in out

    # And the DB row was actually rewritten.
    row = db.execute("SELECT content FROM summaries WHERE summary_id = 'leaf_repair'").fetchone()
    assert row[0] == "freshly rewritten clean summary"


def test_skipped_targets_render_in_deferred_section(db: sqlite3.Connection) -> None:
    """A summarizer returning marker-bearing output → ``Deferred`` section.

    The summarizer keeps emitting a marker-bearing string, so the target
    is skipped (not overwritten). The handler renders it under
    ``Deferred`` with the skip reason.
    """
    _seed_broken_leaf(db, summary_id="leaf_skip")
    # Summarizer output still carries a doctor marker → skipped.
    summarizer = _RecordingSummarizer(output=_broken("still broken"))
    parsed = _FakeParsed(
        engine=_FakeEngine(_db=db, current_session_id="sess-1", summarize=summarizer)
    )
    out = run_apply(parsed)

    assert "repaired summaries: 0" in out
    assert "skipped summaries: 1" in out
    assert "no repairs applied" in out
    assert "**Deferred**" in out
    assert "leaf_skip: rewritten content still contains a doctor marker" in out


def test_clean_conversation_renders_no_writes(db: sqlite3.Connection) -> None:
    """A conversation with no broken summaries renders ``clean; no writes ran``.

    With zero targets, :func:`apply_scoped_doctor_repair` short-circuits
    to an empty ``"applied"`` result — and the handler's ``result`` line
    reports the clean state. The summarizer is never consulted.
    """
    # A clean (marker-free) leaf — not a repair target.
    cursor = db.execute(
        """
        INSERT INTO messages (conversation_id, seq, role, content, token_count, created_at)
          VALUES (1, 0, 'user', 'a message', 10, '2026-04-22 10:00:00')
        """
    )
    message_id = int(cursor.lastrowid or 0)
    db.execute(
        """
        INSERT INTO summaries (summary_id, conversation_id, kind, content, depth, token_count)
          VALUES ('leaf_clean', 1, 'leaf', 'a perfectly clean summary', 0, 100)
        """
    )
    db.execute(
        "INSERT INTO summary_messages (summary_id, message_id, ordinal) VALUES ('leaf_clean', ?, 0)",
        (message_id,),
    )
    db.execute(
        """
        INSERT INTO context_items (conversation_id, ordinal, item_type, summary_id)
          VALUES (1, 0, 'summary', 'leaf_clean')
        """
    )

    summarizer = _RecordingSummarizer()
    parsed = _FakeParsed(
        engine=_FakeEngine(_db=db, current_session_id="sess-1", summarize=summarizer)
    )
    out = run_apply(parsed)

    assert "detected summaries: 0" in out
    assert "repaired summaries: 0" in out
    assert "clean; no writes ran" in out
    # No targets → summarizer never consulted.
    assert summarizer.calls == []
    # No Repaired / Deferred sections on a clean run.
    assert "**Repaired summaries**" not in out
    assert "**Deferred**" not in out


def test_handler_never_raises_on_write_failure(db: sqlite3.Connection) -> None:
    """A DB-level write failure renders a ``failed`` section, not a crash.

    :func:`apply_scoped_doctor_repair` lets the final write transaction
    propagate a :class:`sqlite3.Error`; the handler catches it and
    renders a ``status: failed`` section (ports the TS ``catch (error)``
    at ``lcm-command.ts:2508-2529``).
    """
    _seed_broken_leaf(db, summary_id="leaf_boom")
    # A trigger that aborts the repair write.
    db.execute(
        """
        CREATE TRIGGER doctor_cmd_write_guard
          BEFORE UPDATE OF content ON summaries
        BEGIN
          SELECT RAISE(ABORT, 'synthetic write failure');
        END
        """
    )
    summarizer = _RecordingSummarizer(output="clean rewrite")
    parsed = _FakeParsed(
        engine=_FakeEngine(_db=db, current_session_id="sess-1", summarize=summarizer)
    )

    # The handler must NOT raise — it renders the failure.
    out = run_apply(parsed)
    assert "Lossless Hermes Doctor Apply" in out
    assert "status: failed" in out
    assert "synthetic write failure" in out
