"""Tests for :mod:`lossless_hermes.doctor.shared` and
:mod:`lossless_hermes.doctor.contract` (issue 08-06).

Per ``epics/08-cli-ops/08-06-doctor-shared.md`` "Test inventory":

    "Doctor cleaners and ``applyScopedDoctorRepair`` have no dedicated
    test file on this branch... this is a coverage gap worth filling
    in the Python port."

This file fills the gap for the shared-contract surface. Three
mandated tests from the issue spec:

* :func:`test_detect_doctor_marker_table` — 20-case fixture covering
  each marker shape as prefix, suffix, within-window, beyond-window
  (plus the clean-content / empty-string baseline).
* :func:`test_load_doctor_targets_ordering` — seeded DB with mixed
  ``depth`` / ``created_at`` / ``conversation_id``, asserts the
  documented ordering on both the unfiltered and the filtered branch.
* :func:`test_get_doctor_summary_stats_per_conversation` — 3-conversation
  fixture, asserts per-conv aggregation in :attr:`by_conversation`.

Plus three coverage-completing tests:

* Constant-equality test (AC: "all six constants verbatim string-equal").
* Pre-filter false-positive drop test (the SQL INSTR matches mid-content
  but :func:`detect_doctor_marker` returns :data:`None` → row dropped).
* DB-wide vs conversation-filtered branch coverage.

See:

* ``epics/08-cli-ops/08-06-doctor-shared.md`` — this issue.
* ``docs/porting-guides/doctor-ops.md`` §"Doctor marker detection"
  lines 193-201.
* ``lossless-claw/src/plugin/lcm-doctor-shared.ts:1-270`` — TS source
  pinned at commit ``1f07fbd``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Literal, Optional

import pytest

from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.doctor.contract import (
    FALLBACK_SUMMARY_MARKER,
    FALLBACK_SUMMARY_MARKER_V41_FULL,
    FALLBACK_SUMMARY_MARKER_V41_TRUNC,
    FALLBACK_SUMMARY_WINDOW,
    TRUNCATED_SUMMARY_PREFIX,
    TRUNCATED_SUMMARY_WINDOW,
    DoctorMarkerKind,
)
from lossless_hermes.doctor.shared import (
    detect_doctor_marker,
    get_doctor_summary_stats,
    load_doctor_targets,
)

# ---------------------------------------------------------------------------
# Fixtures + seed helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    """In-memory SQLite with the full migration ladder applied.

    Two conversations seeded for the per-conversation tests:

    * conv 1: session_key ``"sk1"``
    * conv 2: session_key ``"sk2"``
    * conv 3: session_key ``"sk3"``
    """
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(conn, fts5_available=False, seed_default_prompts=False)
    conn.execute("INSERT INTO conversations (session_id, session_key) VALUES ('s1', 'sk1')")
    conn.execute("INSERT INTO conversations (session_id, session_key) VALUES ('s2', 'sk2')")
    conn.execute("INSERT INTO conversations (session_id, session_key) VALUES ('s3', 'sk3')")
    try:
        yield conn
    finally:
        conn.close()


def _insert_summary(
    db: sqlite3.Connection,
    *,
    summary_id: str,
    conversation_id: int = 1,
    kind: Literal["leaf", "condensed"] = "leaf",
    content: str,
    depth: int = 0,
    token_count: int = 100,
    created_at: Optional[str] = None,
) -> None:
    """Insert one summary row.

    The migration's ``summaries.created_at`` column has a
    ``DEFAULT (datetime('now'))`` clause; pass ``created_at`` explicitly
    when the test depends on ordering relative to other rows (the helper
    forwards ``None`` to take the default).
    """
    if created_at is None:
        db.execute(
            """
            INSERT INTO summaries
              (summary_id, conversation_id, kind, content, depth, token_count)
              VALUES (?, ?, ?, ?, ?, ?)
            """,
            (summary_id, conversation_id, kind, content, depth, token_count),
        )
    else:
        db.execute(
            """
            INSERT INTO summaries
              (summary_id, conversation_id, kind, content, depth, token_count, created_at)
              VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (summary_id, conversation_id, kind, content, depth, token_count, created_at),
        )


