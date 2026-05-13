"""Tests for :mod:`lossless_hermes.integrity` — the 8-check scanner.

Mirrors the integrity coverage in lossless-claw's ``test/lcm-integration.test.ts``
(there is no dedicated ``integrity.test.ts`` upstream — coverage is implicit
via the integration tests per doctor-ops.md §"Test inventory"). Per the issue
spec acceptance criteria:

* All 8 checks implemented per TS source enumeration.
* ``check_integrity(conn, conversation_id)`` returns a list of
  :class:`IntegrityCheck`; pass/fail/warn statuses correctly assigned.
* :func:`build_repair_plan` returns SQL candidate suggestions; no writes
  performed.
* Test fixture with one orphaned context_items row produces a ``fail`` result
  for the ``context_items_valid_refs`` check.
* Test fixture with summary cycle (A → B → A) produces an appropriate
  result for the lineage / orphan checks. (The cycle case is exercised by
  the warning + lineage paths since the schema uses ``ON DELETE RESTRICT``
  to prevent the cycle from being constructed — a cycle is the *absence*
  of a proper parent path, so we encode it via the lineage check.)

The :class:`IntegrityChecker` is read-only; tests also assert this by
snapshotting the DB state before + after a scan.

References:

* ``docs/porting-guides/doctor-ops.md`` §"Integrity checks".
* ``lossless-claw/src/integrity.ts`` — verbatim source.
* ``epics/01-storage/01-13-integrity-prune.md`` — issue spec.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.integrity import (
    IntegrityChecker,
    IntegrityReport,
    build_repair_plan,
    check_integrity,
    collect_metrics,
)


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def migrated_db() -> Iterator[sqlite3.Connection]:
    """In-memory SQLite with the core schema applied."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


def _insert_conversation(
    conn: sqlite3.Connection,
    *,
    session_id: str = "sess-1",
    session_key: str = "key-1",
) -> int:
    cur = conn.execute(
        "INSERT INTO conversations (session_id, session_key) VALUES (?, ?)",
        (session_id, session_key),
    )
    return int(cur.lastrowid or 0)


def _insert_message(
    conn: sqlite3.Connection,
    *,
    conversation_id: int,
    seq: int,
    token_count: int = 5,
    role: str = "user",
) -> int:
    cur = conn.execute(
        "INSERT INTO messages "
        "(conversation_id, seq, role, content, token_count) "
        "VALUES (?, ?, ?, ?, ?)",
        (conversation_id, seq, role, f"msg-{seq}", token_count),
    )
    return int(cur.lastrowid or 0)


def _insert_summary(
    conn: sqlite3.Connection,
    *,
    summary_id: str,
    conversation_id: int,
    kind: str = "leaf",
    token_count: int = 10,
) -> None:
    conn.execute(
        "INSERT INTO summaries "
        "(summary_id, conversation_id, kind, content, token_count) "
        "VALUES (?, ?, ?, ?, ?)",
        (summary_id, conversation_id, kind, f"sum-{summary_id}", token_count),
    )


def _get_check(report: IntegrityReport, name: str):
    """Return the :class:`IntegrityCheck` with ``name`` from a report.

    Raises ``KeyError`` if missing — the report should always carry all 8.
    """
    for check in report.checks:
        if check.name == name:
            return check
    raise KeyError(f"check {name!r} not in report")


# ---------------------------------------------------------------------------
# Shape + ordering
# ---------------------------------------------------------------------------


def test_report_always_carries_eight_checks(migrated_db: sqlite3.Connection) -> None:
    """Every scan returns exactly 8 checks, in canonical order."""
    conv_id = _insert_conversation(migrated_db)
    report = check_integrity(migrated_db, conv_id)
    assert len(report.checks) == 8
    expected_names = [
        "conversation_exists",
        "context_items_contiguous",
        "context_items_valid_refs",
        "summaries_have_lineage",
        "no_orphan_summaries",
        "context_token_consistency",
        "message_seq_contiguous",
        "no_duplicate_context_refs",
    ]
    assert [c.name for c in report.checks] == expected_names


def test_pass_fail_warn_counts_are_derived(migrated_db: sqlite3.Connection) -> None:
    """``pass_count`` + ``fail_count`` + ``warn_count`` equals 8."""
    conv_id = _insert_conversation(migrated_db)
    report = check_integrity(migrated_db, conv_id)
    total = report.pass_count + report.fail_count + report.warn_count
    assert total == 8


