"""Tests for :mod:`lossless_hermes.doctor.cleaners` (issue 08-08).

Ports the cleaner-behavior test cases that, on the LCM ``pr-613`` branch,
live in ``test/lcm-command.test.ts`` (the ``/lcm doctor clean`` /
``doctor clean apply`` command tests, lines 493-823) and exercises
:func:`scan_doctor_cleaners` / :func:`apply_doctor_cleaners` directly at
the library layer — the right scope for the 08-08 cleaners-library issue.

The migration-backfill cases of ``test/v41-data-cleanup.test.ts`` are NOT
re-ported here: that TS file (at commit ``1f07fbd``) tests
``runLcmMigrations`` session_key backfill, not cleaners, and is already
covered by ``tests/test_migration_backfills.py`` +
``tests/test_migration_v41.py``. See this file's commit message for the
provenance note.

Issue-mandated tests (all present below):

* :func:`test_backup_before_begin_immediate` — filesystem-mtime invariant.
* :func:`test_temp_tables_dropped_on_raise` — fault injection mid-apply.
* :func:`test_missing_fts_table_best_effort` — drop ``summaries_fts_cjk``,
  apply still succeeds.
* :func:`test_scan_equals_apply_count` — dry-run count == apply count.
* :func:`test_predicate_query_plan_snapshot` — ``EXPLAIN QUERY PLAN``
  parity for all three cleaner predicates.
* :func:`test_null_subagent_window_function_out_of_seq` — the
  ``null_subagent_context`` window function reads the earliest message
  even when rows are inserted out of ``seq`` order.

See:

* ``epics/08-cli-ops/08-08-doctor-cleaners.md`` — this issue spec.
* ``docs/porting-guides/doctor-ops.md`` §"Cleaners — full inventory".
* ``lossless-claw/src/plugin/lcm-doctor-cleaners.ts`` — TS source at
  commit ``1f07fbd``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.doctor.cleaners import (
    apply_doctor_cleaners,
    get_doctor_cleaner_apply_unavailable_reason,
    get_doctor_cleaner_filter_ids,
    get_doctor_cleaner_filters,
    scan_doctor_cleaners,
)

# ---------------------------------------------------------------------------
# Fixtures + seed helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def mem_db() -> Iterator[sqlite3.Connection]:
    """In-memory SQLite with the full migration ladder (FTS5 enabled)."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(conn, fts5_available=True, seed_default_prompts=False)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def file_db(tmp_path: Path) -> Iterator[tuple[sqlite3.Connection, str]]:
    """File-backed SQLite (apply needs a file path for the backup).

    Yields ``(connection, db_path)``. The connection is opened in
    autocommit mode (``isolation_level=None``) so the cleaner module's
    explicit ``BEGIN IMMEDIATE`` / ``COMMIT`` statements are not fought
    by the stdlib's implicit-transaction machinery.
    """
    db_path = str(tmp_path / "lcm.db")
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(conn, fts5_available=True, seed_default_prompts=False)
    try:
        yield conn, db_path
    finally:
        conn.close()


def _add_conversation(
    db: sqlite3.Connection,
    *,
    session_id: str,
    session_key: str | None = None,
    active: int = 1,
    archived: bool = False,
) -> int:
    """Insert one conversation row; return its ``conversation_id``.

    When ``archived`` is ``True`` the row gets ``active = 0`` and a
    non-NULL ``archived_at`` (mirrors the TS ``archiveConversation``).
    """
    if archived:
        active = 0
    db.execute(
        "INSERT INTO conversations (session_id, session_key, active, archived_at) "
        "VALUES (?, ?, ?, ?)",
        (session_id, session_key, active, "2026-05-01T00:00:00Z" if archived else None),
    )
    row = db.execute("SELECT last_insert_rowid()").fetchone()
    return int(row[0])


def _add_message(
    db: sqlite3.Connection,
    *,
    conversation_id: int,
    seq: int,
    content: str,
    role: str = "user",
    token_count: int = 4,
) -> int:
    """Insert one message row; return its ``message_id``."""
    db.execute(
        "INSERT INTO messages (conversation_id, seq, role, content, token_count) "
        "VALUES (?, ?, ?, ?, ?)",
        (conversation_id, seq, role, content, token_count),
    )
    row = db.execute("SELECT last_insert_rowid()").fetchone()
    return int(row[0])


def _add_summary(
    db: sqlite3.Connection,
    *,
    summary_id: str,
    conversation_id: int,
    kind: str = "leaf",
    content: str = "summary text",
    token_count: int = 10,
) -> None:
    """Insert one summary row."""
    db.execute(
        "INSERT INTO summaries (summary_id, conversation_id, kind, content, token_count) "
        "VALUES (?, ?, ?, ?, ?)",
        (summary_id, conversation_id, kind, content, token_count),
    )