def _insert_child_link(
    db: sqlite3.Connection,
    *,
    parent_summary_id: str,
    child_summary_id: str,
    ordinal: int = 0,
) -> None:
    """Insert a ``summary_parents`` row so ``child_count`` increments.

    Per the schema (``migration.py:277``), ``summary_parents.summary_id``
    is the **child** column (the rolled-up summary), and
    ``parent_summary_id`` points at the **leaf** it consumed. So a
    condensed summary ``cond_x`` rolling up leaf ``leaf_a`` is:
    ``INSERT INTO summary_parents (summary_id, parent_summary_id) =
    (cond_x, leaf_a)``. To count "leaf_a has 1 child", we need a row
    with ``summary_id = leaf_a``; but that requires another summary
    consuming leaf_a as a parent — so we insert a row with
    ``summary_id = parent_summary_id`` of the parameter. The helper
    inverts the semantic to express the test intent ("the parent has
    this child").

    In other words: from doctor's POV, ``child_count`` for a summary
    counts how many rows have ``summary_parents.summary_id = THAT
    summary``. So to make ``leaf_a.child_count = 1``, we INSERT
    ``(leaf_a, child_of_leaf_a)`` — exactly how the helper expresses it.
    """
    db.execute(
        """
        INSERT INTO summary_parents (summary_id, parent_summary_id, ordinal)
          VALUES (?, ?, ?)
        """,
        (parent_summary_id, child_summary_id, ordinal),
    )


# ---------------------------------------------------------------------------
# AC: constants verbatim string-equal to TS counterparts
# ---------------------------------------------------------------------------


def test_marker_constants_are_byte_equal_to_ts() -> None:
    """All six constants are verbatim string-equal to their TS counterparts.

    These are wire-protocol strings. Changing any of them is a breaking
    change — DBs written by other LCM hosts become invisible to detection.

    Ports the constant equality check from the issue AC:

        ``assert FALLBACK_SUMMARY_MARKER == "[LCM fallback summary; truncated for context management]"``

    Cross-reference: ``lossless-claw/src/plugin/lcm-doctor-shared.ts:9-16``.
    """
    assert FALLBACK_SUMMARY_MARKER == "[LCM fallback summary; truncated for context management]"
    assert FALLBACK_SUMMARY_MARKER_V41_TRUNC == (
        "[LCM fallback summary — model unavailable; raw source truncated for context management]"
    )
    assert FALLBACK_SUMMARY_MARKER_V41_FULL == (
        "[LCM fallback summary — model unavailable; raw source preserved verbatim below]"
    )
    assert TRUNCATED_SUMMARY_PREFIX == "[Truncated from "
    assert TRUNCATED_SUMMARY_WINDOW == 40
    assert FALLBACK_SUMMARY_WINDOW == 80