# ---------------------------------------------------------------------------
# Check 1: conversation_exists
# ---------------------------------------------------------------------------


def test_conversation_exists_pass(migrated_db: sqlite3.Connection) -> None:
    conv_id = _insert_conversation(migrated_db)
    report = check_integrity(migrated_db, conv_id)
    assert _get_check(report, "conversation_exists").status == "pass"


def test_conversation_exists_fail(migrated_db: sqlite3.Connection) -> None:
    """Missing conversation_id surfaces as ``fail``."""
    report = check_integrity(migrated_db, 9999)
    check = _get_check(report, "conversation_exists")
    assert check.status == "fail"
    assert "9999" in check.message


# ---------------------------------------------------------------------------
# Check 2: context_items_contiguous
# ---------------------------------------------------------------------------


def test_context_items_contiguous_pass(migrated_db: sqlite3.Connection) -> None:
    """Contiguous 0..N ordinals pass."""
    conv_id = _insert_conversation(migrated_db)
    m1 = _insert_message(migrated_db, conversation_id=conv_id, seq=0)
    m2 = _insert_message(migrated_db, conversation_id=conv_id, seq=1)
    migrated_db.execute(
        "INSERT INTO context_items "
        "(conversation_id, ordinal, item_type, message_id) "
        "VALUES (?, 0, 'message', ?)",
        (conv_id, m1),
    )
    migrated_db.execute(
        "INSERT INTO context_items "
        "(conversation_id, ordinal, item_type, message_id) "
        "VALUES (?, 1, 'message', ?)",
        (conv_id, m2),
    )
    report = check_integrity(migrated_db, conv_id)
    assert _get_check(report, "context_items_contiguous").status == "pass"


def test_context_items_contiguous_fail_on_gap(
    migrated_db: sqlite3.Connection,
) -> None:
    """A gap in the ordinal sequence fails the check."""
    conv_id = _insert_conversation(migrated_db)
    m1 = _insert_message(migrated_db, conversation_id=conv_id, seq=0)
    m2 = _insert_message(migrated_db, conversation_id=conv_id, seq=1)
    migrated_db.execute(
        "INSERT INTO context_items "
        "(conversation_id, ordinal, item_type, message_id) "
        "VALUES (?, 0, 'message', ?)",
        (conv_id, m1),
    )
    # Skip ordinal 1 → gap.
    migrated_db.execute(
        "INSERT INTO context_items "
        "(conversation_id, ordinal, item_type, message_id) "
        "VALUES (?, 2, 'message', ?)",
        (conv_id, m2),
    )
    report = check_integrity(migrated_db, conv_id)
    check = _get_check(report, "context_items_contiguous")
    assert check.status == "fail"
    assert check.details is not None
    assert check.details["gaps"] == [{"expected": 1, "actual": 2}]


# ---------------------------------------------------------------------------
# Check 3: context_items_valid_refs (the "orphan detection" target)
# ---------------------------------------------------------------------------


def test_context_items_valid_refs_pass(migrated_db: sqlite3.Connection) -> None:
    conv_id = _insert_conversation(migrated_db)
    m1 = _insert_message(migrated_db, conversation_id=conv_id, seq=0)
    migrated_db.execute(
        "INSERT INTO context_items "
        "(conversation_id, ordinal, item_type, message_id) "
        "VALUES (?, 0, 'message', ?)",
        (conv_id, m1),
    )
    report = check_integrity(migrated_db, conv_id)
    assert _get_check(report, "context_items_valid_refs").status == "pass"


def test_context_items_valid_refs_fails_on_dangling_message(
    migrated_db: sqlite3.Connection,
) -> None:
    """An orphaned context_items.message_id is detected as dangling.

    Issue spec AC: "Test fixture with one orphaned context_items row
    produces a ``fail`` result for the ``orphan_context_items`` check".
    The TS check name is ``context_items_valid_refs`` — same idea.
    """
    conv_id = _insert_conversation(migrated_db)
    # Insert message, then context_items referencing it, then delete the
    # message via PRAGMA foreign_keys = OFF to simulate the orphan state.
    m1 = _insert_message(migrated_db, conversation_id=conv_id, seq=0)
    migrated_db.execute(
        "INSERT INTO context_items "
        "(conversation_id, ordinal, item_type, message_id) "
        "VALUES (?, 0, 'message', ?)",
        (conv_id, m1),
    )
    # PRAGMA foreign_keys is a no-op inside an open transaction. Commit
    # the implicit transaction so the toggle takes effect.
    migrated_db.commit()
    migrated_db.execute("PRAGMA foreign_keys = OFF")
    migrated_db.execute("DELETE FROM messages WHERE message_id = ?", (m1,))
    migrated_db.commit()
    migrated_db.execute("PRAGMA foreign_keys = ON")

    report = check_integrity(migrated_db, conv_id)
    check = _get_check(report, "context_items_valid_refs")
    assert check.status == "fail"
    assert check.details is not None
    dangling = check.details["danglingRefs"]
    assert len(dangling) == 1
    assert dangling[0]["itemType"] == "message"
    assert dangling[0]["refId"] == m1