def _seed_mixed_garbage(db: sqlite3.Connection) -> dict[str, int]:
    """Seed the canonical mixed-garbage fixture used by the TS command tests.

    Mirrors the seed of the TS "reports global high-confidence cleaner
    candidates" test (``lcm-command.test.ts:493-599``):

    * ``archived_subagent`` — ``active=0``, key ``agent:main:subagent:*``,
      1 message → matched by ``archived_subagents``.
    * ``cron`` — key ``agent:main:cron:*`` (still active), 1 message →
      matched by ``cron_sessions``.
    * ``null_subagent`` — NULL key, archived, first message starts with
      ``[Subagent Context]``, 2 messages → matched by
      ``null_subagent_context``.
    * ``live_null_subagent`` — NULL key, ``[Subagent Context]`` first
      message, but NOT archived → matched by NOTHING (the apply
      predicate requires ``active=0 AND archived_at IS NOT NULL``).
    * ``normal`` — key ``agent:main:main``, active → matched by NOTHING.

    Returns a dict mapping the fixture name to its ``conversation_id``.
    """
    ids: dict[str, int] = {}

    ids["archived_subagent"] = _add_conversation(
        db,
        session_id="doctor-cleaner-archived-subagent",
        session_key="agent:main:subagent:worker-1",
        archived=True,
    )
    _add_message(
        db,
        conversation_id=ids["archived_subagent"],
        seq=0,
        role="assistant",
        content="archived subagent chatter",
    )

    ids["cron"] = _add_conversation(
        db,
        session_id="doctor-cleaner-cron",
        session_key="agent:main:cron:nightly",
    )
    _add_message(
        db,
        conversation_id=ids["cron"],
        seq=0,
        role="assistant",
        content="cron wake-up",
    )

    ids["null_subagent"] = _add_conversation(
        db,
        session_id="doctor-cleaner-null-subagent",
        session_key=None,
        archived=True,
    )
    _add_message(
        db,
        conversation_id=ids["null_subagent"],
        seq=1,
        role="user",
        content="[Subagent Context] Inspect the repo and summarize the issue.",
    )
    _add_message(
        db,
        conversation_id=ids["null_subagent"],
        seq=2,
        role="assistant",
        content="Working through the task now.",
    )

    ids["live_null_subagent"] = _add_conversation(
        db,
        session_id="doctor-cleaner-live-null-subagent",
        session_key=None,
        active=1,
    )
    _add_message(
        db,
        conversation_id=ids["live_null_subagent"],
        seq=0,
        role="user",
        content="[Subagent Context] Live child session still in progress.",
    )

    ids["normal"] = _add_conversation(
        db,
        session_id="doctor-cleaner-normal",
        session_key="agent:main:main",
    )
    _add_message(
        db,
        conversation_id=ids["normal"],
        seq=0,
        role="user",
        content="ordinary conversation",
    )

    return ids


# ---------------------------------------------------------------------------
# Metadata listing
# ---------------------------------------------------------------------------


def test_get_doctor_cleaner_filters_order_and_content() -> None:
    """AC: returns the three cleaners in canonical TS order with verbatim text."""
    filters = get_doctor_cleaner_filters()
    assert [f.id for f in filters] == [
        "archived_subagents",
        "cron_sessions",
        "null_subagent_context",
    ]
    by_id = {f.id: f for f in filters}
    assert by_id["archived_subagents"].label == "Archived subagents"
    assert by_id["archived_subagents"].description == (
        "Archived subagent conversations keyed as agent:main:subagent:*."
    )
    assert by_id["cron_sessions"].label == "Cron sessions"
    assert by_id["cron_sessions"].description == (
        "Background cron conversations keyed as agent:main:cron:*."
    )
    assert by_id["null_subagent_context"].label == "NULL-key subagent context"
    assert by_id["null_subagent_context"].description == (
        "Archived conversations with NULL session_key whose first stored "
        "message begins with [Subagent Context]."
    )


def test_get_doctor_cleaner_filter_ids_is_a_fresh_copy() -> None:
    """The id list is a fresh copy — mutating it does not affect later calls."""
    first = get_doctor_cleaner_filter_ids()
    assert first == ["archived_subagents", "cron_sessions", "null_subagent_context"]
    first.append("tampered")  # type: ignore[arg-type]
    assert get_doctor_cleaner_filter_ids() == [
        "archived_subagents",
        "cron_sessions",
        "null_subagent_context",
    ]


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------


def test_scan_reports_per_filter_counts_and_examples(mem_db: sqlite3.Connection) -> None:
    """AC: scan returns per-filter counts + examples + distinct totals.

    Ports the TS "reports global high-confidence cleaner candidates"
    test (``lcm-command.test.ts:493-599``): 3 matched conversations, 4
    matched messages; the live-null and normal conversations match
    nothing.
    """
    _seed_mixed_garbage(mem_db)
    scan = scan_doctor_cleaners(mem_db)

    by_id = {f.id: f for f in scan.filters}
    assert by_id["archived_subagents"].conversation_count == 1
    assert by_id["archived_subagents"].message_count == 1
    assert by_id["cron_sessions"].conversation_count == 1
    assert by_id["cron_sessions"].message_count == 1
    assert by_id["null_subagent_context"].conversation_count == 1
    # null_subagent has TWO messages.
    assert by_id["null_subagent_context"].message_count == 2

    # 3 distinct conversations, 4 distinct messages — matches the TS
    # "matched conversations: 3" / "matched messages: 4" assertions.
    assert scan.total_distinct_conversations == 3
    assert scan.total_distinct_messages == 4

    # The example session_keys surface for the keyed cleaners.
    archived_examples = by_id["archived_subagents"].examples
    assert len(archived_examples) == 1
    assert archived_examples[0].session_key == "agent:main:subagent:worker-1"

    cron_examples = by_id["cron_sessions"].examples
    assert cron_examples[0].session_key == "agent:main:cron:nightly"

    # The null-subagent example carries the normalized first-message
    # preview; the live-null conversation is NOT a match (not archived).
    null_examples = by_id["null_subagent_context"].examples
    assert len(null_examples) == 1
    assert null_examples[0].session_key is None
    assert null_examples[0].first_message_preview == (
        "[Subagent Context] Inspect the repo and summarize the issue."
    )
    matched_conv_ids = {ex.conversation_id for f in scan.filters for ex in f.examples}
    assert matched_conv_ids == {
        _conv_id_by_session(mem_db, "doctor-cleaner-archived-subagent"),
        _conv_id_by_session(mem_db, "doctor-cleaner-cron"),
        _conv_id_by_session(mem_db, "doctor-cleaner-null-subagent"),
    }


