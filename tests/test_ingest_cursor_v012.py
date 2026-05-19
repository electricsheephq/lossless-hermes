"""Regression tests for the v0.1.3 ingest-cursor fix (issue #130).

The ingest cursor ``_last_seen_message_idx`` had two coupled defects
surfaced by an architecture review against the sibling project
``hermes-lcm``:

* **Defect 1 — restart re-ingestion duplicates the transcript.**
  ``_last_seen_message_idx`` is a process-local dict, never persisted.
  ``on_session_start`` had no cursor-restore logic, so on a gateway
  restart the cursor reset to 0 and the next ``post_llm_call`` re-diffed
  the whole replayed history. ``messages`` has no UNIQUE on
  ``identity_hash`` (only ``UNIQUE(conversation_id, seq)``), and the
  ingest path assigns a fresh ``seq`` per row with no dedup lookup, so
  every restart silently re-ingested the entire transcript. Before
  v0.1.2 the symptom was masked by a separate durability bug (the store
  was rolled back on session close); with v0.1.2's durability fix in
  place the restart path is fully live and this fix is load-bearing.

* **Defect 2 — compaction desyncs the cursor → silent ingest stop.**
  When ``compress()`` / ``preassemble()`` perform a genuine compaction /
  DAG substitution, the absolute cursor is left stranded at the
  pre-substitution length and ``_do_ingest_history_diff`` desyncs.

The fix:

* Defect 1 — :meth:`_IngestMixin._reconcile_ingest_cursor` reconciles
  the cursor from the durable ``messages`` store the first time a
  process sees a session, replay-evidence-gated so a genuinely-new
  first turn after restart still ingests.
* Defect 2 — :meth:`_CompactMixin._reset_ingest_cursor_after_compaction`
  resets the cursor to ``len(result)`` after a genuine compaction / DAG
  substitution. The reset is gated on a real "compaction occurred"
  signal — for ``preassemble`` / the experimental ``compress`` path,
  the ``did_substitute`` flag from
  :meth:`_AssembleMixin._assemble_with_signal` (``True`` only when the
  real :class:`ContextAssembler` produced the list, ``False`` on every
  ``_safe_fallback`` path) — **not** a list-length test. A length test
  both over-fires (``_safe_fallback``'s trailing-``assistant`` strip is
  a non-compaction shortening) and under-fires (a genuine same-length
  substitution never trips it). ``compress`` and ``preassemble`` both
  call the reset, skipping it when ``_infer_session_id`` returns empty.

The Defect-2 tests below drive the **real** ``_assemble`` /
``_safe_fallback`` path (mock stores, but the genuine assembler) rather
than monkeypatching ``_assemble`` with hand-built lists — so the
"compaction occurred" signal is exercised end-to-end, including the
over-fire (``_safe_fallback`` strip → no reset) and under-fire
(same-length substitution → reset) cases that the length-based v0.1.2
spec got wrong.

References:

* GitHub issue #130 — the bug report.
* ``hermes-lcm`` commits 79629c2 (#111), 17578a0 (#113), 4bc6b9c (#1),
  and the reset sites at ``engine.py:855-861`` / ``:3483-3486`` /
  ``:908`` (content test / unconditional post-compaction reset — never
  a list-length test) — the production-tested reference this fix is
  reimplemented from.
* ``docs/adr/009-per-message-ingest.md`` — the diff-ingest seam.
* ``docs/adr/032-per-turn-assembly-not-required.md`` — supersedes
  ADR-010; ``preassemble`` is slated for v0.2.0 demotion but stays a
  live v0.1.x path the reset must remain correct for.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from lossless_hermes.db.config import LcmConfig
from lossless_hermes.engine import LCMEngine
from lossless_hermes.store.conversation import ConversationRecord, MessageRecord
from lossless_hermes.store.summary import ContextItemRecord, SummaryRecord

# ---------------------------------------------------------------------------
# Skip marker — actions/setup-python macOS builds lack enable_load_extension
# ---------------------------------------------------------------------------
#
# Mirrors ``_skip_no_extension_loading`` in ``tests/test_engine_ingest.py``.
# The restart-simulation tests need a full ``open_lcm_db`` connection (the
# real ConversationStore + ``seq`` assignment is the whole point), so they
# skip on Apple's system Python where sqlite-vec cannot load.
_skip_no_extension_loading = pytest.mark.skipif(
    not hasattr(sqlite3.Connection, "enable_load_extension"),
    reason=(
        "actions/setup-python on macOS ships a CPython build without "
        "--enable-loadable-sqlite-extensions; sqlite-vec cannot load. "
        "Restart-simulation tests require the full lifecycle DB."
    ),
)

_SESSION = "sess-restart"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _new_engine(home: Path) -> LCMEngine:
    """A fresh :class:`LCMEngine` bound to ``home`` with the DB open.

    Each call simulates a distinct gateway PROCESS: the new instance's
    ``_last_seen_message_idx`` starts empty (the in-memory cursor is
    never persisted), but ``on_session_start`` reopens the SAME
    ``lcm.db`` file under ``home`` — so the durable transcript survives
    while the cursor does not. That is exactly the restart scenario.
    """
    eng = LCMEngine(hermes_home=home / ".hermes", config=LcmConfig())
    eng.on_session_start(_SESSION)
    return eng


def _all_messages(engine: LCMEngine, session_id: str) -> List[Any]:
    """Return every persisted message row for ``session_id``."""
    conv = engine._conversation_store.get_conversation_by_session_id(session_id)
    if conv is None:
        return []
    return engine._conversation_store.get_messages(conv.conversation_id)


def _ingest(engine: LCMEngine, session_id: str, history: List[Dict[str, Any]]) -> None:
    """Drive one ``post_llm_call`` ingest turn."""
    engine._on_post_llm_call(session_id=session_id, conversation_history=history)


def _end_process(engine: LCMEngine, session_id: str) -> None:
    """Close a simulated gateway process, flushing ingested rows to disk.

    The explicit ``engine._db.commit()`` here is load-bearing for the
    restart simulation. The diff-ingest path calls
    ``ConversationStore.create_conversation`` OUTSIDE ``with_transaction``
    (see ``engine/ingest.py`` — "NOT wrapped in with_transaction"). Under
    stdlib sqlite3's legacy ``isolation_level=""`` that bare ``INSERT``
    auto-opens an implicit deferred transaction, after which
    ``with_transaction`` observes ``in_transaction is True`` and degrades
    to a SAVEPOINT that never issues a top-level ``COMMIT`` — so a plain
    ``on_session_end`` close ROLLS BACK the whole session's ingest. That
    is a pre-existing durability defect in the DB layer, OUT OF SCOPE for
    issue #130 (the cursor fix), and it is reported separately.

    For these cursor-reconciliation regression tests we commit
    explicitly so the simulated "process 1" leaves a genuinely durable
    transcript for "process 2" to rebind against — which is exactly the
    state a real gateway restart sees once the durability defect is
    fixed, and also the state a hard crash leaves when committed WAL
    frames survive. Reconciliation is what must then prevent the
    re-ingest; that is the behavior under test here.
    """
    if engine._db is not None:
        engine._db.commit()
    engine.on_session_end(session_id, [])


# ---------------------------------------------------------------------------
# Mock-store builders — drive the REAL _assemble / _safe_fallback path
# ---------------------------------------------------------------------------
#
# The Defect-2 tests below need the genuine assembler so the
# ``did_substitute`` "compaction occurred" signal is exercised end-to-end.
# They use mock stores (mirroring ``tests/test_engine_assemble.py``'s
# ``_wire_mock_stores``) so the file still runs on the actions/setup-python
# macOS cell where sqlite-vec cannot load — but the assembler itself, the
# ``_assemble`` wrapper, ``_assemble_with_signal``, and ``_safe_fallback``
# are all the real production code. ``_assemble`` is never monkeypatched.

_NOW = datetime.now(timezone.utc)


def _mk_msg(message_id: int, *, role: str = "user", content: str = "") -> MessageRecord:
    """A minimal :class:`MessageRecord` for the mock conversation store."""
    return MessageRecord(
        message_id=message_id,
        conversation_id=1,
        seq=message_id,
        role=role,  # type: ignore[arg-type]
        content=content,
        token_count=0,
        created_at=_NOW,
    )


def _mk_ctx(
    ordinal: int,
    *,
    item_type: str = "message",
    message_id: int | None = None,
    summary_id: str | None = None,
) -> ContextItemRecord:
    """A :class:`ContextItemRecord` (raw ``message`` or ``summary``)."""
    return ContextItemRecord(
        conversation_id=1,
        ordinal=ordinal,
        item_type=item_type,  # type: ignore[arg-type]
        message_id=message_id,
        summary_id=summary_id,
        created_at=_NOW,
    )


def _mk_conv(*, session_id: str) -> ConversationRecord:
    """A :class:`ConversationRecord` for the mock conversation store."""
    return ConversationRecord(
        conversation_id=1,
        session_id=session_id,
        session_key=None,
        active=True,
        archived_at=None,
        title=None,
        bootstrapped_at=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _mk_summary(*, summary_id: str = "sum_a", content: str = "summary text") -> SummaryRecord:
    """A leaf :class:`SummaryRecord` for the mock summary store."""
    return SummaryRecord(
        summary_id=summary_id,
        conversation_id=1,
        kind="leaf",  # type: ignore[arg-type]
        depth=0,
        content=content,
        token_count=5,
        file_ids=[],
        earliest_at=_NOW,
        latest_at=_NOW,
        descendant_count=3,
        descendant_token_count=30,
        source_message_token_count=30,
        model="test-model",
        created_at=_NOW,
    )


def _wire_assemble_stores(
    engine: LCMEngine,
    *,
    session_id: str,
    context_items: List[ContextItemRecord],
    messages_by_id: Dict[int, MessageRecord],
    summaries_by_id: Dict[str, SummaryRecord] | None = None,
    crash_resolve: bool = False,
) -> None:
    """Attach mock conversation + summary stores so the REAL assembler runs.

    Mirrors ``_wire_mock_stores`` in ``tests/test_engine_assemble.py``.
    ``_assemble`` reads the DAG (``context_items`` + resolved messages /
    summaries) through these mocks and either produces a genuine
    substitution (``did_substitute=True``) or falls back to
    :meth:`_safe_fallback` (``did_substitute=False``).

    Args:
        crash_resolve: When ``True``, ``get_message_parts`` raises — this
            drives the assembler-exception ``_safe_fallback`` branch (a
            non-compaction reshape: the signal must stay ``False``).
    """
    cstore = MagicMock()
    cstore.get_conversation_by_session_id.side_effect = lambda sid: (
        _mk_conv(session_id=session_id) if sid == session_id else None
    )
    cstore.get_message_by_id.side_effect = lambda mid: messages_by_id.get(mid)
    if crash_resolve:
        cstore.get_message_parts.side_effect = RuntimeError("synthetic resolve crash")
    else:
        cstore.get_message_parts.side_effect = lambda _mid: []

    sstore = MagicMock()
    sstore.get_summary.side_effect = lambda sid: (summaries_by_id or {}).get(sid)
    sstore.get_summary_parents.side_effect = lambda _sid: []
    sstore.get_context_items.side_effect = lambda _cid: list(context_items)

    engine._conversation_store = cstore
    engine._summary_store = sstore


# ===========================================================================
# Defect 1 — restart re-ingestion
# ===========================================================================


class TestRestartReingestion:
    """Defect 1: cursor lost on restart must be reconciled, not duplicated."""

    @_skip_no_extension_loading
    def test_restart_full_replay_does_not_duplicate(self, tmp_home: Path) -> None:
        """Restart + full history replay re-ingests NOTHING (the bug).

        Pre-fix: the second engine's cursor defaults to 0, the full
        replayed history is re-diffed, and every row lands again with a
        fresh ``seq`` — the transcript doubles. Post-fix: the cursor is
        reconciled to the stored count, so the replay is a no-op.
        """
        history = [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "second question"},
            {"role": "assistant", "content": "second answer"},
        ]

        # ── Process 1: ingest the transcript ──────────────────────
        eng1 = _new_engine(tmp_home)
        try:
            _ingest(eng1, _SESSION, history)
            assert eng1._last_seen_message_idx[_SESSION] == 4
            assert len(_all_messages(eng1, _SESSION)) == 4
        finally:
            _end_process(eng1, _SESSION)

        # ── Process 2 (restart): replay the SAME full history ─────
        # The new engine has an empty cursor dict — exactly the lost-
        # in-memory-state condition Defect 1 describes.
        eng2 = _new_engine(tmp_home)
        try:
            assert _SESSION not in eng2._last_seen_message_idx
            _ingest(eng2, _SESSION, history)

            # The transcript MUST NOT have grown — still 4 rows, not 8.
            persisted = _all_messages(eng2, _SESSION)
            assert len(persisted) == 4, (
                f"restart re-ingested the transcript: expected 4 rows, "
                f"got {len(persisted)} — Defect 1 regression"
            )
            # Content is the original, in order — no duplicates.
            assert [m.content for m in persisted] == [
                "first question",
                "first answer",
                "second question",
                "second answer",
            ]
            # ``seq`` values are still the original contiguous 1..4.
            assert sorted(m.seq for m in persisted) == [1, 2, 3, 4]
            # Cursor was reconciled to the stored length.
            assert eng2._last_seen_message_idx[_SESSION] == 4
        finally:
            _end_process(eng2, _SESSION)

    @_skip_no_extension_loading
    def test_restart_then_new_turn_ingests_only_the_new_turn(self, tmp_home: Path) -> None:
        """After a restart, a genuinely-new turn ingests — exactly once.

        The replayed prefix is reconciled away; only the appended
        messages cross the cursor. This proves reconciliation does not
        over-correct into data loss.
        """
        original = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
        ]
        eng1 = _new_engine(tmp_home)
        try:
            _ingest(eng1, _SESSION, original)
            assert len(_all_messages(eng1, _SESSION)) == 2
        finally:
            _end_process(eng1, _SESSION)

        # Restart, then the user asks a new question: the live history
        # is the full replay PLUS the new turn.
        replayed_plus_new = original + [
            {"role": "user", "content": "q2-brand-new"},
            {"role": "assistant", "content": "a2-brand-new"},
        ]
        eng2 = _new_engine(tmp_home)
        try:
            _ingest(eng2, _SESSION, replayed_plus_new)
            persisted = _all_messages(eng2, _SESSION)
            # 2 original + 2 new = 4. The 2 originals are NOT duplicated.
            assert len(persisted) == 4
            assert [m.content for m in persisted] == [
                "q1",
                "a1",
                "q2-brand-new",
                "a2-brand-new",
            ]
            assert eng2._last_seen_message_idx[_SESSION] == 4
        finally:
            _end_process(eng2, _SESSION)

    @_skip_no_extension_loading
    def test_restart_with_no_replay_evidence_still_ingests(self, tmp_home: Path) -> None:
        """A post-restart turn that shares NO prefix with the store ingests.

        The data-loss guard (hermes-lcm #113): if the first post-restart
        turn delivers only delta messages that happen NOT to match the
        durable tail, the cursor stays at 0 so those messages are
        persisted rather than silently dropped.
        """
        eng1 = _new_engine(tmp_home)
        try:
            _ingest(
                eng1,
                _SESSION,
                [
                    {"role": "user", "content": "stored-q"},
                    {"role": "assistant", "content": "stored-a"},
                ],
            )
            assert len(_all_messages(eng1, _SESSION)) == 2
        finally:
            _end_process(eng1, _SESSION)

        # Restart. The live history shares no leading prefix with the
        # stored transcript (a fresh delta-only snapshot).
        eng2 = _new_engine(tmp_home)
        try:
            _ingest(
                eng2,
                _SESSION,
                [
                    {"role": "user", "content": "totally-different-turn"},
                ],
            )
            persisted = _all_messages(eng2, _SESSION)
            # The new message MUST be persisted — 2 stored + 1 new = 3.
            assert len(persisted) == 3
            assert persisted[-1].content == "totally-different-turn"
        finally:
            _end_process(eng2, _SESSION)

    @_skip_no_extension_loading
    def test_fresh_session_first_ingest_unaffected(self, tmp_home: Path) -> None:
        """A brand-new session (no prior store rows) ingests normally.

        Reconciliation must be inert when there is nothing to reconcile
        against — and must NOT create a spurious empty conversation row.
        """
        eng = _new_engine(tmp_home)
        try:
            # Session never seen, store empty — reconciliation is a
            # no-op and the normal diff path ingests both messages.
            _ingest(
                eng,
                "brand-new-session",
                [
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "hi"},
                ],
            )
            assert eng._last_seen_message_idx["brand-new-session"] == 2
            assert len(_all_messages(eng, "brand-new-session")) == 2
        finally:
            _end_process(eng, "brand-new-session")

    @_skip_no_extension_loading
    def test_reconciliation_runs_once_per_session_per_process(self, tmp_home: Path) -> None:
        """Once a session is reconciled, later turns skip reconciliation.

        The ``_ingest_cursor_reconciled`` set guards this: a second turn
        in the same process trusts the in-memory cursor and does not
        re-probe the store.
        """
        eng1 = _new_engine(tmp_home)
        try:
            _ingest(
                eng1,
                _SESSION,
                [{"role": "user", "content": "seed"}],
            )
        finally:
            _end_process(eng1, _SESSION)

        eng2 = _new_engine(tmp_home)
        try:
            history = [{"role": "user", "content": "seed"}]
            _ingest(eng2, _SESSION, history)
            assert _SESSION in eng2._ingest_cursor_reconciled
            # A second turn appends one message; reconciliation does NOT
            # re-run (the session is already in the set), and the
            # in-memory cursor drives a clean single-message ingest.
            history2 = history + [{"role": "assistant", "content": "reply"}]
            _ingest(eng2, _SESSION, history2)
            assert len(_all_messages(eng2, _SESSION)) == 2
            assert eng2._last_seen_message_idx[_SESSION] == 2
        finally:
            _end_process(eng2, _SESSION)


# ===========================================================================
# Defect 2 — compaction desync
# ===========================================================================


class TestCompactionCursorReset:
    """Defect 2: a genuine compaction substitution must reset the cursor.

    Every test here drives the **real** ``_assemble`` /
    ``_assemble_with_signal`` / ``_safe_fallback`` code through mock
    stores — ``_assemble`` is never monkeypatched. The reset gate under
    test is the ``did_substitute`` "compaction occurred" signal, not a
    list-length test.
    """

    # -- shorter substitution: the canonical Defect-2 desync -----------

    def test_preassemble_real_substitution_resets_cursor(self) -> None:
        """A genuine ``preassemble`` DAG substitution resets the cursor.

        The DAG holds a summary + a raw item; the real assembler folds
        a long live list into a short substituted one. ``did_substitute``
        is ``True``, so the absolute cursor — stranded at the
        pre-substitution length — is reset to the substituted length.
        Pre-fix the stranded cursor makes every later ``post_llm_call``
        early-return; this is the canonical Defect-2 silent ingest stop.
        """
        engine = LCMEngine()
        engine._last_seen_message_idx["sess-X"] = 20

        # DAG: 1 summary + 1 raw item → ``has_summary_items`` is True so
        # the raw-only-trails-live guard does not fire and the real
        # assembler runs, folding the 20-message live list to a short
        # substituted list.
        _wire_assemble_stores(
            engine,
            session_id="sess-X",
            context_items=[
                _mk_ctx(0, item_type="summary", summary_id="sum_a"),
                _mk_ctx(1, message_id=1),
            ],
            messages_by_id={1: _mk_msg(1, content="stored-1")},
            summaries_by_id={"sum_a": _mk_summary()},
        )
        live = [{"role": "user", "content": f"m{i}", "session_id": "sess-X"} for i in range(20)]

        out = engine.preassemble(live, budget_tokens=1_000_000)

        # The real assembler produced a genuine, shorter substitution.
        assert out is not live
        assert len(out) < len(live)
        # Cursor reset to the substituted length — NOT left at 20.
        assert engine._last_seen_message_idx["sess-X"] == len(out)

    def test_compress_experimental_real_substitution_resets_cursor(self) -> None:
        """The experimental ``compress`` path resets on a real substitution.

        ADR-010 Option A: when ``experimental_always_on_via_compress``
        is set and Hermes lacks ``preassemble``, ``compress`` runs the
        same ``_assemble_with_signal`` substitution body. A genuine
        substitution must reset the cursor exactly as ``preassemble``
        does. (ADR-032 supersedes ADR-010 and demotes ``preassemble``;
        the experimental ``compress`` path stays a live v0.1.x path.)
        """
        cfg = LcmConfig(experimental_always_on_via_compress=True)
        engine = LCMEngine(config=cfg)
        assert engine._has_preassemble is False
        engine._last_seen_message_idx["sess-Y"] = 12

        _wire_assemble_stores(
            engine,
            session_id="sess-Y",
            context_items=[
                _mk_ctx(0, item_type="summary", summary_id="sum_a"),
                _mk_ctx(1, message_id=1),
            ],
            messages_by_id={1: _mk_msg(1, content="stored-1")},
            summaries_by_id={"sum_a": _mk_summary()},
        )
        live = [{"role": "user", "content": f"m{i}", "session_id": "sess-Y"} for i in range(12)]

        out = engine.compress(live, current_tokens=99_000)

        assert out is not live
        assert len(out) < len(live)
        assert engine._last_seen_message_idx["sess-Y"] == len(out)

    # -- UNDER-FIRE: same-length substitution must STILL reset ---------

    def test_same_length_substitution_resets_cursor(self) -> None:
        """A genuine SAME-LENGTH compaction substitution resets the cursor.

        The under-fire case the v0.1.2 length guard got wrong: a real
        compaction can substitute N raw turns with N summary + fresh-tail
        messages — ``len(result) == len(original)``. A ``len(result) <
        len(original)`` guard never trips, so Defect 2 stays unfixed for
        that case. The corrected gate is the ``did_substitute`` signal:
        the assembler genuinely ran, the content differs, the absolute
        cursor is just as desynced as in the shortening case, so the
        reset MUST fire.

        The DAG here is 3 raw ``message`` context-items; with a generous
        budget the assembler full-fits all 3 → a 3-message substituted
        list for a 3-message live list. Same length, different content.
        """
        engine = LCMEngine()
        engine._last_seen_message_idx["sess-EQ"] = 99  # stale absolute cursor

        _wire_assemble_stores(
            engine,
            session_id="sess-EQ",
            context_items=[_mk_ctx(i - 1, message_id=i) for i in (1, 2, 3)],
            messages_by_id={i: _mk_msg(i, content=f"stored-{i}") for i in (1, 2, 3)},
        )
        # 3-message live list — SAME length as the assembler's output.
        live = [{"role": "user", "content": f"live-{i}", "session_id": "sess-EQ"} for i in range(3)]

        out = engine.preassemble(live, budget_tokens=1_000_000)

        # The assembler ran and produced a same-length but
        # content-different substitution.
        assert len(out) == len(live)
        assert out != live
        assert [m.get("content") for m in out] == ["stored-1", "stored-2", "stored-3"]
        # Cursor reset to the substituted length despite NO shortening —
        # a length guard would have left it stranded at 99.
        assert engine._last_seen_message_idx["sess-EQ"] == len(out)

    # -- OVER-FIRE: _safe_fallback's trailing strip must NOT reset -----

    def test_safe_fallback_trailing_strip_does_not_reset_cursor(self) -> None:
        """``_safe_fallback``'s trailing-assistant strip must NOT reset the cursor.

        The over-fire case the v0.1.2 length guard got wrong. When
        ``_assemble`` falls back to :meth:`_safe_fallback`, the fallback
        strips trailing ``assistant`` messages — a NON-compaction
        shortening of the live list. The v0.1.2 ``len(result) <
        len(original)`` guard fires on this shortening and resets the
        cursor *backward*; the next ``post_llm_call`` then re-ingests the
        stripped trailing message with a fresh ``seq`` and no
        ``identity_hash`` dedup — i.e. it re-introduces Defect 1's
        duplication.

        Here the DAG has only 2 raw items but the live list has 5
        messages → the raw-only-trails-live guard fires → ``_assemble``
        returns ``_safe_fallback(live)``, which strips the 2 trailing
        ``assistant`` messages (5 → 3, a genuine shortening).
        ``did_substitute`` is ``False`` (no real substitution), so the
        corrected gate leaves the cursor exactly where ingest left it.
        """
        engine = LCMEngine()
        engine._last_seen_message_idx["sess-SF"] = 5  # ingest's real cursor

        _wire_assemble_stores(
            engine,
            session_id="sess-SF",
            # Only 2 raw items, no summary → raw-only-trails-live guard
            # fires for a 5-message live list → _safe_fallback path.
            context_items=[_mk_ctx(i - 1, message_id=i) for i in (1, 2)],
            messages_by_id={i: _mk_msg(i, content=f"stored-{i}") for i in (1, 2)},
        )
        # Live list ENDS with two assistant messages — _safe_fallback
        # will strip them, shortening 5 → 3.
        live = [
            {"role": "user", "content": "u1", "session_id": "sess-SF"},
            {"role": "assistant", "content": "a1", "session_id": "sess-SF"},
            {"role": "user", "content": "u2", "session_id": "sess-SF"},
            {"role": "assistant", "content": "a2", "session_id": "sess-SF"},
            {"role": "assistant", "content": "a3-prefill", "session_id": "sess-SF"},
        ]

        out = engine.preassemble(live, budget_tokens=1_000_000)

        # _safe_fallback ran: the output is a SHORTER list (the two
        # trailing assistant messages were stripped).
        assert len(out) == 3, "expected _safe_fallback to strip 2 trailing assistants"
        assert out[-1]["role"] == "user"
        assert len(out) < len(live)
        # The cursor MUST be untouched at 5 — this shortening is NOT a
        # compaction. A length-based guard would have wrongly reset it
        # to 3 here, re-introducing Defect 1's duplication on the next
        # post_llm_call.
        assert engine._last_seen_message_idx["sess-SF"] == 5, (
            "over-fire regression: _safe_fallback's trailing-assistant "
            "strip reset the cursor — Defect 2 guard fired on a "
            "non-compaction reshape"
        )

    def test_safe_fallback_on_assembler_exception_does_not_reset_cursor(self) -> None:
        """An assembler exception → ``_safe_fallback`` → cursor NOT reset.

        A second non-compaction ``_safe_fallback`` path: when the
        assembler raises, ``_assemble`` catches it and returns
        ``_safe_fallback(live)``. ``did_substitute`` is ``False``; the
        cursor must not move. Belt-and-suspenders coverage of the
        over-fire guard on the exception branch (distinct from the
        raw-only-trails-live branch above).
        """
        engine = LCMEngine()
        engine._last_seen_message_idx["sess-EX"] = 6

        _wire_assemble_stores(
            engine,
            session_id="sess-EX",
            context_items=[_mk_ctx(0, message_id=1)],
            messages_by_id={1: _mk_msg(1, content="stored-1")},
            crash_resolve=True,  # get_message_parts raises → assembler boom
        )
        # No trailing assistant — _safe_fallback returns the list as-is
        # (same length), so this also confirms the gate does not depend
        # on length in EITHER direction.
        live = [{"role": "user", "content": f"m{i}", "session_id": "sess-EX"} for i in range(6)]

        out = engine.preassemble(live, budget_tokens=1_000_000)

        # _safe_fallback returned the live messages (no trailing
        # assistant to strip) — equal length, no substitution.
        assert len(out) == len(live)
        # Cursor untouched — the exception fallback is not a compaction.
        assert engine._last_seen_message_idx["sess-EX"] == 6

    # -- session_id caveat: skip rather than mis-key -------------------

    def test_compaction_reset_skipped_when_no_session_id(self) -> None:
        """No inferable session_id → reset is SKIPPED, not mis-keyed.

        The issue #130 caveat: ``compress`` / ``preassemble`` do not
        receive ``session_id``. When ``_infer_session_id`` returns ``""``
        the reset must be skipped — writing ``_last_seen_message_idx[""]``
        would plant a bogus key and never fix the real session.
        ``compress`` returns early on the empty session_id before any
        assemble runs, so no stores are needed here.
        """
        cfg = LcmConfig(experimental_always_on_via_compress=True)
        engine = LCMEngine(config=cfg)
        engine._last_seen_message_idx["real-session"] = 7

        # Live messages carry NO session_id metadata, and nothing has
        # populated the cached recent-session id → inference fails.
        live = [{"role": "user", "content": f"m{i}"} for i in range(10)]

        out = engine.compress(live, current_tokens=99_000)

        # compress returned the live messages unchanged (no session_id).
        assert out is live
        # No bogus empty-string key was planted.
        assert "" not in engine._last_seen_message_idx
        # The real session's cursor is untouched.
        assert engine._last_seen_message_idx["real-session"] == 7

    # -- overflow-recovery fallthrough: passthrough, no reset ----------

    def test_compress_overflow_fallthrough_passthrough_no_reset(self) -> None:
        """The overflow-recovery fallthrough is a guarded no-op today.

        With no experimental flag and no ``preassemble``, ``compress``
        falls through to the overflow-recovery branch, which at v0.1.x
        returns ``messages`` unchanged (``result is messages``). The
        post-compaction cursor-reset call is wired for Epic 04 (whose
        real algorithm WILL return a fresh compacted list), but its
        ``compaction_occurred`` signal is ``result is not messages`` —
        ``False`` for the current passthrough — so the cursor is
        untouched. This is the forward-compat seam, not a length guard.
        """
        engine = LCMEngine()  # no experimental flag, no preassemble
        engine._last_seen_message_idx["sess-Z"] = 4
        live = [{"role": "user", "content": f"m{i}", "session_id": "sess-Z"} for i in range(6)]
        out = engine.compress(live, current_tokens=1_000)
        # Passthrough — same list object, cursor untouched.
        assert out is live
        assert engine._last_seen_message_idx["sess-Z"] == 4

    # -- end-to-end: ingest resumes after a real compaction ------------

    @_skip_no_extension_loading
    def test_ingest_continues_after_real_compaction(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end: after a genuine compaction substitution, ingest resumes.

        Drives a real lifecycle DB for the ingest turns, and a genuine
        ``_assemble`` substitution for the compaction (mock stores wired
        ONTO the live engine just for the ``preassemble`` call, then torn
        down so the post-compaction ingest turn uses the real stores
        again). Pre-fix the cursor would be stranded past
        ``len(substituted)`` and ``_do_ingest_history_diff`` would
        early-return on every later turn — a permanent silent ingest
        stop. Post-fix the cursor is reset on the ``did_substitute``
        signal, so the next turn's appended message ingests.
        """
        engine = _new_engine(tmp_home)
        try:
            # Turn 1: ingest a 3-message transcript through the REAL
            # lifecycle DB stores.
            history = [
                {"role": "user", "content": "u1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "u2"},
            ]
            _ingest(engine, _SESSION, history)
            assert engine._last_seen_message_idx[_SESSION] == 3
            assert len(_all_messages(engine, _SESSION)) == 3

            # A compaction fires via preassemble. Swap the real stores
            # for mock stores wired with a summary-bearing DAG so the
            # genuine assembler runs and folds the live list down.
            real_cstore = engine._conversation_store
            real_sstore = engine._summary_store
            _wire_assemble_stores(
                engine,
                session_id=_SESSION,
                context_items=[
                    _mk_ctx(0, item_type="summary", summary_id="sum_a"),
                    _mk_ctx(1, message_id=1),
                ],
                messages_by_id={1: _mk_msg(1, content="stored-1")},
                summaries_by_id={"sum_a": _mk_summary(content="[compacted summary]")},
            )
            out = engine.preassemble(
                [dict(m, session_id=_SESSION) for m in history],
                budget_tokens=1_000_000,
            )
            # The real assembler produced a genuine, shorter substitution.
            assert out is not history
            assert len(out) < len(history)
            compacted_len = len(out)
            # Cursor reset from 3 down to the substituted length.
            assert engine._last_seen_message_idx[_SESSION] == compacted_len

            # Restore the real lifecycle stores for the ingest turn.
            engine._conversation_store = real_cstore
            engine._summary_store = real_sstore

            # Turn 2 (post-compaction): the live list is the compacted
            # context PLUS a freshly appended user turn. With the reset
            # cursor, the new turn ingests rather than being skipped.
            post_compaction_history = list(out) + [
                {"role": "user", "content": "u3-after-compaction"},
            ]
            _ingest(engine, _SESSION, post_compaction_history)

            # The new message landed — ingest did NOT silently stop.
            persisted = _all_messages(engine, _SESSION)
            assert any(m.content == "u3-after-compaction" for m in persisted), (
                "post-compaction ingest stalled — Defect 2 regression"
            )
            assert engine._last_seen_message_idx[_SESSION] == compacted_len + 1
        finally:
            _end_process(engine, _SESSION)