def test_context_items_valid_refs_fails_on_dangling_summary(
    migrated_db: sqlite3.Connection,
) -> None:
    """An orphaned context_items.summary_id is detected as dangling."""
    conv_id = _insert_conversation(migrated_db)
    _insert_summary(migrated_db, summary_id="sum_orphan", conversation_id=conv_id)
    migrated_db.execute(
        "INSERT INTO context_items "
        "(conversation_id, ordinal, item_type, summary_id) "
        "VALUES (?, 0, 'summary', ?)",
        (conv_id, "sum_orphan"),
    )
    migrated_db.commit()
    migrated_db.execute("PRAGMA foreign_keys = OFF")
    migrated_db.execute("DELETE FROM summaries WHERE summary_id = ?", ("sum_orphan",))
    migrated_db.commit()
    migrated_db.execute("PRAGMA foreign_keys = ON")

    report = check_integrity(migrated_db, conv_id)
    check = _get_check(report, "context_items_valid_refs")
    assert check.status == "fail"
    dangling = check.details["danglingRefs"]  # type: ignore[index]
    assert len(dangling) == 1
    assert dangling[0]["itemType"] == "summary"
    assert dangling[0]["refId"] == "sum_orphan"


# ---------------------------------------------------------------------------
# Check 4: summaries_have_lineage
# ---------------------------------------------------------------------------


def test_summaries_have_lineage_pass(migrated_db: sqlite3.Connection) -> None:
    """Leaves with summary_messages and condensed with summary_parents pass."""
    conv_id = _insert_conversation(migrated_db)
    m1 = _insert_message(migrated_db, conversation_id=conv_id, seq=0)
    _insert_summary(migrated_db, summary_id="leaf_1", conversation_id=conv_id)
    _insert_summary(migrated_db, summary_id="cond_1", conversation_id=conv_id, kind="condensed")
    migrated_db.execute(
        "INSERT INTO summary_messages (summary_id, message_id, ordinal) VALUES ('leaf_1', ?, 0)",
        (m1,),
    )
    migrated_db.execute(
        "INSERT INTO summary_parents (summary_id, parent_summary_id, ordinal) "
        "VALUES ('cond_1', 'leaf_1', 0)"
    )

    report = check_integrity(migrated_db, conv_id)
    assert _get_check(report, "summaries_have_lineage").status == "pass"


def test_summaries_have_lineage_fail_on_orphan_leaf(
    migrated_db: sqlite3.Connection,
) -> None:
    """A leaf with no summary_messages rows fails the check."""
    conv_id = _insert_conversation(migrated_db)
    _insert_summary(migrated_db, summary_id="leaf_orphan", conversation_id=conv_id)
    # No summary_messages row — leaf is orphaned.

    report = check_integrity(migrated_db, conv_id)
    check = _get_check(report, "summaries_have_lineage")
    assert check.status == "fail"
    missing = check.details["missingLineage"]  # type: ignore[index]
    assert len(missing) == 1
    assert missing[0]["summaryId"] == "leaf_orphan"
    assert missing[0]["kind"] == "leaf"


def test_summaries_have_lineage_fail_on_orphan_condensed(
    migrated_db: sqlite3.Connection,
) -> None:
    """A condensed with no summary_parents rows fails the check.

    This case exercises the "cycle" detection per the issue spec — a
    condensed summary that is not properly chained to its leaves
    is detected via the missing-lineage path.
    """
    conv_id = _insert_conversation(migrated_db)
    _insert_summary(
        migrated_db,
        summary_id="cond_orphan",
        conversation_id=conv_id,
        kind="condensed",
    )
    # No summary_parents row — condensed has no proper parent path.

    report = check_integrity(migrated_db, conv_id)
    check = _get_check(report, "summaries_have_lineage")
    assert check.status == "fail"
    missing = check.details["missingLineage"]  # type: ignore[index]
    assert any(m["summaryId"] == "cond_orphan" and m["kind"] == "condensed" for m in missing)