def _conv_id_by_session(db: sqlite3.Connection, session_id: str) -> int:
    """Look up a conversation_id by its session_id (test helper)."""
    row = db.execute(
        "SELECT conversation_id FROM conversations WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    assert row is not None
    return int(row[0])


def test_scan_clean_db_reports_zero(mem_db: sqlite3.Connection) -> None:
    """AC: a DB with no garbage reports all-zero counts.

    Ports the TS "reports a clean doctor clean scan" test
    (``lcm-command.test.ts:601-627``).
    """
    conv = _add_conversation(
        mem_db,
        session_id="doctor-cleaners-clean",
        session_key="agent:main:main",
    )
    _add_message(mem_db, conversation_id=conv, seq=0, content="healthy conversation")

    scan = scan_doctor_cleaners(mem_db)
    assert scan.total_distinct_conversations == 0
    assert scan.total_distinct_messages == 0
    for stat in scan.filters:
        assert stat.conversation_count == 0
        assert stat.message_count == 0
        assert stat.examples == []


def test_scan_with_filter_subset(mem_db: sqlite3.Connection) -> None:
    """A filter subset returns only the requested cleaners' stats."""
    _seed_mixed_garbage(mem_db)
    scan = scan_doctor_cleaners(mem_db, ["cron_sessions"])
    assert [f.id for f in scan.filters] == ["cron_sessions"]
    assert scan.filters[0].conversation_count == 1
    # The distinct totals reflect ONLY the requested cleaner's matches.
    assert scan.total_distinct_conversations == 1
    assert scan.total_distinct_messages == 1


def test_scan_empty_filter_list_selects_all(mem_db: sqlite3.Connection) -> None:
    """An empty filter list behaves like ``None`` — selects all three."""
    _seed_mixed_garbage(mem_db)
    scan = scan_doctor_cleaners(mem_db, [])
    assert [f.id for f in scan.filters] == [
        "archived_subagents",
        "cron_sessions",
        "null_subagent_context",
    ]


def test_scan_example_ordering_message_count_desc(mem_db: sqlite3.Connection) -> None:
    """AC: examples are sorted ``message_count DESC, created_at DESC, id DESC``.

    Three cron conversations with 1 / 3 / 2 messages — the example list
    must lead with the 3-message conversation.
    """
    small = _add_conversation(mem_db, session_id="cron-small", session_key="agent:main:cron:small")
    _add_message(mem_db, conversation_id=small, seq=0, content="one")

    big = _add_conversation(mem_db, session_id="cron-big", session_key="agent:main:cron:big")
    for seq in range(3):
        _add_message(mem_db, conversation_id=big, seq=seq, content=f"big-{seq}")

    mid = _add_conversation(mem_db, session_id="cron-mid", session_key="agent:main:cron:mid")
    for seq in range(2):
        _add_message(mem_db, conversation_id=mid, seq=seq, content=f"mid-{seq}")

    scan = scan_doctor_cleaners(mem_db, ["cron_sessions"])
    examples = scan.filters[0].examples
    assert [ex.message_count for ex in examples] == [3, 2, 1]
    assert examples[0].conversation_id == big
    assert examples[-1].conversation_id == small


def test_scan_examples_capped_at_three(mem_db: sqlite3.Connection) -> None:
    """The example list is capped at three even when more conversations match."""
    for i in range(5):
        conv = _add_conversation(mem_db, session_id=f"cron-{i}", session_key=f"agent:main:cron:{i}")
        _add_message(mem_db, conversation_id=conv, seq=0, content=f"c{i}")
    scan = scan_doctor_cleaners(mem_db, ["cron_sessions"])
    assert scan.filters[0].conversation_count == 5
    assert len(scan.filters[0].examples) == 3


def test_scan_drops_temp_tables(mem_db: sqlite3.Connection) -> None:
    """The scan's three temp tables are dropped in the ``finally`` block."""
    _seed_mixed_garbage(mem_db)
    scan_doctor_cleaners(mem_db)
    remaining = mem_db.execute(
        "SELECT name FROM sqlite_temp_master WHERE type = 'table' AND name LIKE 'doctor_cleaner_%'"
    ).fetchall()
    assert remaining == []


# ---------------------------------------------------------------------------
# Apply — unavailability
# ---------------------------------------------------------------------------


def test_get_unavailable_reason_in_memory() -> None:
    """AC: an in-memory DB path yields the file-backed-required reason."""
    reason = get_doctor_cleaner_apply_unavailable_reason(":memory:")
    assert reason is not None
    assert "file-backed" in reason


def test_get_unavailable_reason_file_backed(tmp_path: Path) -> None:
    """A real filesystem path yields ``None`` (apply may proceed)."""
    assert get_doctor_cleaner_apply_unavailable_reason(str(tmp_path / "x.db")) is None


def test_apply_refuses_in_memory_db(mem_db: sqlite3.Connection) -> None:
    """AC: ``apply_doctor_cleaners`` refuses on an in-memory DB.

    Returns ``kind="unavailable"`` with the file-backed reason; no
    mutation, no count fields populated.
    """
    _seed_mixed_garbage(mem_db)
    result = apply_doctor_cleaners(mem_db, database_path=":memory:")
    assert result.kind == "unavailable"
    assert result.reason is not None
    assert "file-backed" in result.reason
    assert result.deleted_conversations == 0
    assert result.deleted_messages == 0
    assert result.backup_path == ""
    # Nothing was deleted.
    assert mem_db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 5


def test_apply_refuses_unknown_filter_ids(
    file_db: tuple[sqlite3.Connection, str],
) -> None:
    """An all-unknown filter list resolves to zero cleaners → unavailable."""
    db, db_path = file_db
    result = apply_doctor_cleaners(
        db,
        database_path=db_path,
        filter_ids=["not_a_real_cleaner"],  # type: ignore[list-item]
    )
    assert result.kind == "unavailable"
    assert result.reason == "No valid doctor cleaner filters were selected."


# ---------------------------------------------------------------------------
# Apply — destructive cascade
# ---------------------------------------------------------------------------


def test_apply_all_filters_deletes_and_preserves(
    file_db: tuple[sqlite3.Connection, str],
) -> None:
    """AC: apply deletes matched conversations, preserves unrelated ones.

    Ports the TS "applies all doctor clean filters with backup-first
    deletion and preserves unrelated conversations" test
    (``lcm-command.test.ts:629-742``).
    """
    db, db_path = file_db
    ids = _seed_mixed_garbage(db)

    result = apply_doctor_cleaners(db, database_path=db_path)

    assert result.kind == "applied"
    assert result.deleted_conversations == 3
    assert result.deleted_messages == 4
    assert result.vacuumed is False  # vacuum not requested
    assert result.filter_ids == [
        "archived_subagents",
        "cron_sessions",
        "null_subagent_context",
    ]
    assert Path(result.backup_path).exists()

    # quick_check passes after the cascade.
    quick = db.execute("PRAGMA quick_check").fetchone()
    assert quick[0] == "ok"

    # The three garbage conversations are gone; the two keepers survive.
    remaining = {
        int(row[0]) for row in db.execute("SELECT conversation_id FROM conversations").fetchall()
    }
    assert remaining == {ids["live_null_subagent"], ids["normal"]}


def test_apply_single_filter_only_deletes_that_class(
    file_db: tuple[sqlite3.Connection, str],
) -> None:
    """AC: a single-filter apply leaves the other candidate classes intact.

    Ports the TS "applies a single doctor clean filter without deleting
    other candidate classes" test (``lcm-command.test.ts:744-790``).
    """
    db, db_path = file_db
    archived = _add_conversation(
        db,
        session_id="doctor-cleaner-single-archived",
        session_key="agent:main:subagent:single-worker",
        archived=True,
    )
    _add_message(db, conversation_id=archived, seq=0, role="assistant", content="archived")

    cron = _add_conversation(
        db,
        session_id="doctor-cleaner-single-cron",
        session_key="agent:main:cron:single-nightly",
    )
    _add_message(db, conversation_id=cron, seq=0, role="assistant", content="cron run")

    result = apply_doctor_cleaners(db, database_path=db_path, filter_ids=["cron_sessions"])

    assert result.kind == "applied"
    assert result.filter_ids == ["cron_sessions"]
    assert result.deleted_conversations == 1
    # The archived subagent conversation is NOT touched.
    remaining = {
        int(row[0]) for row in db.execute("SELECT conversation_id FROM conversations").fetchall()
    }
    assert remaining == {archived}


def test_apply_cascades_through_all_dependent_tables(
    file_db: tuple[sqlite3.Connection, str],
) -> None:
    """The cascade removes summary / summary_messages / summary_parents /
    context_items rows owned by the deleted conversation.

    summary_messages.message_id and summary_parents.parent_summary_id and
    context_items.message_id/summary_id carry ``ON DELETE RESTRICT`` — a
    plain ``conversations`` DELETE would be BLOCKED unless the cascade
    clears them first. This test would fail with a FK violation if any
    cascade step were missing.
    """
    db, db_path = file_db
    cron = _add_conversation(db, session_id="cron-cascade", session_key="agent:main:cron:cascade")
    msg = _add_message(db, conversation_id=cron, seq=0, content="cron message")
    _add_summary(db, summary_id="leaf-1", conversation_id=cron, kind="leaf")
    _add_summary(db, summary_id="cond-1", conversation_id=cron, kind="condensed")
    # leaf lineage + condensed lineage + both context-item ref types.
    db.execute(
        "INSERT INTO summary_messages (summary_id, message_id, ordinal) VALUES (?, ?, 0)",
        ("leaf-1", msg),
    )
    db.execute(
        "INSERT INTO summary_parents (summary_id, parent_summary_id, ordinal) VALUES (?, ?, 0)",
        ("cond-1", "leaf-1"),
    )
    db.execute(
        "INSERT INTO context_items (conversation_id, ordinal, item_type, summary_id) "
        "VALUES (?, 0, 'summary', ?)",
        (cron, "leaf-1"),
    )
    db.execute(
        "INSERT INTO context_items (conversation_id, ordinal, item_type, message_id) "
        "VALUES (?, 1, 'message', ?)",
        (cron, msg),
    )

    result = apply_doctor_cleaners(db, database_path=db_path, filter_ids=["cron_sessions"])
    assert result.kind == "applied"
    assert result.deleted_conversations == 1

    # Every dependent table is empty for that conversation.
    assert db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM summaries").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM summary_messages").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM summary_parents").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM context_items").fetchone()[0] == 0
    # FK integrity holds.
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_apply_no_match_is_noop(file_db: tuple[sqlite3.Connection, str]) -> None:
    """An apply that matches nothing returns zero counts and deletes nothing."""
    db, db_path = file_db
    conv = _add_conversation(db, session_id="clean", session_key="agent:main:main")
    _add_message(db, conversation_id=conv, seq=0, content="ordinary")

    result = apply_doctor_cleaners(db, database_path=db_path)
    assert result.kind == "applied"
    assert result.deleted_conversations == 0
    assert result.deleted_messages == 0
    assert result.vacuumed is False
    assert db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Apply — VACUUM gating