# ---------------------------------------------------------------------------
# AC: detect_doctor_marker returns the same value as TS for all four marker
# shapes (20-case fixture).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        # 1. Empty content → clean
        ("", None),
        # 2. Plain clean content → clean
        ("This is a clean summary with no markers.", None),
        # 3. v4.1 truncated marker as prefix → FALLBACK
        (FALLBACK_SUMMARY_MARKER_V41_TRUNC + "\n\nraw content here", DoctorMarkerKind.FALLBACK),
        # 4. v4.1 full marker as prefix → FALLBACK
        (FALLBACK_SUMMARY_MARKER_V41_FULL + "\n\nverbatim source", DoctorMarkerKind.FALLBACK),
        # 5. v4.1 truncated marker mid-content → none of the prefix branches
        #    fire; the legacy suffix branch doesn't match either (different
        #    string). Returns None.
        ("Some preamble.\n" + FALLBACK_SUMMARY_MARKER_V41_TRUNC + "\nmore content", None),
        # 6. v4.1 full marker mid-content → None (same reason as case 5)
        ("Some preamble.\n" + FALLBACK_SUMMARY_MARKER_V41_FULL + "\nmore content", None),
        # 7. Legacy marker as prefix → OLD (defense-in-depth path)
        (FALLBACK_SUMMARY_MARKER + " ...rest of content", DoctorMarkerKind.OLD),
        # 8. Legacy marker as trailing suffix in window → FALLBACK
        ("Brief content. " + FALLBACK_SUMMARY_MARKER, DoctorMarkerKind.FALLBACK),
        # 9. Legacy marker as trailing suffix just inside window (79 char tail)
        (
            "x" * (FALLBACK_SUMMARY_WINDOW - 1 - len(FALLBACK_SUMMARY_MARKER))
            + FALLBACK_SUMMARY_MARKER
            + "y",  # 1 char after marker
            # Tail length = 1 + len(MARKER) which is < FALLBACK_SUMMARY_WINDOW
            DoctorMarkerKind.FALLBACK,
        ),
        # 10. Legacy marker beyond window (way before end) → None
        (FALLBACK_SUMMARY_MARKER + "z" * 200, DoctorMarkerKind.OLD),
        # ^ Note: case 10 has the legacy marker at byte 0, so the
        # ``startswith(FALLBACK_SUMMARY_MARKER)`` branch fires (OLD), not
        # the suffix branch.
        # 11. Legacy marker beyond-window when not a prefix → None
        ("X" + FALLBACK_SUMMARY_MARKER + "z" * 200, None),
        # 12. TRUNCATED_SUMMARY_PREFIX as trailing suffix in window → NEW
        ("Real summary content. " + TRUNCATED_SUMMARY_PREFIX + "42 tokens]", DoctorMarkerKind.NEW),
        # 13. TRUNCATED_SUMMARY_PREFIX exactly at window edge — last 40 chars
        (
            "y" * (TRUNCATED_SUMMARY_WINDOW - 1 - len(TRUNCATED_SUMMARY_PREFIX))
            + TRUNCATED_SUMMARY_PREFIX
            + "1]",
            DoctorMarkerKind.NEW,
        ),
        # 14. TRUNCATED_SUMMARY_PREFIX beyond window (>40 chars from end) → None
        (
            TRUNCATED_SUMMARY_PREFIX + "n tokens]" + "z" * 100,
            None,
        ),
        # 15. TRUNCATED_SUMMARY_PREFIX as a prefix at start (no trailing content
        #     of any length) → in-window suffix branch fires because the
        #     remainder (len(content) - 0 = full length of prefix) is < 40
        #     iff the content is shorter than 40 chars.
        (TRUNCATED_SUMMARY_PREFIX + "20]", DoctorMarkerKind.NEW),
        # 16. Both v4.1 truncated marker + truncated-suffix in same content →
        #     v4.1 prefix wins (checked first). FALLBACK.
        (
            FALLBACK_SUMMARY_MARKER_V41_TRUNC + "\nmid\n" + TRUNCATED_SUMMARY_PREFIX + "5]",
            DoctorMarkerKind.FALLBACK,
        ),
        # 17. Legacy marker AND truncated-suffix in same content; legacy as
        #     prefix wins (OLD branch checks before the suffix branches).
        (
            FALLBACK_SUMMARY_MARKER + " more " + TRUNCATED_SUMMARY_PREFIX + "9]",
            DoctorMarkerKind.OLD,
        ),
        # 18. Truncated-prefix appears multiple times — find() returns the
        #     FIRST occurrence. If the first is beyond the window, returns
        #     None even though a later occurrence is in-window.
        #     (Matches TS ``indexOf`` semantics — first match only.)
        (
            TRUNCATED_SUMMARY_PREFIX + "early]" + "x" * 200 + TRUNCATED_SUMMARY_PREFIX + "late]",
            None,
        ),
        # 19. Legacy marker mid-content with neither prefix nor suffix
        #     position — None.
        (
            "Beginning. " + FALLBACK_SUMMARY_MARKER + " " + "z" * 200,
            None,
        ),
        # 20. Whitespace-only content → None (no marker)
        ("   \n\t  \n  ", None),
    ],
)
def test_detect_doctor_marker_table(content: str, expected: Optional[DoctorMarkerKind]) -> None:
    """20-case fixture covering each marker shape as prefix, suffix,
    within-window, and beyond-window.

    Mandated by issue 08-06 AC ("test fixture with ~20 cases covering
    each marker as prefix, suffix, within-window, beyond-window").
    """
    assert detect_doctor_marker(content) == expected