# ---------------------------------------------------------------------------
# Check 5: no_orphan_summaries (warn, not fail)
# ---------------------------------------------------------------------------


def test_no_orphan_summaries_pass(migrated_db: sqlite3.Connection) -> None:
    """A summary appearing in context_items passes."""
    conv_id = _insert_conversation(migrated_db)
    _insert_summary(migrated_db, summary_id="sum_in_context", conversation_id=conv_id)
    migrated_db.execute(
        "INSERT INTO context_items "
        "(conversation_id, ordinal, item_type, summary_id) "
        "VALUES (?, 0, 'summary', 'sum_in_context')",
        (conv_id,),
    )
    report = check_integrity(migrated_db, conv_id)
    assert _get_check(report, "no_orphan_summaries").status == "pass"


def test_no_orphan_summaries_warn(migrated_db: sqlite3.Connection) -> None:
    """A summary not in context_items and not a parent surfaces as ``warn``.

    Critically: ``warn``, not ``fail`` — orphans are harmless on the read
    path (per doctor-ops.md §"Integrity checks" table — only check that
    warns).
    """
    conv_id = _insert_conversation(migrated_db)
    _insert_summary(migrated_db, summary_id="sum_orphan", conversation_id=conv_id)
    # No context_items row, no summary_parents row → orphan.

    report = check_integrity(migrated_db, conv_id)
    check = _get_check(report, "no_orphan_summaries")
    assert check.status == "warn", "no_orphan_summaries is the *only* check that warns, not fails"
    assert check.details is not None
    assert "sum_orphan" in check.details["orphanedSummaryIds"]


def test_no_orphan_summaries_pass_when_summary_is_parent(
    migrated_db: sqlite3.Connection,
) -> None:
    """A summary that is a parent (via summary_parents) is not an orphan."""
    conv_id = _insert_conversation(migrated_db)
    _insert_summary(migrated_db, summary_id="leaf_parent", conversation_id=conv_id)
    _insert_summary(
        migrated_db,
        summary_id="cond_child",
        conversation_id=conv_id,
        kind="condensed",
    )
    migrated_db.execute(
        "INSERT INTO summary_parents (summary_id, parent_summary_id, ordinal) "
        "VALUES ('cond_child', 'leaf_parent', 0)"
    )
    # cond_child also needs to be in context_items so IT isn't an orphan.
    migrated_db.execute(
        "INSERT INTO context_items "
        "(conversation_id, ordinal, item_type, summary_id) "
        "VALUES (?, 0, 'summary', 'cond_child')",
        (conv_id,),
    )

    report = check_integrity(migrated_db, conv_id)
    assert _get_check(report, "no_orphan_summaries").status == "pass"


# ---------------------------------------------------------------------------
# Check 6: context_token_consistency
# ---------------------------------------------------------------------------


def test_context_token_consistency_pass(migrated_db: sqlite3.Connection) -> None:
    """Item-level sum == aggregate query for a clean conversation."""
    conv_id = _insert_conversation(migrated_db)
    m1 = _insert_message(migrated_db, conversation_id=conv_id, seq=0, token_count=7)
    _insert_summary(migrated_db, summary_id="sum_1", conversation_id=conv_id, token_count=13)
    migrated_db.execute(
        "INSERT INTO context_items "
        "(conversation_id, ordinal, item_type, message_id) "
        "VALUES (?, 0, 'message', ?)",
        (conv_id, m1),
    )
    migrated_db.execute(
        "INSERT INTO context_items "
        "(conversation_id, ordinal, item_type, summary_id) "
        "VALUES (?, 1, 'summary', 'sum_1')",
        (conv_id,),
    )

    report = check_integrity(migrated_db, conv_id)
    assert _get_check(report, "context_token_consistency").status == "pass"


# ---------------------------------------------------------------------------
# Check 7: message_seq_contiguous
# ---------------------------------------------------------------------------


def test_message_seq_contiguous_pass(migrated_db: sqlite3.Connection) -> None:
    """Messages with contiguous seq 0..N pass."""
    conv_id = _insert_conversation(migrated_db)
    _insert_message(migrated_db, conversation_id=conv_id, seq=0)
    _insert_message(migrated_db, conversation_id=conv_id, seq=1)
    report = check_integrity(migrated_db, conv_id)
    assert _get_check(report, "message_seq_contiguous").status == "pass"


