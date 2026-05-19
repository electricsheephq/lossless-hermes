"""Tests for :mod:`lossless_hermes.doctor.apply` (issue 08-07).

Ports the behavior-coverage gap for ``applyScopedDoctorRepair``. Per
``docs/porting-guides/doctor-ops.md`` §"Test inventory" line 430:

    "Doctor cleaners and ``applyScopedDoctorRepair`` have no dedicated
    test file on this branch... this is a coverage gap worth filling in
    the Python port."

Five mandated tests from the issue spec's acceptance criteria:

* :func:`test_leaves_first_then_condensed` — confirms the ordering
  invariant (active leaves by ``context_items.ordinal``, then orphan
  leaves, then condensed).
* :func:`test_overrides_map_propagation` — a rewritten leaf's content
  reaches the condensed re-summarization input.
* :func:`test_marker_in_output_skipped` — a summarizer that keeps
  returning marker-bearing output is short-circuited (not overwritten).
* :func:`test_unavailable_when_no_summarizer` — an unresolvable
  summarizer yields ``{"kind": "unavailable"}``.
* :func:`test_atomic_write` — a partial failure mid-loop rolls back ALL
  writes via the single ``BEGIN IMMEDIATE``.

Plus coverage-completing tests for the empty-output skip, the
three-fallback previous-summary resolution, the no-targets early
return, the ``summaries_fts`` mirror update, and the per-target
exception capture.

See:

* ``epics/08-cli-ops/08-07-doctor-apply.md`` — this issue.
* ``docs/porting-guides/doctor-ops.md`` §"Doctor marker detection"
  lines 202-212.
* ``lossless-claw/src/plugin/lcm-doctor-apply.ts:1-541`` — TS source
  pinned at commit ``1f07fbd``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Literal, Optional

import pytest

from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.doctor import apply as apply_mod
from lossless_hermes.doctor.apply import DoctorSummarizeFn, apply_scoped_doctor_repair
from lossless_hermes.doctor.contract import (
    FALLBACK_SUMMARY_MARKER_V41_TRUNC,
    DoctorApplyResult,
)
from lossless_hermes.summarize import LcmSummarizeOptions

# ---------------------------------------------------------------------------
# Fixtures + seed helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    """In-memory SQLite with the full migration ladder + FTS5 tables.

    ``isolation_level=None`` (autocommit) is required so
    :func:`lossless_hermes.transaction_mutex.with_database_transaction`
    can drive ``BEGIN IMMEDIATE`` / ``COMMIT`` / ``ROLLBACK`` explicitly
    (the production
    :func:`lossless_hermes.db.connection.open_lcm_db` uses the same
    setting).

    ``fts5_available=True`` so the ``summaries_fts`` mirror exists and
    the best-effort FTS-update path in
    :func:`~lossless_hermes.doctor.apply._update_summary_fts` is
    exercised. FTS5 is compiled into CPython's bundled SQLite on every
    CI platform.
    """
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(conn, fts5_available=True, seed_default_prompts=False)
    conn.execute("INSERT INTO conversations (session_id, session_key) VALUES ('s1', 'sk1')")
    try:
        yield conn
    finally:
        conn.close()


def _insert_message(
    db: sqlite3.Connection,
    *,
    conversation_id: int = 1,
    seq: int,
    content: str,
    role: str = "user",
    created_at: str = "2026-04-22 10:00:00",
) -> int:
    """Insert one ``messages`` row; return its autoincrement ``message_id``."""
    cursor = db.execute(
        """
        INSERT INTO messages (conversation_id, seq, role, content, token_count, created_at)
          VALUES (?, ?, ?, ?, ?, ?)
        """,
        (conversation_id, seq, role, content, len(content), created_at),
    )
    return int(cursor.lastrowid or 0)


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
    earliest_at: Optional[str] = None,
    latest_at: Optional[str] = None,
) -> None:
    """Insert one ``summaries`` row."""
    db.execute(
        """
        INSERT INTO summaries
          (summary_id, conversation_id, kind, content, depth, token_count,
           created_at, earliest_at, latest_at)
          VALUES (?, ?, ?, ?, ?, ?,
                  COALESCE(?, datetime('now')), ?, ?)
        """,
        (
            summary_id,
            conversation_id,
            kind,
            content,
            depth,
            token_count,
            created_at,
            earliest_at,
            latest_at,
        ),
    )


def _link_summary_message(
    db: sqlite3.Connection,
    *,
    summary_id: str,
    message_id: int,
    ordinal: int,
) -> None:
    """Insert a ``summary_messages`` row (leaf → its source message)."""
    db.execute(
        "INSERT INTO summary_messages (summary_id, message_id, ordinal) VALUES (?, ?, ?)",
        (summary_id, message_id, ordinal),
    )


def _link_summary_parent(
    db: sqlite3.Connection,
    *,
    condensed_id: str,
    child_id: str,
    ordinal: int,
) -> None:
    """Link a condensed summary to one child summary it rolled up.

    Per the schema (``migration.py:277``), ``summary_parents.summary_id``
    is the **rolled-up** (condensed) summary and ``parent_summary_id``
    is the **child** it consumed. So a condensed ``cond`` rolling up
    leaf ``leaf_a`` is ``(summary_id=cond, parent_summary_id=leaf_a)``.
    """
    db.execute(
        "INSERT INTO summary_parents (summary_id, parent_summary_id, ordinal) VALUES (?, ?, ?)",
        (condensed_id, child_id, ordinal),
    )


def _add_context_item(
    db: sqlite3.Connection,
    *,
    conversation_id: int = 1,
    ordinal: int,
    summary_id: str,
) -> None:
    """Insert a summary-typed ``context_items`` row at ``ordinal``."""
    db.execute(
        """
        INSERT INTO context_items (conversation_id, ordinal, item_type, summary_id)
          VALUES (?, ?, 'summary', ?)
        """,
        (conversation_id, ordinal, summary_id),
    )


def _broken(text: str = "stale broken content") -> str:
    """Build a summary body that :func:`detect_doctor_marker` flags FALLBACK.

    Uses the v4.1 truncated marker as a PREFIX — the dominant broken
    shape on post-Wave-4 DBs.
    """
    return f"{FALLBACK_SUMMARY_MARKER_V41_TRUNC}\n\n{text}"


# ---------------------------------------------------------------------------
# Recording summarizer doubles
# ---------------------------------------------------------------------------


class _RecordingSummarizer:
    """A :data:`DoctorSummarizeFn` double that records every call.

    Returns a fixed ``output`` for every call (or a per-call sequence
    when ``outputs`` is supplied). The ``calls`` list captures the
    ``(text, aggressive, options)`` of every invocation so tests can
    assert ordering + override propagation.
    """

    def __init__(
        self,
        *,
        output: str = "clean rewritten summary",
        outputs: Optional[list[str]] = None,
    ) -> None:
        self._output = output
        self._outputs = outputs
        self.calls: list[tuple[str, bool, LcmSummarizeOptions]] = []

    def __call__(self, text: str, aggressive: bool, options: LcmSummarizeOptions) -> str:
        index = len(self.calls)
        self.calls.append((text, aggressive, options))
        if self._outputs is not None:
            return self._outputs[index] if index < len(self._outputs) else self._outputs[-1]
        return self._output


def _config(*, timezone: str = "UTC", custom_instructions: str = "") -> dict[str, str]:
    """Minimal config double — :func:`apply_scoped_doctor_repair` only
    reads ``timezone`` + ``custom_instructions`` off it.
    """
    return {"timezone": timezone, "custom_instructions": custom_instructions}


# ---------------------------------------------------------------------------
# AC: target ordering is leaves-first, then condensed.
# ---------------------------------------------------------------------------


def test_leaves_first_then_condensed(db: sqlite3.Connection) -> None:
    """Repair order is active leaves, then orphan leaves, then condensed.

    Mandated by Epic README "Verification gates" #6 + the issue AC:
    "Target ordering is leaves-first, then condensed; within each kind,
    active items by ``context_items.ordinal``, orphan items by
    ``(depth, created_at, summary_id)``."

    Fixture: two active leaves (``leaf_b`` at context-ordinal 1,
    ``leaf_a`` at context-ordinal 0 — inserted out of order to prove the
    sort), one orphan leaf (``leaf_orphan`` — no ``context_items`` row),
    and one condensed summary (``cond``). The summarizer records the
    call order; the assertion is that ``leaf_a`` precedes ``leaf_b``
    (context-ordinal sort), both leaves precede ``leaf_orphan``, and all
    three leaves precede ``cond``.
    """
    # Leaf A — context ordinal 0. Its source message.
    msg_a = _insert_message(db, seq=0, content="message for leaf A")
    _insert_summary(db, summary_id="leaf_a", kind="leaf", depth=0, content=_broken("leaf A"))
    _link_summary_message(db, summary_id="leaf_a", message_id=msg_a, ordinal=0)

    # Leaf B — context ordinal 1.
    msg_b = _insert_message(db, seq=1, content="message for leaf B")
    _insert_summary(db, summary_id="leaf_b", kind="leaf", depth=0, content=_broken("leaf B"))
    _link_summary_message(db, summary_id="leaf_b", message_id=msg_b, ordinal=0)

    # Orphan leaf — broken, but NOT in context_items.
    msg_orphan = _insert_message(db, seq=2, content="message for orphan leaf")
    _insert_summary(db, summary_id="leaf_orphan", kind="leaf", depth=0, content=_broken("orphan"))
    _link_summary_message(db, summary_id="leaf_orphan", message_id=msg_orphan, ordinal=0)

    # Condensed summary rolling up leaf_a + leaf_b.
    _insert_summary(db, summary_id="cond", kind="condensed", depth=1, content=_broken("cond"))
    _link_summary_parent(db, condensed_id="cond", child_id="leaf_a", ordinal=0)
    _link_summary_parent(db, condensed_id="cond", child_id="leaf_b", ordinal=1)

    # context_items inserted out of ordinal order to prove the sort:
    # leaf_b first (ordinal 1), then leaf_a (ordinal 0).
    _add_context_item(db, ordinal=1, summary_id="leaf_b")
    _add_context_item(db, ordinal=0, summary_id="leaf_a")
    _add_context_item(db, ordinal=2, summary_id="cond")

    summarizer = _RecordingSummarizer(output="clean")
    result = apply_scoped_doctor_repair(
        db=db,
        config=_config(),
        conversation_id=1,
        summarize=summarizer,
    )

    assert result.kind == "applied"
    assert result.detected == 4

    # The source text of each call identifies which target it was.
    # leaf calls carry the message content; the condensed call carries
    # child-summary content.
    call_texts = [text for (text, _aggr, _opts) in summarizer.calls]
    # leaf_a (ctx ordinal 0) BEFORE leaf_b (ctx ordinal 1).
    assert call_texts[0].count("message for leaf A") == 1
    assert call_texts[1].count("message for leaf B") == 1
    # orphan leaf after both active leaves.
    assert call_texts[2].count("message for orphan leaf") == 1
    # condensed LAST (its call carries leaf content, not a raw message).
    assert call_texts[3].count("message for leaf") == 0
    is_condensed_flags = [opts.is_condensed for (_t, _a, opts) in summarizer.calls]
    assert is_condensed_flags == [False, False, False, True]


# ---------------------------------------------------------------------------
# AC: in-memory overrides map carries rewritten leaf content into the
# condensed source-text construction.
# ---------------------------------------------------------------------------


def test_overrides_map_propagation(db: sqlite3.Connection) -> None:
    """A rewritten leaf's content reaches the condensed re-summarization.

    Mandated by the issue AC: "In-memory ``overrides`` map carries
    rewritten leaf content into condensed source-text construction
    (verified by a 3-leaf + 1-condensed fixture where leaf rewrite
    changes the condensed input)."

    Fixture: three broken leaves rolled up by one broken condensed
    summary. The summarizer rewrites each leaf to a distinctive marker
    string. The assertion: the condensed call's source text contains
    those rewritten leaf strings — proving the condensed builder read
    from ``overrides`` rather than the stale DB content.
    """
    leaf_ids = ["leaf_1", "leaf_2", "leaf_3"]
    for index, leaf_id in enumerate(leaf_ids):
        msg = _insert_message(db, seq=index, content=f"raw message {index}")
        _insert_summary(
            db, summary_id=leaf_id, kind="leaf", depth=0, content=_broken(f"stale {index}")
        )
        _link_summary_message(db, summary_id=leaf_id, message_id=msg, ordinal=0)
        _add_context_item(db, ordinal=index, summary_id=leaf_id)

    _insert_summary(
        db, summary_id="cond", kind="condensed", depth=1, content=_broken("stale condensed")
    )
    for index, leaf_id in enumerate(leaf_ids):
        _link_summary_parent(db, condensed_id="cond", child_id=leaf_id, ordinal=index)
    _add_context_item(db, ordinal=3, summary_id="cond")

    # The summarizer emits "REWRITTEN <call-index>" for each call. The
    # three leaf calls come first (calls 0/1/2), the condensed call last
    # (call 3) — so the condensed input must contain "REWRITTEN 0/1/2".
    summarizer = _RecordingSummarizer(
        outputs=[
            "REWRITTEN-LEAF-0",
            "REWRITTEN-LEAF-1",
            "REWRITTEN-LEAF-2",
            "REWRITTEN-CONDENSED",
        ]
    )

    result = apply_scoped_doctor_repair(
        db=db,
        config=_config(),
        conversation_id=1,
        summarize=summarizer,
    )

    assert result.kind == "applied"
    assert result.repaired == 4

    # The 4th (last) call is the condensed re-summarization. Its source
    # text must carry the THREE rewritten leaf strings — proving
    # override propagation. The stale DB content must NOT appear.
    condensed_source = summarizer.calls[3][0]
    assert "REWRITTEN-LEAF-0" in condensed_source
    assert "REWRITTEN-LEAF-1" in condensed_source
    assert "REWRITTEN-LEAF-2" in condensed_source
    assert "stale" not in condensed_source

    # And the persisted condensed content is the condensed rewrite.
    row = db.execute("SELECT content FROM summaries WHERE summary_id = 'cond'").fetchone()
    assert row[0] == "REWRITTEN-CONDENSED"


# ---------------------------------------------------------------------------
# AC: output containing a marker is skipped (avoids repair loops).
# ---------------------------------------------------------------------------


def test_marker_in_output_skipped(db: sqlite3.Connection) -> None:
    """A summarizer that returns marker-bearing output is short-circuited.

    Mandated by the issue AC: "Output containing a marker (after
    re-running ``detect_doctor_marker``) is skipped with ``reason:
    'rewritten content still contains a doctor marker'``."

    A misbehaving / provider-down summarizer keeps emitting deterministic
    fallbacks (which carry the marker). Doctor-apply must NOT overwrite
    the row with that output — overwriting would let the doctor "repair"
    a broken row into an identically-broken row on every run. The target
    lands in ``skipped`` and the DB content is left UNCHANGED.
    """
    msg = _insert_message(db, seq=0, content="raw message")
    _insert_summary(db, summary_id="leaf_x", kind="leaf", depth=0, content=_broken("original"))
    _link_summary_message(db, summary_id="leaf_x", message_id=msg, ordinal=0)
    _add_context_item(db, ordinal=0, summary_id="leaf_x")

    # The summarizer keeps returning marker-bearing output.
    summarizer = _RecordingSummarizer(output=_broken("still broken after rewrite"))

    result = apply_scoped_doctor_repair(
        db=db,
        config=_config(),
        conversation_id=1,
        summarize=summarizer,
    )

    assert result.kind == "applied"
    assert result.detected == 1
    assert result.repaired == 0
    assert result.repaired_summary_ids == []
    assert result.skipped == [
        {
            "summary_id": "leaf_x",
            "reason": "rewritten content still contains a doctor marker",
        }
    ]
    # The DB row is untouched.
    row = db.execute("SELECT content FROM summaries WHERE summary_id = 'leaf_x'").fetchone()
    assert row[0] == _broken("original")


# ---------------------------------------------------------------------------
# AC: empty summarizer output is skipped.
# ---------------------------------------------------------------------------


def test_empty_output_skipped(db: sqlite3.Connection) -> None:
    """Empty summarizer output is skipped with the documented reason.

    Mandated by the issue AC: "Empty output from the summarizer causes
    ``skipped`` with ``reason: 'summarizer returned empty output'``."
    (The TS skip reason text is verbatim ``"summarizer returned empty
    output"`` — ``lcm-doctor-apply.ts:113``.)
    """
    msg = _insert_message(db, seq=0, content="raw message")
    _insert_summary(db, summary_id="leaf_e", kind="leaf", depth=0, content=_broken())
    _link_summary_message(db, summary_id="leaf_e", message_id=msg, ordinal=0)
    _add_context_item(db, ordinal=0, summary_id="leaf_e")

    # Whitespace-only output trims to empty.
    summarizer = _RecordingSummarizer(output="   \n  ")

    result = apply_scoped_doctor_repair(
        db=db,
        config=_config(),
        conversation_id=1,
        summarize=summarizer,
    )

    assert result.kind == "applied"
    assert result.repaired == 0
    assert result.skipped == [
        {"summary_id": "leaf_e", "reason": "summarizer returned empty output"}
    ]


# ---------------------------------------------------------------------------
# AC: unavailable when no summarizer can be resolved.
# ---------------------------------------------------------------------------


def test_unavailable_when_no_summarizer(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Summarizer factory raising yields ``{"kind": "unavailable"}``.

    Mandated by the issue AC: "Returns ``{'kind': 'unavailable',
    'reason': '...'}`` when summarizer factory raises; never raises out
    of the function."

    The summarizer is resolved from ``deps`` (no explicit ``summarize``
    callable). We patch :class:`~lossless_hermes.summarize.LcmSummarizer`
    (as imported into the apply module) with a stub whose constructor
    raises — proving the factory-failure path is caught and converted to
    the ``"unavailable"`` arm.
    """
    msg = _insert_message(db, seq=0, content="raw message")
    _insert_summary(db, summary_id="leaf_u", kind="leaf", depth=0, content=_broken())
    _link_summary_message(db, summary_id="leaf_u", message_id=msg, ordinal=0)
    _add_context_item(db, ordinal=0, summary_id="leaf_u")

    class _ExplodingSummarizer:
        def __init__(self, **_kwargs: object) -> None:
            raise RuntimeError("synthetic factory failure")

    monkeypatch.setattr(apply_mod, "LcmSummarizer", _ExplodingSummarizer)

    # A non-None deps sentinel so resolution takes the factory path
    # (rather than the `deps is None -> None` short-circuit).
    result = apply_scoped_doctor_repair(
        db=db,
        config=_config(),
        conversation_id=1,
        deps=object(),  # type: ignore[arg-type]
    )

    assert result.kind == "unavailable"
    assert result.reason is not None
    assert "summarizer" in result.reason.lower()
    # Count fields stay at defaults on the unavailable arm.
    assert result.detected == 0
    assert result.repaired == 0
    assert result.repaired_summary_ids == []
    # The broken row is untouched — no summarizer means no repair.
    row = db.execute("SELECT content FROM summaries WHERE summary_id = 'leaf_u'").fetchone()
    assert row[0] == _broken()


def test_unavailable_when_deps_none_and_no_summarize(db: sqlite3.Connection) -> None:
    """No ``summarize`` AND no ``deps`` resolves to ``"unavailable"``.

    Ports the TS ``if (!params.deps) return undefined`` guard
    (``lcm-doctor-apply.ts:181-183``) — with neither resolution input,
    there is no way to build a summarizer.
    """
    msg = _insert_message(db, seq=0, content="raw message")
    _insert_summary(db, summary_id="leaf_n", kind="leaf", depth=0, content=_broken())
    _link_summary_message(db, summary_id="leaf_n", message_id=msg, ordinal=0)
    _add_context_item(db, ordinal=0, summary_id="leaf_n")

    result = apply_scoped_doctor_repair(
        db=db,
        config=_config(),
        conversation_id=1,
        # deps=None, summarize=None — both defaults.
    )

    assert result.kind == "unavailable"
    assert result.reason is not None


def test_unavailable_when_candidate_chain_empty(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An :class:`LcmSummarizer` with an empty candidate chain → unavailable.

    A summarizer constructed with no resolvable ``(provider, model)``
    candidates would raise on its first ``summarize()`` call. Doctor-apply
    detects the empty chain up front and reports ``"unavailable"`` — the
    same outcome the TS factory produces when it returns ``undefined``.
    """
    msg = _insert_message(db, seq=0, content="raw message")
    _insert_summary(db, summary_id="leaf_c", kind="leaf", depth=0, content=_broken())
    _link_summary_message(db, summary_id="leaf_c", message_id=msg, ordinal=0)
    _add_context_item(db, ordinal=0, summary_id="leaf_c")

    class _NoCandidateSummarizer:
        candidates: list[object] = []

        def __init__(self, **_kwargs: object) -> None:
            pass

        def summarize(self, *_args: object, **_kwargs: object) -> str:  # pragma: no cover
            raise AssertionError("summarize must not be called when chain is empty")

    monkeypatch.setattr(apply_mod, "LcmSummarizer", _NoCandidateSummarizer)

    result = apply_scoped_doctor_repair(
        db=db,
        config=_config(),
        conversation_id=1,
        deps=object(),  # type: ignore[arg-type]
    )

    assert result.kind == "unavailable"
    assert result.reason is not None


# ---------------------------------------------------------------------------
# AC: all writes happen in one BEGIN IMMEDIATE — partial failure rolls
# back ALL writes.
# ---------------------------------------------------------------------------


def test_atomic_write(db: sqlite3.Connection) -> None:
    """A failure mid-write-loop rolls back EVERY queued write.

    Mandated by the issue AC: "partial failure mid-loop rolls back ALL
    writes via ``BEGIN IMMEDIATE``" + "All writes happen in one ``BEGIN
    IMMEDIATE`` at the end (not per-target)."

    Two broken leaves are both rewritten by the summarizer, so both are
    queued in ``repaired_summary_ids``. A ``BEFORE UPDATE`` trigger
    ``RAISE(ABORT, ...)``s the moment the SECOND row is written. Because
    every write runs inside ONE ``BEGIN IMMEDIATE``, the abort rolls back
    the FIRST row's already-applied write too — neither row changes.

    The repair-write transaction is the one place doctor-apply lets an
    exception propagate (a DB-level write failure is genuinely
    exceptional — distinct from a per-target source-text/summarizer
    failure, which is captured as a ``skipped`` entry). The TS port has
    the identical behavior: ``applyScopedDoctorRepair`` does NOT wrap
    ``withDatabaseTransaction``. So the assertion is "raises + rolls
    back fully", not "returns".
    """
    msg_1 = _insert_message(db, seq=0, content="raw message one")
    _insert_summary(db, summary_id="leaf_one", kind="leaf", depth=0, content=_broken("one"))
    _link_summary_message(db, summary_id="leaf_one", message_id=msg_1, ordinal=0)
    _add_context_item(db, ordinal=0, summary_id="leaf_one")

    msg_2 = _insert_message(db, seq=1, content="raw message two")
    _insert_summary(db, summary_id="leaf_two", kind="leaf", depth=0, content=_broken("two"))
    _link_summary_message(db, summary_id="leaf_two", message_id=msg_2, ordinal=0)
    _add_context_item(db, ordinal=1, summary_id="leaf_two")

    # A trigger that aborts the transaction the moment leaf_two's
    # content is updated to its rewrite. leaf_one is written first (it
    # has the lower context ordinal), so when this fires, leaf_one's
    # write is already pending inside the same BEGIN IMMEDIATE.
    db.execute(
        """
        CREATE TRIGGER doctor_apply_atomic_guard
          BEFORE UPDATE OF content ON summaries
          WHEN NEW.summary_id = 'leaf_two'
        BEGIN
          SELECT RAISE(ABORT, 'synthetic mid-write failure');
        END
        """
    )

    summarizer = _RecordingSummarizer(output="clean rewrite")

    # RAISE(ABORT, ...) surfaces as a sqlite3.Error subclass; the
    # transaction helper ROLLBACKs and re-raises it.
    with pytest.raises(sqlite3.Error, match="synthetic mid-write failure"):
        apply_scoped_doctor_repair(
            db=db,
            config=_config(),
            conversation_id=1,
            summarize=summarizer,
        )

    # CRITICAL: leaf_one's write must have been rolled back too — even
    # though its UPDATE ran successfully before the trigger fired. One
    # BEGIN IMMEDIATE means all-or-nothing.
    row_one = db.execute("SELECT content FROM summaries WHERE summary_id = 'leaf_one'").fetchone()
    row_two = db.execute("SELECT content FROM summaries WHERE summary_id = 'leaf_two'").fetchone()
    assert row_one[0] == _broken("one"), "leaf_one must roll back with the failed transaction"
    assert row_two[0] == _broken("two"), "leaf_two must be unchanged"

    # And the connection is left in a clean (no-open-transaction) state.
    assert not db.in_transaction


# ---------------------------------------------------------------------------
# No-targets early return.
# ---------------------------------------------------------------------------


def test_no_targets_returns_empty_applied(db: sqlite3.Connection) -> None:
    """A conversation with no broken summaries returns an empty result.

    Ports the TS early-return at ``lcm-doctor-apply.ts:57-67``. With no
    targets the summarizer is NEVER resolved — so this returns
    ``"applied"`` (all zeros) even with no ``deps`` / ``summarize``.
    """
    # A clean (marker-free) leaf — not a repair target.
    msg = _insert_message(db, seq=0, content="raw message")
    _insert_summary(
        db, summary_id="leaf_clean", kind="leaf", depth=0, content="a perfectly clean summary"
    )
    _link_summary_message(db, summary_id="leaf_clean", message_id=msg, ordinal=0)
    _add_context_item(db, ordinal=0, summary_id="leaf_clean")

    # No deps, no summarize — would be "unavailable" IF there were
    # targets. There are none, so it short-circuits to empty "applied".
    result = apply_scoped_doctor_repair(
        db=db,
        config=_config(),
        conversation_id=1,
    )

    assert result == DoctorApplyResult(
        kind="applied",
        detected=0,
        repaired=0,
        unchanged=0,
        skipped=[],
        repaired_summary_ids=[],
    )


# ---------------------------------------------------------------------------
# Unchanged branch + repair idempotency.
# ---------------------------------------------------------------------------


def test_unchanged_branch_is_unreachable_without_skip(db: sqlite3.Connection) -> None:
    """The ``unchanged`` branch never fires for a single-pass target.

    Ports the TS ``rewritten === target.content.trim()`` branch
    (``lcm-doctor-apply.ts:124-127``). The marker check runs BEFORE the
    equality check, so for ``unchanged`` to fire the summarizer output
    must be marker-free AND byte-equal to ``target.content.strip()``.
    But any genuine doctor target has a marker at a load-bearing
    position, and ``str.strip`` only removes surrounding whitespace —
    it never removes a prefix or in-window-suffix marker. So
    ``target.content.strip()`` always still carries the marker; a
    summarizer echoing it back is caught by the marker check (skip),
    never the equality check.

    This test pins that behavior: a summarizer that echoes the stored
    content verbatim produces a ``skipped`` entry — NOT an ``unchanged``
    count. The ``unchanged`` branch is faithful defense-in-depth ported
    from the TS (which has the identical unreachable property), kept so
    a future marker-model change does not silently start overwriting
    no-op rewrites.
    """
    msg = _insert_message(db, seq=0, content="raw message")
    stored = _broken("body that the summarizer will echo verbatim")
    _insert_summary(db, summary_id="leaf_echo", kind="leaf", depth=0, content=stored)
    _link_summary_message(db, summary_id="leaf_echo", message_id=msg, ordinal=0)
    _add_context_item(db, ordinal=0, summary_id="leaf_echo")

    # The summarizer echoes the stored (broken) content verbatim.
    summarizer = _RecordingSummarizer(output=stored)
    result = apply_scoped_doctor_repair(
        db=db, config=_config(), conversation_id=1, summarize=summarizer
    )

    # Marker check wins over the equality check → skip, not unchanged.
    assert result.kind == "applied"
    assert result.repaired == 0
    assert result.unchanged == 0
    assert result.skipped == [
        {
            "summary_id": "leaf_echo",
            "reason": "rewritten content still contains a doctor marker",
        }
    ]


def test_repair_is_idempotent_across_runs(db: sqlite3.Connection) -> None:
    """Running apply twice repairs once, then finds nothing the second time.

    Run 1 rewrites the broken leaf to clean content; run 2 finds no
    targets (the row is no longer marker-bearing) and returns an empty
    ``"applied"`` result — proving the repair does not loop or re-touch
    an already-clean row.
    """
    msg = _insert_message(db, seq=0, content="raw message")
    _insert_summary(db, summary_id="leaf_idem", kind="leaf", depth=0, content=_broken("body"))
    _link_summary_message(db, summary_id="leaf_idem", message_id=msg, ordinal=0)
    _add_context_item(db, ordinal=0, summary_id="leaf_idem")

    summarizer = _RecordingSummarizer(output="cleaned body")
    first = apply_scoped_doctor_repair(
        db=db, config=_config(), conversation_id=1, summarize=summarizer
    )
    assert first.repaired == 1
    assert first.detected == 1

    # Second run: the row is now clean → no targets → empty applied.
    second = apply_scoped_doctor_repair(
        db=db, config=_config(), conversation_id=1, summarize=summarizer
    )
    assert second == DoctorApplyResult(
        kind="applied",
        detected=0,
        repaired=0,
        unchanged=0,
        skipped=[],
        repaired_summary_ids=[],
    )
    # The summarizer was called exactly once (run 1), never in run 2.
    assert len(summarizer.calls) == 1


# ---------------------------------------------------------------------------
# Three-fallback previous-summary resolution.
# ---------------------------------------------------------------------------


def test_previous_summary_via_context_items(db: sqlite3.Connection) -> None:
    """Fallback 1: previous summary resolved via ``context_items`` ordinal.

    A broken leaf at context-ordinal 1 should resolve its
    previous-summary context to the leaf at context-ordinal 0 (same
    depth). The resolved text is forwarded as the
    :attr:`LcmSummarizeOptions.previous_summary` option.
    """
    # leaf_prev at ordinal 0 — clean, the predecessor.
    msg_prev = _insert_message(db, seq=0, content="predecessor message")
    _insert_summary(
        db, summary_id="leaf_prev", kind="leaf", depth=0, content="PRIOR-SUMMARY-CONTENT"
    )
    _link_summary_message(db, summary_id="leaf_prev", message_id=msg_prev, ordinal=0)
    _add_context_item(db, ordinal=0, summary_id="leaf_prev")

    # leaf_target at ordinal 1 — broken, the repair target.
    msg_target = _insert_message(db, seq=1, content="target message")
    _insert_summary(db, summary_id="leaf_target", kind="leaf", depth=0, content=_broken())
    _link_summary_message(db, summary_id="leaf_target", message_id=msg_target, ordinal=0)
    _add_context_item(db, ordinal=1, summary_id="leaf_target")

    summarizer = _RecordingSummarizer(output="clean")
    result = apply_scoped_doctor_repair(
        db=db, config=_config(), conversation_id=1, summarize=summarizer
    )

    assert result.kind == "applied"
    # The single call's previous_summary option is the predecessor's
    # content.
    assert len(summarizer.calls) == 1
    assert summarizer.calls[0][2].previous_summary == "PRIOR-SUMMARY-CONTENT"


def test_previous_summary_via_timestamp_neighbor(db: sqlite3.Connection) -> None:
    """Fallback 3: previous summary resolved via ``created_at`` neighbor.

    When the target has NO ``context_items`` row (orphan) and is NOT a
    child in ``summary_parents``, resolution falls through to the
    timestamp neighbor: the same-depth summary with the greatest
    ``created_at`` strictly earlier than the target's.
    """
    # An earlier orphan summary — the timestamp predecessor.
    msg_early = _insert_message(db, seq=0, content="early message")
    _insert_summary(
        db,
        summary_id="leaf_early",
        kind="leaf",
        depth=0,
        content="EARLIER-NEIGHBOR-CONTENT",
        created_at="2026-04-22 09:00:00",
    )
    _link_summary_message(db, summary_id="leaf_early", message_id=msg_early, ordinal=0)

    # The broken orphan target — later timestamp, no context_items row.
    msg_late = _insert_message(db, seq=1, content="late message")
    _insert_summary(
        db,
        summary_id="leaf_late",
        kind="leaf",
        depth=0,
        content=_broken(),
        created_at="2026-04-22 11:00:00",
    )
    _link_summary_message(db, summary_id="leaf_late", message_id=msg_late, ordinal=0)

    summarizer = _RecordingSummarizer(output="clean")
    result = apply_scoped_doctor_repair(
        db=db, config=_config(), conversation_id=1, summarize=summarizer
    )

    assert result.kind == "applied"
    assert result.repaired == 1
    # leaf_late is the only target; its previous-summary context is the
    # timestamp neighbor leaf_early (context_items + summary_parents
    # both miss → fallback 3 wins).
    assert summarizer.calls[0][2].previous_summary == "EARLIER-NEIGHBOR-CONTENT"


def test_previous_summary_none_when_no_predecessor(db: sqlite3.Connection) -> None:
    """A target with no predecessor resolves ``previous_summary`` to ``None``.

    The first (and only) summary in a conversation has nothing before
    it on any of the three fallback chains — the option is :data:`None`.
    """
    msg = _insert_message(db, seq=0, content="lone message")
    _insert_summary(db, summary_id="leaf_lone", kind="leaf", depth=0, content=_broken())
    _link_summary_message(db, summary_id="leaf_lone", message_id=msg, ordinal=0)
    _add_context_item(db, ordinal=0, summary_id="leaf_lone")

    summarizer = _RecordingSummarizer(output="clean")
    result = apply_scoped_doctor_repair(
        db=db, config=_config(), conversation_id=1, summarize=summarizer
    )

    assert result.kind == "applied"
    assert summarizer.calls[0][2].previous_summary is None


# ---------------------------------------------------------------------------
# summaries_fts mirror update.
# ---------------------------------------------------------------------------


def test_summaries_fts_mirror_updated(db: sqlite3.Connection) -> None:
    """A successful repair updates the ``summaries_fts`` mirror row.

    Mandated by the issue AC: writes update "the ``summaries_fts``
    mirror". The migration seeds an FTS row for every summary, so the
    repair takes the ``UPDATE`` branch of
    :func:`~lossless_hermes.doctor.apply._update_summary_fts`.
    """
    msg = _insert_message(db, seq=0, content="raw message")
    _insert_summary(db, summary_id="leaf_fts", kind="leaf", depth=0, content=_broken())
    _link_summary_message(db, summary_id="leaf_fts", message_id=msg, ordinal=0)
    _add_context_item(db, ordinal=0, summary_id="leaf_fts")

    # Seed the FTS row to match how summaries are normally inserted (the
    # migration seeds existing rows; new rows are mirrored by callers).
    db.execute(
        "INSERT INTO summaries_fts (summary_id, content) VALUES ('leaf_fts', ?)",
        (_broken(),),
    )

    summarizer = _RecordingSummarizer(output="searchable clean content")
    result = apply_scoped_doctor_repair(
        db=db, config=_config(), conversation_id=1, summarize=summarizer
    )

    assert result.repaired == 1
    fts_row = db.execute(
        "SELECT content FROM summaries_fts WHERE summary_id = 'leaf_fts'"
    ).fetchone()
    assert fts_row[0] == "searchable clean content"


def test_summaries_fts_mirror_inserts_when_missing(db: sqlite3.Connection) -> None:
    """When no FTS row exists, the repair INSERTs one (UPDATE→0→INSERT).

    Ports the TS ``update.changes === 0 → INSERT`` branch
    (``lcm-doctor-apply.ts:535-537``).
    """
    msg = _insert_message(db, seq=0, content="raw message")
    _insert_summary(db, summary_id="leaf_noidx", kind="leaf", depth=0, content=_broken())
    _link_summary_message(db, summary_id="leaf_noidx", message_id=msg, ordinal=0)
    _add_context_item(db, ordinal=0, summary_id="leaf_noidx")
    # Deliberately do NOT seed a summaries_fts row for leaf_noidx.

    summarizer = _RecordingSummarizer(output="freshly indexed content")
    result = apply_scoped_doctor_repair(
        db=db, config=_config(), conversation_id=1, summarize=summarizer
    )

    assert result.repaired == 1
    fts_row = db.execute(
        "SELECT content FROM summaries_fts WHERE summary_id = 'leaf_noidx'"
    ).fetchone()
    assert fts_row is not None
    assert fts_row[0] == "freshly indexed content"


def test_repair_succeeds_without_summaries_fts_table() -> None:
    """The repair commits even when ``summaries_fts`` does not exist.

    The FTS mirror update is best-effort (try/except
    :class:`sqlite3.Error`). A DB created with ``fts5_available=False``
    has no ``summaries_fts`` table — the repair must still update
    ``summaries.content`` and return ``"applied"``.
    """
    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        run_lcm_migrations(conn, fts5_available=False, seed_default_prompts=False)
        conn.execute("INSERT INTO conversations (session_id, session_key) VALUES ('s1', 'sk1')")
        msg = _insert_message(conn, seq=0, content="raw message")
        _insert_summary(conn, summary_id="leaf_nofts", kind="leaf", depth=0, content=_broken())
        _link_summary_message(conn, summary_id="leaf_nofts", message_id=msg, ordinal=0)
        _add_context_item(conn, ordinal=0, summary_id="leaf_nofts")

        summarizer = _RecordingSummarizer(output="clean content no fts")
        result = apply_scoped_doctor_repair(
            db=conn, config=_config(), conversation_id=1, summarize=summarizer
        )

        assert result.kind == "applied"
        assert result.repaired == 1
        row = conn.execute(
            "SELECT content, token_count FROM summaries WHERE summary_id = 'leaf_nofts'"
        ).fetchone()
        assert row[0] == "clean content no fts"
        # token_count was re-estimated from the rewrite.
        assert row[1] > 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Per-target exception capture.
# ---------------------------------------------------------------------------


def test_leaf_with_no_messages_is_skipped(db: sqlite3.Connection) -> None:
    """A leaf with no linked messages is captured as a skip, not a crash.

    :func:`~lossless_hermes.doctor.apply._build_leaf_source_text` raises
    ``ValueError("no messages linked to summary")`` for a leaf with no
    ``summary_messages`` rows; the per-target ``except`` converts it to
    a ``skipped`` entry (mirrors the TS ``catch (error)`` at lines
    135-140).
    """
    # A broken leaf with NO summary_messages link.
    _insert_summary(db, summary_id="leaf_empty", kind="leaf", depth=0, content=_broken())
    _add_context_item(db, ordinal=0, summary_id="leaf_empty")

    summarizer = _RecordingSummarizer(output="clean")
    result = apply_scoped_doctor_repair(
        db=db, config=_config(), conversation_id=1, summarize=summarizer
    )

    assert result.kind == "applied"
    assert result.detected == 1
    assert result.repaired == 0
    assert len(result.skipped) == 1
    assert result.skipped[0]["summary_id"] == "leaf_empty"
    assert "no messages linked" in result.skipped[0]["reason"]
    # The summarizer was never called (source-text build failed first).
    assert summarizer.calls == []


def test_mixed_repair_skip_and_unchanged(db: sqlite3.Connection) -> None:
    """A pass with a mix of repaired, skipped, and unchanged targets.

    Integration check: three leaves — one repairs cleanly, one returns a
    marker (skipped), one has no source messages (skipped). The result
    counts must reflect each outcome and the repaired row's content must
    be the only one that changed.
    """
    # leaf_ok — repairs cleanly.
    msg_ok = _insert_message(db, seq=0, content="ok message")
    _insert_summary(db, summary_id="leaf_ok", kind="leaf", depth=0, content=_broken("ok"))
    _link_summary_message(db, summary_id="leaf_ok", message_id=msg_ok, ordinal=0)
    _add_context_item(db, ordinal=0, summary_id="leaf_ok")

    # leaf_bad — summarizer returns a marker (skipped).
    msg_bad = _insert_message(db, seq=1, content="bad message")
    _insert_summary(db, summary_id="leaf_bad", kind="leaf", depth=0, content=_broken("bad"))
    _link_summary_message(db, summary_id="leaf_bad", message_id=msg_bad, ordinal=0)
    _add_context_item(db, ordinal=1, summary_id="leaf_bad")

    # leaf_nomsg — no source messages (skipped via ValueError).
    _insert_summary(db, summary_id="leaf_nomsg", kind="leaf", depth=0, content=_broken("nomsg"))
    _add_context_item(db, ordinal=2, summary_id="leaf_nomsg")

    # Per-call outputs: call 0 (leaf_ok) clean, call 1 (leaf_bad) marker.
    # leaf_nomsg never reaches the summarizer.
    summarizer = _RecordingSummarizer(outputs=["clean rewrite for ok", _broken("still bad")])

    result = apply_scoped_doctor_repair(
        db=db, config=_config(), conversation_id=1, summarize=summarizer
    )

    assert result.kind == "applied"
    assert result.detected == 3
    assert result.repaired == 1
    assert result.repaired_summary_ids == ["leaf_ok"]
    assert result.unchanged == 0
    # Two skips: leaf_bad (marker) + leaf_nomsg (no messages).
    skip_ids = {entry["summary_id"] for entry in result.skipped}
    assert skip_ids == {"leaf_bad", "leaf_nomsg"}

    # Only leaf_ok's content changed.
    assert (
        db.execute("SELECT content FROM summaries WHERE summary_id = 'leaf_ok'").fetchone()[0]
        == "clean rewrite for ok"
    )
    assert db.execute("SELECT content FROM summaries WHERE summary_id = 'leaf_bad'").fetchone()[
        0
    ] == _broken("bad")


# ---------------------------------------------------------------------------
# Leaf source-text construction — timestamp header.
# ---------------------------------------------------------------------------


def test_leaf_source_text_has_timestamp_headers(db: sqlite3.Connection) -> None:
    """The leaf source text concatenates ``[timestamp]\\ncontent`` per message.

    Ports the TS ``buildLeafSourceText`` join (``lcm-doctor-apply.ts:274-295``).
    Two messages → the source text has two timestamp-headed blocks in
    ``summary_messages.ordinal`` order, joined by a blank line.
    """
    msg_1 = _insert_message(
        db, seq=0, content="first message body", created_at="2026-04-22 08:15:00"
    )
    msg_2 = _insert_message(
        db, seq=1, content="second message body", created_at="2026-04-22 09:45:00"
    )
    _insert_summary(db, summary_id="leaf_ts", kind="leaf", depth=0, content=_broken())
    _link_summary_message(db, summary_id="leaf_ts", message_id=msg_1, ordinal=0)
    _link_summary_message(db, summary_id="leaf_ts", message_id=msg_2, ordinal=1)
    _add_context_item(db, ordinal=0, summary_id="leaf_ts")

    summarizer = _RecordingSummarizer(output="clean")
    apply_scoped_doctor_repair(
        db=db, config=_config(timezone="UTC"), conversation_id=1, summarize=summarizer
    )

    source = summarizer.calls[0][0]
    # Both message bodies present, in order.
    assert source.index("first message body") < source.index("second message body")
    # Each has a UTC timestamp header (formatted by compaction's
    # _format_timestamp).
    assert "[2026-04-22 08:15 UTC]\nfirst message body" in source
    assert "[2026-04-22 09:45 UTC]\nsecond message body" in source


def test_condensed_source_text_has_time_range_header(db: sqlite3.Connection) -> None:
    """Condensed source text prepends a ``[earliest - latest]`` header per child.

    Ports the TS ``buildCondensedSourceText`` header logic
    (``lcm-doctor-apply.ts:297-349``). A child with ``earliest_at`` /
    ``latest_at`` set gets a time-range header line before its content.
    """
    # A clean child leaf with an explicit time range.
    msg_child = _insert_message(db, seq=0, content="child message")
    _insert_summary(
        db,
        summary_id="leaf_child",
        kind="leaf",
        depth=0,
        content="CHILD-SUMMARY-BODY",
        earliest_at="2026-04-22 08:00:00",
        latest_at="2026-04-22 10:00:00",
    )
    _link_summary_message(db, summary_id="leaf_child", message_id=msg_child, ordinal=0)
    _add_context_item(db, ordinal=0, summary_id="leaf_child")

    # The broken condensed summary that rolls up leaf_child.
    _insert_summary(db, summary_id="cond_ts", kind="condensed", depth=1, content=_broken())
    _link_summary_parent(db, condensed_id="cond_ts", child_id="leaf_child", ordinal=0)
    _add_context_item(db, ordinal=1, summary_id="cond_ts")

    summarizer = _RecordingSummarizer(output="clean")
    apply_scoped_doctor_repair(
        db=db, config=_config(timezone="UTC"), conversation_id=1, summarize=summarizer
    )

    # The condensed call is the last one (leaf_child is clean → not a
    # target, so the only call is the condensed one).
    condensed_source = summarizer.calls[-1][0]
    assert "[2026-04-22 08:00 UTC - 2026-04-22 10:00 UTC]\nCHILD-SUMMARY-BODY" in condensed_source


# ---------------------------------------------------------------------------
# DoctorSummarizeFn type is exported.
# ---------------------------------------------------------------------------


def test_doctor_summarize_fn_type_is_exported() -> None:
    """:data:`DoctorSummarizeFn` is part of the module's public surface."""
    assert "DoctorSummarizeFn" in apply_mod.__all__
    assert DoctorSummarizeFn is not None