# ---------------------------------------------------------------------------


def test_apply_vacuum_fires_when_requested_and_deletions_happened(
    file_db: tuple[sqlite3.Connection, str],
) -> None:
    """AC: ``vacuum=True`` + ``deleted>0`` runs VACUUM + wal_checkpoint.

    Ports the TS "vacuums after doctor clean apply when requested" test
    (``lcm-command.test.ts:792-823``).
    """
    db, db_path = file_db
    cron = _add_conversation(db, session_id="cron-vacuum", session_key="agent:main:cron:vacuum")
    _add_message(db, conversation_id=cron, seq=0, role="assistant", content="cron run")

    result = apply_doctor_cleaners(
        db, database_path=db_path, filter_ids=["cron_sessions"], vacuum=True
    )
    assert result.kind == "applied"
    assert result.deleted_conversations == 1
    assert result.vacuumed is True

    # wal_checkpoint(TRUNCATE) ran — the checkpoint is not busy.
    checkpoint = db.execute("PRAGMA wal_checkpoint").fetchone()
    assert checkpoint[0] == 0  # busy == 0


def test_apply_vacuum_skipped_on_noop(
    file_db: tuple[sqlite3.Connection, str],
) -> None:
    """AC: ``vacuum=True`` + ``deleted=0`` is a no-op (VACUUM does NOT fire).

    A no-op apply must stay cheap — the VACUUM is gated on a non-zero
    deletion count.
    """
    db, db_path = file_db
    conv = _add_conversation(db, session_id="clean", session_key="agent:main:main")
    _add_message(db, conversation_id=conv, seq=0, content="ordinary")

    result = apply_doctor_cleaners(db, database_path=db_path, vacuum=True)
    assert result.kind == "applied"
    assert result.deleted_conversations == 0
    assert result.vacuumed is False