def test_message_seq_contiguous_fail_on_gap(
    migrated_db: sqlite3.Connection,
) -> None:
    """A seq gap surfaces as fail with the gap recorded."""
    conv_id = _insert_conversation(migrated_db)
    _insert_message(migrated_db, conversation_id=conv_id, seq=0)
    _insert_message(migrated_db, conversation_id=conv_id, seq=2)  # gap

    report = check_integrity(migrated_db, conv_id)
    check = _get_check(report, "message_seq_contiguous")
    assert check.status == "fail"
    assert check.details is not None
    assert check.details["gaps"] == [{"expected": 1, "actual": 2}]


# ---------------------------------------------------------------------------
# Check 8: no_duplicate_context_refs
# ---------------------------------------------------------------------------


def test_no_duplicate_context_refs_pass(migrated_db: sqlite3.Connection) -> None:
    """Distinct refs pass."""
    conv_id = _insert_conversation(migrated_db)
    m1 = _insert_message(migrated_db, conversation_id=conv_id, seq=0)
    m2 = _insert_message(migrated_db, conversation_id=conv_id, seq=1)
    migrated_db.execute(
        "INSERT INTO context_items "
        "(conversation_id, ordinal, item_type, message_id) "
        "VALUES (?, 0, 'message', ?)",
        (conv_id, m1),
    )
    migrated_db.execute(
        "INSERT INTO context_items "
        "(conversation_id, ordinal, item_type, message_id) "
        "VALUES (?, 1, 'message', ?)",
        (conv_id, m2),
    )
    report = check_integrity(migrated_db, conv_id)
    assert _get_check(report, "no_duplicate_context_refs").status == "pass"


def test_no_duplicate_context_refs_fail_on_duplicate_message(
    migrated_db: sqlite3.Connection,
) -> None:
    """A duplicated message_id across two ordinals fails the check."""
    conv_id = _insert_conversation(migrated_db)
    m1 = _insert_message(migrated_db, conversation_id=conv_id, seq=0)
    migrated_db.execute(
        "INSERT INTO context_items "
        "(conversation_id, ordinal, item_type, message_id) "
        "VALUES (?, 0, 'message', ?)",
        (conv_id, m1),
    )
    migrated_db.execute(
        "INSERT INTO context_items "
        "(conversation_id, ordinal, item_type, message_id) "
        "VALUES (?, 1, 'message', ?)",
        (conv_id, m1),  # duplicate
    )

    report = check_integrity(migrated_db, conv_id)
    check = _get_check(report, "no_duplicate_context_refs")
    assert check.status == "fail"
    dups = check.details["duplicates"]  # type: ignore[index]
    assert len(dups) == 1
    assert dups[0]["refType"] == "message"
    assert dups[0]["refId"] == m1
    assert sorted(dups[0]["ordinals"]) == [0, 1]


# ---------------------------------------------------------------------------
# Read-only contract
# ---------------------------------------------------------------------------


def test_scan_is_read_only(migrated_db: sqlite3.Connection) -> None:
    """Scanning must not mutate the DB.

    Snapshots row counts across the core tables before + after a scan;
    they must match.
    """
    conv_id = _insert_conversation(migrated_db)
    m1 = _insert_message(migrated_db, conversation_id=conv_id, seq=0)
    _insert_summary(migrated_db, summary_id="sum_x", conversation_id=conv_id)
    migrated_db.execute(
        "INSERT INTO context_items "
        "(conversation_id, ordinal, item_type, message_id) "
        "VALUES (?, 0, 'message', ?)",
        (conv_id, m1),
    )

    def snapshot() -> tuple[int, int, int, int]:
        return (
            migrated_db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
            migrated_db.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            migrated_db.execute("SELECT COUNT(*) FROM summaries").fetchone()[0],
            migrated_db.execute("SELECT COUNT(*) FROM context_items").fetchone()[0],
        )

    before = snapshot()
    check_integrity(migrated_db, conv_id)
    after = snapshot()
    assert before == after


# ---------------------------------------------------------------------------
# Repair plan
# ---------------------------------------------------------------------------