# ---------------------------------------------------------------------------
# AC: load_doctor_targets returns rows in deterministic order
# ---------------------------------------------------------------------------


def test_load_doctor_targets_ordering_unfiltered(db: sqlite3.Connection) -> None:
    """DB-wide: ``conversation_id ASC, depth ASC, created_at ASC, summary_id ASC``.

    Mandated by issue 08-06 AC ("seeded DB with mixed depth/created_at,
    asserts ordering"). Seeds 6 rows across 2 conversations with mixed
    depth + created_at to verify all 4 sort keys.
    """
    # Conv 2 first (so insertion order is NOT the same as sort order)
    _insert_summary(
        db,
        summary_id="c2_d1_early",
        conversation_id=2,
        depth=1,
        created_at="2026-01-01T00:00:00",
        content=FALLBACK_SUMMARY_MARKER_V41_TRUNC + "\nbody",
    )
    _insert_summary(
        db,
        summary_id="c2_d0_late",
        conversation_id=2,
        depth=0,
        created_at="2026-03-01T00:00:00",
        content=FALLBACK_SUMMARY_MARKER_V41_FULL + "\nbody",
    )
    _insert_summary(
        db,
        summary_id="c1_d0_early_b",
        conversation_id=1,
        depth=0,
        created_at="2026-02-01T00:00:00",
        content="body " + TRUNCATED_SUMMARY_PREFIX + "9]",
    )
    _insert_summary(
        db,
        summary_id="c1_d0_early_a",
        conversation_id=1,
        depth=0,
        created_at="2026-02-01T00:00:00",  # SAME created_at as _b → summary_id tiebreaker
        content="body " + TRUNCATED_SUMMARY_PREFIX + "9]",
    )
    _insert_summary(
        db,
        summary_id="c1_d2_oldest",
        conversation_id=1,
        depth=2,
        kind="condensed",
        created_at="2025-12-01T00:00:00",
        content=FALLBACK_SUMMARY_MARKER_V41_TRUNC + "\nbody",
    )
    _insert_summary(
        db,
        summary_id="c1_d0_oldest",
        conversation_id=1,
        depth=0,
        created_at="2025-12-15T00:00:00",
        content=FALLBACK_SUMMARY_MARKER_V41_TRUNC + "\nbody",
    )

    rows = load_doctor_targets(db, conversation_id=None)
    ids = [r.summary_id for r in rows]

    # Expected order:
    #   conv 1 first (1 < 2):
    #     depth 0 first:
    #       created_at 2025-12-15 (c1_d0_oldest)
    #       created_at 2026-02-01 ties: c1_d0_early_a < c1_d0_early_b
    #     depth 2:
    #       c1_d2_oldest
    #   conv 2:
    #     depth 0: c2_d0_late
    #     depth 1: c2_d1_early
    assert ids == [
        "c1_d0_oldest",
        "c1_d0_early_a",
        "c1_d0_early_b",
        "c1_d2_oldest",
        "c2_d0_late",
        "c2_d1_early",
    ]


