"""Regression tests for the v0.1.2 ingest-cursor fix (issue #130).

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
  every restart silently re-ingested the entire transcript.

* **Defect 2 — compaction desyncs the cursor → silent ingest stop.**
  When ``compress()`` / ``preassemble()`` substitute a SHORTER list,
  ``last_idx >= len(snapshot)`` becomes permanently true and
  ``_do_ingest_history_diff`` early-returns forever.

The fix:

* Defect 1 — :meth:`_IngestMixin._reconcile_ingest_cursor` reconciles
  the cursor from the durable ``messages`` store the first time a
  process sees a session, replay-evidence-gated so a genuinely-new
  first turn after restart still ingests.
* Defect 2 — :meth:`_CompactMixin._reset_ingest_cursor_after_compaction`
  resets the cursor to ``len(result)`` after any compaction branch
  returns a shortened list; ``compress`` and ``preassemble`` both call
  it (skipping the reset when ``_infer_session_id`` returns empty).

References:

* GitHub issue #130 — the bug report.
* ``hermes-lcm`` commits 79629c2 (#111), 17578a0 (#113), 4bc6b9c (#1)
  — the production-tested reference this fix is reimplemented from.
* ``docs/adr/009-per-message-ingest.md`` — the diff-ingest seam.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List

import pytest

from lossless_hermes.db.config import LcmConfig
from lossless_hermes.engine import LCMEngine

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
    """Defect 2: a compaction-shortened list must reset the cursor."""

    def test_preassemble_shortening_resets_cursor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When ``preassemble`` substitutes a SHORTER list, cursor resets.

        Pre-fix: the cursor stays at the pre-substitution length and
        every later ``post_llm_call`` early-returns. Post-fix: the
        cursor is reset to ``len(substituted)`` so ingest continues.
        """
        engine = LCMEngine()
        # Pretend ingest has advanced the cursor over a long live list.
        engine._last_seen_message_idx["sess-X"] = 20

        # A 20-message live list; the substitution folds it to 5.
        live = [{"role": "user", "content": f"m{i}", "session_id": "sess-X"} for i in range(20)]
        substituted = [{"role": "user", "content": f"s{i}"} for i in range(5)]
        monkeypatch.setattr(engine, "_assemble", lambda **_kw: substituted)

        out = engine.preassemble(live, budget_tokens=50_000)

        assert out is substituted
        # Cursor reset to the shortened length — NOT left at 20.
        assert engine._last_seen_message_idx["sess-X"] == 5

    def test_preassemble_no_shortening_leaves_cursor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A same-length / longer substitution must NOT touch the cursor.

        The length guard: resetting on a non-shortening result could
        skip un-ingested messages if the cursor were legitimately
        behind.
        """
        engine = LCMEngine()
        engine._last_seen_message_idx["sess-X"] = 3
        live = [{"role": "user", "content": f"m{i}", "session_id": "sess-X"} for i in range(5)]
        # Substitution returns a list of the SAME length.
        same_len = [{"role": "user", "content": f"x{i}"} for i in range(5)]
        monkeypatch.setattr(engine, "_assemble", lambda **_kw: same_len)

        engine.preassemble(live, budget_tokens=50_000)

        # Cursor untouched — no shortening happened.
        assert engine._last_seen_message_idx["sess-X"] == 3

    def test_compress_experimental_shortening_resets_cursor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The experimental ``compress`` substitution path resets the cursor.

        ADR-010 Option A: when ``experimental_always_on_via_compress``
        is set and Hermes lacks ``preassemble``, ``compress`` runs the
        substitution. A shortened result must reset the cursor exactly
        as the production ``preassemble`` path does.
        """
        cfg = LcmConfig(experimental_always_on_via_compress=True)
        engine = LCMEngine(config=cfg)
        assert engine._has_preassemble is False
        engine._last_seen_message_idx["sess-Y"] = 12

        live = [{"role": "user", "content": f"m{i}", "session_id": "sess-Y"} for i in range(12)]
        substituted = [{"role": "user", "content": f"s{i}"} for i in range(4)]
        monkeypatch.setattr(engine, "_assemble", lambda **_kw: substituted)

        out = engine.compress(live, current_tokens=99_000)

        assert out is substituted
        assert engine._last_seen_message_idx["sess-Y"] == 4

    def test_compaction_reset_skipped_when_no_session_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No inferable session_id → reset is SKIPPED, not mis-keyed.

        The issue #130 caveat: ``compress`` / ``preassemble`` do not
        receive ``session_id``. When ``_infer_session_id`` returns ``""``
        the reset must be skipped — writing ``_last_seen_message_idx[""]``
        would plant a bogus key and never fix the real session.
        """
        cfg = LcmConfig(experimental_always_on_via_compress=True)
        engine = LCMEngine(config=cfg)
        engine._last_seen_message_idx["real-session"] = 7

        # Live messages carry NO session_id metadata, and nothing has
        # populated the cached recent-session id → inference fails.
        live = [{"role": "user", "content": f"m{i}"} for i in range(10)]
        # ``_assemble`` is unreachable in this path (compress returns
        # early on the empty session_id), but patch defensively.
        monkeypatch.setattr(engine, "_assemble", lambda **_kw: live[:2])

        engine.compress(live, current_tokens=99_000)

        # No bogus empty-string key was planted.
        assert "" not in engine._last_seen_message_idx
        # The real session's cursor is untouched.
        assert engine._last_seen_message_idx["real-session"] == 7

    def test_compress_overflow_fallthrough_passthrough_no_reset(self) -> None:
        """The 03-09 overflow-recovery fallthrough is a guarded no-op.

        At 03-09 the fallthrough returns ``messages`` unchanged. The
        cursor-reset call is wired (so Epic 04's real shortening
        algorithm is covered), but the length guard makes it inert on
        the current passthrough body.
        """
        engine = LCMEngine()  # no experimental flag, no preassemble
        engine._last_seen_message_idx["sess-Z"] = 4
        live = [{"role": "user", "content": f"m{i}", "session_id": "sess-Z"} for i in range(6)]
        out = engine.compress(live, current_tokens=1_000)
        # Passthrough — same list, cursor untouched.
        assert out is live
        assert engine._last_seen_message_idx["sess-Z"] == 4

    @_skip_no_extension_loading
    def test_ingest_continues_after_compaction_shortening(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end: after a compaction shortens the list, ingest resumes.

        Pre-fix the cursor would be stranded past ``len(shortened)`` and
        ``_do_ingest_history_diff`` would early-return on every later
        turn — a permanent silent ingest stop. Post-fix the cursor is
        reset, so the next turn's appended message ingests.
        """
        engine = _new_engine(tmp_home)
        try:
            # Turn 1: ingest a 3-message transcript normally.
            history = [
                {"role": "user", "content": "u1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "u2"},
            ]
            _ingest(engine, _SESSION, history)
            assert engine._last_seen_message_idx[_SESSION] == 3
            assert len(_all_messages(engine, _SESSION)) == 3

            # A compaction fires via preassemble and folds the live
            # list down to 1 summary-bearing message. The substituted
            # list carries the session_id so ``_infer_session_id``
            # resolves it.
            shortened = [{"role": "system", "content": "[summary]", "session_id": _SESSION}]
            monkeypatch.setattr(engine, "_assemble", lambda **_kw: shortened)
            out = engine.preassemble(
                [dict(m, session_id=_SESSION) for m in history],
                budget_tokens=50_000,
            )
            assert out is shortened
            # Cursor reset from 3 down to 1 — the compaction boundary.
            assert engine._last_seen_message_idx[_SESSION] == 1
            monkeypatch.undo()

            # Turn 2 (post-compaction): the live list is the 1-message
            # compacted context PLUS a freshly appended user turn. With
            # the reset cursor at 1, the new turn at index 1 ingests.
            post_compaction_history = shortened + [
                {"role": "user", "content": "u3-after-compaction"},
            ]
            _ingest(engine, _SESSION, post_compaction_history)

            # The new message landed — ingest did NOT silently stop.
            persisted = _all_messages(engine, _SESSION)
            assert any(m.content == "u3-after-compaction" for m in persisted), (
                "post-compaction ingest stalled — Defect 2 regression"
            )
            assert engine._last_seen_message_idx[_SESSION] == 2
        finally:
            _end_process(engine, _SESSION)