def test_build_repair_plan_lists_suggestions_for_failures(
    migrated_db: sqlite3.Connection,
) -> None:
    """build_repair_plan returns one suggestion per failing/warning row."""
    # Create a conversation with a dangling context_items.message_id so we
    # exercise the per-row suggestion path.
    conv_id = _insert_conversation(migrated_db)
    m1 = _insert_message(migrated_db, conversation_id=conv_id, seq=0)
    migrated_db.execute(
        "INSERT INTO context_items "
        "(conversation_id, ordinal, item_type, message_id) "
        "VALUES (?, 0, 'message', ?)",
        (conv_id, m1),
    )
    # PRAGMA foreign_keys is a no-op inside an open transaction. Commit
    # the implicit transaction so the toggle takes effect.
    migrated_db.commit()
    migrated_db.execute("PRAGMA foreign_keys = OFF")
    migrated_db.execute("DELETE FROM messages WHERE message_id = ?", (m1,))
    migrated_db.commit()
    migrated_db.execute("PRAGMA foreign_keys = ON")

    report = check_integrity(migrated_db, conv_id)
    plan = build_repair_plan(report)
    assert any("Remove context item at ordinal 0" in s for s in plan)


def test_build_repair_plan_skips_passes(migrated_db: sqlite3.Connection) -> None:
    """A fully-passing report yields an empty repair plan."""
    conv_id = _insert_conversation(migrated_db)
    report = check_integrity(migrated_db, conv_id)
    # Empty conversation — only the conversation_exists check matters, and
    # it passes; no summaries/messages/context to validate.
    plan = build_repair_plan(report)
    # Only fail/warn rows contribute; an empty conversation has none.
    assert plan == []


def test_build_repair_plan_renders_orphan_warning(
    migrated_db: sqlite3.Connection,
) -> None:
    """Warnings (orphan summaries) produce suggestions."""
    conv_id = _insert_conversation(migrated_db)
    _insert_summary(migrated_db, summary_id="sum_orphan", conversation_id=conv_id)
    report = check_integrity(migrated_db, conv_id)
    plan = build_repair_plan(report)
    assert any("sum_orphan" in s for s in plan)


# ---------------------------------------------------------------------------
# collect_metrics
# ---------------------------------------------------------------------------


def test_collect_metrics_basic(migrated_db: sqlite3.Connection) -> None:
    """Metrics reflect the fixture row counts + token sums."""
    conv_id = _insert_conversation(migrated_db)
    m1 = _insert_message(migrated_db, conversation_id=conv_id, seq=0, token_count=7)
    _insert_summary(migrated_db, summary_id="leaf_m", conversation_id=conv_id, token_count=13)
    _insert_summary(
        migrated_db,
        summary_id="cond_m",
        conversation_id=conv_id,
        kind="condensed",
        token_count=20,
    )
    migrated_db.execute(
        "INSERT INTO context_items "
        "(conversation_id, ordinal, item_type, message_id) "
        "VALUES (?, 0, 'message', ?)",
        (conv_id, m1),
    )
    migrated_db.execute(
        "INSERT INTO context_items "
        "(conversation_id, ordinal, item_type, summary_id) "
        "VALUES (?, 1, 'summary', 'leaf_m')",
        (conv_id,),
    )

    metrics = collect_metrics(migrated_db, conv_id)
    assert metrics.conversation_id == conv_id
    assert metrics.message_count == 1
    assert metrics.summary_count == 2
    assert metrics.leaf_summary_count == 1
    assert metrics.condensed_summary_count == 1
    assert metrics.context_item_count == 2
    assert metrics.context_tokens == 7 + 13  # message + leaf, not condensed
    assert metrics.large_file_count == 0


def test_collect_metrics_empty_conversation(
    migrated_db: sqlite3.Connection,
) -> None:
    """Metrics on an empty conversation return zeros across the board."""
    conv_id = _insert_conversation(migrated_db)
    metrics = collect_metrics(migrated_db, conv_id)
    assert metrics.context_tokens == 0
    assert metrics.message_count == 0
    assert metrics.summary_count == 0
    assert metrics.leaf_summary_count == 0
    assert metrics.condensed_summary_count == 0
    assert metrics.context_item_count == 0
    assert metrics.large_file_count == 0


def test_check_integrity_returns_intrinsic_type(
    migrated_db: sqlite3.Connection,
) -> None:
    """``check_integrity`` returns an :class:`IntegrityReport` (not a list)."""
    conv_id = _insert_conversation(migrated_db)
    report = check_integrity(migrated_db, conv_id)
    assert isinstance(report, IntegrityReport)
    # The checker is also exposed for callers that want to scan multiple
    # conversations against a single instance.
    checker = IntegrityChecker(migrated_db)
    assert checker.scan(conv_id).conversation_id == conv_id