def test_load_doctor_targets_ordering_filtered(db: sqlite3.Connection) -> None:
    """Filtered to one conversation: ``depth ASC, created_at ASC, summary_id ASC``."""
    _insert_summary(
        db,
        summary_id="a_d1",
        conversation_id=1,
        depth=1,
        created_at="2026-01-01T00:00:00",
        content=FALLBACK_SUMMARY_MARKER_V41_TRUNC + "\nbody",
    )
    _insert_summary(
        db,
        summary_id="b_d0_late",
        conversation_id=1,
        depth=0,
        created_at="2026-02-01T00:00:00",
        content=FALLBACK_SUMMARY_MARKER_V41_TRUNC + "\nbody",
    )
    _insert_summary(
        db,
        summary_id="z_d0_early",
        conversation_id=1,
        depth=0,
        created_at="2026-01-15T00:00:00",
        content=FALLBACK_SUMMARY_MARKER_V41_TRUNC + "\nbody",
    )
    _insert_summary(
        db,
        summary_id="c_d0_late_again",
        conversation_id=1,
        depth=0,
        created_at="2026-02-01T00:00:00",  # SAME as b_d0_late → tiebreak by summary_id
        content=FALLBACK_SUMMARY_MARKER_V41_TRUNC + "\nbody",
    )
    # Seed a row in another conversation; must NOT appear
    _insert_summary(
        db,
        summary_id="other_conv_row",
        conversation_id=2,
        depth=0,
        content=FALLBACK_SUMMARY_MARKER_V41_TRUNC + "\nbody",
    )

    rows = load_doctor_targets(db, conversation_id=1)
    ids = [r.summary_id for r in rows]
    assert ids == [
        "z_d0_early",  # depth=0, earliest
        "b_d0_late",  # depth=0, tied created_at, summary_id 'b' < 'c'
        "c_d0_late_again",  # depth=0, tied created_at, after 'b'
        "a_d1",  # depth=1
    ]
    # Filter actually filters
    assert "other_conv_row" not in ids


def test_load_doctor_targets_filter_only_returns_that_conversation(
    db: sqlite3.Connection,
) -> None:
    """``conversation_id=42`` filters to that conversation; unfiltered returns DB-wide."""
    _insert_summary(
        db,
        summary_id="conv1_marker",
        conversation_id=1,
        content=FALLBACK_SUMMARY_MARKER_V41_TRUNC + "\nbody",
    )
    _insert_summary(
        db,
        summary_id="conv2_marker",
        conversation_id=2,
        content=FALLBACK_SUMMARY_MARKER_V41_TRUNC + "\nbody",
    )

    rows_conv1 = load_doctor_targets(db, conversation_id=1)
    rows_conv2 = load_doctor_targets(db, conversation_id=2)
    rows_all = load_doctor_targets(db, conversation_id=None)

    assert [r.summary_id for r in rows_conv1] == ["conv1_marker"]
    assert [r.summary_id for r in rows_conv2] == ["conv2_marker"]
    assert {r.summary_id for r in rows_all} == {"conv1_marker", "conv2_marker"}


def test_load_doctor_targets_skips_pre_filter_false_positives(
    db: sqlite3.Connection,
) -> None:
    """INSTR matches anywhere in content; the precise detector drops false positives.

    A summary with the truncated-prefix string mid-content (not in the
    last 40 chars and not a leading legacy marker) matches the SQL INSTR
    pre-filter but :func:`detect_doctor_marker` returns :data:`None`.
    The row must be dropped.
    """
    # The "[Truncated from " string appears at position 0 but the
    # remainder is > 40 chars long, so the suffix window check fails.
    # No leading legacy marker. → detect_doctor_marker returns None.
    fake_content = TRUNCATED_SUMMARY_PREFIX + "early]" + "x" * 200
    _insert_summary(db, summary_id="false_positive", content=fake_content)

    rows = load_doctor_targets(db, conversation_id=None)
    assert rows == []


def test_load_doctor_targets_marker_kind_per_row(db: sqlite3.Connection) -> None:
    """Re-classification per row assigns the correct ``marker_kind``."""
    _insert_summary(
        db,
        summary_id="row_old",
        content=FALLBACK_SUMMARY_MARKER + " more text",
    )
    _insert_summary(
        db,
        summary_id="row_new",
        content="body " + TRUNCATED_SUMMARY_PREFIX + "5]",
    )
    _insert_summary(
        db,
        summary_id="row_fallback_v41",
        content=FALLBACK_SUMMARY_MARKER_V41_TRUNC + "\nbody",
    )
    _insert_summary(
        db,
        summary_id="row_fallback_legacy",
        content="body " + FALLBACK_SUMMARY_MARKER,  # trailing suffix → FALLBACK
    )

    rows = load_doctor_targets(db, conversation_id=None)
    by_id = {r.summary_id: r.marker_kind for r in rows}
    assert by_id["row_old"] == DoctorMarkerKind.OLD
    assert by_id["row_new"] == DoctorMarkerKind.NEW
    assert by_id["row_fallback_v41"] == DoctorMarkerKind.FALLBACK
    assert by_id["row_fallback_legacy"] == DoctorMarkerKind.FALLBACK