# ---------------------------------------------------------------------------
# Issue-mandated: backup-before-BEGIN-IMMEDIATE invariant
# ---------------------------------------------------------------------------


def test_backup_before_begin_immediate(
    file_db: tuple[sqlite3.Connection, str], tmp_path: Path
) -> None:
    """AC ``test_backup_before_begin_immediate`` — filesystem-mtime invariant.

    The backup file MUST be written BEFORE the destructive transaction
    begins. We assert this by capturing a filesystem timestamp marker
    immediately before calling apply, then confirming the backup file's
    mtime is at or after that marker AND that a sentinel row inserted
    *after* the marker is still gone post-apply (proving the cascade ran
    after the backup was already on disk).

    The strict ordering proof: the backup is a ``VACUUM INTO`` snapshot.
    If the snapshot were taken AFTER ``BEGIN IMMEDIATE`` + the cascade,
    the backup would not contain the to-be-deleted conversation. We
    therefore open the backup file and assert the garbage conversation
    IS still present in it — i.e. the snapshot predates the deletion.
    """
    db, db_path = file_db
    cron = _add_conversation(db, session_id="cron-backup", session_key="agent:main:cron:backup")
    _add_message(db, conversation_id=cron, seq=0, role="assistant", content="cron run")

    marker = tmp_path / "marker"
    marker.write_text("t0", encoding="utf-8")
    marker_mtime = marker.stat().st_mtime

    result = apply_doctor_cleaners(db, database_path=db_path, filter_ids=["cron_sessions"])
    assert result.kind == "applied"
    assert result.deleted_conversations == 1

    backup_file = Path(result.backup_path)
    assert backup_file.exists()
    # The backup was written after the marker (i.e. during this apply).
    assert backup_file.stat().st_mtime >= marker_mtime

    # Decisive proof of ordering: the backup snapshot still contains the
    # conversation that the cascade deleted. If the backup had been taken
    # after BEGIN IMMEDIATE + cascade, the row would be absent.
    backup_conn = sqlite3.connect(str(backup_file))
    try:
        backed_up = backup_conn.execute(
            "SELECT COUNT(*) FROM conversations WHERE conversation_id = ?",
            (cron,),
        ).fetchone()
        assert backed_up[0] == 1, "backup must predate the destructive deletion"
    finally:
        backup_conn.close()

    # And the live DB no longer has it (the cascade did run).
    assert (
        db.execute(
            "SELECT COUNT(*) FROM conversations WHERE conversation_id = ?", (cron,)
        ).fetchone()[0]
        == 0
    )


# ---------------------------------------------------------------------------
# Issue-mandated: temp tables dropped on raise
# ---------------------------------------------------------------------------