def test_load_doctor_targets_child_count_aggregated(db: sqlite3.Connection) -> None:
    """``child_count`` reflects the LEFT JOIN row-count from ``summary_parents``.

    A leaf with 2 ``summary_parents`` rows pointing at it as
    ``parent_summary_id``... wait — the doctor's ``child_count`` is the
    count of rows where ``summary_parents.summary_id = THIS_summary``.
    See the helper docstring for the schema-vs-doctor terminology gap.
    """
    # leaf_a has the marker; we want child_count = 2 for the doctor row.
    _insert_summary(
        db,
        summary_id="leaf_a",
        content=FALLBACK_SUMMARY_MARKER_V41_TRUNC + "\nbody",
    )
    # Two extra summaries that act as the "children" — these don't need
    # markers because we're only checking ``leaf_a.child_count``.
    _insert_summary(db, summary_id="child_x", content="clean")
    _insert_summary(db, summary_id="child_y", content="clean")
    # Each child row points up at leaf_a as its parent. From doctor's
    # query: ``summary_parents WHERE summary_id = leaf_a`` returns 2 rows.
    _insert_child_link(db, parent_summary_id="leaf_a", child_summary_id="child_x", ordinal=0)
    _insert_child_link(db, parent_summary_id="leaf_a", child_summary_id="child_y", ordinal=1)

    rows = load_doctor_targets(db, conversation_id=1)
    leaf_a = next(r for r in rows if r.summary_id == "leaf_a")
    assert leaf_a.child_count == 2

    # Leaves with no child rows → child_count = 0 (LEFT JOIN miss).
    _insert_summary(
        db,
        summary_id="leaf_b",
        conversation_id=2,
        content=FALLBACK_SUMMARY_MARKER_V41_TRUNC + "\nbody",
    )
    rows_all = load_doctor_targets(db, conversation_id=None)
    leaf_b = next(r for r in rows_all if r.summary_id == "leaf_b")
    assert leaf_b.child_count == 0


def test_load_doctor_targets_kind_narrowing(db: sqlite3.Connection) -> None:
    """Both ``leaf`` and ``condensed`` rows materialize."""
    _insert_summary(
        db,
        summary_id="a_leaf",
        kind="leaf",
        content=FALLBACK_SUMMARY_MARKER_V41_TRUNC + "\nbody",
    )
    _insert_summary(
        db,
        summary_id="b_condensed",
        kind="condensed",
        content=FALLBACK_SUMMARY_MARKER_V41_TRUNC + "\nbody",
    )

    rows = load_doctor_targets(db, conversation_id=None)
    by_id = {r.summary_id: r.kind for r in rows}
    assert by_id["a_leaf"] == "leaf"
    assert by_id["b_condensed"] == "condensed"


def test_load_doctor_targets_on_clean_db_returns_empty(db: sqlite3.Connection) -> None:
    """No marker-bearing summaries → empty list (not :data:`None`)."""
    _insert_summary(db, summary_id="clean_a", content="purely clean content")
    _insert_summary(db, summary_id="clean_b", content="more clean content")
    assert load_doctor_targets(db, conversation_id=None) == []
    assert load_doctor_targets(db, conversation_id=1) == []


# ---------------------------------------------------------------------------
# AC: get_doctor_summary_stats returns per-conv aggregation
# ---------------------------------------------------------------------------


def test_get_doctor_summary_stats_per_conversation(db: sqlite3.Connection) -> None:
    """3-conversation fixture, asserts per-conv aggregation in
    :attr:`DoctorSummaryStats.by_conversation`.

    Mandated by issue 08-06 AC.

    Seed:

    * conv 1: 2 OLD + 1 NEW + 1 FALLBACK → total 4
    * conv 2: 1 FALLBACK → total 1
    * conv 3: 0 (clean rows only) → not present in ``by_conversation``

    Plus one false-positive row (clean content) and one absolutely-clean
    row in conv 1 to verify they don't inflate counts.
    """
    # Conv 1
    _insert_summary(
        db,
        summary_id="c1_old_a",
        conversation_id=1,
        content=FALLBACK_SUMMARY_MARKER + " ...rest",
    )
    _insert_summary(
        db,
        summary_id="c1_old_b",
        conversation_id=1,
        content=FALLBACK_SUMMARY_MARKER + " ...rest",
    )
    _insert_summary(
        db,
        summary_id="c1_new",
        conversation_id=1,
        content="body " + TRUNCATED_SUMMARY_PREFIX + "9]",
    )
    _insert_summary(
        db,
        summary_id="c1_fallback",
        conversation_id=1,
        content=FALLBACK_SUMMARY_MARKER_V41_TRUNC + "\nbody",
    )
    _insert_summary(db, summary_id="c1_clean", conversation_id=1, content="no markers")
    # Conv 2
    _insert_summary(
        db,
        summary_id="c2_fallback",
        conversation_id=2,
        content=FALLBACK_SUMMARY_MARKER_V41_FULL + "\nbody",
    )
    # Conv 3 — entirely clean
    _insert_summary(db, summary_id="c3_clean_a", conversation_id=3, content="all good")
    _insert_summary(db, summary_id="c3_clean_b", conversation_id=3, content="also good")

    stats = get_doctor_summary_stats(db, conversation_id=None)
    # Top-level counts
    assert stats.total == 5
    assert stats.old == 2
    assert stats.truncated == 1
    assert stats.fallback == 2
    # candidates list ordering follows load_doctor_targets ordering
    assert {c.summary_id for c in stats.candidates} == {
        "c1_old_a",
        "c1_old_b",
        "c1_new",
        "c1_fallback",
        "c2_fallback",
    }
    # by_conversation: only conv 1 and conv 2 (conv 3 has zero markers)
    assert set(stats.by_conversation.keys()) == {1, 2}
    c1 = stats.by_conversation[1]
    assert c1.total == 4
    assert c1.old == 2
    assert c1.truncated == 1
    assert c1.fallback == 1
    c2 = stats.by_conversation[2]
    assert c2.total == 1
    assert c2.old == 0
    assert c2.truncated == 0
    assert c2.fallback == 1


def test_get_doctor_summary_stats_filtered_to_one_conversation(
    db: sqlite3.Connection,
) -> None:
    """When ``conversation_id`` is passed, ``by_conversation`` has at most one key."""
    _insert_summary(
        db,
        summary_id="c1_marker",
        conversation_id=1,
        content=FALLBACK_SUMMARY_MARKER_V41_TRUNC + "\nbody",
    )
    _insert_summary(
        db,
        summary_id="c2_marker",
        conversation_id=2,
        content=FALLBACK_SUMMARY_MARKER_V41_TRUNC + "\nbody",
    )
    stats = get_doctor_summary_stats(db, conversation_id=1)
    assert stats.total == 1
    assert list(stats.by_conversation.keys()) == [1]
    assert stats.fallback == 1


def test_get_doctor_summary_stats_clean_db(db: sqlite3.Connection) -> None:
    """Clean DB → empty stats, never :data:`None`."""
    stats = get_doctor_summary_stats(db, conversation_id=None)
    assert stats.total == 0
    assert stats.old == 0
    assert stats.truncated == 0
    assert stats.fallback == 0
    assert stats.candidates == []
    assert stats.by_conversation == {}


# ---------------------------------------------------------------------------
# AC: pre-filter INSTR clause uses indexes (EXPLAIN QUERY PLAN snapshot)
# ---------------------------------------------------------------------------