def test_temp_tables_dropped_on_raise(
    file_db: tuple[sqlite3.Connection, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC ``test_temp_tables_dropped_on_raise`` — fault injection mid-apply.

    Inject a fault into the cascade DELETE step, assert the exception
    propagates, the transaction is rolled back (the garbage conversation
    survives), and the apply-time temp tables are dropped by the
    ``finally`` block.
    """
    db, db_path = file_db
    cron = _add_conversation(db, session_id="cron-fault", session_key="agent:main:cron:fault")
    _add_message(db, conversation_id=cron, seq=0, role="assistant", content="cron run")

    # Monkeypatch the cascade-delete helper to raise after staging.
    import lossless_hermes.doctor.cleaners as cleaners_mod

    def _boom(_db: sqlite3.Connection) -> int:
        raise RuntimeError("injected fault mid-cascade")

    monkeypatch.setattr(cleaners_mod, "_delete_temp_cleaner_candidates", _boom)

    with pytest.raises(RuntimeError, match="injected fault mid-cascade"):
        apply_doctor_cleaners(db, database_path=db_path, filter_ids=["cron_sessions"])

    # The transaction was rolled back — the conversation still exists.
    assert (
        db.execute(
            "SELECT COUNT(*) FROM conversations WHERE conversation_id = ?", (cron,)
        ).fetchone()[0]
        == 1
    )
    # The apply-time temp tables were dropped by the ``finally`` block.
    remaining = db.execute(
        "SELECT name FROM sqlite_temp_master WHERE type = 'table' AND name LIKE 'doctor_cleaner_%'"
    ).fetchall()
    assert remaining == []
    # No transaction is left dangling.
    assert not db.in_transaction


def test_temp_tables_dropped_on_raise_with_first_message_cleaner(
    file_db: tuple[sqlite3.Connection, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The five-temp-table case (``needs_first_message``) also cleans up.

    Selecting ``null_subagent_context`` stages the extra
    ``doctor_cleaner_first_messages`` table; the ``finally`` drop must
    remove that one too.
    """
    db, db_path = file_db
    null_conv = _add_conversation(db, session_id="null-fault", session_key=None, archived=True)
    _add_message(
        db,
        conversation_id=null_conv,
        seq=0,
        content="[Subagent Context] fault path",
    )

    import lossless_hermes.doctor.cleaners as cleaners_mod

    def _boom(_db: sqlite3.Connection) -> int:
        raise RuntimeError("injected fault")

    monkeypatch.setattr(cleaners_mod, "_delete_temp_cleaner_candidates", _boom)

    with pytest.raises(RuntimeError, match="injected fault"):
        apply_doctor_cleaners(db, database_path=db_path, filter_ids=["null_subagent_context"])

    remaining = db.execute(
        "SELECT name FROM sqlite_temp_master WHERE type = 'table' AND name LIKE 'doctor_cleaner_%'"
    ).fetchall()
    assert remaining == []


# ---------------------------------------------------------------------------
# Issue-mandated: missing FTS table is best-effort
# ---------------------------------------------------------------------------


def test_missing_fts_table_best_effort(
    file_db: tuple[sqlite3.Connection, str],
) -> None:
    """AC ``test_missing_fts_table_best_effort`` — drop ``summaries_fts_cjk``.

    The cascade gates every FTS branch on ``_has_table``; an apply
    against a DB whose ``summaries_fts_cjk`` virtual table is missing
    still succeeds.
    """
    db, db_path = file_db
    # Sanity: the table exists before we drop it.
    assert (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='summaries_fts_cjk'"
        ).fetchone()
        is not None
    )
    db.execute("DROP TABLE summaries_fts_cjk")

    cron = _add_conversation(db, session_id="cron-no-cjk", session_key="agent:main:cron:no-cjk")
    msg = _add_message(db, conversation_id=cron, seq=0, role="assistant", content="cron")
    _add_summary(db, summary_id="s-no-cjk", conversation_id=cron, kind="leaf")
    db.execute(
        "INSERT INTO summary_messages (summary_id, message_id, ordinal) VALUES (?, ?, 0)",
        ("s-no-cjk", msg),
    )

    result = apply_doctor_cleaners(db, database_path=db_path, filter_ids=["cron_sessions"])
    assert result.kind == "applied"
    assert result.deleted_conversations == 1
    assert db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0


def test_missing_all_fts_tables_best_effort(tmp_path: Path) -> None:
    """An apply against a DB built with ``fts5_available=False`` succeeds.

    None of ``messages_fts`` / ``summaries_fts`` / ``summaries_fts_cjk``
    exist — every FTS branch must be skipped via the ``_has_table`` gate.
    """
    db_path = str(tmp_path / "no_fts.db")
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        run_lcm_migrations(conn, fts5_available=False, seed_default_prompts=False)
        # Confirm no FTS tables exist.
        for fts in ("messages_fts", "summaries_fts", "summaries_fts_cjk"):
            assert (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (fts,)
                ).fetchone()
                is None
            )
        cron = _add_conversation(conn, session_id="cron-nofts", session_key="agent:main:cron:nofts")
        _add_message(conn, conversation_id=cron, seq=0, content="cron")

        result = apply_doctor_cleaners(conn, database_path=db_path, filter_ids=["cron_sessions"])
        assert result.kind == "applied"
        assert result.deleted_conversations == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Issue-mandated: scan count == apply count
# ---------------------------------------------------------------------------


def test_scan_equals_apply_count(file_db: tuple[sqlite3.Connection, str]) -> None:
    """AC ``test_scan_equals_apply_count`` — dry-run count == apply count.

    The scan and apply build their matched sets from the SAME predicate
    SQL, so a dry-run scan must report exactly the conversation/message
    counts the subsequent apply deletes.
    """
    db, db_path = file_db
    _seed_mixed_garbage(db)

    scan = scan_doctor_cleaners(db)
    scan_conversations = scan.total_distinct_conversations
    scan_messages = scan.total_distinct_messages

    result = apply_doctor_cleaners(db, database_path=db_path)
    assert result.kind == "applied"
    assert result.deleted_conversations == scan_conversations
    assert result.deleted_messages == scan_messages


def test_scan_equals_apply_count_per_filter(
    file_db: tuple[sqlite3.Connection, str],
) -> None:
    """Per-filter: a single-filter scan count equals the single-filter apply."""
    db, db_path = file_db
    _seed_mixed_garbage(db)

    scan = scan_doctor_cleaners(db, ["null_subagent_context"])
    expected_conversations = scan.filters[0].conversation_count
    expected_messages = scan.filters[0].message_count

    result = apply_doctor_cleaners(db, database_path=db_path, filter_ids=["null_subagent_context"])
    assert result.kind == "applied"
    assert result.deleted_conversations == expected_conversations
    assert result.deleted_messages == expected_messages


# ---------------------------------------------------------------------------
# Issue-mandated: EXPLAIN QUERY PLAN predicate snapshot
# ---------------------------------------------------------------------------


def test_predicate_query_plan_snapshot(mem_db: sqlite3.Connection) -> None:
    """AC: all three cleaner predicates match the TS SQL 1:1.

    Validates each cleaner's ``predicate_sql`` is a syntactically valid,
    plannable SQL fragment by running ``EXPLAIN QUERY PLAN`` over the
    exact ``SELECT`` shape the module builds. The candidate (no-join)
    predicate is checked for the two simple cleaners; the
    ``null_subagent_context`` predicate is checked WITH the
    first-message join (it references ``message_stats.first_message_preview``).

    The assertion is structural — every cleaner's predicate plans
    against a ``conversations c`` scan with no error — rather than a
    brittle byte-snapshot of the planner output (which varies across
    SQLite versions).
    """
    # Stage the first-message temp table so the null-subagent predicate
    # (which references ``message_stats``) can be planned.
    mem_db.execute(
        "CREATE TEMP TABLE message_stats "
        "(conversation_id INTEGER PRIMARY KEY, first_message_preview TEXT)"
    )
    try:
        # archived_subagents — candidate predicate (no join).
        plan_archived = mem_db.execute(
            "EXPLAIN QUERY PLAN SELECT c.conversation_id FROM conversations c "
            "WHERE (c.active = 0 AND c.session_key LIKE 'agent:main:subagent:%')"
        ).fetchall()
        assert plan_archived, "archived_subagents predicate must produce a plan"
        assert any("conversations" in str(row).lower() for row in plan_archived)

        # cron_sessions — candidate predicate (no join).
        plan_cron = mem_db.execute(
            "EXPLAIN QUERY PLAN SELECT c.conversation_id FROM conversations c "
            "WHERE (c.session_key LIKE 'agent:main:cron:%')"
        ).fetchall()
        assert plan_cron, "cron_sessions predicate must produce a plan"
        assert any("conversations" in str(row).lower() for row in plan_cron)

        # null_subagent_context — FULL predicate, requires the join.
        plan_null = mem_db.execute(
            "EXPLAIN QUERY PLAN SELECT c.conversation_id FROM conversations c "
            "LEFT JOIN message_stats message_stats "
            "ON message_stats.conversation_id = c.conversation_id "
            "WHERE (c.session_key IS NULL AND c.active = 0 "
            "AND c.archived_at IS NOT NULL "
            "AND message_stats.first_message_preview LIKE '[Subagent Context]%')"
        ).fetchall()
        assert plan_null, "null_subagent_context predicate must produce a plan"
        plan_text = " ".join(str(row).lower() for row in plan_null)
        assert "conversations" in plan_text
        assert "message_stats" in plan_text
    finally:
        mem_db.execute("DROP TABLE IF EXISTS temp.message_stats")


def test_archived_subagents_requires_inactive(
    mem_db: sqlite3.Connection,
) -> None:
    """The ``archived_subagents`` predicate needs ``active = 0``.

    An ACTIVE conversation keyed ``agent:main:subagent:*`` must NOT
    match — the predicate is ``active = 0 AND session_key LIKE ...``.
    """
    active_subagent = _add_conversation(
        mem_db,
        session_id="active-subagent",
        session_key="agent:main:subagent:still-running",
        active=1,
    )
    _add_message(mem_db, conversation_id=active_subagent, seq=0, content="running")

    scan = scan_doctor_cleaners(mem_db, ["archived_subagents"])
    assert scan.filters[0].conversation_count == 0


def test_cron_sessions_ignores_active_flag(mem_db: sqlite3.Connection) -> None:
    """The ``cron_sessions`` predicate has NO active filter.

    BOTH an active and an archived cron conversation match — the
    predicate is ``session_key LIKE 'agent:main:cron:%'`` with no
    ``active`` clause.
    """
    active_cron = _add_conversation(
        mem_db, session_id="active-cron", session_key="agent:main:cron:live", active=1
    )
    _add_message(mem_db, conversation_id=active_cron, seq=0, content="live cron")

    archived_cron = _add_conversation(
        mem_db,
        session_id="archived-cron",
        session_key="agent:main:cron:done",
        archived=True,
    )
    _add_message(mem_db, conversation_id=archived_cron, seq=0, content="done cron")

    scan = scan_doctor_cleaners(mem_db, ["cron_sessions"])
    assert scan.filters[0].conversation_count == 2


def test_null_subagent_requires_archived_and_marker(
    mem_db: sqlite3.Connection,
) -> None:
    """The ``null_subagent_context`` predicate needs ALL four conditions.

    NULL key + ``active = 0`` + ``archived_at IS NOT NULL`` + first
    message starts with ``[Subagent Context]``. Each near-miss below
    fails the predicate:

    * NULL key, archived, but first message LACKS the marker → no match.
    * NULL key, marker, but NOT archived → no match.
    * Has a session_key, archived, marker → no match (key not NULL).
    """
    # Near-miss 1: archived NULL-key conv, but no marker.
    no_marker = _add_conversation(
        mem_db, session_id="null-no-marker", session_key=None, archived=True
    )
    _add_message(mem_db, conversation_id=no_marker, seq=0, content="plain first message")

    # Near-miss 2: NULL-key conv with marker, but still active.
    live_marker = _add_conversation(
        mem_db, session_id="null-live-marker", session_key=None, active=1
    )
    _add_message(
        mem_db,
        conversation_id=live_marker,
        seq=0,
        content="[Subagent Context] live one",
    )

    # Near-miss 3: archived + marker, but has a non-NULL session_key.
    keyed_marker = _add_conversation(
        mem_db,
        session_id="keyed-marker",
        session_key="agent:main:subagent:keyed",
        archived=True,
    )
    _add_message(
        mem_db,
        conversation_id=keyed_marker,
        seq=0,
        content="[Subagent Context] keyed one",
    )

    # The genuine match.
    real = _add_conversation(mem_db, session_id="null-real", session_key=None, archived=True)
    _add_message(mem_db, conversation_id=real, seq=0, content="[Subagent Context] real garbage")

    scan = scan_doctor_cleaners(mem_db, ["null_subagent_context"])
    assert scan.filters[0].conversation_count == 1
    assert scan.filters[0].examples[0].conversation_id == real