def test_load_doctor_targets_filtered_branch_uses_index(db: sqlite3.Connection) -> None:
    """The conversation-filtered branch's ``WHERE conversation_id = ?``
    predicate must hit a ``conversation_id``-keyed index so
    per-conversation doctor scans don't full-table-scan ``summaries``.

    Pins the EXPLAIN QUERY PLAN output: ``s`` is SEARCHed via an index
    on conversation_id rather than SCANned. The exact index name varies
    across SQLite versions (3.40 picks ``summaries_conv_created_idx``;
    3.45+ picks ``summaries_conv_depth_kind_idx``) — both are valid
    partial-index hits per AC ("uses partial index where applicable").
    """
    plan_sql = """
    EXPLAIN QUERY PLAN
    SELECT s.conversation_id, s.summary_id, s.kind,
      COALESCE(s.depth, 0) AS depth,
      COALESCE(s.token_count, 0) AS token_count,
      COALESCE(s.content, '') AS content,
      COALESCE(s.created_at, '') AS created_at,
      COALESCE(spc.child_count, 0) AS child_count
    FROM summaries s
    LEFT JOIN (
      SELECT summary_id, COUNT(*) AS child_count
      FROM summary_parents
      GROUP BY summary_id
    ) spc ON spc.summary_id = s.summary_id
    WHERE s.conversation_id = ?
      AND (
        INSTR(COALESCE(s.content, ''), ?) > 0
        OR INSTR(COALESCE(s.content, ''), ?) > 0
        OR INSTR(COALESCE(s.content, ''), ?) > 0
        OR INSTR(COALESCE(s.content, ''), ?) > 0
      )
    ORDER BY COALESCE(s.depth, 0) ASC, s.created_at ASC, s.summary_id ASC
    """
    plan = db.execute(plan_sql, (1, "a", "b", "c", "d")).fetchall()
    plan_text = "\n".join(str(row) for row in plan)
    # The filter MUST resolve to an index SEARCH on s, not a SCAN.
    # SQLite version-dependent which conversation_id-keyed index wins
    # (summaries_conv_created_idx, summaries_conv_depth_kind_idx, etc.);
    # the spec's AC is that A partial index is used, not a specific one.
    assert "SEARCH s USING INDEX summaries_conv_" in plan_text, plan_text
    # And specifically: not a full table scan.
    assert "SCAN s " not in plan_text, plan_text


def test_load_doctor_targets_unfiltered_branch_uses_index(db: sqlite3.Connection) -> None:
    """The DB-wide branch traverses ``summaries`` via an index
    (covering or otherwise), not a raw rowid scan.

    Pins that EXPLAIN QUERY PLAN names an index on the ``s`` traversal.
    The specific index varies across SQLite versions (older picks
    ``summaries_conv_created_idx``; newer prefers
    ``summaries_conv_depth_kind_idx``) — both satisfy the AC ("uses
    partial index where applicable").
    """
    plan_sql = """
    EXPLAIN QUERY PLAN
    SELECT s.conversation_id, s.summary_id, s.kind,
      COALESCE(s.depth, 0) AS depth,
      COALESCE(s.token_count, 0) AS token_count,
      COALESCE(s.content, '') AS content,
      COALESCE(s.created_at, '') AS created_at,
      COALESCE(spc.child_count, 0) AS child_count
    FROM summaries s
    LEFT JOIN (
      SELECT summary_id, COUNT(*) AS child_count
      FROM summary_parents
      GROUP BY summary_id
    ) spc ON spc.summary_id = s.summary_id
    WHERE INSTR(COALESCE(s.content, ''), ?) > 0
       OR INSTR(COALESCE(s.content, ''), ?) > 0
       OR INSTR(COALESCE(s.content, ''), ?) > 0
       OR INSTR(COALESCE(s.content, ''), ?) > 0
    ORDER BY s.conversation_id ASC,
             COALESCE(s.depth, 0) ASC,
             s.created_at ASC,
             s.summary_id ASC
    """
    plan = db.execute(plan_sql, ("a", "b", "c", "d")).fetchall()
    plan_text = "\n".join(str(row) for row in plan)
    # The DB-wide branch must traverse `s` via an index, not a rowid scan
    # of the table heap. SQLite phrases an index-driven traversal as either
    # ``SCAN s USING INDEX ...`` (full index scan) or
    # ``SCAN s USING COVERING INDEX ...`` (covering); both are acceptable.
    # An unqualified ``SCAN s`` (no USING INDEX) would be a regression.
    assert "SCAN s USING " in plan_text or "SEARCH s USING " in plan_text, plan_text