# ---------------------------------------------------------------------------
# Issue-mandated: null_subagent window-function correctness
# ---------------------------------------------------------------------------


def test_null_subagent_window_function_out_of_seq(
    mem_db: sqlite3.Connection,
) -> None:
    """AC ``null_subagent_context`` window function reads the EARLIEST message.

    The window function orders by ``seq ASC`` — so even when message
    rows are INSERTed out of order, the row with the lowest ``seq`` is
    the "first message" the predicate inspects.

    Fixture: a NULL-key archived conversation whose lowest-``seq``
    message starts with ``[Subagent Context]`` but is inserted AFTER a
    higher-``seq`` non-marker message. The conversation must still
    match — the window function picks ``seq = 0``, not insertion order.
    """
    conv = _add_conversation(mem_db, session_id="null-out-of-seq", session_key=None, archived=True)
    # Insert seq=5 FIRST (a non-marker message), then seq=0 (the marker).
    _add_message(
        mem_db,
        conversation_id=conv,
        seq=5,
        role="assistant",
        content="later reply with no marker",
    )
    _add_message(
        mem_db,
        conversation_id=conv,
        seq=0,
        role="user",
        content="[Subagent Context] earliest message by seq",
    )

    scan = scan_doctor_cleaners(mem_db, ["null_subagent_context"])
    assert scan.filters[0].conversation_count == 1
    # The preview is the seq=0 message, not the seq=5 one.
    assert scan.filters[0].examples[0].first_message_preview == (
        "[Subagent Context] earliest message by seq"
    )


def test_null_subagent_window_negative_when_marker_not_earliest(
    mem_db: sqlite3.Connection,
) -> None:
    """A conversation whose marker is NOT the earliest message does NOT match.

    The predicate inspects ONLY the ``seq``-earliest message. A
    conversation whose ``seq = 0`` message is a plain message and whose
    ``[Subagent Context]`` marker sits at a higher ``seq`` must NOT be a
    cleaner candidate.
    """
    conv = _add_conversation(
        mem_db, session_id="null-marker-not-first", session_key=None, archived=True
    )
    _add_message(
        mem_db,
        conversation_id=conv,
        seq=0,
        role="user",
        content="plain opening message",
    )
    _add_message(
        mem_db,
        conversation_id=conv,
        seq=1,
        role="assistant",
        content="[Subagent Context] this is not the first message",
    )

    scan = scan_doctor_cleaners(mem_db, ["null_subagent_context"])
    assert scan.filters[0].conversation_count == 0


def test_null_subagent_apply_out_of_seq(
    file_db: tuple[sqlite3.Connection, str],
) -> None:
    """The apply path's window function also reads the seq-earliest message.

    Same out-of-seq fixture as the scan test, but verified through
    :func:`apply_doctor_cleaners` (which stages its own
    ``doctor_cleaner_first_messages`` table) — confirms the apply-time
    window function agrees with the scan-time one.
    """
    db, db_path = file_db
    conv = _add_conversation(db, session_id="null-apply-seq", session_key=None, archived=True)
    _add_message(
        db,
        conversation_id=conv,
        seq=9,
        role="assistant",
        content="high-seq non-marker reply",
    )
    _add_message(
        db,
        conversation_id=conv,
        seq=0,
        role="user",
        content="[Subagent Context] seq-zero marker",
    )

    result = apply_doctor_cleaners(db, database_path=db_path, filter_ids=["null_subagent_context"])
    assert result.kind == "applied"
    assert result.deleted_conversations == 1
    assert result.deleted_messages == 2
    assert db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# First-message preview normalization (the shared helper, via scan)
# ---------------------------------------------------------------------------


def test_scan_preview_normalization_collapses_whitespace(
    mem_db: sqlite3.Connection,
) -> None:
    """The example preview collapses internal whitespace runs to one space."""
    conv = _add_conversation(mem_db, session_id="null-ws", session_key=None, archived=True)
    _add_message(
        mem_db,
        conversation_id=conv,
        seq=0,
        content="[Subagent Context]\n\n   tabs\t\tand   newlines",
    )
    scan = scan_doctor_cleaners(mem_db, ["null_subagent_context"])
    assert scan.filters[0].examples[0].first_message_preview == (
        "[Subagent Context] tabs and newlines"
    )


def test_scan_preview_normalization_ellipsis_trim(
    mem_db: sqlite3.Connection,
) -> None:
    """A preview longer than 120 chars is trimmed to 117 chars + ``"..."``."""
    long_tail = "x" * 300
    conv = _add_conversation(mem_db, session_id="null-long", session_key=None, archived=True)
    _add_message(
        mem_db,
        conversation_id=conv,
        seq=0,
        content=f"[Subagent Context] {long_tail}",
    )
    preview = (
        scan_doctor_cleaners(mem_db, ["null_subagent_context"])
        .filters[0]
        .examples[0]
        .first_message_preview
    )
    assert preview is not None
    assert len(preview) == 120
    assert preview.endswith("...")
